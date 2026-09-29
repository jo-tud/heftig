"""Combining documents: the pages of several documents become one new document.

Typical cases: the pages of one letter were scanned separately, or two copies of the same
paper belong together (one signed, one with handwritten notes). The new document is an
ordinary archived PDF; the parts are never changed:

- PDF pages are copied as they are (text layer included), images become PDF pages with their
  JPEG data unchanged (other images are stored as JPEG);
- the recognised text of the parts is carried over page by page - no second (paid) text
  recognition; only the classification runs again, on the whole text, and respects the fields
  the user locked on the first part;
- notes, attachments, the paper location and scan session are carried over, the metadata of
  the first part is the starting point;
- the parts move to the Papierkorb as one batch ``combine-<new id>``. Undoing restores them
  and moves the combined document to the Papierkorb.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pypdfium2 as pdfium
from PIL import Image, ImageOps, ImageSequence

from . import documents as docs
from . import trash
from .archive import Archive
from .db import now_iso, write_tx
from .i18n import N_, _, language, translate_text
from .ingest import ingest_stream
from .media import PDFIUM_LOCK, make_preview
from .models import DocumentMetadata, HistoryEntry, PageText, TextPages

log = logging.getLogger("heftig.combine")

BATCH_PREFIX = "combine-"
A4_WIDTH_PT = 595.28
JPEG_QUALITY = 92


class CombineError(ValueError):
    pass


def batch_for(doc_id: str) -> str:
    return BATCH_PREFIX + doc_id


# --- building the PDF --------------------------------------------------------------------


def _image_pages(path, mime: str) -> list[tuple[bytes, float, float]]:
    """(JPEG bytes, width pt, height pt) per image frame. JPEGs without rotation stay as
    they are; everything else is encoded once as a high-quality JPEG."""
    out = []
    with Image.open(path) as im:
        frames = ImageSequence.Iterator(im) if mime == "image/tiff" else [im]
        for frame in frames:
            orientation = frame.getexif().get(0x0112, 1) if mime == "image/jpeg" else 1
            dpi = frame.info.get("dpi")
            if mime == "image/jpeg" and orientation in (None, 1) and frame.mode in ("RGB", "L"):
                data = path.read_bytes()
                w, h = frame.size
            else:
                img = (
                    ImageOps.exif_transpose(frame.copy()) if mime == "image/jpeg" else frame.copy()
                )
                img = img.convert("L" if img.mode in ("1", "L", "LA", "I;16") else "RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                data = buf.getvalue()
                w, h = img.size
            try:
                dx = float(dpi[0]) if dpi else 0.0
            except (TypeError, ValueError, IndexError):
                dx = 0.0
            if 50 <= dx <= 1200:
                wpt, hpt = w / dx * 72, h / dx * 72
            else:  # unknown resolution: as wide as an A4 page
                wpt, hpt = A4_WIDTH_PT, A4_WIDTH_PT * h / w
            out.append((data, wpt, hpt))
    return out


def build_pdf(archive: Archive, metas: list[DocumentMetadata]) -> bytes:
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument.new()
        try:
            for m in metas:
                path = archive.paths.resolve(m.original_relpath)
                if m.mime_type == "application/pdf":
                    src = pdfium.PdfDocument(str(path))
                    try:
                        pdf.import_pages(src)
                    finally:
                        src.close()
                    continue
                for data, wpt, hpt in _image_pages(path, m.mime_type):
                    page = pdf.new_page(wpt, hpt)
                    img = pdfium.PdfImage.new(pdf)
                    img.load_jpeg(io.BytesIO(data), inline=False, autoclose=True)
                    img.set_matrix(pdfium.PdfMatrix().scale(wpt, hpt))
                    page.insert_obj(img)
                    page.gen_content()
                    page.close()
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue()
        finally:
            pdf.close()


# --- carrying data over ------------------------------------------------------------------


def _text(archive: Archive, metas: list[DocumentMetadata]) -> TextPages | None:
    """The parts' recognised text, pages renumbered - None if a part has none yet."""
    pages: list[PageText] = []
    confirmed = True
    for m in metas:
        tp = docs.load_text_pages(archive, m.id)
        if tp is None or m.text_status == "pending":
            return None
        confirmed = confirmed and tp.user_confirmed
        for p in tp.pages:
            pages.append(p.model_copy(update={"page": len(pages) + 1}))
    return TextPages(
        page_count=len(pages), pages=pages, extracted_at=now_iso(), user_confirmed=confirmed
    )


