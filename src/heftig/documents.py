"""Document persistence: sidecar files are written first, then the database row and index.

Every change goes through :func:`persist` inside a write transaction. The sidecar carries a
``revision`` counter; if the process dies between writing the sidecar and committing the
database, ``heftig repair`` sees the newer sidecar revision and reloads it.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import index as fts
from . import taxonomy as tax
from .archive import Archive
from .db import get_meta, now_iso, parse_iso, set_meta, write_tx
from .i18n import N_, _, translate_text
from .models import (
    LOCKABLE_FIELDS,
    CustomField,
    DocumentMetadata,
    HistoryEntry,
    TextPages,
)
from .storage import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    fsync_dir,
    read_json,
)
from .textnorm import clean_display_name, normalize_name

MAX_HISTORY = 200


class DocumentNotFound(LookupError):
    pass


@dataclass(frozen=True)
class DocFiles:
    dir: Path

    @property
    def metadata(self) -> Path:
        return self.dir / "metadata.json"

    @property
    def text_md(self) -> Path:
        return self.dir / "text.md"

    @property
    def text_pages(self) -> Path:
        return self.dir / "text_pages.json"

    @property
    def preview(self) -> Path:
        return self.dir / "preview.webp"


def files(archive: Archive, doc_id: str) -> DocFiles:
    return DocFiles(archive.paths.doc_dir(doc_id))


_ORIGINAL = re.compile(r"originals/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{1,5}")


def _originals(entry: Any) -> list[str]:
    rels = entry.get("originals") if isinstance(entry, dict) else None
    if not isinstance(rels, list):
        return []
    return [r for r in rels if isinstance(r, str) and _ORIGINAL.fullmatch(r)]


def split_originals(source_details: dict[str, Any]) -> list[str]:
    """The files of the documents this one was split from (``split_from.originals``, the
    nearest first). Only well-formed paths below ``originals/``."""
    return _originals(source_details.get("split_from"))


def source_originals(source_details: dict[str, Any]) -> list[str]:
    """Every original file this document was made from - split from (``split_from``) or
    combined from (``combined_from[].originals``): they are kept as long as it exists."""
    out = split_originals(source_details)
    combined = source_details.get("combined_from")
    for part in combined if isinstance(combined, list) else []:
        out += _originals(part)
    return list(dict.fromkeys(out))


def load_meta(archive: Archive, doc_id: str) -> DocumentMetadata:
    try:
        f = files(archive, doc_id)
    except ValueError as e:
        raise DocumentNotFound(doc_id) from e
    if not f.metadata.exists():
        raise DocumentNotFound(doc_id)
    return DocumentMetadata.model_validate(read_json(f.metadata))


def cache_writable(archive: Archive, doc_id: str) -> bool:
    """Regenerable caches (page images, OCR pages, word boxes) are only written while the
    document is live: a writer that finishes after the document went to the Papierkorb must
    not recreate its folder (that would block restoring it)."""
    return files(archive, doc_id).metadata.exists()


def load_text_pages(archive: Archive, doc_id: str) -> TextPages | None:
    f = files(archive, doc_id)
    if not f.text_pages.exists():
        return None
    return TextPages.model_validate(read_json(f.text_pages))


# --- blank pages (empty backs of duplex scans) ------------------------------------------------


def blank_pages(tp: TextPages | None, overrides: dict | None = None) -> list[int]:
    """Pages (1-based) hidden as blank: detected ones, unless the user decided otherwise
    (`DocumentMetadata.page_blank`). A document is never hidden completely."""
    if tp is None:
        return []
    blank = {p.page for p in tp.pages if p.blank}
    for k, v in (overrides or {}).items():
        (blank.add if v else blank.discard)(int(k))
    blank &= {p.page for p in tp.pages}
    return sorted(blank) if len(blank) < len(tp.pages) else []


def cover_page(archive: Archive, meta: DocumentMetadata) -> int:
    """Index (0-based) of the first page that is not blank - for previews and thumbnails."""
    if (meta.page_count or 1) < 2:
        return 0
    try:
        hidden = set(blank_pages(load_text_pages(archive, meta.id), meta.page_blank))
    except (OSError, ValueError):
        return 0
    return next((i for i in range(meta.page_count or 1) if i + 1 not in hidden), 0)


def set_page_blank(archive: Archive, doc_id: str, page: int, blank: bool) -> DocumentMetadata:
    """The user says a page is blank (hide it) or not (always show it)."""
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        tp = load_text_pages(archive, doc_id)
        if tp is None or not any(p.page == page for p in tp.pages):
            raise EditError(_("There is no page %(num)s.", num=page))
        detected = any(p.page == page and p.blank for p in tp.pages)
        before = cover_page(archive, meta)
        if blank == detected:
            meta.page_blank.pop(page, None)
        else:
            meta.page_blank[page] = blank
        persist(archive, meta)
    if cover_page(archive, meta) != before:
        refresh_preview(archive, meta)
    return meta


def rotation(meta: DocumentMetadata, page: int) -> int:
    """Degrees clockwise the user turned page `page` (1-based)."""
    return int(meta.page_rotation.get(page, 0))


def rotate_pages(
    archive: Archive, doc_id: str, pages: list[int] | None, degrees: int = 90
) -> DocumentMetadata:
    """Turn pages clockwise by `degrees` (-90 turns back); None: every page. Only the display
    changes (viewer, previews, comparison, text recognition) - never the original file."""
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        count = meta.page_count or 1
        todo = list(range(1, count + 1)) if pages is None else pages
        if not todo or any(not 1 <= p <= count for p in todo):
            raise EditError(_("There is no page %(num)s.", num=max(todo or [0])))
        cover = cover_page(archive, meta) + 1
        for p in todo:
            turned = (rotation(meta, p) + degrees) % 360
            if turned:
                meta.page_rotation[p] = turned  # type: ignore[assignment]
            else:
                meta.page_rotation.pop(p, None)
        persist(archive, meta)  # a new revision: the lists fetch the turned thumbnail
    if cover in todo:
        refresh_preview(archive, meta)
    return meta


def refresh_preview(archive: Archive, meta: DocumentMetadata) -> None:
    from .media import make_preview

    cover = cover_page(archive, meta)
    try:
        data = make_preview(
            archive.paths.resolve(meta.original_relpath), meta.mime_type,
            archive.settings.max_image_megapixels, page_index=cover,
            rotation=rotation(meta, cover + 1),
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - the preview is optional and regenerable
        return
    with write_tx(archive.conn):
        write_preview(archive, meta.id, data)


def add_history(meta: DocumentMetadata, entry: HistoryEntry) -> None:
    meta.processing_history.append(entry)
    if len(meta.processing_history) > MAX_HISTORY:
        meta.processing_history = meta.processing_history[-MAX_HISTORY:]


def _canonicalize(
    conn: sqlite3.Connection, meta: DocumentMetadata
) -> tuple[int | None, int | None]:
    """Map names onto canonical taxonomy terms (creating missing ones)."""
    corr_id = type_id = None
    if meta.correspondent:
        origin = meta.field_sources.get("correspondent", "user")
        corr_id = tax.get_or_create(conn, "correspondent", meta.correspondent, origin)
        meta.correspondent = tax.term_name(conn, corr_id)
    if meta.document_type:
        origin = meta.field_sources.get("document_type", "user")
        type_id = tax.get_or_create(conn, "document_type", meta.document_type, origin)
        meta.document_type = tax.term_name(conn, type_id)
    tags: list[str] = []
    seen: set[str] = set()
    for t in meta.tags:
        if not normalize_name(t):
            continue
        tid = tax.get_or_create(conn, "tag", t, meta.field_sources.get("tags", "user"))
        name = tax.term_name(conn, tid) or t
        if name not in seen:
            seen.add(name)
            tags.append(name)
    meta.tags = tags
    return corr_id, type_id


def persist(
    archive: Archive, meta: DocumentMetadata, *, bump: bool = True, create: bool = False
) -> None:
    """Write sidecar + DB row + index. Must run inside write_tx.

    Only ingest/import/repair may create a document (``create=True``); every other caller
    updates an existing one, so a worker can never resurrect a document deleted meanwhile.
    """
    conn = archive.conn
    assert conn.in_transaction, "persist() requires an open write transaction"
    if not create and not conn.execute("SELECT 1 FROM documents WHERE id=?", (meta.id,)).fetchone():
        raise DocumentNotFound(meta.id)
    n_terms = conn.execute("SELECT COUNT(*) FROM taxonomy").fetchone()[0]
    corr_id, type_id = _canonicalize(conn, meta)
    if conn.execute("SELECT COUNT(*) FROM taxonomy").fetchone()[0] != n_terms:
        tax.write_sidecar(conn, archive.paths)
    if bump:
        meta.revision += 1
    meta.updated_at = now_iso()
    f = files(archive, meta.id)
    if not f.dir.exists():
        f.dir.mkdir(parents=True, mode=0o700)
        fsync_dir(f.dir.parent)
    data = meta.model_dump(mode="json")
    atomic_write_json(f.metadata, data)
    _upsert_row(conn, meta, corr_id, type_id, data)
    fts.index_document(conn, meta.id)


def _upsert_row(
    conn: sqlite3.Connection,
    meta: DocumentMetadata,
    corr_id: int | None,
    type_id: int | None,
    data: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO documents(id, sha256, original_filename, original_relpath, mime_type,
            size_bytes, page_count, source, received_at, ingest_sequence, document_date,
            filed_at, filing_sequence, filing_section, paper, title, correspondent_id,
            document_type_id, summary, status, text_status, review_reasons, revision,
            updated_at, metadata_json, scan_session_id, paper_location, paper_discarded_at,
            filing_binder)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            original_filename=excluded.original_filename, page_count=excluded.page_count,
            document_date=excluded.document_date, filed_at=excluded.filed_at,
            filing_sequence=excluded.filing_sequence, filing_section=excluded.filing_section,
            paper=excluded.paper, title=excluded.title,
            correspondent_id=excluded.correspondent_id,
            document_type_id=excluded.document_type_id, summary=excluded.summary,
            status=excluded.status, text_status=excluded.text_status,
            review_reasons=excluded.review_reasons, revision=excluded.revision,
            updated_at=excluded.updated_at, metadata_json=excluded.metadata_json,
            scan_session_id=excluded.scan_session_id, paper_location=excluded.paper_location,
            paper_discarded_at=excluded.paper_discarded_at, filing_binder=excluded.filing_binder
        """,
        (
            meta.id,
            meta.sha256,
            meta.original_filename,
            meta.original_relpath,
            meta.mime_type,
            meta.size_bytes,
            meta.page_count,
            meta.source,
            meta.received_at,
            meta.ingest_sequence,
            meta.document_date,
            meta.filed_at,
            meta.filing_sequence,
            meta.filing_section,
            int(meta.paper),
            meta.title,
            corr_id,
            type_id,
            meta.summary,
            meta.status,
            meta.text_status,
            json.dumps(meta.review_reasons, ensure_ascii=False),
            meta.revision,
            meta.updated_at,
            json.dumps(data, ensure_ascii=False),
            meta.scan_session.id if meta.scan_session else None,
            meta.paper_location,
            meta.paper_discarded_at,
            meta.filing_binder,
        ),
    )
    conn.execute("DELETE FROM document_tags WHERE doc_id=?", (meta.id,))
    for t in meta.tags:
        tid = tax.find_term(conn, "tag", t)
        if tid is not None:
            conn.execute(
                "INSERT OR IGNORE INTO document_tags(doc_id, tag_id) VALUES(?,?)", (meta.id, tid)
            )
    conn.execute("DELETE FROM custom_field_values WHERE doc_id=?", (meta.id,))
    for key, cf in meta.custom_fields.items():
        num = None
        if cf.type in ("number", "monetary") and cf.value is not None:
            try:
                num = float(cf.value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                num = None
        conn.execute(
            "INSERT INTO custom_field_values(doc_id, key, type, value_text, value_num) "
            "VALUES(?,?,?,?,?)",
            (meta.id, key, cf.type, None if cf.value is None else str(cf.value), num),
        )


def write_text(archive: Archive, doc_id: str, pages: TextPages) -> str:
    """Store extracted text (sidecars first) and mirror it into the database. In write_tx."""
    f = files(archive, doc_id)
    parts = []
    for p in pages.pages:
        parts.append(f"<!-- page {p.page} -->\n{p.text.strip()}\n")
    full = "\n".join(parts)
    atomic_write_json(f.text_pages, pages.model_dump(mode="json"))
    atomic_write_text(f.text_md, full)
    plain = "\n\n".join(p.text.strip() for p in pages.pages if p.text.strip())
    archive.conn.execute(
        "INSERT INTO document_text(doc_id, content) VALUES(?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET content=excluded.content",
        (doc_id, plain),
    )
    return plain


def write_preview(archive: Archive, doc_id: str, data: bytes) -> None:
    atomic_write_bytes(files(archive, doc_id).preview, data)


def get_text(archive: Archive, doc_id: str) -> str:
    row = archive.conn.execute(
        "SELECT content FROM document_text WHERE doc_id=?", (doc_id,)
    ).fetchone()
    return row[0] if row else ""


# --- user edits ------------------------------------------------------------------------


class EditError(ValueError):
    pass


def update_fields(
    archive: Archive,
    doc_id: str,
    changes: dict[str, Any],
    locks: dict[str, bool] | None = None,
    *,
    lock_changed: bool = True,
) -> DocumentMetadata:
    """Apply user edits. Changed fields are marked as user-provided and locked.

    ``tags`` replaces the tag list; additions/removals relative to the current list are
    remembered in ``tag_overrides`` so that re-classification respects them.
    """
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        changed: list[str] = []
        for field, value in changes.items():
            if field not in LOCKABLE_FIELDS:
                raise EditError(_("Field “%(field)s” cannot be edited.", field=field))
            if field == "tags":
                new = [clean_display_name(t) for t in (value or []) if normalize_name(str(t))]
                cur_norm = {normalize_name(t) for t in meta.tags}
                new_norm = {normalize_name(t) for t in new}
                ov = meta.tag_overrides
                for t in new:
                    n = normalize_name(t)
                    if n not in cur_norm:
                        ov.removed = [x for x in ov.removed if normalize_name(x) != n]
                        if n not in {normalize_name(x) for x in ov.added}:
                            ov.added.append(t)
                for t in meta.tags:
                    n = normalize_name(t)
                    if n not in new_norm:
                        ov.added = [x for x in ov.added if normalize_name(x) != n]
                        if n not in {normalize_name(x) for x in ov.removed}:
                            ov.removed.append(t)
                if new_norm != cur_norm:
                    meta.tags = new
                    meta.field_sources["tags"] = "user"
                    changed.append("tags")
                # a tag suggestion is settled once its tags are there (accepted or typed)
                meta.suggestions = [
                    sg for sg in meta.suggestions
                    if sg.field != "tags" or not all(
                        normalize_name(str(v)) in new_norm
                        for v in (sg.value if isinstance(sg.value, list) else [sg.value])
                    )
                ]  # fmt: skip
                continue
            if field == "document_date":
                value = value or None
                if value is not None:
                    value = str(value).strip()
                    try:
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                            raise ValueError(value)
                        date.fromisoformat(value)
                    except ValueError as e:
                        raise EditError(_("Please enter the date as YYYY-MM-DD.")) from e
                meta.document_date = value
                meta.document_date_status = "user"
                meta.document_date_reason = None
            elif field == "custom_fields":
                meta.custom_fields = {
                    str(k)[:60]: CustomField.model_validate(v) for k, v in (value or {}).items()
                }
            elif field in ("correspondent", "document_type"):
                setattr(meta, field, clean_display_name(value) if value else None)
            else:
                setattr(
                    meta,
                    field,
                    clean_display_name(value or "") if field == "title" else value or "",
                )
            meta.field_sources[field] = "user"
            changed.append(field)
            # a manual value supersedes open AI suggestions for that field
            meta.suggestions = [s for s in meta.suggestions if s.field != field]
        if lock_changed:
            for field in changed:
                if field != "tags":
                    meta.field_locks[field] = True
        for field, flag in (locks or {}).items():
            if field not in LOCKABLE_FIELDS:
                raise EditError(_("Field “%(field)s” cannot be locked.", field=field))
            if flag:
                meta.field_locks[field] = True
            else:
                meta.field_locks.pop(field, None)
        if meta.status == "needs_review" and not meta.suggestions and _review_resolved(meta):
            meta.status = "done"
            meta.review_reasons = []
        add_history(
            meta,
            HistoryEntry(task="edit", at=now_iso(), status="ok", fields=changed, by="user"),
        )
        persist(archive, meta)
        return meta


def field_snapshot(meta: DocumentMetadata, field: str) -> dict[str, Any]:
    """Everything an edit of ``field`` changes, so that ``restore_field`` can undo it."""
    data = meta.model_dump(mode="json")
    snap: dict[str, Any] = {
        "field": field,
        "value": data.get(field),
        "locked": bool(meta.field_locks.get(field)),
        "source": meta.field_sources.get(field),
        "suggestions": [sg for sg in data["suggestions"] if sg["field"] == field],
        "status": meta.status,
        "review_reasons": list(meta.review_reasons),
    }
    if field == "tags":
        snap["tag_overrides"] = data["tag_overrides"]
    if field == "document_date":
        snap["date_status"] = meta.document_date_status
        snap["date_reason"] = meta.document_date_reason
    return snap


def restore_field(archive: Archive, doc_id: str, snap: dict[str, Any]) -> DocumentMetadata:
    """Undo an edit: the field, its lock, source and suggestions as they were before."""
    field = str(snap.get("field") or "")
    if field not in LOCKABLE_FIELDS:
        raise EditError(_("Field “%(field)s” cannot be edited.", field=field))
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        data = meta.model_dump(mode="json")
        data[field] = snap.get("value")
        locks, sources = dict(data["field_locks"]), dict(data["field_sources"])
        locks.pop(field, None)
        if snap.get("locked"):
            locks[field] = True
        sources.pop(field, None)
        if snap.get("source"):
            sources[field] = snap["source"]
        data["field_locks"], data["field_sources"] = locks, sources
        data["suggestions"] = [sg for sg in data["suggestions"] if sg["field"] != field] + list(
            snap.get("suggestions") or []
        )
        if snap.get("status") in ("done", "needs_review"):
            data["status"] = snap["status"]
            data["review_reasons"] = list(snap.get("review_reasons") or [])
        if field == "tags" and isinstance(snap.get("tag_overrides"), dict):
            data["tag_overrides"] = snap["tag_overrides"]
        if field == "document_date":
            data["document_date_status"] = snap.get("date_status") or data["document_date_status"]
            data["document_date_reason"] = snap.get("date_reason")
        try:
            restored = DocumentMetadata.model_validate(data)
        except ValueError as e:
            raise EditError(_("This change cannot be undone any more.")) from e
        add_history(
            restored,
            HistoryEntry(task="edit", at=now_iso(), status="undone", fields=[field], by="user"),
        )
        persist(archive, restored)
        return restored


def _review_resolved(meta: DocumentMetadata) -> bool:
    return meta.text_status in ("ok", "empty") and meta.document_date_status != "ai_uncertain"


def accept_suggestion(archive: Archive, doc_id: str, index: int) -> DocumentMetadata:
    meta = load_meta(archive, doc_id)
    if not 0 <= index < len(meta.suggestions):
        raise EditError(_("Suggestion not found."))
    s = meta.suggestions[index]
    value = s.value
    if s.field == "tags":
        value = sorted(set(meta.tags) | set(value if isinstance(value, list) else [value]))
    return update_fields(archive, doc_id, {s.field: value})


def dismiss_suggestion(archive: Archive, doc_id: str, index: int) -> DocumentMetadata:
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        if not 0 <= index < len(meta.suggestions):
            raise EditError(_("Suggestion not found."))
        meta.suggestions.pop(index)
        if meta.status == "needs_review" and not meta.suggestions and _review_resolved(meta):
            meta.status = "done"
            meta.review_reasons = []
        persist(archive, meta)
        return meta


def mark_reviewed(archive: Archive, doc_id: str) -> DocumentMetadata:
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        if meta.document_date_status == "ai_uncertain":
            meta.document_date_status = "user"
            meta.field_locks["document_date"] = True
        meta.suggestions = []
        meta.review_reasons = []
        if meta.status == "needs_review":
            meta.status = "done"
        add_history(meta, HistoryEntry(task="edit", at=now_iso(), status="reviewed", by="user"))
        persist(archive, meta)
        return meta


# --- paper filing ----------------------------------------------------------------------


def next_sequence(conn: sqlite3.Connection, column: str) -> int:
    """Monotonic counter (never reuses numbers of deleted documents). In write_tx."""
    assert column in ("ingest_sequence", "filing_sequence")
    top = conn.execute(f"SELECT COALESCE(MAX({column}), 0) FROM documents").fetchone()[0]
    # documents in the Papierkorb keep their numbers (restorable; after a rebuild-db the
    # counter in the meta table starts from scratch)
    trashed = conn.execute(
        f"SELECT COALESCE(MAX(json_extract(metadata_json, '$.{column}')), 0) FROM trash"
    ).fetchone()[0]
    seq = max(int(top), int(trashed or 0), int(get_meta(conn, f"last_{column}", "0") or 0)) + 1
    set_meta(conn, f"last_{column}", str(seq))
    return seq


def filing_section_for(archive: Archive, filed_at: str) -> str:
    local = parse_iso(filed_at).astimezone()  # type: ignore[union-attr]
    if archive.settings.filing_granularity == "year":
        return f"{local.year:04d}"
    return f"{local.year:04d}-{local.month:02d}"


def mark_filed(
    archive: Archive,
    doc_id: str,
    *,
    by: str = "user",
    filed_at: str | None = None,
    binder: str | None = None,
) -> DocumentMetadata:
    """Record that the paper was physically filed now: on top of the current section of the
    current binder (or of ``binder``)."""
    from . import binders

    conn = archive.conn
    binder = binder or binders.current(archive)
    with write_tx(conn):
        meta = load_meta(archive, doc_id)
        if meta.filing_sequence is not None and meta.filing_binder == binder:
            if meta.paper_location:  # it was taken out: back in its place
                return put_back(archive, doc_id)
            return meta
        meta.filed_at = filed_at or now_iso()
        meta.filing_sequence = next_sequence(conn, "filing_sequence")
        meta.filing_section = filing_section_for(archive, meta.filed_at)
        meta.filing_binder = binder
        meta.paper_location = meta.paper_discarded_at = None
        meta.paper = True
        add_history(
            meta,
            HistoryEntry(task="filing", at=now_iso(), status="filed", by=by),  # type: ignore[arg-type]
        )
        persist(archive, meta)
        return meta


def take_out(archive: Archive, doc_id: str, where: str = "") -> DocumentMetadata:
    """The sheet was taken out of its binder (lent, sent, in use). It keeps its place, so
    ``put_back`` returns it there; ``where`` says where it is meanwhile."""
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        if meta.filing_sequence is None:
            raise EditError(_("The paper is not filed."))
        meta.paper_location = " ".join(where.split())[:200] or N_("taken out")
        add_history(meta, HistoryEntry(task="filing", at=now_iso(), status="taken out", by="user"))
        persist(archive, meta)
        return meta


def put_back(archive: Archive, doc_id: str) -> DocumentMetadata:
    """A sheet taken out is back in its old place."""
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        meta.paper_location = None
        add_history(meta, HistoryEntry(task="filing", at=now_iso(), status="put back", by="user"))
        persist(archive, meta)
        return meta


def unmark_filed(archive: Archive, doc_id: str) -> DocumentMetadata:
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        meta.filed_at = meta.filing_section = meta.filing_binder = None
        meta.filing_sequence = None
        meta.paper_location = None
        add_history(meta, HistoryEntry(task="filing", at=now_iso(), status="unfiled", by="user"))
        persist(archive, meta)
        return meta


@dataclass
class FilingPosition:
    binder: str | None
    section: str
    position_from_top: int
    total_in_section: int
    above: list[dict[str, Any]]  # newer documents lying on top of this one
    below: list[dict[str, Any]]


def set_paper_state(
    archive: Archive, doc_id: str, *, location: str | None = None, discarded: bool = False
) -> DocumentMetadata:
    """Where the paper is, besides Heftig's own filing: in an existing folder (`location`),
    shredded (`discarded`), or - both empty - not yet dealt with."""
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        meta.paper_location = (location or "").strip()[:200] or None
        meta.paper_discarded_at = now_iso() if discarded else None
        # not kept: no place in a binder any more. Somewhere else: a filed sheet keeps its
        # place (taken out, see take_out); an unfiled one is simply elsewhere
        if discarded or (meta.paper_location and meta.filing_sequence is None):
            meta.filed_at = meta.filing_section = meta.filing_binder = None
            meta.filing_sequence = None
        status = "discarded" if discarded else ("in_folder" if meta.paper_location else "reset")
        add_history(meta, HistoryEntry(task="filing", at=now_iso(), status=status, by="user"))
        persist(archive, meta)
        return meta


def set_keep_original(archive: Archive, doc_id: str, keep: bool) -> DocumentMetadata:
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        meta.keep_original = keep
        meta.keep_original_source = "user"
        persist(archive, meta)
        return meta


def filing_position(archive: Archive, meta: DocumentMetadata, n: int = 2) -> FilingPosition | None:
    """Where the sheet lies: binder, section, position counted from the top among the sheets
    that are there. Taken-out and not-kept sheets don't count (a taken-out one keeps its
    place); sheets of deleted documents that stayed in the binder do count."""
    from . import binders

    if meta.filing_sequence is None or not meta.filing_section:
        return None
    conn = archive.conn
    sec, seq, binder = meta.filing_section, meta.filing_sequence, meta.filing_binder
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, title, original_filename, filing_sequence FROM documents "
            "WHERE filing_section=? AND filing_binder IS ? AND paper_location IS NULL "
            "AND paper_discarded_at IS NULL AND id != ?",
            (sec, binder, meta.id),
        )
    ]
    for e in binders.kept_sheets(archive.paths, binder, sec):
        if e.get("doc_id") != meta.id:
            rows.append({"id": None, "title": e.get("title") or "", "original_filename": "",
                         "filing_sequence": e["sequence"], "kept": True})  # fmt: skip
    above = sorted(
        (r for r in rows if r["filing_sequence"] > seq), key=lambda r: r["filing_sequence"]
    )
    below = sorted((r for r in rows if r["filing_sequence"] < seq),
                   key=lambda r: r["filing_sequence"], reverse=True)  # fmt: skip
    return FilingPosition(
        binder=binder,
        section=sec,
        position_from_top=len(above) + 1,
        total_in_section=len(rows) + 1,
        above=list(reversed(above[:n])),  # the nearest n, listed from the top down
        below=below[:n],
    )


