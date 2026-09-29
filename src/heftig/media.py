"""File type detection by content and resource-limited PDF/image handling.

The extension of an incoming file is never trusted: the type is sniffed from magic bytes and
then confirmed by actually opening the file with the PDF/image library.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageSequence

from .i18n import N_

# pdfium is not thread-safe; serialise all calls into it.
PDFIUM_LOCK = threading.RLock()

SUPPORTED = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tif",
}


class UnsupportedFileError(ValueError):
    """The file is not one of the supported document formats (or is broken)."""


@dataclass
class MediaInfo:
    mime_type: str
    ext: str
    page_count: int


def sniff_mime(head: bytes) -> str | None:
    if head.startswith(b"%PDF-") or (b"%PDF-" in head[:1024]):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image/tiff"
    return None


def _set_pixel_limit(max_megapixels: int) -> None:
    Image.MAX_IMAGE_PIXELS = max_megapixels * 1_000_000


def inspect_file(path: Path, max_pages: int, max_megapixels: int) -> MediaInfo:
    """Validate a file and return its type and page count, or raise UnsupportedFileError."""
    with open(path, "rb") as f:
        head = f.read(2048)
    mime = sniff_mime(head)
    if mime is None:
        raise UnsupportedFileError(
            N_("File type not supported. Allowed are PDF, JPEG, PNG and TIFF.")
        )
    if mime == "application/pdf":
        try:
            with PDFIUM_LOCK:
                pdf = pdfium.PdfDocument(str(path))
                try:
                    pages = len(pdf)
                finally:
                    pdf.close()
        except pdfium.PdfiumError as e:
            msg = str(e)
            if "password" in msg.lower():
                raise UnsupportedFileError(N_("PDF is password-protected.")) from e
            raise UnsupportedFileError(
                N_("PDF is damaged or unreadable (%(error)s).") % {"error": msg}
            ) from e
        if pages < 1:
            raise UnsupportedFileError(N_("PDF has no pages."))
    else:
        _set_pixel_limit(max_megapixels)
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                pages = getattr(im, "n_frames", 1) if mime == "image/tiff" else 1
                # every frame of a TIFF, not only the first
                for i in range(min(pages, max_pages + 1)):
                    if i:
                        im.seek(i)
                    w, h = im.size
                    if w * h > max_megapixels * 1_000_000:
                        raise UnsupportedFileError(N_("Image is too large."))
        except Image.DecompressionBombError as e:
            raise UnsupportedFileError(N_("Image is too large (pixel limit).")) from e
        except UnsupportedFileError:
            raise
        except Exception as e:  # PIL raises many exception types for broken files
            raise UnsupportedFileError(
                N_("Image is damaged or unreadable (%(error)s).") % {"error": e}
            ) from e
    if pages > max_pages:
        raise UnsupportedFileError(
            N_("Too many pages (%(pages)s, the limit is %(max)s).")
            % {"pages": pages, "max": max_pages}
        )
    return MediaInfo(mime_type=mime, ext=SUPPORTED[mime], page_count=pages)


def pdf_embedded_text(path: Path) -> list[str]:
    """Embedded text layer per page (empty string for image-only pages)."""
    out: list[str] = []
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    tp = page.get_textpage()
                    try:
                        text = tp.get_text_bounded()
                    finally:
                        tp.close()
                finally:
                    page.close()
                out.append(text.replace("\r\n", "\n").replace("\r", "\n"))
        finally:
            pdf.close()
    return out


def render_page(
    path: Path, mime: str, page_index: int, dpi: int, max_megapixels: int
) -> Image.Image:
    """Render one page (PDF) or frame (image) to an RGB PIL image, size-capped."""
    _set_pixel_limit(max_megapixels)
    if mime == "application/pdf":
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                page = pdf[page_index]
                try:
                    w, h = page.get_size()  # points (1/72 inch)
                    scale = dpi / 72
                    limit = max_megapixels * 1_000_000
                    if w * h * scale * scale > limit:
                        scale = (limit / (w * h)) ** 0.5
                    img = page.render(scale=scale).to_pil()
                finally:
                    page.close()
            finally:
                pdf.close()
        return img.convert("RGB")
    with Image.open(path) as im:
        if mime == "image/tiff":
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i == page_index:
                    return _upright(frame.copy()).convert("RGB")
            raise IndexError(page_index)
        return _upright(im.copy()).convert("RGB")


def _upright(img: Image.Image) -> Image.Image:
    from PIL import ImageOps

    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def to_png_bytes(img: Image.Image, max_side: int | None = None) -> bytes:
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def ink_ratio(path: Path, mime: str, page_index: int, max_megapixels: int) -> float:
    """Share of clearly dark pixels on a page (margins trimmed), from a 40 dpi rendering.

    Scanner edge shadows are cut off and faint show-through from the back is ignored, so a
    blank duplex back side gives ~0; a single short line of text still gives ~0.0005-0.001.
    """
    img = render_page(path, mime, page_index, 40, max_megapixels).convert("L")
    w, h = img.size
    mx, my = int(w * 0.06), int(h * 0.05)
    if w - 2 * mx > 10 and h - 2 * my > 10:
        img = img.crop((mx, my, w - mx, h - my))
    hist = img.histogram()
    total = sum(hist)
    # background = median brightness (paper), dark = clearly darker than the paper
    acc, bg = 0, 255
    for v in range(255, -1, -1):
        acc += hist[v]
        if acc >= total * 0.5:
            bg = v
            break
    threshold = max(0, bg - 90)
    dark = sum(hist[:threshold])
    return dark / max(1, total)


MAX_OCR_IMAGE_BYTES = 3_500_000  # providers limit images to ~5 MB (base64 adds a third)


def ocr_image_bytes(img: Image.Image, max_side: int) -> tuple[bytes, str]:
    """Page image for a cloud OCR provider: at most `max_side` px long and a few MB.

    Providers downscale large images themselves, so this loses nothing the model would see,
    but noisy 300 dpi scans as PNG easily reach 10-15 MB and get rejected. PNG while small
    enough (sharpest text), otherwise JPEG.
    """
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    if buf.tell() <= MAX_OCR_IMAGE_BYTES:
        return buf.getvalue(), "image/png"
    rgb = img.convert("RGB")
    for quality in (90, 80, 70):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_OCR_IMAGE_BYTES:
            break
    return buf.getvalue(), "image/jpeg"


# List thumbnails. At 72 px width body text is unreadable anyway; what people recognise a
# document by at that size is layout, colour and logo (Kaasten et al. 2002), with the title and
# sender shown as text next to it. So: as much page as possible (margins and scanner edges
# trimmed), scanned paper evened out to white (contrast before downscaling helps recognition,
# Burton et al. 1995), exact sizes per screen density (no second resampling in the browser),
# sharpened lightly at the final size.
THUMB_VERSION = 2
THUMB_WIDTHS = (54, 72, 108, 144, 162, 216)
PREVIEW_WIDTH = 144  # the stored preview.webp (exports, fallbacks)


def _trim(img: Image.Image, max_share: float = 0.12) -> Image.Image:
    """Cut empty margins and dark scanner edges, at most max_share per side; the bottom stays
    (the list box shows the top of the page)."""
    from PIL import ImageChops, ImageFilter

    g = img.convert("L")
    small = g.resize((max(1, g.width // 8), max(1, g.height // 8)), Image.BOX)
    bg = small.filter(ImageFilter.MaxFilter(7))
    ink = ImageChops.subtract(bg, small).point(lambda v: 255 if v > 40 else 0)
    box = ink.getbbox()
    if not box:
        return img
    sx, sy = g.width / small.width, g.height / small.height
    pad = 0.02 * g.width
    left = max(0.0, min(box[0] * sx - pad, g.width * max_share))
    top = max(0.0, min(box[1] * sy - pad, g.height * max_share))
    right = min(float(g.width), max(box[2] * sx + pad, g.width * (1 - max_share)))
    return img.crop((round(left), round(top), round(right), g.height))


def _even_paper(img: Image.Image) -> Image.Image:
    """Scans: divide by the estimated paper brightness (uneven light, grey or yellowish paper)
    and stretch, the same gain on all channels (logos and stamps keep their colour). Pages that
    are mostly white already (born-digital) are left alone."""
    from PIL import ImageChops, ImageFilter

    rgb = img.convert("RGB")
    g = rgb.convert("L")
    hist, acc, median = g.histogram(), 0, 255
    for v, n in enumerate(hist):
        acc += n
        if acc >= g.width * g.height / 2:
            median = v
            break
    if median >= 242:
        return rgb
    small = g.resize((max(8, g.width // 16), max(8, g.height // 16)), Image.BOX)
    bg = (
        small.filter(ImageFilter.MaxFilter(5))
        .filter(ImageFilter.GaussianBlur(2))
        .resize(g.size, Image.BILINEAR)
    )
    gain = bg.point(lambda v: min(255, round(255 * 235 / max(v, 60))))  # multiply: x * gain/255
    flat = Image.merge("RGB", [ImageChops.multiply(ch, gain) for ch in rgb.split()])
    return flat.point(lambda v: max(0, min(255, round((v - 20) * 255 / 215))))


def make_thumbnail(
    path: Path, mime: str, max_megapixels: int, width: int, page_index: int = 0
) -> bytes:
    from PIL import ImageFilter

    img = render_width(path, mime, page_index, width * 3, max_megapixels)
    img = _even_paper(_trim(img))
    img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS,
                     reducing_gap=2.0)  # fmt: skip
    img = img.filter(ImageFilter.UnsharpMask(radius=0.6, percent=50, threshold=3))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=88, method=4)
    return buf.getvalue()


def make_preview(
    path: Path, mime: str, max_megapixels: int, width: int = PREVIEW_WIDTH, page_index: int = 0
) -> bytes:
    return make_thumbnail(path, mime, max_megapixels, width, page_index)


def page_sizes(path: Path, mime: str) -> list[tuple[float, float]]:
    """Width/height of every page (PDF points or image pixels) - for the page viewer layout."""
    if mime == "application/pdf":
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                out = []
                for i in range(len(pdf)):
                    page = pdf[i]
                    try:
                        out.append(tuple(page.get_size()))
                    finally:
                        page.close()
                return out  # type: ignore[return-value]
            finally:
                pdf.close()
    with Image.open(path) as im:
        if mime == "image/tiff":
            return [tuple(f.size) for f in ImageSequence.Iterator(im)]  # type: ignore[misc]
        img = _upright(im.copy())
        return [img.size]  # type: ignore[list-item]


def render_width(
    path: Path, mime: str, page_index: int, width: int, max_megapixels: int
) -> Image.Image:
    """Render one page so that it is `width` pixels wide (aspect ratio kept)."""
    if mime == "application/pdf":
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                page = pdf[page_index]
                try:
                    w, h = page.get_size()
                    scale = width / w
                    limit = max_megapixels * 1_000_000
                    if w * h * scale * scale > limit:
                        scale = (limit / (w * h)) ** 0.5
                    img = page.render(scale=scale).to_pil()
                finally:
                    page.close()
            finally:
                pdf.close()
        return img.convert("RGB")
    img = render_page(path, mime, page_index, 72, max_megapixels)
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    return img
