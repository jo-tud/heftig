"""Interface languages: English source texts, translations from ``locale/<lang>/*.po``.

The code and the templates are written in English; ``_("Inbox")`` returns the text in the
language of the current request (a context variable set by the web app). Catalogues are plain
gettext ``.po`` files, read at startup - no compile step.

Texts that are *stored* (review reasons, error notes, duplicate reasons) are kept in English -
the worker always runs in English - and translated when shown: :func:`translate_text` recognises
stored texts made from catalogue entries with placeholders, e.g. ``"Sender: a similar name
already exists"`` from ``"%(field)s: a similar name already exists"``.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGES = {"en": "English", "de": "Deutsch"}
DEFAULT = "en"
LOCALE_DIR = Path(__file__).parent / "locale"

_current: ContextVar[str] = ContextVar("heftig_language", default=DEFAULT)

# msgid (or "context\x04msgid") -> msgstr, or the list of plural forms
Catalogue = dict[str, "str | list[str]"]


# --- catalogues ----------------------------------------------------------------------------


def _unquote(s: str) -> str:
    return ast.literal_eval(s) if s.startswith('"') else ""


def parse_po(text: str) -> tuple[Catalogue, dict[str, str]]:
    """(translations, msgid -> msgid_plural) of a .po file; fuzzy and empty entries skipped."""
    out: Catalogue = {}
    plurals: dict[str, str] = {}
    entry: dict[str, Any] = {}
    fuzzy = False
    last: str | None = None

    def flush() -> None:
        nonlocal entry, fuzzy, last
        mid = entry.get("msgid")
        if mid and not fuzzy:
            key = f"{entry['msgctxt']}\x04{mid}" if "msgctxt" in entry else mid
            if "msgid_plural" in entry:
                forms = [entry.get(f"msgstr[{i}]", "") for i in range(2)]
                if all(forms):
                    out[key] = forms
                plurals[key] = entry["msgid_plural"]
            elif entry.get("msgstr"):
                out[key] = entry["msgstr"]
        entry, fuzzy, last = {}, False, None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                if entry:
                    flush()
                fuzzy = True
            continue
        if line.startswith('"'):
            if last:
                entry[last] = entry.get(last, "") + _unquote(line)
            continue
        m = re.match(r"(msgctxt|msgid_plural|msgid|msgstr(?:\[\d\])?)\s+(\".*\")$", line)
        if not m:
            continue
        key, value = m.groups()
        if key in ("msgctxt", "msgid") and any(k.startswith("msgstr") for k in entry):
            flush()
        entry[key] = _unquote(value)
        last = key
    flush()
    return out, plurals


@lru_cache
def catalogue(lang: str) -> Catalogue:
    out: Catalogue = {}
    folder = LOCALE_DIR / lang
    if lang == DEFAULT or not folder.is_dir():
        return out
    for f in sorted(folder.glob("*.po")):
        out.update(parse_po(f.read_text(encoding="utf-8"))[0])
    return out


# --- current language ----------------------------------------------------------------------


def current() -> str:
    return _current.get()


def set_language(lang: str | None):
    """Set the language of the current context (request/thread); returns the reset token."""
    return _current.set(lang if lang in LANGUAGES else DEFAULT)


@contextmanager
def language(lang: str | None) -> Iterator[None]:
    token = set_language(lang)
    try:
        yield
    finally:
        _current.reset(token)


def pick(accept_language: str | None) -> str:
    """The best supported language for an Accept-Language header."""
    best, best_q = DEFAULT, -1.0
    for part in (accept_language or "").split(","):
        tag, _, params = part.strip().partition(";")
        code = tag.strip().lower().split("-")[0]
        q = 1.0
        m = re.search(r"q=([\d.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if code in LANGUAGES and q > best_q:
            best, best_q = code, q
    return best


# --- lookups -------------------------------------------------------------------------------


def gettext(message: str, **params: Any) -> str:
    text = catalogue(current()).get(message, message)
    if not isinstance(text, str):
        text = text[0]
    return text % params if params else text


def ngettext(singular: str, plural: str, n: int, **params: Any) -> str:
    text = raw_ngettext(singular, plural, n)
    params.setdefault("num", n)
    return text % params


def pgettext(context: str, message: str, **params: Any) -> str:
    text = catalogue(current()).get(f"{context}\x04{message}", message)
    if not isinstance(text, str):
        text = text[0]
    return text % params if params else text


_ = gettext


# for Jinja's i18n extension (newstyle): it fills the placeholders itself
def raw_gettext(message: str) -> str:
    text = catalogue(current()).get(message, message)
    return text if isinstance(text, str) else text[0]


def raw_ngettext(singular: str, plural: str, n: int) -> str:
    forms = catalogue(current()).get(singular)
    if isinstance(forms, list):
        return forms[0 if n == 1 else 1]
    return singular if n == 1 else plural


def raw_pgettext(context: str, message: str) -> str:
    text = catalogue(current()).get(f"{context}\x04{message}", message)
    return text if isinstance(text, str) else text[0]


def N_(message: str) -> str:
    """Marks a text for the catalogue without translating it (translated when shown)."""
    return message


class Labels(dict):
    """A dict of English labels that returns them translated (for templates and code alike)."""

    def __getitem__(self, key: Any) -> str:
        return gettext(super().__getitem__(key))

    def get(self, key: Any, default: Any = None) -> Any:
        return self[key] if key in self else default  # noqa: SIM401 - translates

    def items(self):  # type: ignore[override]
        return [(k, self[k]) for k in self]

    def values(self):  # type: ignore[override]
        return [self[k] for k in self]


# --- stored texts --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")


# placeholders that always hold a number: "Page %(page)s" must not match "Page 3 is missing"
_NUMERIC = {"num", "page", "pages", "pct", "percent", "budget", "count", "max", "mb", "kb",
            "failed", "uid"}  # fmt: skip
_NUMBER = r"-?\d[\d.,]*%?"


@lru_cache
def _patterns(lang: str) -> list[tuple[re.Pattern[str], str]]:
    """Catalogue entries with placeholders as patterns that match the English text."""
    out = []
    for msgid, msgstr in catalogue(lang).items():
        if "\x04" in msgid or not isinstance(msgstr, str) or "%(" not in msgid:
            continue
        parts = _PLACEHOLDER.split(msgid)
        literal = "".join(parts[0::2])
        if sum(c.isalpha() for c in literal) < 3 and not any(p in _NUMERIC for p in parts[1::2]):
            continue  # nothing to recognise a stored text by
        rx = "".join(
            re.escape(p) if i % 2 == 0 else f"(?P<{p}>{_NUMBER if p in _NUMERIC else '.+?'})"
            for i, p in enumerate(parts)
        )
        try:
            out.append((re.compile(rx + r"\Z", re.S), msgstr))
        except re.error:  # the same placeholder twice
            continue
    out.sort(key=lambda x: -len(x[0].pattern))  # the most specific first
    return out


def _exact(text: str, cat: Catalogue) -> str:
    hit = cat.get(text)
    return hit if isinstance(hit, str) else text


@lru_cache(maxsize=4096)
def _translate_text(text: str, lang: str) -> str:
    cat = catalogue(lang)
    hit = cat.get(text)
    if isinstance(hit, str):
        return hit
    if "; " in text:  # several stored texts joined (errors of several pages)
        return "; ".join(_translate_text(part, lang) for part in text.split("; "))
    for rx, msgstr in _patterns(lang):
        m = rx.match(text)
        if m:
            # parameters: only labels that are catalogue entries themselves ("Sender") - never
            # patterns again, so free text inside (a title, an error) stays as it is
            params = {k: _exact(v, cat) for k, v in m.groupdict().items()}
            return _PLACEHOLDER.sub(lambda p, params=params: params.get(p.group(1), ""), msgstr)
    return text


def translate_text(text: str | None) -> str:
    """A stored English text (reason, note, error) in the current language."""
    if not text or current() == DEFAULT:
        return text or ""
    return _translate_text(text, current())


# --- formats -------------------------------------------------------------------------------

_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_date(d: date | datetime) -> str:
    if current() == "de":
        return d.strftime("%d.%m.%Y")
    return f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}"


def format_number(value: float, decimals: int = 2) -> str:
    s = f"{value:,.{decimals}f}"
    if current() == "de":
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s


# t("...") / tn("...", ...) in the browser scripts; the (first) text must be a plain literal
_JS_CALL = re.compile(r"""\btn?\(\s*(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')""")


def js_msgids(static_dir: Path) -> list[str]:
    """The texts in ``t("...")`` calls of the browser scripts."""
    out: dict[str, None] = {}
    for f in sorted(static_dir.glob("*.js")):
        if f.name == "i18n.js":
            continue
        for m in _JS_CALL.finditer(f.read_text(encoding="utf-8")):
            raw = m.group(1)
            if raw is None:
                raw = m.group(2).replace("\\'", "'").replace('"', '\\"')
            out[json.loads(f'"{raw}"')] = None
    return list(out)


def js_messages(msgids: list[str]) -> dict[str, str | list[str]]:
    """The translations the browser scripts need (only in a non-English language)."""
    cat = catalogue(current())
    return {m: cat[m] for m in msgids if m in cat}
