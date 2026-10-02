"""Papierkorb: deleting a document moves it here; it can be restored until it is purged.

Layout: the document's sidecar folder moves from ``documents/<id>/`` to ``trash/<id>/``
(metadata.json then carries ``trashed_at``, ``trash_reason``, ``trash_batch``); the original and
attachments stay where they are. The ``trash`` table mirrors the folder and is rebuilt from it.
A trashed document is completely out of the documents table, so search, facets, duplicates,
exports and the MCP connection only ever see live documents.

Purging - after ``trash_retention_days`` automatically, or explicitly - deletes the folder and
the original / attachments that no other document (live or trashed) references. The original
of a split document is kept: it is neither purged automatically nor by emptying the trash as
long as one of its parts exists (live or trashed), and its file stays as long as a part names
it (``split_from.originals``).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from datetime import timedelta
from typing import Any

from . import documents as docs
from . import index as fts
from . import jobs
from .archive import Archive
from .db import iso, now_iso, parse_iso, utcnow, write_tx
from .i18n import N_, _
from .models import DocumentMetadata, TextPages
from .storage import atomic_write_bytes, atomic_write_json, fsync_dir, read_json

log = logging.getLogger("heftig.trash")


class TrashError(ValueError):
    pass


def new_batch() -> str:
    return secrets.token_hex(6)


def _dir(archive: Archive, doc_id: str):
    return archive.paths.trash / docs.files(archive, doc_id).dir.name


def _merge_leftover(src, leftover) -> None:
    """A trash folder of this id already exists (an interrupted earlier attempt): keep what
    only it has (e.g. the text of a half-deleted document), then remove it."""
    for f in leftover.rglob("*"):
        if f.is_file():
            target = src / f.relative_to(leftover)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(f, target)
    shutil.rmtree(leftover)


def _move(src, dst) -> None:
    os.replace(src, dst)  # same file system: atomic
    fsync_dir(dst.parent)
    fsync_dir(src.parent)


def trash_document(
    archive: Archive, doc_id: str, reason: str = "", batch: str | None = None, by: str = "web"
) -> dict[str, Any]:
    """Database first, the folder move last (inside the transaction); if anything fails after
    the move, the folder goes back, so a document is never half in the Papierkorb."""
    conn = archive.conn
    src, dst = docs.files(archive, doc_id).dir, _dir(archive, doc_id)
    moved = False
    original_sidecar: bytes | None = None
    try:
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            original_sidecar = (src / "metadata.json").read_bytes()
            row = conn.execute(
                "SELECT rowid, sha256, original_relpath, title, original_filename FROM documents "
                "WHERE id=?",
                (doc_id,),
            ).fetchone()
            if row is None:
                raise docs.DocumentNotFound(doc_id)
            meta.trashed_at = now_iso()
            meta.trash_reason = (reason or "")[:300]
            meta.trash_batch = batch
            data = meta.model_dump(mode="json")
            fts.remove_document(conn, row["rowid"])
            conn.execute(
                "DELETE FROM duplicate_candidates WHERE doc_a=? OR doc_b=?", (doc_id, doc_id)
            )
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.execute(
                "DELETE FROM jobs WHERE doc_id=? AND status IN ('queued','failed')", (doc_id,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO trash(id, sha256, original_relpath, title, trashed_at, "
                "reason, batch, metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, row["sha256"], row["original_relpath"],
                 row["title"] or row["original_filename"], meta.trashed_at, meta.trash_reason,
                 batch, json.dumps(data, ensure_ascii=False)),
            )  # fmt: skip
            title = row["title"] or row["original_filename"]
            message = (
                N_("“%(title)s” moved to the trash (%(reason)s)")
                % {"title": title, "reason": reason}
                if reason
                else N_("“%(title)s” moved to the trash") % {"title": title}
            )
            _event(conn, doc_id, row["sha256"], row["original_filename"], by, "deleted", message)
            archive.paths.trash.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                _merge_leftover(src, dst)
            _move(src, dst)
            moved = True
            atomic_write_json(dst / "metadata.json", data)
    except BaseException:
        if moved:
            _move_back(dst, src)
            if original_sidecar is not None:
                atomic_write_bytes(src / "metadata.json", original_sidecar)
        raise
    return {"id": doc_id, "sha256": row["sha256"], "batch": batch}


def _move_back(moved_to, original) -> None:
    """Undo a folder move after a failed transaction (the caller restores the sidecar)."""
    try:
        _move(moved_to, original)
    except OSError:
        log.exception("could not move %s back to %s", moved_to, original)


def _conflicts(conn, meta: DocumentMetadata) -> list[str]:
    """Numbers that another document took meanwhile (e.g. the combined document holds the
    filing position of its parts). The internal arrival number is renumbered silently; a
    taken filing position is released and the user asked to check where the paper is."""
    notes = []
    if conn.execute(
        "SELECT 1 FROM documents WHERE ingest_sequence=?", (meta.ingest_sequence,)
    ).fetchone():
        meta.ingest_sequence = docs.next_sequence(conn, "ingest_sequence")
    if meta.filing_sequence is not None:
        holder = conn.execute(
            "SELECT title, original_filename FROM documents WHERE filing_sequence=?",
            (meta.filing_sequence,),
        ).fetchone()
        if holder:
            meta.filed_at = meta.filing_sequence = meta.filing_section = None
            notes.append(
                N_(
                    "Filing: The filing position now belongs to “%(title)s” – please check "
                    "where the paper is."
                )
                % {"title": holder[0] or holder[1]}
            )
    return notes


def restore(archive: Archive, doc_id: str) -> DocumentMetadata:
    conn = archive.conn
    src, dst = _dir(archive, doc_id), docs.files(archive, doc_id).dir
    moved = False
    original_sidecar: bytes | None = None
    try:
        with write_tx(conn):
            row = conn.execute("SELECT * FROM trash WHERE id=?", (doc_id,)).fetchone()
            if row is None or not (src / "metadata.json").exists():
                raise TrashError(_("No longer in the trash."))
            original_sidecar = (src / "metadata.json").read_bytes()
            meta = DocumentMetadata.model_validate(read_json(src / "metadata.json"))
            other = conn.execute(
                "SELECT title FROM documents WHERE sha256=?", (meta.sha256,)
            ).fetchone()
            if other:
                raise TrashError(
                    _(
                        "The same file is back in the archive (“%(title)s”) – not restored.",
                        title=other[0],
                    )
                )
            if not archive.paths.resolve(meta.original_relpath).exists():
                raise TrashError(_("The original file is missing – cannot be restored."))
            if dst.exists():
                if (dst / "metadata.json").exists():
                    raise TrashError(_("A document with this ID already exists."))
                shutil.rmtree(dst)  # only caches written after the deletion: regenerable
            meta.trashed_at = meta.trash_reason = meta.trash_batch = None
            for note in _conflicts(conn, meta):
                if note not in meta.review_reasons:
                    meta.review_reasons.append(note)
                    meta.status = "needs_review"
            _move(src, dst)
            moved = True
            requeue = _needs_processing(conn, meta)
            if requeue:
                meta.status = "queued"
            docs.persist(archive, meta, create=True)  # the row first: document_text refers to it
            tp_path = dst / "text_pages.json"
            if tp_path.exists():
                tp = TextPages.model_validate(read_json(tp_path))
                text = "\n\n".join(p.text.strip() for p in tp.pages if p.text.strip())
                conn.execute(
                    "INSERT OR REPLACE INTO document_text(doc_id, content) VALUES(?,?)",
                    (doc_id, text),
                )
                fts.index_document(conn, doc_id)
            if requeue:
                jobs.enqueue(
                    conn, "process", doc_id, {"stages": ["extract", "classify"]},
                    max_attempts=archive.settings.job_max_attempts,
                )  # fmt: skip
            conn.execute("DELETE FROM trash WHERE id=?", (doc_id,))
            _event(conn, doc_id, meta.sha256, meta.original_filename, "web", "restored",
                   N_("“%(title)s” restored") % {"title": meta.title or meta.original_filename})  # fmt: skip
    except BaseException:
        if moved:
            _move_back(dst, src)
            if original_sidecar is not None:
                atomic_write_bytes(src / "metadata.json", original_sidecar)
        raise
    # back in the archive: if its paper had stayed in the binder, the document holds the place
    from . import binders

    binders.sheet_taken_out(archive, doc_id)
    return meta


def _needs_processing(conn, meta: DocumentMetadata) -> bool:
    """Deleted while it was waiting or being processed: its job is gone (or failed)."""
    if meta.status not in ("queued", "processing") and meta.text_status != "pending":
        return False
    return not conn.execute(
        "SELECT 1 FROM jobs WHERE doc_id=? AND status IN ('queued','processing')", (meta.id,)
    ).fetchone()


def restore_batch(archive: Archive, batch: str) -> dict[str, Any]:
    ids = [r[0] for r in archive.conn.execute("SELECT id FROM trash WHERE batch=?", (batch,))]
    done, failed = [], []
    for doc_id in ids:
        try:
            restore(archive, doc_id)
            done.append(doc_id)
        except TrashError as e:
            failed.append(str(e))
    return {"restored": len(done), "ids": done, "failed": failed}


def purge(archive: Archive, doc_id: str) -> None:
    """Delete for good: folder, and original/attachments nothing else references. The check
    and the deletion happen under the write lock, so an upload of the same file in between
    (which reuses the existing original) cannot lose it."""
    conn = archive.conn
    with write_tx(conn):
        row = conn.execute("SELECT * FROM trash WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            raise TrashError(_("Not in the trash."))
        meta = json.loads(row["metadata_json"])
        conn.execute("DELETE FROM trash WHERE id=?", (doc_id,))
        paths = [row["original_relpath"]] + [a["relpath"] for a in meta.get("attachments") or []]
        # the last part of a split document also releases the original it kept
        paths += docs.split_originals(meta.get("source_details") or {})
        for rel in dict.fromkeys(paths):
            if not is_referenced(archive, rel):
                p = archive.paths.resolve(rel)
                if p.exists():
                    p.unlink()
    shutil.rmtree(_dir(archive, doc_id), ignore_errors=True)


def purge_expired(archive: Archive) -> int:
    cutoff = iso(utcnow() - timedelta(days=archive.settings.trash_retention_days))
    kept = kept_originals(archive)
    ids = [
        r[0]
        for r in archive.conn.execute("SELECT id FROM trash WHERE trashed_at < ?", (cutoff,))
        if r[0] not in kept
    ]
    for doc_id in ids:
        purge(archive, doc_id)
    return len(ids)


def empty(archive: Archive) -> int:
    kept = kept_originals(archive)
    ids = [r[0] for r in archive.conn.execute("SELECT id FROM trash") if r[0] not in kept]
    for doc_id in ids:
        purge(archive, doc_id)
    return len(ids)


def kept_originals(archive: Archive) -> set[str]:
    """Trashed originals of split documents that one of their parts (live or trashed) still
    refers to: they stay in the trash until the last part is gone."""
    ref = "json_extract(metadata_json, '$.source_details.split_from.id')"
    refs: set[str] = set()
    for table in ("documents", "trash"):
        refs.update(r[0] for r in archive.conn.execute(f"SELECT {ref} FROM {table}") if r[0])
    return {r[0] for r in archive.conn.execute("SELECT id FROM trash") if r[0] in refs}


def is_referenced(archive: Archive, relpath: str) -> bool:
    """Also by name in a document's metadata: an attachment, or the original a part was split
    from (``split_from.originals``)."""
    conn = archive.conn
    pattern = f'%"{relpath}"%'
    return bool(
        conn.execute("SELECT 1 FROM documents WHERE original_relpath=?", (relpath,)).fetchone()
        or conn.execute("SELECT 1 FROM trash WHERE original_relpath=?", (relpath,)).fetchone()
        or conn.execute(
            "SELECT 1 FROM documents WHERE metadata_json LIKE ? LIMIT 1", (pattern,)
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM trash WHERE metadata_json LIKE ? LIMIT 1", (pattern,)
        ).fetchone()
    )


def referenced_files(archive: Archive) -> set[str]:
    """Originals and attachments held by trashed documents, and the originals of split
    documents named by their parts (for the integrity check)."""
    out: set[str] = set()
    for rel, mj in archive.conn.execute("SELECT original_relpath, metadata_json FROM trash"):
        data = json.loads(mj)
        out.add(rel)
        out.update(a["relpath"] for a in data.get("attachments") or [])
        out.update(docs.split_originals(data.get("source_details") or {}))
    for (mj,) in archive.conn.execute(
        "SELECT metadata_json FROM documents WHERE json_extract(metadata_json, "
        "'$.source_details.split_from.originals') IS NOT NULL"
    ):
        out.update(docs.split_originals(json.loads(mj).get("source_details") or {}))
    return out


def listing(archive: Archive) -> list[dict[str, Any]]:
    """Trash grouped: one entry per bulk batch, single deletions on their own; newest first.
    ``kept``: the original of a split document, kept as long as one of its parts exists
    (``purge_at`` is then None)."""
    keep = archive.settings.trash_retention_days
    kept = kept_originals(archive)
    groups: dict[str, dict[str, Any]] = {}
    for r in archive.conn.execute("SELECT * FROM trash ORDER BY trashed_at DESC, title"):
        key = r["batch"] or r["id"]
        g = groups.setdefault(key, {"batch": r["batch"], "trashed_at": r["trashed_at"],
                                    "reason": r["reason"], "items": []})  # fmt: skip
        g["items"].append({"id": r["id"], "title": r["title"], "reason": r["reason"]})
    for g in groups.values():
        started = parse_iso(g["trashed_at"]) or utcnow()
        g["kept"] = all(it["id"] in kept for it in g["items"])
        g["purge_at"] = None if g["kept"] else iso(started + timedelta(days=keep))
    return list(groups.values())


def rebuild(archive: Archive) -> int:
    """Recreate the trash table from the trash folder (rebuild-db)."""
    conn = archive.conn
    n = 0
    with write_tx(conn):
        conn.execute("DELETE FROM trash")
        if not archive.paths.trash.exists():
            return 0
        for d in sorted(archive.paths.trash.iterdir()):
            f = d / "metadata.json"
            if not f.exists():
                continue
            data = read_json(f)
            conn.execute(
                "INSERT OR REPLACE INTO trash(id, sha256, original_relpath, title, trashed_at, "
                "reason, batch, metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                (data["id"], data["sha256"], data["original_relpath"],
                 data.get("title") or data.get("original_filename", ""),
                 data.get("trashed_at") or now_iso(), data.get("trash_reason") or "",
                 data.get("trash_batch"), json.dumps(data, ensure_ascii=False)),
            )  # fmt: skip
            n += 1
    return n


def _event(conn, doc_id, sha, filename, source, result, message) -> None:
    conn.execute(
        "INSERT INTO ingest_events(doc_id, sha256, source, filename, result, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (doc_id, sha, source if source in ("web", "api") else "web", filename, result, message,
         now_iso()),
    )  # fmt: skip
