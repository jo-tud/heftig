"""Word positions on a page, for marking search hits in the page viewer.

PDFs with a text layer: positions of the embedded text (pdfium). Scans and photos: word boxes
from Tesseract on the rendered page - computed on the first request and cached next to the
rendered page images (``documents/<id>/cache/``, regenerable like them). Without Tesseract there
are no boxes; the viewer still jumps to the pages with hits.

Boxes are normalised to 0..1 (x from the left, y from the top of the page as displayed).
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

import pypdfium2 as pdfium

from . import documents as docs
from .archive import Archive
from .media import PDFIUM_LOCK, render_width, to_png_bytes
from .models import DocumentMetadata
from .storage import atomic_write_json, read_json

log = logging.getLogger("heftig.wordboxes")

CACHE_VERSION = 1
OCR_WIDTH = 1700  # px; enough for Tesseract on A4 without taking long
MIN_TEXT_CHARS = 20  # fewer characters in the text layer -> treat the page as a scan
Word = list  # [text, x0, y0, x1, y1]
_OCR_LOCK = threading.Lock()  # one Tesseract run at a time (CPU), others then hit the cache


def _pdf_words(path: Path, index: int) -> list[Word] | None:
    """Words of the embedded text layer, or None if the page has (almost) no text layer."""
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            page = pdf[index]
            try:
                if page.get_rotation() % 360:
                    return None  # rotated pages: the displayed box differs, OCR instead
                left, bottom, right, top = page.get_cropbox()
                pw, ph = right - left, top - bottom
                tp = page.get_textpage()
                try:
                    words: list[Word] = []
                    cur: list = []
                    nchars = 0

                    def flush() -> None:
                        if cur:
                            text = "".join(c[0] for c in cur)
                            x0 = min(c[1] for c in cur)
                            x1 = max(c[3] for c in cur)
                            y0 = min(c[2] for c in cur)
                            y1 = max(c[4] for c in cur)
                            words.append([
                                text, round((x0 - left) / pw, 4), round((top - y1) / ph, 4),
                                round((x1 - left) / pw, 4), round((top - y0) / ph, 4),
                            ])  # fmt: skip
                            cur.clear()

                    for i in range(tp.count_chars()):
                        ch = tp.get_text_range(i, 1)
                        if not ch or ch.isspace():
                            flush()
                            continue
                        cl, cb, cr, ct = tp.get_charbox(i)
                        if cr <= cl or ct <= cb:
                            continue
                        nchars += 1
                        cur.append((ch, cl, cb, cr, ct))
                    flush()
                finally:
                    tp.close()
            finally:
                page.close()
        finally:
            pdf.close()
    return words if nchars >= MIN_TEXT_CHARS else None


def _ocr_words(archive: Archive, meta: DocumentMetadata, index: int) -> list[Word]:
    if not shutil.which("tesseract"):
        return []
    from .providers.base import ProviderError, ProviderUnavailable
    from .providers.tesseract import TesseractExtractor

    img = render_width(
        archive.paths.resolve(meta.original_relpath), meta.mime_type, index, OCR_WIDTH,
        archive.settings.max_image_megapixels,
    )  # fmt: skip
    w, h = img.size
    try:
        boxes = TesseractExtractor(timeout=120).word_boxes(
            to_png_bytes(img), index + 1, archive.settings.ocr_languages
        )
    except (ProviderError, ProviderUnavailable) as e:
        log.warning("word boxes for %s page %s: %s", meta.id, index + 1, e)
        return []
    return [
        [t, round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)]
        for t, x0, y0, x1, y1 in boxes
    ]


def page_words(archive: Archive, meta: DocumentMetadata, page: int) -> tuple[list[Word], str]:
    """Words of one page (1-based) and where they come from: "pdf", "ocr" or "none"."""
    cache = docs.files(archive, meta.id).dir / "cache" / f"words-p{page}.json"

    def cached() -> tuple[list[Word], str] | None:
        try:
            data = read_json(cache)
        except (OSError, ValueError):
            return None
        if data.get("v") == CACHE_VERSION and data.get("sha256") == meta.sha256:
            return data["words"], data["source"]
        return None

    hit = cached()
    if hit is not None:
        return hit
    words = None
    if meta.mime_type == "application/pdf":
        words = _pdf_words(archive.paths.resolve(meta.original_relpath), page - 1)
    source = "pdf"
    if words is None:
        with _OCR_LOCK:
            hit = cached()
            if hit is not None:
                return hit
            words = _ocr_words(archive, meta, page - 1)
        source = "ocr" if words else "none"
    if docs.cache_writable(archive, meta.id):
        atomic_write_json(
            cache, {"v": CACHE_VERSION, "sha256": meta.sha256, "source": source, "words": words}
        )
    return words, source
