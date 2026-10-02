"""Splitting a document: its pages - cut into parts, put in a new order, some left out, some
turned - become new documents.

Typical case: one scan holds two letters, or a letter plus an unrelated sheet and a blank
page. Like combining, nothing is changed in place - originals are never modified:

- each part is a new archived PDF; PDF (and e-mail) pages are copied as they are (text layer
  included), image frames become PDF pages with their JPEG data;
- the recognised text is carried over page by page - no second (paid) text recognition;
- cut into several parts, every part is a new document: classified from scratch on its own
  text (title, date, sender, type, tags, summary, fields - nothing of the original's, no
  locks, as those described all of it); it keeps only where it came from (source, arrival,
  paper, scan session, where the paper is);
- only rearranged (one part: pages turned, moved or left out), it is the same document: it
  keeps the original's fields and their locks, the classification respects them;
- the user's own additions - notes, attachments - and the filing position go to the first
  part; the paper of the others lies with it;
- pages the user turned keep their turn (as ``page_rotation``, like any turned page);
- the original moves to the Papierkorb as batch ``split-<original id>``, but is kept there
  (not purged) as long as one of its parts exists, live or in the Papierkorb; every part
  names the original file in ``split_from.originals``, so the file itself stays as long as a
  part does (``trash.is_referenced``). Undoing restores it and moves the parts (also those of
  a part that was split again) to the Papierkorb.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pypdfium2 as pdfium

from . import documents as docs
from . import trash
from .archive import Archive
from .combine import add_image_page, take_filing
from .db import now_iso
from .i18n import N_, _, language, translate_text
from .ingest import ingest_stream
from .media import PAGED, PDFIUM_LOCK, image_pdf_pages, make_preview, open_pdf
from .models import DocumentMetadata, HistoryEntry, PageText, TextPages

log = logging.getLogger("heftig.split")

BATCH_PREFIX = "split-"
TURNS = (0, 90, 180, 270)
# provenance of the original that does not describe a part
_NOT_CARRIED = ("action", "combined_from", "split_from")
# what a document that was only rearranged keeps of the original (a split part: none of it)
_SAME_DOCUMENT = (
    "title", "document_date", "document_date_status", "document_date_reason", "correspondent",
    "document_type", "tags", "summary", "custom_fields", "field_locks", "field_sources",
    "tag_overrides", "keep_original", "keep_original_reason", "keep_original_source",
    "not_duplicate_of",
)  # fmt: skip
# documents being split right now (a second request for the same one is refused)
_BUSY: set[str] = set()
_BUSY_LOCK = threading.Lock()


class SplitError(ValueError):
    pass


def batch_for(doc_id: str) -> str:
    return BATCH_PREFIX + doc_id


# --- checking the request ----------------------------------------------------------------


def check(
    archive: Archive, doc_id: str, parts: list[list[int]], turns: dict[int, int] | None = None
) -> tuple[DocumentMetadata, list[list[int]], dict[int, int]]:
    """The original, the non-empty parts and the turn of every kept page (degrees clockwise,
    absolute) - or SplitError with a reason the user can act on."""
    meta = docs.load_meta(archive, doc_id)
    count = meta.page_count or 1
    parts = [list(p) for p in parts if p]
    seen = [n for p in parts for n in p]
    if not seen:
        raise SplitError(_("Keep at least one page."))
    bad = next((n for n in seen if not 1 <= n <= count), None)
    if bad is not None:
        raise SplitError(_("There is no page %(num)s.", num=bad))
    if len(set(seen)) != len(seen):
        raise SplitError(_("Each page can only be in one part."))
    turns = {n: int((turns or {}).get(n, docs.rotation(meta, n))) % 360 for n in seen}
    if any(t not in TURNS for t in turns.values()):
        raise SplitError(_("Pages can only be turned in steps of 90 degrees."))
    if parts == [list(range(1, count + 1))] and all(
        turns[n] == docs.rotation(meta, n) for n in seen
    ):
        raise SplitError(_("Nothing to change – cut, move, turn or remove a page first."))
    busy = archive.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing') AND doc_id=?",
        (doc_id,),
    ).fetchone()[0]
    if busy:
        raise SplitError(_("The document is still being processed – please wait a moment."))
    return meta, parts, turns


# --- building the PDFs -------------------------------------------------------------------


def build_pdfs(archive: Archive, meta: DocumentMetadata, parts: list[list[int]]) -> list[bytes]:
    """One PDF per part, with the original's pages (1-based) in the given order."""
    path = archive.paths.resolve(meta.original_relpath)
    frames = None if meta.mime_type in PAGED else image_pdf_pages(path, meta.mime_type)
    out = []
    with PDFIUM_LOCK:
        src = open_pdf(path, meta.mime_type) if frames is None else None
        try:
            for pages in parts:
                pdf = pdfium.PdfDocument.new()
                try:
                    if src is not None:
                        pdf.import_pages(src, [n - 1 for n in pages])
                    else:
                        for n in pages:
                            add_image_page(pdf, *frames[n - 1])  # type: ignore[index]
                    buf = io.BytesIO()
                    pdf.save(buf)
                finally:
                    pdf.close()
                out.append(buf.getvalue())
        finally:
            if src is not None:
                src.close()
    return out


