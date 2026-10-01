"""An e-mail as PDF pages for the page viewer, thumbnails and search marks.

Derived from the archived ``.eml`` (never stored as an original, recreated when needed) and
deterministic: the same mail always gives the same pages, so word positions and page texts
stay valid. A small PDF writer with the standard fonts Helvetica / Helvetica-Bold (no font
files, no extra dependency); characters these fonts cannot show are drawn as their nearest
Latin letter or "?" - the document's text keeps them (it is taken from the mail itself).
"""

from __future__ import annotations

import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime

from .mail import Mail

PAGE_W, PAGE_H = 595.28, 841.89  # A4 in points
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 62.0, 60.0, 58.0
TEXT_W = PAGE_W - 2 * MARGIN_X

# advance widths (1/1000 em) of the standard fonts for the characters 32..126 (Adobe AFM)
_REGULAR_ASCII = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,  # space../
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,  # 0..?
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,  # @..O
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,  # P.._
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,  # `..o
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,  # p..~
]  # fmt: skip
_BOLD_ASCII = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]  # fmt: skip
# the other WinAnsi characters that are not a letter with an accent (those take the width
# of the plain letter): (regular, bold)
_SPECIAL = {
    "€": (556, 556), "‚": (222, 278), "ƒ": (556, 556), "„": (333, 500), "…": (1000, 1000),
    "†": (556, 556), "‡": (556, 556), "ˆ": (333, 333), "‰": (1000, 1000), "‹": (333, 333),
    "Œ": (1000, 1000), "‘": (222, 278), "’": (222, 278), "“": (333, 500), "”": (333, 500),
    "•": (350, 350), "–": (556, 556), "—": (1000, 1000), "˜": (333, 333), "™": (1000, 1000),
    "›": (333, 333), "œ": (944, 944), "\xa0": (278, 278), "¡": (333, 333), "¢": (556, 556),
    "£": (556, 556), "¤": (556, 556), "¥": (556, 556), "¦": (260, 280), "§": (556, 556),
    "¨": (333, 333), "©": (737, 737), "ª": (370, 370), "«": (556, 556), "¬": (584, 584),
    "\xad": (333, 333), "®": (737, 737), "¯": (333, 333), "°": (400, 400), "±": (584, 584),
    "²": (333, 333), "³": (333, 333), "´": (333, 333), "µ": (556, 611), "¶": (537, 556),
    "·": (278, 278), "¸": (333, 333), "¹": (333, 333), "º": (365, 365), "»": (556, 556),
    "¼": (834, 834), "½": (834, 834), "¾": (834, 834), "¿": (611, 611), "Æ": (1000, 1000),
    "Ð": (722, 722), "×": (584, 584), "Ø": (778, 778), "Þ": (667, 667), "ß": (611, 611),
    "æ": (889, 889), "ð": (556, 611), "÷": (584, 584), "ø": (611, 611), "þ": (556, 611),
}  # fmt: skip
# what is drawn for characters outside WinAnsi that have no Latin base letter
_SUBSTITUTE = {
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "−": "-",
    "‐": "-", "‑": "-", "‒": "–", "―": "—", "•": "•", "●": "•",
    "▪": "•", "→": "->", "←": "<-", "≤": "<=", "≥": ">=",
    "✓": "v", "✔": "v", "‘": "‘", "‛": "'", "′": "'", "″": '"',
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "ħ": "h",
}  # fmt: skip


def _width_table(ascii_widths: list[int], bold: bool) -> list[int]:
    """Widths for the codes 32..255 of the WinAnsi encoding."""
    out = list(ascii_widths)
    for code in range(127, 256):
        ch = bytes([code]).decode("cp1252", "replace")
        out.append(_char_width(ch, ascii_widths, bold))
    return out


def _char_width(ch: str, ascii_widths: list[int], bold: bool) -> int:
    if ch in _SPECIAL:
        return _SPECIAL[ch][bold]
    base = unicodedata.normalize("NFKD", ch)[:1]
    if base and 32 <= ord(base) <= 126:
        return ascii_widths[ord(base) - 32]
    return 556


WIDTHS = {"F1": _width_table(_REGULAR_ASCII, False), "F2": _width_table(_BOLD_ASCII, True)}
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold"}


