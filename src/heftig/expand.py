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
from pathlib import Path
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
SIMILAR_SCAN = 20000  # rows read to find similar spellings
LINKS = ("s", "es", "n", "en", "e", "er")  # joints between the parts of German compounds
INFLECTIONS = ("en", "es", "e", "n", "s", "er", "ern")

# Function words that carry no meaning of their own in a search ("die Rechnung vom Zahnarzt").
# They are left out when the query has other words; phrases in quotes keep them.
STOPWORDS = frozenset(
    """
    der die das den dem des ein eine einer eines einem einen kein keine keinen keinem keiner
    und oder aber denn doch sondern dass ob wenn als weil damit sodass falls sobald bevor
    nachdem waehrend obwohl
    von vom zu zum zur im in ins am an auf aus bei beim mit fuer ueber unter nach vor bis ab
    durch gegen ohne um seit zwischen hinter neben per pro je
    ich du er sie es wir ihr mich mir dich dir sich uns euch ihn ihm ihnen man
    mein meine meiner meines meinem meinen dein deine deiner deines deinem deinen
    sein seine seiner seines seinem seinen ihre ihrer ihres ihrem ihren
    unser unsere unserer unseres unserem unseren euer eure eurer eures eurem euren
    dies diese dieser dieses diesem diesen jene jener jenes jenem jenen welche welcher welches
    welchem welchen was wer wen wem wessen wie wo woher wohin wann warum weshalb wieso womit
    wofuer worueber wobei wozu
    bin bist ist sind seid war warst waren wart waere waeren gewesen habe hast hat haben habt
    hatte hattest hatten haette haetten gehabt werde wirst wird werden werdet wurde wurden
    wuerde wuerden geworden kann kannst koennen koennt konnte konnten koennte koennten muss
    musst muessen muesst musste mussten muesste soll sollst sollen sollt sollte sollten darf
    darfst duerfen durfte duerfte will willst wollen wollte moechte moechten mag
    nicht auch noch schon nur sehr so dann da hier dort jetzt immer bitte gibt etwas alle
    allem allen aller alles andere anderen mehr viel viele einige etwa ja nein
    the a an of for from to on at by and or my with about is are was were be been do does did
    how what when where which who why can could should would will i you we it this that
    """.split()  # noqa: SIM905 - readable as a block
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
        db = conn.execute("PRAGMA database_list").fetchone()[2]
        self.root = Path(db).parent if db else None  # archive directory (own synonyms)

    @property
    def state(self) -> tuple:
        """Changes with every change of the index and of the archive's own synonyms."""
        if self._state is None:
            from .index import generation

            self._state = (str(self.root), generation(self.conn), synonyms.version(self.root))
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

    def starting(
        self, prefix: str, limit: int = MAX_SCAN, lengths: tuple[int, int] | None = None
    ) -> list[tuple[str, int]]:
        """Indexed words starting with `prefix` (term, number of documents), optionally only
        those with a length in the range `lengths`."""
        if lengths:
            return self.conn.execute(
                "SELECT term, doc FROM doc_vocab WHERE term >= ? AND term < ? "
                "AND length(term) BETWEEN ? AND ? LIMIT ?",
                (prefix, _next(prefix), *lengths, limit),
            ).fetchall()
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

    def compound_candidates(self, part: str, inner: bool, slack: int) -> list[tuple[str, int]]:
        """Indexed words that end in `part` (plus up to `slack` letters: the rest of a longer
        form and an inflection) or, with `inner`, have it in the middle with at least three
        letters before it and some after it (a full vocabulary scan, cached; the exact test is
        done by the caller)."""
        # instr() finds the first occurrence; a later one in the middle is rare enough to miss
        sql = (
            "SELECT term, doc FROM doc_vocab WHERE instr(term, :p) > 1 AND "
            "(length(term) - instr(term, :p) - length(:p) + 1 <= :slack"
            + (
                " OR (instr(term, :p) > 3 AND length(term) - instr(term, :p) - length(:p) >= 3))"
                if inner
                else ")"
            )
        )
        return self.cached(
            "in",
            (part, inner, slack),
            lambda: self.conn.execute(sql, {"p": part, "slack": slack}).fetchall(),
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


def same_stem(v: Vocab, tok: str, scan: int = 1000) -> list[str]:
    """Indexed words with the stem of `tok` (kindern -> kind, kinder, kindes). `scan`: words
    read per spelling of the stem at most (the ones with the same stem are short, so they come
    early)."""
    if not tok.isalpha() or len(tok) < MIN_STEM_WORD:
        return []

    def run() -> list[str]:
        s = stem(tok)
        if len(s) < 3:
            return []
        found: dict[str, int] = {}
        for prefix in _umlaut_variants(s):
            # a word with this stem is at most a few letters longer than it
            for term, n in v.starting(prefix, scan, (len(prefix), len(prefix) + 6)):
                if term.isalpha() and stem(term) == s:
                    found[term] = n
        found.pop(tok, None)
        return [t for t, _ in sorted(found.items(), key=lambda x: (-x[1], x[0]))[:MAX_FORMS]]

    return v.cached("stem", (tok, scan), run)


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
        # one vocabulary scan per spelling of the stem that the forms contain (versicher for
        # versicherung, versicherungen, versichert ...), else per form
        cores = [c for c in _umlaut_variants(stem(tok)) if len(c) >= MIN_STEM_WORD]
        wanted: dict[str, int] = {}  # scan -> letters a form may add after it
        for f in usable:
            core = next((c for c in cores if c in f), f)
            wanted[core] = max(wanted.get(core, 0), len(f) - f.index(core) - len(core))
        queries = [q for q in wanted if not any(q != g and g in q for g in wanted)]
        found: dict[str, int] = {}
        for q in queries:
            slack = max(n for g, n in wanted.items() if q in g) + 3
            for term, n in v.compound_candidates(q, len(q) >= min_inner, slack):
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
    indexed word, a word with the same stem or with synonyms; as the last part also the start
    of an indexed word (nummer in nummern), as the first part the start of an indexed word
    whose rest is a word (muell in muellabfuhr needs abfuhr)."""
    if len(part) < 3 or not part.isalpha():
        return 0
    n = v.docs(part)
    if n:
        return n
    if related((part,), v.root):
        return 1
    if len(part) < MIN_STEM_WORD:
        return 0
    if same_stem(v, part, scan=50):
        return 1
    if tail:
        return sum(d for _, d in v.starting(part, 50))
    return sum(
        1
        for term, _ in v.starting(part, 200)
        if len(term) > len(part) and _joint_word(v, term[len(part) :], head=False)
    )


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
        whole = v.docs(tok)
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
        # as Koehn & Knight: split only if the parts are more common than the whole word
        return (best[2], best[3]) if best and best[0] > whole else None

    return v.cached("split", tok, run)


def related(words: tuple[str, ...], root: Path | None = None) -> list[tuple[str, ...]]:
    """Other words for a word or phrase: built-in groups and the archive's own."""
    return synonyms.alternatives(words, root)


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


@lru_cache(maxsize=65536)
def _bigrams(w: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(len(w) - 1):
        out[w[i : i + 2]] = out.get(w[i : i + 2], 0) + 1
    return out


def _close_letters(a: str, b: str, max_d: int) -> bool:
    """Cheap test before the edit distance (q-gram lemma): words within k edits share at least
    max(len) - 1 - 2k letter pairs; an OCR confusion (rn/m) changes three, so 3k here."""
    ba, bb = _bigrams(a), _bigrams(b)
    shared = sum(min(n, bb.get(g, 0)) for g, n in ba.items())
    return shared >= max(len(a), len(b)) - 1 - 3 * max_d


def similar(v: Vocab, tok: str, covered) -> list[str]:
    """Indexed words spelt almost like `tok` (same first two letters), for OCR errors in the
    text: kuendigung -> kuendiqunq, kaltmiete -> kaltrniete. `covered(word)` tells which words
    the other levels already match."""
    max_d = allowed_edits(tok)
    if not max_d or not tok.isalpha():
        return []

    def run() -> list[str]:
        found = []
        lengths = (len(tok) - 2 * max_d, len(tok) + 2 * max_d)
        rows = v.starting(tok[:2], SIMILAR_SCAN, lengths)
        if len(rows) == SIMILAR_SCAN:  # a large vocabulary: the first three letters then
            rows = v.starting(tok[:3], SIMILAR_SCAN, lengths)
        for term, n in rows:
            if not term.isalpha() or term == tok or not _close_letters(tok, term, max_d):
                continue
            dist = distance(tok, term, max_d)
            if dist <= max_d:
                found.append((dist, -n, term))
        return [t for _, _, t in sorted(found)]

    return [t for t in v.cached("sim", tok, run) if not covered(t)][:MAX_SIMILAR]


def correct(v: Vocab, tok: str, stats: dict[str, Any]) -> str | None:
    """The closest indexed word for a word that matches nothing: edit distance 1 (up to five
    letters) or 2; the first letter may be one that sounds alike (Wodafone -> vodafone).
    Cached (suggestions while typing search again with every letter)."""
    found, checked = v.cached("fix", tok, lambda: _correct(v, tok))
    stats["candidates_checked"] = stats.get("candidates_checked", 0) + checked
    return found


def _correct(v: Vocab, tok: str) -> tuple[str | None, int]:
    max_d = 1 if len(tok) <= 5 else 2
    best: tuple[int, int, str] | None = None
    checked = 0
    for first in tok[0] + SOUND_ALIKE.get(tok[0], ""):
        extra = 0 if first == tok[0] else 1
        for term, doc_count in v.starting(first, 100000, (len(tok) - max_d, len(tok) + max_d)):
            if not term.isalpha() or not _close_letters(tok, term, max_d):
                continue
            checked += 1
            d = distance(tok, term, max_d)
            if d <= max_d:
                key = (d + extra, -doc_count, term)
                if best is None or key < best:
                    best = key
    return (best[2] if best else None), checked
