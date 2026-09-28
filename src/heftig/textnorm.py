"""Text normalisation shared by the search index, queries and taxonomy matching.

``fold`` maps German spellings onto one form: ``Müller``, ``Mueller`` and ``MÜLLER`` all become
``mueller``; ``Straße`` becomes ``strasse``. Other diacritics are stripped (``é`` -> ``e``).
"""

from __future__ import annotations

import re
import unicodedata

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
# tokens as the FTS5 unicode61 tokenizer sees them: letters/digits, split on everything else
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# words broken by OCR/line wrapping: "Rech-\nnung"
_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\r?\n[ \t]*(\w)")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]+")


def fold(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold().translate(_UMLAUTS)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(fold(text))


def normalize_name(name: str) -> str:
    """Key for taxonomy comparison: folded tokens joined by single spaces."""
    return " ".join(tokens(name))


def clean_display_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable())
    return re.sub(r"\s+", " ", name).strip()


def dehyphenate(text: str) -> str:
    return _HYPHEN_BREAK.sub(r"\1\2", text)


def index_text(text: str) -> str:
    """Folded text for the index, with line-break hyphenation joined as an extra form."""
    folded = fold(text)
    joined = fold(dehyphenate(text))
    if joined != folded:
        return folded + "\n" + joined
    return folded


def identifiers(text: str) -> list[str]:
    """Separator-free forms of number-like sequences.

    ``"8372 9381"`` and ``"8372-9381"`` both yield ``"83729381"``, ``"DE12 3456 78"`` yields
    ``"de12345678"``, so an exact number search also finds numbers printed with separators.
    Single tokens are already in the regular index and are not repeated here.
    """
    out: set[str] = set()
    run: list[str] = []
    last_end = -1

    def flush() -> None:
        for i in range(len(run)):
            for j in range(i + 2, min(len(run), i + 6) + 1):
                out.add(fold("".join(run[i:j])))

    for m in _ALNUM_RE.finditer(text):
        part = m.group(0)
        has_digit = any(c.isdigit() for c in part)
        adjacent = m.start() - last_end == 1 and text[last_end] in " ./-"
        if has_digit and run and adjacent:
            run.append(part)
        else:
            flush()
            run = [part] if has_digit else []
        last_end = m.end()
    flush()
    return sorted(out)


def levenshtein(a: str, b: str, max_dist: int) -> int:
    """Bounded edit distance; returns max_dist + 1 when exceeded."""
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            row_min = min(row_min, cur[j])
        if row_min > max_dist:
            return max_dist + 1
        prev = cur
    return prev[-1] if prev[-1] <= max_dist else max_dist + 1


def parse_number(value: str) -> float | None:
    """German, English and plain number formats: "1.234,56", "1234,56", "1.234" (= 1234),
    "12.5", "1,234.56" (English thousands separator with decimals)."""
    v = str(value).replace("\u00a0", " ").strip()
    v = re.sub(r"^(?:[$€£]|EUR|USD|GBP)\s*|\s*(?:€|EUR|USD|GBP|\$|£)$", "", v).strip()
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+\.\d+", v):
        v = v.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?|-?\d{1,3}( \d{3})+(,\d+)?", v):
        v = v.replace(".", "").replace(" ", "").replace(",", ".")
    elif re.fullmatch(r"-?\d+,\d+", v):
        v = v.replace(",", ".")
    try:
        return float(v.replace(" ", ""))
    except ValueError:
        return None