# --- carrying data over ------------------------------------------------------------------


def _text(tp: TextPages | None, pages: list[int]) -> TextPages | None:
    """The original's recognised text of these pages, renumbered - None if there is none."""
    if tp is None:
        return None
    by_page = {p.page: p for p in tp.pages}
    if any(n not in by_page for n in pages):
        return None
    out: list[PageText] = [
        by_page[n].model_copy(update={"page": i}) for i, n in enumerate(pages, 1)
    ]
    return TextPages(
        page_count=len(out), pages=out, extracted_at=now_iso(), user_confirmed=tp.user_confirmed
    )


def _text_status(pages: list[PageText]) -> str:
    """As the text recognition decides it (processing.py), for a part's pages."""
    failed = sum(1 for p in pages if p.error)
    chars = sum(p.chars for p in pages)
    if not failed:
        return "ok" if chars else "empty"
    return "failed" if failed == len(pages) and not chars else "partial"


def _provenance(orig: DocumentMetadata) -> dict[str, Any]:
    return {k: v for k, v in orig.source_details.items() if k not in _NOT_CARRIED}


def _split_from(
    orig: DocumentMetadata, title: str, pages: list[int], num: int, total: int
) -> dict[str, Any]:
    """Where a part came from. ``originals``: the original's file and, if the original was
    itself split or combined, the files before it - kept as long as this part exists."""
    return {
        "id": orig.id, "title": title, "pages": pages, "part": num, "parts": total,
        "filename": orig.original_filename, "mime_type": orig.mime_type,
        "originals": list(
            dict.fromkeys([orig.original_relpath, *docs.source_originals(orig.source_details)])
        ),
    }  # fmt: skip