def reverse_stack(archive: Archive, doc_ids: list[str]) -> int:
    """A stack that went into the binder the other way round: the sheets swap their places
    (the top one gets the place of the bottom one ...); the places of other sheets stay."""
    with write_tx(archive.conn):
        metas = [load_meta(archive, d) for d in doc_ids]
        metas = [m for m in metas if m.filing_sequence is not None]
        places = sorted((m.filing_sequence, m.filing_section, m.filing_binder) for m in metas)
        # places are unique: free them first (same transaction), then hand them out again
        archive.conn.executemany(
            "UPDATE documents SET filing_sequence=NULL WHERE id=?", [(m.id,) for m in metas]
        )
        for m, (seq, section, binder) in zip(
            sorted(metas, key=lambda m: m.filing_sequence or 0), reversed(places), strict=True
        ):
            m.filing_sequence, m.filing_section, m.filing_binder = seq, section, binder
            add_history(m, HistoryEntry(task="filing", at=now_iso(), status="stack reversed",
                                        by="user"))  # fmt: skip
            persist(archive, m)
    return len(metas)


def take_filing(archive: Archive, doc_id: str, placed: DocumentMetadata) -> DocumentMetadata:
    """Give ``doc_id`` the binder place of ``placed`` (a copy that is being deleted): the paper
    lying there now belongs to this document. ``placed`` must be out of the table already."""
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        meta.filed_at, meta.filing_sequence = placed.filed_at, placed.filing_sequence
        meta.filing_section, meta.filing_binder = placed.filing_section, placed.filing_binder
        meta.paper_location, meta.paper_discarded_at = (
            placed.paper_location,
            placed.paper_discarded_at,
        )
        meta.paper = True
        add_history(meta, HistoryEntry(task="filing", at=now_iso(), status="taken over", by="user"))
        persist(archive, meta)
        return meta