def winansi(text: str) -> str:
    """The text as the standard fonts can show it (every character encodable in cp1252)."""
    out = []
    for ch in text:
        if ch in ("\t", "\n"):
            out.append(" ")
            continue
        try:
            ch.encode("cp1252")
            if ord(ch) >= 32 and ch not in "\x7f\x81\x8d\x8f\x90\x9d":
                out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        if ch in _SUBSTITUTE:
            out.append(_SUBSTITUTE[ch])
            continue
        if unicodedata.category(ch) in ("Mn", "Me", "Cf", "Cc"):
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", ch) if unicodedata.category(c) != "Mn"
        )
        try:
            base.encode("cp1252")
            out.append(base if base else "?")
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def text_width(text: str, font: str, size: float) -> float:
    widths = WIDTHS[font]
    total = 0
    for b in winansi(text).encode("cp1252"):
        total += widths[b - 32] if b >= 32 else 0
    return total * size / 1000


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Break one paragraph into lines no wider than `width` (at spaces; overlong words such
    as links are broken anywhere). Leading spaces (indented or quoted text) are kept."""
    if not text.strip():
        return [""]
    indent = text[: len(text) - len(text.lstrip(" "))]
    words = text.strip(" ").split(" ")
    lines: list[str] = []
    line = indent
    space = text_width(" ", font, size)
    line_w = text_width(indent, font, size)
    for word in words:
        if not word:
            continue
        w = text_width(word, font, size)
        if line.strip() and line_w + space + w <= width:
            line += " " + word
            line_w += space + w
            continue
        if line.strip():
            lines.append(line)
            line, line_w = indent, text_width(indent, font, size)
        while line_w + w > width and len(word) > 1:  # break an overlong word
            cut = len(word)
            while cut > 1 and line_w + text_width(word[:cut], font, size) > width:
                cut -= 1
            lines.append(line + word[:cut])
            word = word[cut:]
            line, line_w = indent, text_width(indent, font, size)
            w = text_width(word, font, size)
        line += word
        line_w += w
    if line.strip() or not lines:
        lines.append(line)
    return lines


# --- layout ---------------------------------------------------------------------------------


@dataclass
class _Text:
    font: str
    size: float
    x: float
    y: float  # baseline, from the bottom
    text: str
    gray: float = 0.0


@dataclass
class _Page:
    texts: list[_Text] = field(default_factory=list)
    rules: list[tuple[float, float, float]] = field(default_factory=list)  # x0, x1, y
    lines: list[str] = field(default_factory=list)  # the page's text, line by line


@dataclass
class Labels:
    """The words of the header block, in the archive's language."""

    sender: str = "From"
    to: str = "To"
    cc: str = "Cc"
    date: str = "Date"
    attachments: str = "Attachments"
    no_subject: str = "(no subject)"
    date_format: str = "%d %b %Y, %H:%M"


class _Writer:
    def __init__(self) -> None:
        self.pages: list[_Page] = []
        self.y = 0.0
        self._new_page()

    def _new_page(self) -> None:
        self.pages.append(_Page())
        self.y = PAGE_H - MARGIN_TOP

    @property
    def page(self) -> _Page:
        return self.pages[-1]

    def space(self, points: float) -> None:
        self.y -= points

    def line(self, parts: list[tuple[str, str, float, float, float]], leading: float,
             text: str) -> None:  # fmt: skip
        """One line of text: parts (font, text, size, x, gray); `text` for the page text."""
        if self.y - leading < MARGIN_BOTTOM:
            self._new_page()
        self.y -= leading
        for font, s, size, x, gray in parts:
            if s.strip():
                self.page.texts.append(_Text(font, size, x, self.y, s, gray))
        self.page.lines.append(text)

    def rule(self) -> None:
        if self.y - 12 < MARGIN_BOTTOM:
            self._new_page()
            return
        self.page.rules.append((MARGIN_X, PAGE_W - MARGIN_X, self.y))