def _text_status(metas: list[DocumentMetadata]) -> str:
    st = {m.text_status for m in metas}
    if len(st) == 1:
        return st.pop()
    if st & {"failed", "partial"}:
        return "partial"
    return "ok"


def _carry_over(
    archive: Archive, meta: DocumentMetadata, parts: list[DocumentMetadata], tp: TextPages | None
) -> None:
    first = parts[0]
    ids = {m.id for m in parts}
    for f in ("title", "document_date", "document_date_status", "document_date_reason",
              "correspondent", "document_type", "tags", "summary", "custom_fields",
              "field_locks", "field_sources", "tag_overrides", "source"):  # fmt: skip
        setattr(meta, f, getattr(first, f))
    meta.received_at = min(m.received_at for m in parts)
    meta.paper = any(m.paper for m in parts)
    meta.scan_session = next((m.scan_session for m in parts if m.scan_session), None)
    # where the paper is: from the first part that knows it (the filing position itself is
    # unique and taken over once the parts are in the Papierkorb, see combine())
    placed = _placed(parts)
    if placed is not None and not placed.filed_at:
        meta.paper_location = placed.paper_location
        meta.paper_discarded_at = placed.paper_discarded_at
    keep = next((m for m in parts if m.keep_original), None) or first
    meta.keep_original = keep.keep_original
    meta.keep_original_reason = keep.keep_original_reason
    meta.keep_original_source = keep.keep_original_source
    meta.notes = sorted((n for m in parts for n in m.notes), key=lambda n: n.at)
    seen: set[str] = set()
    meta.attachments = []
    for m in parts:
        for att in m.attachments:
            if att.sha256 not in seen:
                seen.add(att.sha256)
                meta.attachments.append(att)
    meta.not_duplicate_of = sorted({x for m in parts for x in m.not_duplicate_of} - ids - {meta.id})
    meta.ocr_all_pages = any(m.ocr_all_pages for m in parts)
    offset = 0
    meta.page_blank = {}
    for m in parts:  # the user's blank-page decisions, on the pages' new numbers
        meta.page_blank.update({offset + int(k): v for k, v in m.page_blank.items()})
        offset += m.page_count or 1
    meta.source_details = {
        **meta.source_details,
        "combined_from": [
            {"id": m.id, "title": m.title or m.original_filename, "pages": m.page_count or 1}
            for m in parts
        ],
    }
    if tp is not None:
        docs.write_text(archive, meta.id, tp)
        meta.text_status = _text_status(parts)  # type: ignore[assignment]
        meta.review_reasons = list(
            dict.fromkeys(r for m in parts for r in m.review_reasons if r.startswith("Text:"))
        )
        if any("extract" in m.ai_pending for m in parts):
            meta.ai_pending = ["extract"]
    try:
        preview = make_preview(
            archive.paths.resolve(meta.original_relpath), meta.mime_type,
            archive.settings.max_image_megapixels, page_index=docs.cover_page(archive, meta),
        )  # fmt: skip
        docs.write_preview(archive, meta.id, preview)
    except Exception as e:  # noqa: BLE001 - the preview is optional and regenerable
        log.warning("combined doc %s: preview failed: %s", meta.id, type(e).__name__)
    docs.add_history(
        meta,
        HistoryEntry(
            task="merge", at=now_iso(), by="user",
            status=N_("combined from %(num)s documents") % {"num": len(parts)},
        ),
    )  # fmt: skip
    docs.persist(archive, meta, bump=False)


def _placed(parts: list[DocumentMetadata]) -> DocumentMetadata | None:
    return next((m for m in parts if m.filed_at or m.paper_location or m.paper_discarded_at), None)


def _take_filing(archive: Archive, doc_id: str, placed: DocumentMetadata) -> None:
    with write_tx(archive.conn):
        meta = docs.load_meta(archive, doc_id)
        meta.filed_at, meta.filing_sequence, meta.filing_section, meta.filing_binder = (
            placed.filed_at, placed.filing_sequence, placed.filing_section, placed.filing_binder,
        )  # fmt: skip
        meta.paper_location, meta.paper_discarded_at = (
            placed.paper_location,
            placed.paper_discarded_at,
        )
        docs.persist(archive, meta)


# --- the operation -----------------------------------------------------------------------


