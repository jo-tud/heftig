"""Synthetic test documents (no real personal data)."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def text_pdf(pages: list[str]) -> bytes:
    """PDF with an embedded text layer, one string per page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("test")
    for page in pages:
        y = 800
        for line in page.splitlines():
            c.setFont("Helvetica", 11)
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def _font(size: int):
    for p in (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    ):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def text_image(text: str, size=(1240, 1754)) -> Image.Image:
    """White page image with black text (an A4 'scan' at 150 dpi)."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    font = _font(34)
    y = 120
    for line in text.splitlines():
        d.text((100, y), line, fill="black", font=font)
        y += 52
    return img


def image_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def scan_pdf(pages: list[str]) -> bytes:
    """Image-only PDF (no text layer), like a scanner produces."""
    imgs = [text_image(t) for t in pages]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:], resolution=150)
    return buf.getvalue()


def multipage_tiff(pages: list[str]) -> bytes:
    imgs = [text_image(t).convert("L") for t in pages]
    buf = io.BytesIO()
    imgs[0].save(buf, format="TIFF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()