# --- deletion --------------------------------------------------------------------------


def delete_document(archive: Archive, doc_id: str) -> dict[str, Any]:
    """Explicitly delete a document incl. its original file. Irreversible."""
    conn = archive.conn
    try:
        old_attachments = load_meta(archive, doc_id).attachments
    except DocumentNotFound:
        old_attachments = []
    with write_tx(conn):
        row = conn.execute(
            "SELECT rowid, sha256, original_relpath, title, original_filename "
            "FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise DocumentNotFound(doc_id)
        fts.remove_document(conn, row["rowid"])
        conn.execute("DELETE FROM duplicate_candidates WHERE doc_a=? OR doc_b=?", (doc_id, doc_id))
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.execute("DELETE FROM jobs WHERE doc_id=? AND status IN ('queued','failed')", (doc_id,))
        conn.execute(
            "INSERT INTO ingest_events(doc_id, sha256, source, filename, result, message, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (
                doc_id,
                row["sha256"],
                "web",
                row["original_filename"],
                "deleted",
                N_("Document “%(title)s” deleted")
                % {"title": row["title"] or row["original_filename"]},
                now_iso(),
            ),
        )
    # files after commit: a crash in between leaves an orphan that `heftig check` reports
    for att in old_attachments:
        p = archive.paths.resolve(att.relpath)
        if p.exists() and not attachment_in_use(archive, att.sha256, att.relpath):
            p.unlink()
    shutil.rmtree(files(archive, doc_id).dir, ignore_errors=True)
    orig = archive.paths.resolve(row["original_relpath"])
    if orig.exists():
        orig.unlink()
    return {"id": doc_id, "sha256": row["sha256"]}


