"""Search words and the indexed words they stand for (German word forms, compounds, typos).

Everything here works on the index vocabulary (the `doc_vocab` table), at query time: the index
itself stays a plain word index, nothing needs to be reindexed when a rule changes. The search
(`search.py`) ranks what these functions add in levels below the word itself:

1. **forms** – indexed words with the same Snowball stem (Kindern -> Kind, Kinder; Ärzte -> Arzt)
   and compounds that end in the word or contain it as a part (Steuerbescheid ->
   Einkommensteuerbescheid; Schornsteinfeger -> Bezirksschornsteinfegermeister);
2. **related** – other words for the same thing (`synonyms.py`) and the parts of a compound
   that occurs nowhere as a whole (Stromrechnung -> Strom + Rechnung);
3. **similar** – indexed words one or two letters away, for OCR errors in scans (Kündiqung,
   Kaltrniete) and typing errors in the text.

All results are cached per archive state (number of documents, last change).
"""

from __future__ import annotations

import sqlite3
import threading
from functools import lru_cache
from typing import Any

from snowballstemmer import stemmer as _snowball

from . import synonyms

MIN_STEM_WORD = 4  # shorter words are only searched as they are typed
MIN_COMPOUND_WORD = 5  # query words looked up inside longer words
MIN_INNER_PART = 6  # a part in the middle of a compound (Versicherung in ...versicherungsnummer)
MAX_FORMS = 30
MAX_COMPOUNDS = 40
MAX_SIMILAR = 12
MAX_SCAN = 4000  # vocabulary rows read per lookup at most
LINKS = ("s", "es", "n", "en", "e", "er")  # joints between the parts of German compounds
INFLECTIONS = ("en", "es", "e", "n", "s", "er", "ern")

# Function words that carry no meaning of their own in a search ("die Rechnung vom Zahnarzt").
# They are left out when the query has other words; phrases in quotes keep them.
STOPWORDS = frozenset(
    [
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einer",
        "eines",
        "einem",
        "einen",
        "und",
        "oder",
        "von",
        "vom",
        "zu",
        "zum",
        "zur",
        "im",
        "in",
        "ins",
        "am",
        "an",
        "auf",
        "aus",
        "bei",
        "mit",
        "fuer",
        "ueber",
        "unter",
        "nach",
        "vor",
        "bis",
        "ab",
        "als",
        "wie",
        "was",
        "wo",
        "mein",
        "meine",
        "meiner",
        "meines",
        "meinem",
        "meinen",
        "dein",
        "deine",
        "unser",
        "unsere",
        "ihr",
        "ihre",
        "sein",
        "seine",
        "es",
        "ist",
        "sind",
        "war",
        "the",
        "a",
        "an",
        "of",
        "for",
        "from",
        "to",
        "on",
        "at",
        "by",
        "and",
        "or",
        "my",
        "with",
        "about",
    ]
)

_STEMMER = _snowball("german")
_STEM_LOCK = threading.Lock()


@lru_cache(maxsize=65536)
def stem(word: str) -> str:
    """Snowball stem of a folded word. The German Snowball algorithm (3.x) reads ae/oe/ue as
    umlauts, so the folded forms stem like the original ones: haeusern -> haus."""
    with _STEM_LOCK:  # stemmer objects keep state
        return _STEMMER.stemWord(word)


def _next(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class Vocab:
    """Lookups in the index vocabulary, cached per archive state."""

    _cache: dict[tuple, Any] = {}
    _lock = threading.Lock()

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._state: tuple | None = None

    @property
    def state(self) -> tuple:
        if self._state is None:
            db = self.conn.execute("PRAGMA database_list").fetchone()[2]
            count, last = self.conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM documents"
            ).fetchone()
            self._state = (db, count, last)
        return self._state

    def cached(self, kind: str, key: Any, fn):
        k = (self.state, kind, key)
        with self._lock:
            if k in self._cache:
                return self._cache[k]
        value = fn()
        with self._lock:
            if len(self._cache) > 20000:
                self._cache.clear()
            self._cache[k] = value
        return value

    def starting(self, prefix: str, limit: int = MAX_SCAN) -> list[tuple[str, int]]:
        """Indexed words starting with `prefix` (term, number of documents)."""
        return self.conn.execute(
            "SELECT term, doc FROM doc_vocab WHERE term >= ? AND term < ? LIMIT ?",
            (prefix, _next(prefix), limit),
        ).fetchall()

    def has_prefix(self, prefix: str) -> bool:
        return self.cached("pfx", prefix, lambda: bool(self.starting(prefix, 1)))

    def docs(self, term: str) -> int:
        def run() -> int:
            row = self.conn.execute("SELECT doc FROM doc_vocab WHERE term = ?", (term,)).fetchone()
            return row[0] if row else 0

        return self.cached("docs", term, run)

    def containing(self, part: str) -> list[tuple[str, int]]:
        """Indexed words that contain `part` after their first letter (a full vocabulary scan,
        cached)."""
        return self.cached(
            "in",
            part,
            lambda: self.conn.execute(
                "SELECT term, doc FROM doc_vocab WHERE instr(term, ?) > 1", (part,)
            ).fetchall(),
        )


