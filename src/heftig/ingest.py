"""The single ingestion path shared by web upload, API, folder watcher, IMAP and CLI.

Order of durable steps (each one crash-safe):

1. stream the input into ``archive/tmp`` while hashing and enforcing the size limit
2. validate the type by content (magic bytes + actually opening the file)
3. in one exclusive DB transaction:
   - identical SHA-256 already archived -> record a duplicate event, no second original
   - otherwise move the temp file to ``originals/ab/<sha256>.<ext>`` (atomic rename, fsync),
     write ``metadata.json``, insert the DB row, the ingest event and the processing job
4. only after the commit returns may the caller delete/move the source (folder, mail)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from . import binders, jobs, sessions
from . import documents as docs
from .archive import Archive
from .db import now_iso, write_tx
from .i18n import N_, translate_text
from .media import MAIL, UnsupportedFileError, inspect_file
from .models import DocumentMetadata, HistoryEntry, IngestEvent
from .storage import TooLargeError, display_filename, fsync_dir, sha256_file, stream_to_tmp

log = logging.getLogger("heftig.ingest")

SOURCES = ("scanner", "folder", "web", "api", "email", "import")


@dataclass
class IngestResult:
    status: str  # created | duplicate | rejected
    filename: str
    doc_id: str | None = None
    sha256: str | None = None
    message: str = ""
    job_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "filename": self.filename,
            "document_id": self.doc_id,
            "sha256": self.sha256,
            # stored in English; shown in the language of the current request
            "message": translate_text(self.message),
            "job_id": self.job_id,
        }


def _event(
    archive: Archive,
    *,
    doc_id: str | None,
    sha: str | None,
    source: str,
    details: dict[str, Any],
    filename: str,
    result: str,
    message: str,
    import_ref: str | None,
) -> None:
    archive.conn.execute(
        "INSERT INTO ingest_events(doc_id, sha256, source, source_details, filename, result, "
        "message, import_ref, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            sha,
            source,
            json.dumps(details, ensure_ascii=False),
            filename,
            result,
            message,
            import_ref,
            now_iso(),
        ),
    )


def record_rejection(
    archive: Archive,
    *,
    source: str,
    filename: str,
    message: str,
    details: dict[str, Any] | None = None,
    sha: str | None = None,
    import_ref: str | None = None,
) -> IngestResult:
    with write_tx(archive.conn):
        _event(
            archive,
            doc_id=None,
            sha=sha,
            source=source,
            details=details or {},
            filename=filename,
            result="rejected",
            message=message,
            import_ref=import_ref,
        )
    return IngestResult("rejected", filename, sha256=sha, message=message)


def ingest_stream(
    archive: Archive,
    stream: BinaryIO,
    filename: str | None,
    source: str,
    source_details: dict[str, Any] | None = None,
    *,
    paper: bool | None = None,
    import_ref: str | None = None,
    stages: tuple[str, ...] = ("extract", "classify"),
    on_create: Callable[[DocumentMetadata], None] | None = None,
) -> IngestResult:
    """``on_create`` runs inside the ingest transaction right after the new document exists
    (before its processing job is queued) - e.g. to carry over data when documents are
    combined; ``stages`` are the processing stages that job runs."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source}")
    s = archive.settings
    name = display_filename(filename)
    details = dict(source_details or {})
    if import_ref:
        details.setdefault("import_ref", import_ref)
    try:
        tmp, sha, size = stream_to_tmp(stream, archive.paths.tmp, s.max_upload_bytes)
    except TooLargeError as e:
        return record_rejection(
            archive, source=source, filename=name, message=str(e), details=details,
            import_ref=import_ref,
        )  # fmt: skip
    try:
        try:
            info = inspect_file(tmp, s.max_pages, s.max_image_megapixels)
        except UnsupportedFileError as e:
            return record_rejection(
                archive, source=source, filename=name, message=str(e), details=details, sha=sha,
                import_ref=import_ref,
            )  # fmt: skip
        if info.mime_type == MAIL:
            paper = False  # an e-mail is never a sheet of paper
        return _commit(
            archive, tmp, sha, size, info, name, source, details, paper, import_ref, stages,
            on_create,
        )  # fmt: skip
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def ingest_path(archive: Archive, path: Path, source: str, **kw: Any) -> IngestResult:
    with open(path, "rb") as f:
        return ingest_stream(archive, f, path.name, source, **kw)