# --- taxonomy operations that touch documents ------------------------------------------


def merge_terms(archive: Archive, source_id: int, target_id: int) -> int:
    """Merge term `source` into `target`; source name becomes an alias. Returns #docs."""
    conn = archive.conn
    with write_tx(conn):
        src = conn.execute("SELECT * FROM taxonomy WHERE id=?", (source_id,)).fetchone()
        dst = conn.execute("SELECT * FROM taxonomy WHERE id=?", (target_id,)).fetchone()
        if not src or not dst or src["kind"] != dst["kind"] or source_id == target_id:
            raise tax.TaxonomyError(_("Merging is only possible within the same kind."))
        doc_ids = tax.affected_documents(conn, source_id)
        aliases = tax.aliases_for(conn, source_id)
        metas = []
        for doc_id in doc_ids:
            meta = load_meta(archive, doc_id)
            if src["kind"] == "correspondent" and meta.correspondent == src["name"]:
                meta.correspondent = dst["name"]
            elif src["kind"] == "document_type" and meta.document_type == src["name"]:
                meta.document_type = dst["name"]
            elif src["kind"] == "tag":
                meta.tags = [dst["name"] if t == src["name"] else t for t in meta.tags]
                for lst in (meta.tag_overrides.added, meta.tag_overrides.removed):
                    lst[:] = [dst["name"] if t == src["name"] else t for t in lst]
            add_history(
                meta,
                HistoryEntry(
                    task="merge",
                    at=now_iso(),
                    status=f"{src['name']} → {dst['name']}",
                    fields=[src["kind"]],
                    by="user",
                ),
            )
            metas.append(meta)
        # clear references before dropping the source term
        conn.execute(
            "UPDATE documents SET correspondent_id=NULL WHERE correspondent_id=?", (source_id,)
        )
        conn.execute(
            "UPDATE documents SET document_type_id=NULL WHERE document_type_id=?", (source_id,)
        )
        conn.execute("DELETE FROM taxonomy WHERE id=?", (source_id,))
        for a in [src["name"], *aliases]:
            tax.add_alias(conn, target_id, a)
        for meta in metas:
            persist(archive, meta)
        tax.write_sidecar(conn, archive.paths)
    return len(doc_ids)


