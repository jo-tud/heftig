"""E-mails as documents.

An e-mail is archived as it is: the ``.eml`` file (RFC 5322, as Thunderbird, Evolution, KMail,
Apple Mail or Outlook on the web save it) is the original and is never changed. What is shown
and searched is derived from it: the text (a header block, then the body; HTML-only mails
converted to plain text, never shown as HTML) and a PDF rendering for the page viewer
(``mailpdf.py``). Attachments stay inside the original; PDFs and images among them also become
documents of their own (``ingest.py``), the document page lists the others for download.
"""

from __future__ import annotations

import base64
import email
import hashlib
import logging
import quopri
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("heftig.mail")

MIME = "message/rfc822"
EXT = "eml"
MAX_BODY_CHARS = 500_000  # longer bodies are cut (the original keeps everything)

# --- recognising an e-mail file ------------------------------------------------------------

_FIELD = re.compile(rb"^[!-9;-~]{1,76}:")  # RFC 5322 field name and colon
_HEAD_LIMIT = 256 * 1024  # long Received/DKIM chains; a header section is never this large
_TYPICAL = {b"date", b"message-id", b"subject", b"received", b"mime-version", b"to"}


def looks_like_mail(path: Path) -> bool:
    """Does the file start with an e-mail header section? Header fields from the first line
    on (an mbox ``From `` line is allowed), up to an empty line, with a From field and at least
    one other typical field. The extension is not looked at."""
    with open(path, "rb") as f:
        head = f.read(_HEAD_LIMIT)
    return looks_like_mail_bytes(head)


def looks_like_mail_bytes(head: bytes) -> bool:
    if b"\x00" in head[:4096]:
        return False
    lines = head.split(b"\n")
    if lines and lines[0].startswith(b"From "):
        lines = lines[1:]
    names: set[bytes] = set()
    for i, line in enumerate(lines):
        line = line.rstrip(b"\r")
        if not line:
            return i > 0 and b"from" in names and bool(names & _TYPICAL)
        if line[:1] in (b" ", b"\t"):
            if i == 0:
                return False
            continue  # folded continuation of the previous field
        if not _FIELD.match(line):
            return False
        names.add(line.split(b":", 1)[0].strip().lower())
    return False  # no end of the header section within the limit


# --- parsing --------------------------------------------------------------------------------


@dataclass
class MailPart:
    """An attachment of the e-mail (also an attached e-mail, as .eml)."""

    index: int  # position in Mail.attachments - addresses it for downloads
    filename: str
    mime_type: str
    size: int
    sha256: str


@dataclass
class Forwarded:
    """The header block of a message forwarded inline (\"-------- Forwarded Message --------\")."""

    subject: str = ""
    sender: str = ""
    date: datetime | None = None
    date_text: str = ""


@dataclass
class Mail:
    subject: str = ""
    sender: str = ""  # "Name <address>" as written
    sender_address: str = ""
    to: str = ""
    cc: str = ""
    date: datetime | None = None
    date_text: str = ""  # the Date header as written
    message_id: str = ""
    body: str = ""
    attachments: list[MailPart] = field(default_factory=list)
    forwarded: Forwarded | None = None

    @property
    def title_subject(self) -> str:
        """The subject without Re:/Fwd: prefixes - of the forwarded message, if there is one."""
        if self.forwarded and self.forwarded.subject:
            return clean_subject(self.forwarded.subject)
        return clean_subject(self.subject)

    @property
    def document_date(self) -> datetime | None:
        """When it was written: the forwarded message's date for a forward."""
        if self.forwarded and self.forwarded.date:
            return self.forwarded.date
        return self.date