def _commit(
    archive: Archive,
    tmp: Path,
    sha: str,
    size: int,
    info,
    name: str,
    source: str,
    details: dict[str, Any],
    paper: bool | None,
    import_ref: str | None,
    stages: tuple[str, ...] = ("extract", "classify"),
    on_create: Callable[[DocumentMetadata], None] | None = None,
) -> IngestResult:
    conn = archive.conn
    now = now_iso()
    with write_tx(conn):
        row = conn.execute("SELECT id FROM documents WHERE sha256=?", (sha,)).fetchone()
        if row:
            doc_id = row["id"]
            meta = docs.load_meta(archive, doc_id)
            meta.ingest_events.append(
                IngestEvent(
                    at=now,
                    source=source,
                    source_details=details,  # type: ignore[arg-type]
                    original_filename=name,
                    result="duplicate",
                )  # fmt: skip
            )
            docs.persist(archive, meta)
            msg = N_("Identical file is already archived – no second original created.")
            _event(
                archive, doc_id=doc_id, sha=sha, source=source, details=details, filename=name,
                result="duplicate", message=msg, import_ref=import_ref,
            )  # fmt: skip
            return IngestResult("duplicate", name, doc_id=doc_id, sha256=sha, message=msg)

        relpath = archive.paths.original_relpath(sha, info.ext)
        dest = archive.paths.resolve(relpath)
        if not dest.parent.exists():
            dest.parent.mkdir(parents=True, mode=0o700)
            fsync_dir(dest.parent.parent)
        if dest.exists():
            # leftover of an interrupted earlier attempt: reuse if intact
            if sha256_file(dest) != sha:
                raise RuntimeError(
                    N_("Hash conflict in %(path)s – please run `heftig check`") % {"path": relpath}
                )
        else:
            os.chmod(tmp, 0o400)
            os.replace(tmp, dest)
            fsync_dir(dest.parent)

        seq = docs.next_sequence(conn, "ingest_sequence")
        is_paper = paper if paper is not None else source == "scanner"
        doc_id = str(uuid.uuid4())
        meta = DocumentMetadata(
            id=doc_id,
            sha256=sha,
            original_filename=name,
            original_relpath=relpath,
            mime_type=info.mime_type,
            size_bytes=size,
            page_count=info.page_count,
            source=source,  # type: ignore[arg-type]
            source_details=details,
            received_at=now,
            ingest_sequence=seq,
            paper=is_paper,
            title=os.path.splitext(name)[0][:200],
            field_sources={"title": "rule"},
            status="queued",
            text_status="pending",
            ingest_events=[
                IngestEvent(
                    at=now,
                    source=source,
                    source_details=details,  # type: ignore[arg-type]
                    original_filename=name,
                    result="created",
                )  # fmt: skip
            ],
            revision=0,
            updated_at=now,
        )
        sessions.attach(archive, meta)  # paper during a scan session belongs to it
        if (
            is_paper
            and meta.scan_session is None
            and source in archive.settings.auto_file_source_set
        ):
            meta.filed_at = now
            meta.filing_sequence = docs.next_sequence(conn, "filing_sequence")
            meta.filing_section = docs.filing_section_for(archive, now)
            meta.filing_binder = binders.current(archive)
            docs.add_history(
                meta, HistoryEntry(task="filing", at=now, status="auto-filed", by="rule")
            )
        docs.persist(archive, meta, create=True)
        _event(
            archive, doc_id=doc_id, sha=sha, source=source, details=details, filename=name,
            result="created", message="", import_ref=import_ref,
        )  # fmt: skip
        if on_create is not None:
            on_create(meta)
        job_id = jobs.enqueue(
            conn,
            "process",
            doc_id,
            {"stages": list(stages)},
            max_attempts=archive.settings.job_max_attempts,
        )
    log.info("ingested document %s (seq %s, source %s)", doc_id, seq, source)
    return IngestResult("created", name, doc_id=doc_id, sha256=sha, job_id=job_id)