def rename_term(archive: Archive, term_id: int, new_name: str, keep_alias: bool = True) -> int:
    conn = archive.conn
    new_name = clean_display_name(new_name)[:200]
    if not normalize_name(new_name):
        raise tax.TaxonomyError(_("The new name must not be empty."))
    with write_tx(conn):
        row = conn.execute("SELECT * FROM taxonomy WHERE id=?", (term_id,)).fetchone()
        if not row:
            raise tax.TaxonomyError(_("Unknown entry"))
        other = tax.find_term(conn, row["kind"], new_name)
        if other is not None and other != term_id:
            raise tax.TaxonomyError(_("“%(name)s” already exists – please merge.", name=new_name))
        old = row["name"]
        conn.execute(
            "DELETE FROM taxonomy_alias WHERE kind=? AND alias_norm=?",
            (row["kind"], normalize_name(new_name)),
        )
        conn.execute(
            "UPDATE taxonomy SET name=?, norm=? WHERE id=?",
            (new_name, normalize_name(new_name), term_id),
        )
        if keep_alias and normalize_name(old) != normalize_name(new_name):
            tax.add_alias(conn, term_id, old)
        doc_ids = tax.affected_documents(conn, term_id)
        for doc_id in doc_ids:
            meta = load_meta(archive, doc_id)
            if meta.correspondent == old:
                meta.correspondent = new_name
            if meta.document_type == old:
                meta.document_type = new_name
            meta.tags = [new_name if t == old else t for t in meta.tags]
            _rename_overrides(meta, old, new_name)
            persist(archive, meta)
        if row["kind"] == "tag":
            # manual decisions of documents that currently do not carry the tag
            for meta in _docs_with_override(archive, old):
                _rename_overrides(meta, old, new_name)
                persist(archive, meta)
        tax.write_sidecar(conn, archive.paths)
    return len(doc_ids)