def message(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 - malformed header: shown as it is
        value = next((v for k, v in msg.raw_items() if k.lower() == name.lower()), "")
    return " ".join(str(value or "").split())


def _addresses(msg: Message, name: str) -> str:
    raw = _header(msg, name)
    try:
        pairs = getaddresses([raw])
    except Exception:  # noqa: BLE001
        return raw
    out = []
    for display, addr in pairs:
        if not addr and not display:
            continue
        out.append(f"{display} <{addr}>" if display and addr else (addr or display))
    return ", ".join(out) or raw


def _date(msg: Message) -> datetime | None:
    raw = _header(msg, "Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None


def _leaves(part: Message):
    """Leaf parts, without descending into attached e-mails (they are one attachment)."""
    if part.get_content_type() == "message/rfc822":
        yield part
    elif part.is_multipart():
        for p in part.get_payload():
            yield from _leaves(p)
    else:
        yield part


def payload(part: Message) -> bytes:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        if not (isinstance(inner, list) and inner):
            return part.get_payload(decode=True) or b""
        cte = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte in ("base64", "quoted-printable"):
            # an attached e-mail encoded once more (some programs do): the parser saw only
            # the encoded text as its body
            body = inner[0].as_bytes(policy=policy.default)
            try:
                return base64.b64decode(body) if cte == "base64" else quopri.decodestring(body)
            except ValueError:
                return body
        return inner[0].as_bytes(policy=policy.default)
    return part.get_payload(decode=True) or b""


def _text(part: Message) -> str:
    try:
        text = part.get_content()  # type: ignore[attr-defined]
        if not isinstance(text, str):
            raise TypeError
    except Exception:  # noqa: BLE001 - unknown charset, broken encoding
        data = part.get_payload(decode=True) or b""
        text = data.decode(part.get_content_charset() or "utf-8", "replace") if data else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if part.get_content_subtype() == "plain" and (
        str(part.get_param("format") or "").lower() == "flowed"
    ):
        text = _unflow(text, str(part.get_param("delsp") or "").lower() == "yes")
    return text


def _unflow(text: str, delsp: bool) -> str:
    """format=flowed (RFC 3676): lines ending in a space continue on the next line."""
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if line.startswith(" "):
            line = line[1:]  # space-stuffing
        if line.endswith(" ") and line != "-- ":
            buf += line[:-1] if delsp else line
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out)


def _is_attachment(part: Message, body_parts: list[Message]) -> bool:
    if any(part is b for b in body_parts):
        return False
    disp = part.get_content_disposition()
    ctype = part.get_content_type()
    if disp == "attachment" or ctype == "message/rfc822":
        return True
    if ctype in ("text/plain", "text/html") or ctype.startswith("multipart/"):
        return False  # alternative bodies
    if ctype in ("application/pgp-signature", "application/pkcs7-signature",
                 "application/x-pkcs7-signature", "message/delivery-status"):  # fmt: skip
        return False
    # inline images referenced by the HTML (logos) have a Content-ID and no own name
    return bool(part.get_filename()) or not part.get("Content-ID")


def part_filename(part: Message, n: int) -> str:
    """The attachment's file name (made up from its type or subject if it has none)."""
    try:
        name = part.get_filename() or ""
    except Exception:  # noqa: BLE001
        name = ""
    name = " ".join(name.replace("/", "_").replace("\\", "_").split())[:200]
    if name:
        return name
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        subject = _header(inner[0], "Subject") if isinstance(inner, list) and inner else ""
        return f"{clean_subject(subject)[:80] or 'message'}.eml"
    ext = part.get_content_subtype().split("+")[0][:8]
    return f"attachment-{n}.{ext}"


def attachment_parts(msg: Message) -> list[Message]:
    body_parts = _body_parts(msg)
    return [p for p in _leaves(msg) if p is not msg and _is_attachment(p, body_parts)]


def _body_parts(msg: Message) -> list[Message]:
    parts = []
    get_body = getattr(msg, "get_body", None)
    if get_body is None:
        return parts
    for kind in ("plain", "html"):
        try:
            p = get_body(preferencelist=(kind,))
        except Exception:  # noqa: BLE001
            p = None
        if p is not None and p.get_content_disposition() != "attachment":
            parts.append(p)
    return parts


def body_text(msg: Message) -> str:
    """The readable text: the plain-text part, or the HTML part converted to text when there is
    no plain part or it is only a stub ("view this e-mail in your browser")."""
    plain = html = None
    for p in _body_parts(msg):
        if p.get_content_subtype() == "plain":
            plain = _text(p)
        else:
            html = html_to_text(_text(p))
    if plain is None and html is None and not msg.is_multipart():
        ctype = msg.get_content_type()
        if ctype.startswith("text/"):
            plain = _text(msg)
    if plain is not None and html is not None and len(plain.strip()) < 0.3 * len(html.strip()):
        plain = None  # a stub
    return _tidy(plain if plain is not None else (html or ""))


def _tidy(text: str) -> str:
    text = text.replace("\t", "    ")
    text = "".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf"))
    lines = [line.rstrip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n[…]"
    return text


def parse(raw: bytes) -> Mail:
    msg = message(raw)
    mail = Mail(
        subject=_header(msg, "Subject"),
        sender=_addresses(msg, "From"),
        sender_address=parseaddr(_header(msg, "From"))[1].lower(),
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        date=_date(msg),
        date_text=_header(msg, "Date"),
        message_id=_header(msg, "Message-ID"),
        body=body_text(msg),
    )
    for n, part in enumerate(attachment_parts(msg)):
        data = payload(part)
        mail.attachments.append(
            MailPart(
                index=n,
                filename=part_filename(part, n + 1),
                mime_type=part.get_content_type(),
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    if is_forward(mail.subject):
        mail.forwarded = forwarded_header(mail.body)
    return mail


def attachment(raw: bytes, index: int) -> tuple[MailPart, bytes]:
    """One attachment (by MailPart.index) and its content; IndexError if there is none."""
    parts = attachment_parts(message(raw))
    if not 0 <= index < len(parts):
        raise IndexError(index)
    part = parts[index]
    data = payload(part)
    info = MailPart(index, part_filename(part, index + 1), part.get_content_type(), len(data),
                    hashlib.sha256(data).hexdigest())  # fmt: skip
    return info, data


# --- subjects and forwards ------------------------------------------------------------------

_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|antw|wg|fw|fwd|wtr|tr|sv|vs|rv|r|enc|doorst)\s*(?:\[\d+\]|\(\d+\))?\s*:\s*)+",
    re.I,
)
_FORWARD = re.compile(r"^\s*(?:(?:re|aw)\s*:\s*)*(?:wg|fw|fwd|wtr|tr|vs|enc|doorst)\s*:", re.I)


def clean_subject(subject: str) -> str:
    """Without Re:/AW:/Fwd:/WG: prefixes and surrounding space."""
    return " ".join(_PREFIX.sub("", subject or "").split())


def _keyword(keyword: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w#])" + re.escape(keyword.strip()) + r"(?!\w)", re.I)


def has_keyword(subject: str, keyword: str) -> bool:
    """Does the subject contain the keyword (as a word of its own, any case)?"""
    return bool(keyword.strip()) and bool(_keyword(keyword).search(subject or ""))


def remove_keyword(subject: str, keyword: str) -> str:
    if not keyword.strip():
        return subject
    return " ".join(_keyword(keyword).sub(" ", subject or "").split())


def is_forward(subject: str) -> bool:
    return bool(_FORWARD.match(subject or ""))


_FWD_MARK = re.compile(
    r"^[ \t>]*(?:-{2,}[ \t]*(?:weitergeleitete nachricht|forwarded message|original message|"
    r"ursprüngliche nachricht|originalnachricht|message transféré|messaggio inoltrato)"
    r"[ \t]*-{2,}|_{10,}|(?:anfang der weitergeleiteten nachricht|begin forwarded message)[ \t]*:)"
    r"[ \t]*$",
    re.I | re.M,
)
_FWD_KEYS = {
    "subject": ("betreff", "subject", "objet", "oggetto"),
    "date": ("datum", "date", "gesendet", "sent", "envoyé", "data"),
    "sender": ("von", "from", "de", "da"),
}


def forwarded_header(body: str) -> Forwarded | None:
    """Subject, sender and date of a message forwarded inline, from the header block after the
    forward line. None if there is no such block."""
    m = _FWD_MARK.search(body)
    if not m:
        return None
    fwd = Forwarded()
    seen = 0
    for line in body[m.end() :].lstrip("\n").split("\n")[:15]:
        line = line.lstrip("> \t").strip()
        if not line:
            if seen:
                break
            continue
        key, sep, value = line.partition(":")
        if not sep:
            if seen:
                break
            continue
        key, value = key.strip().lower().strip("*"), value.strip().strip("*").strip()
        for name, keys in _FWD_KEYS.items():
            if key in keys and not getattr(fwd, name if name != "date" else "date_text"):
                if name == "date":
                    fwd.date_text = value
                    fwd.date = _loose_date(value)
                else:
                    setattr(fwd, name, value)
                seen += 1
    return fwd if seen else None


def _loose_date(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass
    from .providers.rules import find_date

    iso, _ = find_date(text)
    if not iso:
        return None
    t = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    hour, minute = (int(t.group(1)), int(t.group(2))) if t else (0, 0)
    if t and re.search(r"\bpm\b", text, re.I) and hour < 12:
        hour += 12
    try:
        return datetime.fromisoformat(iso).replace(hour=hour % 24, minute=minute)
    except ValueError:
        return None


# --- HTML to text ---------------------------------------------------------------------------

_BLOCK = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul", "center",
}  # fmt: skip
_PARAGRAPH = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "ul", "ol", "pre"}
_SKIP = {"script", "style", "head", "title", "noscript", "template", "svg", "object"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
         "track", "wbr"}  # fmt: skip
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|max-height\s*:\s*0", re.I)


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip: list[str] = []  # open elements whose content is not shown
        self.pre = 0
        self.cells = 0

    def _newline(self, n: int = 1) -> None:
        if not self.out:
            return
        tail = "".join(self.out[-4:])
        have = len(tail) - len(tail.rstrip("\n"))
        if have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.skip:
            if tag not in _VOID:
                self.skip.append(tag)
            return
        attributes = dict(attrs)
        hidden = _HIDDEN.search(attributes.get("style") or "") or "hidden" in attributes
        if tag in _SKIP or (hidden and tag not in _VOID):
            if tag not in _VOID:
                self.skip.append(tag)
            return
        if tag == "br":
            self.out.append("\n")
        elif tag == "tr":
            self._newline()
            self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self.out.append("    ")
            self.cells += 1
        elif tag == "li":
            self._newline()
            self.out.append("• ")
        elif tag == "hr":
            self._newline()
            self.out.append("―" * 20)
            self._newline()
        elif tag in _BLOCK:
            self._newline(2 if tag in _PARAGRAPH else 1)
        if tag == "pre":
            self.pre += 1

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            if tag in self.skip:
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in _BLOCK:
            self._newline(2 if tag in _PARAGRAPH else 1)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        data = data.replace("\xa0", " ")
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            prev = self.out[-1] if self.out else "\n"
            if prev.endswith((" ", "\n")):
                data = data.lstrip(" ")
        if data:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - broken HTML: whatever was read so far
        log.debug("HTML of an e-mail not fully read", exc_info=True)
    lines = [line.strip(" ") if line.strip() else "" for line in "".join(parser.out).split("\n")]
    text = "\n".join(lines)
    text = "".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf"))
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")