# --- level 1: word forms and compounds ------------------------------------------------------


def _umlaut_variants(s: str) -> list[str]:
    """The stem as it may start an indexed word: stems have no umlauts, indexed words spell
    them ae/oe/ue (haus -> haus, haeus)."""
    out = [""]
    i = 0
    while i < len(s):
        if s.startswith("au", i):
            out = [o + v for o in out for v in ("au", "aeu")]
            i += 2
            continue
        ch = s[i]
        spellings = (ch, ch + "e") if ch in "aou" else (ch,)
        out = [o + v for o in out for v in spellings]
        i += 1
        if len(out) > 16:
            out = out[:16]
    return out


def same_stem(v: Vocab, tok: str) -> list[str]:
    """Indexed words with the stem of `tok` (kindern -> kind, kinder, kindes)."""
    if not tok.isalpha() or len(tok) < MIN_STEM_WORD:
        return []

    def run() -> list[str]:
        s = stem(tok)
        if len(s) < 3:
            return []
        found: dict[str, int] = {}
        for prefix in _umlaut_variants(s):
            for term, n in v.starting(prefix):
                if term.isalpha() and stem(term) == s:
                    found[term] = n
        found.pop(tok, None)
        return [t for t, _ in sorted(found.items(), key=lambda x: (-x[1], x[0]))[:MAX_FORMS]]

    return v.cached("stem", tok, run)


def _ends_in(word: str, form: str) -> bool:
    return word.endswith(form) or any(word.endswith(form + s) for s in INFLECTIONS)


def _inner(word: str, form: str) -> bool:
    """`form` is a middle part of `word`: something before it and a word after the joint
    (rentenversicherungsnummer contains versicherung; vermieter does not contain miete)."""
    at = word.find(form, 3)
    while at > 0:
        rest = word[at + len(form) :]
        for link in ("", *LINKS):
            if rest.startswith(link) and len(rest) - len(link) >= 4:
                return True
        at = word.find(form, at + 1)
    return False


def compounds(v: Vocab, tok: str, forms: tuple[str, ...], part: bool = False) -> list[str]:
    """Indexed words that end in one of `forms` (Einkommensteuerbescheid for steuerbescheid) or,
    for longer forms, contain it as a middle part. Most frequent first. `part`: `tok` is a part
    of a split compound, known to be a word - shorter forms are looked up too (Arzt in
    Tierarzt, Zahnarztpraxis)."""
    min_word = MIN_STEM_WORD if part else MIN_COMPOUND_WORD
    min_inner = MIN_STEM_WORD if part else MIN_INNER_PART
    if len(tok) < min_word or not tok.isalpha():
        return []

    def run() -> list[str]:
        usable = sorted({f for f in forms if len(f) >= MIN_STEM_WORD}, key=len)
        queries = [f for f in usable if not any(f != g and g in f for g in usable)]
        found: dict[str, int] = {}
        for q in queries:
            for term, n in v.containing(q):
                if not term.isalpha():
                    continue
                if any(_ends_in(term, f) for f in usable) or any(
                    len(f) >= min_inner and _inner(term, f) for f in usable
                ):
                    found[term] = n
        for f in forms:
            found.pop(f, None)
        return [t for t, _ in sorted(found.items(), key=lambda x: (-x[1], x[0]))[:MAX_COMPOUNDS]]

    return v.cached("cmp", (tok, forms, part), run)


# --- level 2: other words, compound parts -------------------------------------------------


def _word_count(v: Vocab, part: str, tail: bool) -> int:
    """How common `part` is as a word of its own in the archive (0: not a word). A word is an
    indexed word, a word with the same stem, a word with synonyms, or - as the first part - the
    start of an indexed word whose rest is a word (muell in muellabfuhr needs abfuhr), and as
    the last part the end of one whose beginning is a word (nummer in kundennummer)."""
    if len(part) < 3 or not part.isalpha():
        return 0
    n = v.docs(part)
    if n:
        return n
    if len(part) >= MIN_STEM_WORD and same_stem(v, part):
        return 1
    if related((part,)):
        return 1
    if len(part) < MIN_STEM_WORD:
        return 0
    # evidence: the number of compounds it is a part of (next to a word)
    if tail:
        found = [
            term
            for term, _ in v.containing(part)
            if term.endswith(part) and _joint_word(v, term[: -len(part)], head=True)
        ]
    else:
        found = [
            term
            for term, _ in v.starting(part, 200)
            if len(term) > len(part) and _joint_word(v, term[len(part) :], head=False)
        ]
    return len(found)


def _joint_word(v: Vocab, rest: str, head: bool) -> bool:
    """`rest` (with a joint -s-, -en- ... on its compound side) is an indexed word."""
    candidates = [rest]
    for link in LINKS:
        if head and rest.endswith(link):
            candidates.append(rest[: -len(link)])
        if not head and rest.startswith(link):
            candidates.append(rest[len(link) :])
    return any(len(c) >= MIN_STEM_WORD and v.docs(c) for c in candidates)