def _rename_overrides(meta: DocumentMetadata, old: str, new: str | None) -> None:
    n_old = normalize_name(old)
    for lst in (meta.tag_overrides.added, meta.tag_overrides.removed):
        renamed = [new if normalize_name(t) == n_old else t for t in lst]
        lst[:] = [t for t in dict.fromkeys(renamed) if t]


def _docs_with_override(archive: Archive, name: str) -> list[DocumentMetadata]:
    """Documents whose manual tag decisions mention `name` (not visible in document_tags)."""
    n = normalize_name(name)
    out = []
    rows = archive.conn.execute(
        "SELECT id FROM documents WHERE metadata_json LIKE '%\"tag_overrides\"%'"
    ).fetchall()
    for r in rows:
        meta = load_meta(archive, r[0])
        ov = meta.tag_overrides.added + meta.tag_overrides.removed
        if any(normalize_name(t) == n for t in ov):
            out.append(meta)
    return out


def delete_term(archive: Archive, term_id: int) -> int:
    conn = archive.conn
    with write_tx(conn):
        row = conn.execute("SELECT * FROM taxonomy WHERE id=?", (term_id,)).fetchone()
        if not row:
            raise tax.TaxonomyError(_("Unknown entry"))
        doc_ids = tax.affected_documents(conn, term_id)
        for doc_id in doc_ids:
            meta = load_meta(archive, doc_id)
            if meta.correspondent == row["name"]:
                meta.correspondent = None
            if meta.document_type == row["name"]:
                meta.document_type = None
            meta.tags = [t for t in meta.tags if t != row["name"]]
            _rename_overrides(meta, row["name"], None)
            persist(archive, meta)
        if row["kind"] == "tag":
            for meta in _docs_with_override(archive, row["name"]):
                _rename_overrides(meta, row["name"], None)
                persist(archive, meta)
        conn.execute(
            "UPDATE documents SET correspondent_id=NULL WHERE correspondent_id=?", (term_id,)
        )
        conn.execute(
            "UPDATE documents SET document_type_id=NULL WHERE document_type_id=?", (term_id,)
        )
        conn.execute("DELETE FROM taxonomy WHERE id=?", (term_id,))
        tax.write_sidecar(conn, archive.paths)
    return len(doc_ids)