def _carry_over(
    archive: Archive,
    meta: DocumentMetadata,
    orig: DocumentMetadata,
    pages: list[int],
    turns: dict[int, int],
    tp: TextPages | None,
    num: int,
    total: int,
) -> None:
    """Runs inside the ingest transaction of part ``num`` (1-based)."""
    title = orig.title or orig.original_filename
    # where it came from: the same for every part
    meta.source = orig.source
    meta.received_at = orig.received_at
    meta.paper = orig.paper
    meta.scan_session = orig.scan_session
    meta.ocr_all_pages = orig.ocr_all_pages
    meta.source_details = {
        **_provenance(orig),
        "action": "split",
        "split_from": _split_from(orig, title, pages, num, total),
    }
    if total == 1:  # only rearranged: still the same document, with what the user decided
        for f in _SAME_DOCUMENT:
            setattr(meta, f, getattr(orig, f))
    if num == 1:  # what the user added stays with the first part; the rest is classified anew
        meta.notes = list(orig.notes)
        meta.attachments = list(orig.attachments)
        if not orig.filed_at:  # a filing position is taken over once the original has gone
            meta.paper_location = orig.paper_location
            meta.paper_discarded_at = orig.paper_discarded_at
    elif orig.filed_at:  # the sheets lie with the first part: nothing to file
        meta.paper_location = (
            N_("Binder %(name)s, with part 1") % {"name": orig.filing_binder}
            if orig.filing_binder
            else N_("With part 1")
        )
    else:
        meta.paper_location = orig.paper_location
        meta.paper_discarded_at = orig.paper_discarded_at
    # the user's decisions per page, on the pages' new numbers
    meta.page_blank = {
        i: orig.page_blank[n] for i, n in enumerate(pages, 1) if n in orig.page_blank
    }
    meta.page_rotation = {i: turns[n] for i, n in enumerate(pages, 1) if turns[n]}  # type: ignore[misc]
    if tp is not None:
        docs.write_text(archive, meta.id, tp)
        meta.text_status = _text_status(tp.pages)  # type: ignore[assignment]
        failed = sum(1 for p in tp.pages if p.error)
        if failed:
            meta.review_reasons.append(
                N_("Text: %(failed)s of %(pages)s pages without recognized text")
                % {"failed": failed, "pages": len(tp.pages)}
            )
        if "extract" in orig.ai_pending:
            meta.ai_pending = ["extract"]
    try:
        cover = docs.cover_page(archive, meta)
        preview = make_preview(
            archive.paths.resolve(meta.original_relpath), meta.mime_type,
            archive.settings.max_image_megapixels, page_index=cover,
            rotation=docs.rotation(meta, cover + 1),
        )  # fmt: skip
        docs.write_preview(archive, meta.id, preview)
    except Exception as e:  # noqa: BLE001 - the preview is optional and regenerable
        log.warning("split part %s: preview failed: %s", meta.id, type(e).__name__)
    status = (
        N_("split from “%(title)s” (part %(num)s of %(total)s)")
        % {"title": title, "num": num, "total": total}
        if total > 1
        else N_("pages of “%(title)s” rearranged") % {"title": title}
    )
    docs.add_history(meta, HistoryEntry(task="split", at=now_iso(), status=status, by="user"))  # type: ignore[arg-type]
    docs.persist(archive, meta, bump=False)


# --- the operation -----------------------------------------------------------------------


def split(
    archive: Archive,
    doc_id: str,
    parts: list[list[int]],
    turns: dict[int, int] | None = None,
    by: str = "web",
) -> list[DocumentMetadata]:
    """New documents from the original's pages (1-based, in this order, one list per part;
    pages in no part are left out), pages turned as in ``turns`` (absolute, page -> degrees;
    missing: as they are now). The original goes to the Papierkorb (kept there as long as one
    of the parts exists)."""
    with _one_at_a_time(doc_id):
        return _split(archive, doc_id, parts, turns, by)


@contextmanager
def _one_at_a_time(doc_id: str) -> Iterator[None]:
    """Two tabs or a repeated request: the second split of the same document is refused while
    the first runs. (Across processes, the second one fails when it trashes the original -
    and discards its parts.)"""
    with _BUSY_LOCK:
        if doc_id in _BUSY:
            raise SplitError(_("The document is being split right now – please wait a moment."))
        _BUSY.add(doc_id)
    try:
        yield
    finally:
        with _BUSY_LOCK:
            _BUSY.discard(doc_id)