def check(archive: Archive, ids: list[str]) -> list[DocumentMetadata]:
    """The parts in order, or CombineError with a reason the user can act on."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if len(ids) < 2:
        raise CombineError(_("Please select at least two documents."))
    metas = [docs.load_meta(archive, i) for i in ids]
    busy = archive.conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing') AND doc_id IN "
        f"({','.join('?' * len(ids))})",
        ids,
    ).fetchone()[0]
    if busy:
        raise CombineError(_("A document is still being processed – please wait a moment."))
    pages = sum(m.page_count or 1 for m in metas)
    if pages > archive.settings.max_pages:
        raise CombineError(
            _(
                "%(pages)s pages together – the limit is %(max)s.",
                pages=pages,
                max=archive.settings.max_pages,
            )
        )
    return metas


def combine(archive: Archive, ids: list[str], by: str = "web") -> DocumentMetadata:
    """Combine the documents (in this order) into a new one; the parts go to the Papierkorb."""
    parts = check(archive, ids)
    data = build_pdf(archive, parts)
    tp = _text(archive, parts)
    stem = (parts[0].title or parts[0].original_filename.rsplit(".", 1)[0])[:150]
    with language(archive.settings.language):  # a file name: in the archive's language
        filename = _("%(name)s (combined)", name=stem) + ".pdf"
    result = ingest_stream(
        archive, io.BytesIO(data), filename, "web",
        {"action": "combine"}, paper=False,
        stages=("classify",) if tp is not None else ("extract", "classify"),
        on_create=lambda meta: _carry_over(archive, meta, parts, tp),
    )  # fmt: skip
    if result.status != "created" or result.doc_id is None:
        raise CombineError(translate_text(result.message) or _("Combining not possible."))
    # the parts only go once the new document is safely archived
    new = docs.load_meta(archive, result.doc_id)
    reason = N_("combined into “%(title)s”") % {"title": new.title or new.original_filename}
    for m in parts:
        try:
            trash.trash_document(archive, m.id, reason=reason, batch=batch_for(new.id), by=by)
        except docs.DocumentNotFound:
            pass  # deleted meanwhile (by hand, or as an identical copy): nothing to move
    placed = _placed(parts)
    if placed is not None and placed.filed_at:
        _take_filing(archive, new.id, placed)
    return docs.load_meta(archive, new.id)


def undo(archive: Archive, combined_id: str) -> dict[str, Any]:
    """Restore the parts; the combined document goes to the Papierkorb (first: it holds the
    filing position of a part)."""
    try:
        trash.trash_document(archive, combined_id, reason=N_("Combining undone"))
        moved = True
    except docs.DocumentNotFound:
        moved = False  # already deleted by hand
    r = trash.restore_batch(archive, batch_for(combined_id))
    if moved and not r["restored"]:
        trash.restore(archive, combined_id)  # nothing came back: keep the combined document
    return r


# --- choosing the parts ------------------------------------------------------------------

_ROW = (
    "SELECT d.id, d.title, d.original_filename, d.page_count, d.received_at, d.document_date, "
    "d.ingest_sequence, d.revision, t.name AS correspondent FROM documents d "
    "LEFT JOIN taxonomy t ON t.id = d.correspondent_id "
)


def rows(archive: Archive, ids: list[str]) -> list[dict[str, Any]]:
    """The documents in the given order (unknown ids are skipped)."""
    if not ids:
        return []
    found = {
        r["id"]: dict(r)
        for r in archive.conn.execute(_ROW + f"WHERE d.id IN ({','.join('?' * len(ids))})", ids)
    }
    return [found[i] for i in ids if i in found]


def candidates(archive: Archive, selected: list[str], q: str = "") -> list[dict[str, Any]]:
    """What might belong to the selection: a search, else the neighbours in scan order and
    similar documents."""
    from .search import SearchParams, search, similar

    if q:
        found = [i["id"] for i in search(archive.conn, SearchParams(q=q, per_page=20)).items]
    else:
        seqs = [r["ingest_sequence"] for r in rows(archive, selected)]
        found = []
        if seqs:
            found = [
                r[0]
                for r in archive.conn.execute(
                    "SELECT id FROM documents WHERE ingest_sequence BETWEEN ? AND ? "
                    "ORDER BY ABS(ingest_sequence - ?), ingest_sequence",
                    (min(seqs) - 3, max(seqs) + 3, seqs[-1]),
                )
            ]
            found += [s["id"] for s in similar(archive.conn, selected[0], limit=5)]
    return rows(archive, [i for i in dict.fromkeys(found) if i not in selected])[:20]