def add_term_alias(archive: Archive, term_id: int, alias: str) -> None:
    """Add an alias, persist taxonomy.json and reindex affected documents."""
    conn = archive.conn
    with write_tx(conn):
        tax.add_alias(conn, term_id, alias)
        tax.write_sidecar(conn, archive.paths)
        for doc_id in tax.affected_documents(conn, term_id):
            fts.index_document(conn, doc_id)


def remove_term_alias(archive: Archive, term_id: int, alias: str) -> None:
    conn = archive.conn
    with write_tx(conn):
        tax.remove_alias(conn, term_id, alias)
        tax.write_sidecar(conn, archive.paths)
        for doc_id in tax.affected_documents(conn, term_id):
            fts.index_document(conn, doc_id)


# --- notes & attachments ---------------------------------------------------------------

MAX_NOTE_CHARS = 20000


def add_note(
    archive: Archive, doc_id: str, text: str, note_id: str | None = None
) -> DocumentMetadata:
    """A new note. ``note_id`` (chosen by the page) makes adding repeatable: a note with that
    id already there is changed instead - a save sent twice never makes two notes."""
    import uuid as _uuid

    from .models import Note

    if note_id is not None and not re.fullmatch(r"[0-9a-f]{32}", note_id):
        raise EditError(_("Note not found."))
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        exists = note_id is not None and any(n.id == note_id for n in meta.notes)
        if not exists:
            text = text.replace("\r\n", "\n").strip()
            if not text:
                raise EditError(_("The note is empty."))
            if len(text) > MAX_NOTE_CHARS:
                raise EditError(_("Note too long (max. %(num)s characters).", num=MAX_NOTE_CHARS))
            meta.notes.append(Note(id=note_id or _uuid.uuid4().hex, at=now_iso(), text=text))
            add_history(meta, HistoryEntry(task="note", at=now_iso(), status="added", by="user"))
            persist(archive, meta)
            return meta
    return edit_note(archive, doc_id, note_id, text)