def _split(
    archive: Archive,
    doc_id: str,
    parts: list[list[int]],
    turns: dict[int, int] | None,
    by: str,
) -> list[DocumentMetadata]:
    orig, parts, turns = check(archive, doc_id, parts, turns)
    pdfs = build_pdfs(archive, orig, parts)
    shas = [hashlib.sha256(b).hexdigest() for b in pdfs]
    if len(set(shas)) != len(shas):
        raise SplitError(_("Two parts would be identical – please check the cuts."))
    for n, sha in enumerate(shas, 1):
        row = archive.conn.execute(
            "SELECT title, original_filename FROM documents WHERE sha256=?", (sha,)
        ).fetchone()
        if row:
            raise SplitError(
                _("Part %(num)s is already archived as “%(title)s”.", num=n, title=row[0] or row[1])
            )
    tp = docs.load_text_pages(archive, doc_id) if orig.text_status != "pending" else None
    stem = (orig.title or orig.original_filename.rsplit(".", 1)[0])[:150]
    total = len(parts)
    created: list[str] = []
    try:
        for num, (pages, data) in enumerate(zip(parts, pdfs, strict=True), 1):
            ptp = _text(tp, pages)
            with language(archive.settings.language):  # a file name: in the archive's language
                name = (
                    _("%(name)s (part %(num)s of %(total)s)", name=stem, num=num, total=total)
                    if total > 1
                    else _("%(name)s (edited)", name=stem)
                )
            result = ingest_stream(
                archive, io.BytesIO(data), name + ".pdf", "web", {"action": "split"}, paper=False,
                stages=("classify",) if ptp is not None else ("extract", "classify"),
                on_create=lambda meta, pages=pages, ptp=ptp, num=num: _carry_over(
                    archive, meta, orig, pages, turns, ptp, num, total
                ),
            )  # fmt: skip
            if result.status != "created" or result.doc_id is None:
                raise SplitError(translate_text(result.message) or _("Splitting not possible."))
            created.append(result.doc_id)
        # the original only goes once all parts are safely archived
        reason = (
            N_("split into %(num)s documents") % {"num": total}
            if total > 1
            else N_("pages rearranged in a new document")
        )
        try:
            trash.trash_document(archive, orig.id, reason=reason, batch=batch_for(orig.id), by=by)
        except docs.DocumentNotFound:  # split (or deleted) meanwhile by another request
            raise SplitError(
                _("The document was changed meanwhile – please open it again.")
            ) from None
    except BaseException:
        _discard(archive, created)
        raise
    if orig.filed_at:
        take_filing(archive, created[0], orig)
    return [docs.load_meta(archive, i) for i in created]


def _discard(archive: Archive, ids: list[str]) -> None:
    """Parts created before a later part failed: gone again, for good."""
    for i in ids:
        try:
            trash.trash_document(archive, i, reason=N_("Splitting not possible."))
            trash.purge(archive, i)
        except Exception:  # noqa: BLE001 - best effort; the original is untouched
            log.exception("split: could not remove part %s", i)


def parts_of(archive: Archive, orig_id: str) -> list[dict[str, Any]]:
    """The live documents split from ``orig_id``, in part order."""
    rows = archive.conn.execute(
        "SELECT id, title, original_filename, page_count, "
        "json_extract(metadata_json, '$.source_details.split_from.part') AS part "
        "FROM documents WHERE json_extract(metadata_json, '$.source_details.split_from.id') = ? "
        "ORDER BY part",
        (orig_id,),
    )
    return [dict(r) for r in rows]


def descendants(archive: Archive, orig_id: str) -> list[str]:
    """The live documents made from ``orig_id``: its parts, and the parts of a part that was
    split again (that part is in the Papierkorb as batch ``split-<part id>``)."""
    out = [r["id"] for r in parts_of(archive, orig_id)]
    resplit = archive.conn.execute(
        "SELECT id FROM trash WHERE batch = ? || id "
        "AND json_extract(metadata_json, '$.source_details.split_from.id') = ? "
        "ORDER BY json_extract(metadata_json, '$.source_details.split_from.part')",
        (BATCH_PREFIX, orig_id),
    ).fetchall()
    for (part_id,) in resplit:
        out += descendants(archive, part_id)
    return out


def undo(archive: Archive, orig_id: str) -> dict[str, Any]:
    """Restore the original; what was made from it goes to the Papierkorb (first: the first
    part holds the original's filing position)."""
    batch = trash.new_batch()
    moved = []
    for part_id in descendants(archive, orig_id):
        try:
            trash.trash_document(archive, part_id, reason=N_("Splitting undone"), batch=batch)
            moved.append(part_id)
        except docs.DocumentNotFound:
            pass  # deleted meanwhile
    r = trash.restore_batch(archive, batch_for(orig_id))
    if moved and not r["restored"]:
        trash.restore_batch(archive, batch)  # the original did not come back: keep the parts
    return r