def split(v: Vocab, tok: str) -> tuple[str, str] | None:
    """The two parts of a compound, judged by the words of the archive (Koehn & Knight 2003:
    the split whose parts are most common): stromrechnung -> strom + rechnung,
    muellgebuehren -> muell + gebuehren, rentenversicherungsnummer -> rentenversicherung +
    nummer. None if no split into words exists."""
    if not tok.isalpha() or len(tok) < 7:
        return None

    def run() -> tuple[str, str] | None:
        best: tuple[float, int, str, str] | None = None
        for i in range(3, len(tok) - 2):
            head, tail = tok[:i], tok[i:]
            nt = _word_count(v, tail, tail=True)
            if not nt:
                continue
            heads = [head] + [head[: -len(link)] for link in LINKS if head.endswith(link)]
            for h in heads:
                nh = _word_count(v, h, tail=False)
                if nh:
                    key = ((nh * nt) ** 0.5, min(len(h), len(tail)), h, tail)
                    if best is None or key[:2] > best[:2]:
                        best = key
        return (best[2], best[3]) if best else None

    return v.cached("split", tok, run)


def related(words: tuple[str, ...]) -> list[tuple[str, ...]]:
    return synonyms.alternatives(words)


# --- level 3: similar spellings (OCR and typing errors) ---------------------------------------

# Letters OCR confuses as one error: "rn" read as "m", "cl" as "d", "vv" as "w" (and back).
OCR_PAIRS = (("rn", "m"), ("m", "rn"), ("cl", "d"), ("d", "cl"), ("vv", "w"), ("w", "vv"))
# First letters people mix up when they spell by ear (typo correction only).
SOUND_ALIKE = {
    "v": "fw",
    "w": "v",
    "f": "v",
    "c": "kz",
    "k": "cg",
    "z": "cs",
    "s": "z",
    "d": "t",
    "t": "d",
    "b": "p",
    "p": "b",
    "g": "k",
    "i": "jy",
    "j": "iy",
    "y": "ij",
}


def distance(a: str, b: str, max_d: int) -> int:
    """Edit distance where a swap of neighbouring letters (Damerau) and an OCR confusion
    (rn/m, cl/d, vv/w) count as one edit. Returns max_d + 1 when exceeded."""
    if abs(len(a) - len(b)) > 2 * max_d:
        return max_d + 1
    la, lb = len(a), len(b)
    big = max_d + 1
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        row_min = big
        for j in range(1, lb + 1):
            v = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, d[i - 2][j - 2] + 1)
            for x, y in OCR_PAIRS:
                lx, ly = len(x), len(y)
                if i >= lx and j >= ly and a[i - lx : i] == x and b[j - ly : j] == y:
                    v = min(v, d[i - lx][j - ly] + 1)
            d[i][j] = v
            row_min = min(row_min, v)
        if row_min > max_d:
            return big
    return d[la][lb] if d[la][lb] <= max_d else big


def allowed_edits(tok: str) -> int:
    """Edits a word may differ by and still be the same word (as Meilisearch: one from five
    letters, two from nine)."""
    return 0 if len(tok) < 5 else 1 if len(tok) < 9 else 2


def similar(v: Vocab, tok: str, covered) -> list[str]:
    """Indexed words spelt almost like `tok` (same first two letters), for OCR errors in the
    text: kuendigung -> kuendiqunq, kaltmiete -> kaltrniete. `covered(word)` tells which words
    the other levels already match."""
    max_d = allowed_edits(tok)
    if not max_d or not tok.isalpha():
        return []

    def run() -> list[str]:
        found = []
        for term, n in v.starting(tok[:2]):
            if abs(len(term) - len(tok)) > 2 * max_d or not term.isalpha() or term == tok:
                continue
            dist = distance(tok, term, max_d)
            if dist <= max_d:
                found.append((dist, -n, term))
        return [t for _, _, t in sorted(found)]

    return [t for t in v.cached("sim", tok, run) if not covered(t)][:MAX_SIMILAR]


def correct(v: Vocab, tok: str, stats: dict[str, Any]) -> str | None:
    """The closest indexed word for a word that matches nothing: edit distance 1 (up to five
    letters) or 2; the first letter may be one that sounds alike (Wodafone -> vodafone)."""
    max_d = 1 if len(tok) <= 5 else 2
    best: tuple[int, int, str] | None = None
    checked = 0
    for first in tok[0] + SOUND_ALIKE.get(tok[0], ""):
        extra = 0 if first == tok[0] else 1
        for term, doc_count in v.starting(first, 100000):
            if abs(len(term) - len(tok)) > max_d or not term.isalpha():
                continue
            checked += 1
            d = distance(tok, term, max_d)
            if d <= max_d:
                key = (d + extra, -doc_count, term)
                if best is None or key < best:
                    best = key
    stats["candidates_checked"] = stats.get("candidates_checked", 0) + checked
    return best[2] if best else None