def edit_note(archive: Archive, doc_id: str, note_id: str, text: str) -> DocumentMetadata:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return delete_note(archive, doc_id, note_id)
    if len(text) > MAX_NOTE_CHARS:
        raise EditError(_("Note too long (max. %(num)s characters).", num=MAX_NOTE_CHARS))
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        note = next((n for n in meta.notes if n.id == note_id), None)
        if note is None:
            raise EditError(_("Note not found."))
        note.text = text
        note.updated_at = now_iso()
        persist(archive, meta)
        return meta


def delete_note(archive: Archive, doc_id: str, note_id: str) -> DocumentMetadata:
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        before = len(meta.notes)
        meta.notes = [n for n in meta.notes if n.id != note_id]
        if len(meta.notes) == before:
            raise EditError(_("Note not found."))
        add_history(meta, HistoryEntry(task="note", at=now_iso(), status="deleted", by="user"))
        persist(archive, meta)
        return meta


def attachment_path(archive: Archive, doc_id: str, att_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", att_id):
        raise EditError(_("Invalid attachment."))
    meta = load_meta(archive, doc_id)
    att = next((a for a in meta.attachments if a.id == att_id), None)
    if att is None:
        raise EditError(_("Attachment not found."))
    return archive.paths.resolve(att.relpath)


def attachment_in_use(archive: Archive, sha256: str, relpath: str) -> bool:
    """Is this stored file still referenced by a document (also one in the Papierkorb)?"""
    from .trash import is_referenced

    return is_referenced(archive, relpath)


def add_attachment(
    archive: Archive, doc_id: str, stream, filename: str | None, description: str = ""
) -> DocumentMetadata:
    """Store any file byte-identical in originals/attachments (size-limited, never executed)."""
    import os
    import uuid as _uuid

    from .media import sniff_mime
    from .models import Attachment
    from .storage import TooLargeError, display_filename, fsync_dir, sha256_file, stream_to_tmp

    load_meta(archive, doc_id)  # exists?
    try:
        tmp, sha, size = stream_to_tmp(stream, archive.paths.tmp, archive.settings.max_upload_bytes)
    except TooLargeError as e:
        raise EditError(translate_text(str(e))) from e
    try:
        if size == 0:
            raise EditError(_("The file is empty."))
        with open(tmp, "rb") as f:
            mime = sniff_mime(f.read(2048)) or "application/octet-stream"
        name = display_filename(filename)
        relpath = archive.paths.attachment_relpath(sha, name)
        att = Attachment(
            id=_uuid.uuid4().hex,
            filename=name,
            mime_type=mime,
            size_bytes=size,
            sha256=sha,
            relpath=relpath,
            added_at=now_iso(),
            description=(description or "").strip()[:500],
        )
        dest = archive.paths.resolve(relpath)
        with write_tx(archive.conn):
            meta = load_meta(archive, doc_id)
            if dest.exists():
                if sha256_file(dest) != sha:
                    raise EditError(
                        _("Checksum conflict in the attachment store – run `heftig check`.")
                    )
            else:
                if not dest.parent.exists():
                    dest.parent.mkdir(parents=True, mode=0o700)
                    fsync_dir(dest.parent.parent)
                os.chmod(tmp, 0o400)
                os.replace(tmp, dest)
                fsync_dir(dest.parent)
            meta.attachments.append(att)
            add_history(
                meta,
                HistoryEntry(
                    task="attachment", at=now_iso(), status=f"added {att.filename}", by="user"
                ),
            )
            persist(archive, meta)
            return meta
    finally:
        if tmp.exists():
            tmp.unlink()


def delete_attachment(archive: Archive, doc_id: str, att_id: str) -> DocumentMetadata:
    path = attachment_path(archive, doc_id, att_id)
    with write_tx(archive.conn):
        meta = load_meta(archive, doc_id)
        att = next((a for a in meta.attachments if a.id == att_id), None)
        if att is None:
            raise EditError(_("Attachment not found."))
        meta.attachments = [a for a in meta.attachments if a.id != att_id]
        add_history(
            meta,
            HistoryEntry(
                task="attachment", at=now_iso(), status=f"deleted {att.filename}", by="user"
            ),
        )
        persist(archive, meta)
    # file after the commit, only if nothing else uses it; a leftover is reported by `heftig check`
    if path.exists() and not attachment_in_use(archive, att.sha256, att.relpath):
        path.unlink()
    return meta