def _format_date(d: datetime | None, raw: str, fmt: str) -> str:
    if d is None:
        return raw
    return d.strftime(fmt)


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def layout(mail: Mail, labels: Labels) -> list[_Page]:
    w = _Writer()
    subject = mail.subject or labels.no_subject
    for i, line in enumerate(wrap(subject, "F2", 15, TEXT_W)):
        w.line([("F2", line, 15, MARGIN_X, 0)], 19 if i else 15, line)
    w.space(8)

    rows = [(labels.sender, mail.sender), (labels.to, mail.to)]
    if mail.cc:
        rows.append((labels.cc, mail.cc))
    rows.append((labels.date, _format_date(mail.date, mail.date_text, labels.date_format)))
    if mail.attachments:
        names = ", ".join(f"{a.filename} ({_size(a.size)})" for a in mail.attachments)
        rows.append((labels.attachments, names))
    label_w = max(text_width(label, "F2", 9) for label, _ in rows) + 10
    for label, value in rows:
        if not value:
            continue
        for i, line in enumerate(wrap(value, "F1", 9, TEXT_W - label_w)):
            parts = [("F1", line, 9, MARGIN_X + label_w, 0.1)]
            if i == 0:
                parts.append(("F2", label, 9, MARGIN_X, 0.45))
            w.line(parts, 12.5, f"{label}: {line}" if i == 0 else line)
    w.space(9)
    w.rule()
    w.space(8)

    for paragraph in mail.body.split("\n"):
        quoted = paragraph.lstrip().startswith(">")
        for line in wrap(paragraph, "F1", 10, TEXT_W):
            w.line([("F1", line, 10, MARGIN_X, 0.35 if quoted else 0)], 13.6, line)

    total = len(w.pages)
    if total > 1:
        for n, page in enumerate(w.pages, 1):
            label = f"{n} / {total}"
            x = PAGE_W - MARGIN_X - text_width(label, "F1", 8)
            page.texts.append(_Text("F1", 8, x, MARGIN_BOTTOM - 24, label, 0.5))
    return w.pages


# --- PDF ------------------------------------------------------------------------------------


def _pdf_string(text: str) -> bytes:
    out = bytearray(b"(")
    for b in winansi(text).encode("cp1252"):
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif 32 <= b < 127:
            out.append(b)
        else:
            out += b"\\%03o" % b
    return bytes(out + b")")


def _num(v: float) -> bytes:
    return (b"%.2f" % v).rstrip(b"0").rstrip(b".")


def _content(page: _Page) -> bytes:
    ops = []
    for x0, x1, y in page.rules:
        ops.append(b"0.75 G 0.6 w %s %s m %s %s l S" % (_num(x0), _num(y), _num(x1), _num(y)))
    for t in page.texts:
        ops.append(
            b"BT /%s %s Tf %s g 1 0 0 1 %s %s Tm %s Tj ET"
            % (t.font.encode(), _num(t.size), _num(t.gray), _num(t.x), _num(t.y),
               _pdf_string(t.text))
        )  # fmt: skip
    return b"\n".join(ops)


def to_pdf(pages: list[_Page], title: str = "") -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in below
    tree = add(b"")
    fonts = {}
    for name, base in FONTS.items():
        widths = b" ".join(b"%d" % w for w in WIDTHS[name])
        fonts[name] = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding "
            b"/FirstChar 32 /LastChar 255 /Widths [%s] >>" % (base.encode(), widths)
        )
    resources = b"<< /Font << %s >> >>" % b" ".join(
        b"/%s %d 0 R" % (n.encode(), i) for n, i in fonts.items()
    )
    kids = []
    for page in pages:
        data = zlib.compress(_content(page), 9)
        stream = add(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(data), data)
        )
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources %s /Contents %d 0 R >>"
            % (tree, _num(PAGE_W), _num(PAGE_H), resources, stream)
        ))  # fmt: skip
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objects[tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids),
    )  # fmt: skip
    info = add(b"<< /Title %s /Producer (Heftig) >>" % _pdf_string(title[:200]))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, info, xref,
    )  # fmt: skip
    return bytes(out)


def render(mail: Mail, labels: Labels | None = None) -> tuple[bytes, list[str]]:
    """The PDF and the text of each page."""
    pages = layout(mail, labels or Labels())
    return to_pdf(pages, mail.subject), ["\n".join(p.lines).strip("\n") for p in pages]
