"""Local, deterministic full-text search (SQLite FTS5 + BM25 with column weights).

Query handling, in order:
1. exact document ID / SHA-256 -> direct hit
2. field syntax (``correspondent:``, ``type:``, ``tag:``, ``year:``, ``received:``, ``source:``)
   becomes filters; ``"quoted text"`` becomes a phrase
3. remaining words: tokens with digits must match exactly (numbers, IDs), words match as
   prefixes; all words must match (AND)
   German date phrases ("März 2025", "letztes Jahr", "seit 2023") become a document-date
   filter (:mod:`heftig.datephrases`); ``literal`` turns that off
4. a word that occurs nowhere in the index is corrected against the index vocabulary (bounded
   edit distance, same first letter, cheap) - the UI shows the correction
5. if AND finds nothing, fall back to OR and say so

Never calls a network service. Ranking weights: :data:`heftig.index.COLUMN_WEIGHTS`.
"""

from __future__ import annotations

import html
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any

from . import datephrases, expand, i18n
from . import taxonomy as tax
from .expand import STOPWORDS, Vocab
from .i18n import N_, _
from .index import COLUMN_WEIGHTS, FTS_COLUMNS
from .textnorm import TOKEN_RE, fold, normalize_name

SORTS = ("relevance", "received", "document_date", "title")
FIELD_KEYS = {
    "correspondent": "correspondent", "korrespondent": "correspondent", "von": "correspondent",
    "type": "document_type", "typ": "document_type",
    "tag": "tag",
    "year": "year", "jahr": "year",
    "received": "received", "eingang": "received",
    "source": "source", "quelle": "source",
}  # fmt: skip
SOURCES = ("scanner", "folder", "web", "api", "email", "import")
COLUMN_LABELS = i18n.Labels(
    {
        "ident": N_("Number/ID"),
        "title": N_("Title"),
        "correspondent": N_("Sender"),
        "doctype": N_("Document type"),
        "tags": N_("Tag"),
        "custom": N_("Custom field"),
        "filename": N_("File name"),
        "dates": N_("Date"),
        "summary": N_("Summary"),
        "body": N_("Text"),
        "notes": N_("Note/attachment"),
    }
)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_PART = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
_QUERY_TOKEN = re.compile(r'(\w+):"([^"]*)"|(\w+):(\S+)|"([^"]*)"|(\S+)', re.UNICODE)
MAX_TERMS = 12
YEAR_BOOST = 1.5


class SearchSyntaxError(ValueError):
    pass


@dataclass
class SearchParams:
    q: str = ""
    correspondent: list[str] = field(default_factory=list)
    document_type: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    received_from: str | None = None
    received_to: str | None = None
    source: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    filed: str | None = None  # "yes" | "no"
    filing_section: str | None = None
    filing_binder: str | None = None
    cf_key: str | None = None
    cf_min: float | str | None = None  # text: an amount that could not be read
    cf_max: float | str | None = None
    sort: str | None = None
    page: int = 1
    per_page: int = 25
    tag_mode: str = "all"  # "all": every tag must match, "any": at least one
    session: str | None = None  # scan session id
    literal: bool = False  # True: don't turn "März 2025" etc. into a date filter


@dataclass
class Term:
    tokens: list[str]
    phrase: bool = False
    number_run: bool = False  # merged from separately typed numbers
    quoted: bool = False  # typed in quotes: searched as typed, without other forms
    # filled by _prepare_terms from the index vocabulary (heftig.expand), by level:
    # FORMS: indexed words with the same stem ("kindern" -> "kind") and compounds ending in
    # the word or containing it ("steuerbescheid" -> "einkommensteuerbescheid")
    stems: list[str] = field(default_factory=list)
    compounds: list[str] = field(default_factory=list)
    # RELATED: other words for the same thing, and the parts of a compound (all must match)
    synonyms: list[Term] = field(default_factory=list)
    parts: list[Term] = field(default_factory=list)
    exact: bool = False  # a short synonym: the word itself, not words starting with it
    # SIMILAR: indexed words spelt almost the same (OCR errors: "kuendiqunq")
    similar: list[str] = field(default_factory=list)


@dataclass
class ParsedQuery:
    terms: list[Term] = field(default_factory=list)
    filters: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    items: list[dict[str, Any]]
    total: int
    page: int
    per_page: int
    sort: str
    corrections: list[tuple[str, str]] = field(default_factory=list)
    partial: bool = False
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    exact: bool = False
    took_ms: float = 0.0
    fuzzy_stats: dict[str, Any] = field(default_factory=dict)
    date_phrase: datephrases.DatePhrase | None = None
    q_rest: str = ""  # the query without the date phrase
    facets: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "per_page": self.per_page,
            "sort": self.sort,
            "corrections": [{"from": a, "to": b} for a, b in self.corrections],
            "partial_match": self.partial,
            "errors": self.errors,
            "notes": self.notes,
            "exact_match": self.exact,
            "took_ms": round(self.took_ms, 1),
            "fuzzy": self.fuzzy_stats,
            "date_phrase": vars(self.date_phrase) if self.date_phrase else None,
            **({"facets": self.facets} if self.facets is not None else {}),
        }


# --- query parsing -----------------------------------------------------------------------


def parse_query(q: str) -> ParsedQuery:
    out = ParsedQuery()
    if q.count('"') % 2:
        out.errors.append(_("Unclosed quotation mark – searched as plain text."))
        q = q.replace('"', " ")
    for m in _QUERY_TOKEN.finditer(q):
        key = (m.group(1) or m.group(3) or "").lower()
        value = m.group(2) if m.group(1) else m.group(4)
        if key and key in FIELD_KEYS:
            if not value:
                out.errors.append(_("“%(key)s:” without a value was ignored.", key=key))
                continue
            out.filters.setdefault(FIELD_KEYS[key], []).append(value)
            continue
        if key:  # unknown "foo:bar" -> plain text
            text = m.group(0).replace('"', " ")
            _add_text(out, text)
            continue
        if m.group(5) is not None:
            toks = TOKEN_RE.findall(fold(m.group(5)))
            if toks:
                out.terms.append(Term(toks, phrase=len(toks) > 1, quoted=True))
            continue
        _add_text(out, m.group(6))
    # "die Rechnung vom Zahnarzt": function words need not occur (unless that is all there is)
    meaningful = [t for t in out.terms if t.phrase or t.tokens[0] not in STOPWORDS]
    if meaningful:
        out.terms = meaningful
    if len(out.terms) > MAX_TERMS:
        out.terms = out.terms[:MAX_TERMS]
        out.errors.append(_("Only the first %(num)s search terms are used.", num=MAX_TERMS))
    return out


def _add_text(out: ParsedQuery, text: str) -> None:
    toks = TOKEN_RE.findall(fold(text))
    if not toks:
        return
    # "Allianz-Versicherung", "DE12-3456" -> keep together as phrase
    out.terms.append(Term(toks, phrase=len(toks) > 1))


def _year_range(value: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}", value):
        raise SearchSyntaxError(
            _("“year:%(value)s” – please enter a year, e.g. year:2025.", value=value)
        )
    return f"{value}-01-01", f"{value}-12-31"


def _date_range(value: str, label: str) -> tuple[str, str]:
    """YYYY, YYYY-MM, YYYY-MM-DD or A..B -> inclusive ISO date range."""
    if ".." in value:
        a, b = value.split("..", 1)
        lo = _date_range(a, label)[0] if a else "0000-01-01"
        hi = _date_range(b, label)[1] if b else "9999-12-31"
        return lo, hi
    try:
        if re.fullmatch(r"\d{4}", value):
            return f"{value}-01-01", f"{value}-12-31"
        if re.fullmatch(r"\d{4}-\d{2}", value):
            y, m = int(value[:4]), int(value[5:])
            date(y, m, 1)
            nxt = date(y + (m == 12), m % 12 + 1, 1)
            return f"{value}-01", date.fromordinal(nxt.toordinal() - 1).isoformat()
        d = date.fromisoformat(value)
        return d.isoformat(), d.isoformat()
    except ValueError as e:
        raise SearchSyntaxError(
            _(
                "“%(label)s:%(value)s” – please enter the date as YYYY, YYYY-MM, YYYY-MM-DD "
                "or A..B.",
                label=label,
                value=value,
            )
        ) from e


# --- search words and what they stand for -------------------------------------------------
#
# The index matches word beginnings ("rechnung" finds "Rechnungen"). Everything else a word can
# stand for is looked up in the index vocabulary (heftig.expand) and searched in levels: the
# word as typed (LITERAL), its forms and compounds (FORMS), other words for it and its parts
# (RELATED), spellings one or two letters away (SIMILAR). A document that has the word itself
# is ranked above one found through a form, and so on (see _rank_sql).

LITERAL, FORMS, RELATED, SIMILAR = 0, 1, 2, 3

# columns that describe what a document is (vs. what its text mentions)
META_COLUMNS = ("ident", "title", "correspondent", "doctype", "tags", "custom", "dates", "notes")
TIER_STEP = 10000.0  # far beyond any BM25 score: tiers decide first
PROXIMITY_BOOST = 0.5  # BM25 x 1.5 when the search words stand close together
TITLE_BOOST = 1.0  # BM25 x 2 when the title has every search word (in some form)
NEAR_DISTANCE = 10  # words in between

_INFLECTIONS = ("en", "es", "e", "n", "s")
MIN_FORM = 5  # shorter words are not reduced or looked up inside compounds


@lru_cache(maxsize=4096)
def word_forms(tok: str) -> tuple[str, ...]:
    """The word and its likely base form(s): steuerbescheide -> steuerbescheid,
    rechnungen -> rechnung, vertraege -> vertraeg, vertrag."""
    if not tok.isalpha() or len(tok) <= MIN_FORM:
        return (tok,)
    forms = [tok]
    for suf in _INFLECTIONS:
        if tok.endswith(suf) and len(tok) - len(suf) >= MIN_FORM:
            stem = tok[: -len(suf)]
            forms.append(stem)
            # plural with umlaut: vertraeg -> vertrag, kontoauszueg -> kontoauszug
            m = re.search(r"(ae|oe|ue)(?!.*(ae|oe|ue))", stem)
            if m and m.start() >= len(stem) - 5:
                forms.append(stem[: m.start()] + stem[m.start()] + stem[m.end() :])
            break
    return tuple(dict.fromkeys(forms))


def _prefixable(tok: str) -> bool:
    return not any(c.isdigit() for c in tok) and len(tok) >= 3


def _literal_forms(t: Term) -> tuple[str, ...]:
    return (t.tokens[0],) if t.exact or t.quoted else word_forms(t.tokens[0])


def _word_term(words: tuple[str, ...]) -> Term:
    """A synonym as a search term: a phrase, or one word (short ones exactly: "kfz" should not
    find "kfzmeister...")."""
    if len(words) > 1:
        return Term(list(words), phrase=True, quoted=True)
    return Term([words[0]], exact=len(words[0]) < MIN_FORM)


def _expand(v: Vocab, t: Term, deep: bool = True) -> None:
    """Fill the levels of a term from the vocabulary. `deep`: also split compounds and look for
    similar spellings; not deep: the term is a part of a split compound."""
    t.stems, t.compounds, t.synonyms, t.parts, t.similar = [], [], [], [], []
    if t.quoted or t.number_run:
        return
    if t.phrase:
        # "Kfz-Steuer": other words for the whole ("Kraftfahrzeugsteuer"), or its parts in
        # any of their forms
        t.synonyms = [_word_term(a) for a in expand.related(tuple(t.tokens))]
        if deep and all(tok.isalpha() for tok in t.tokens):
            t.parts = [Term([tok]) for tok in t.tokens]
            for p in t.parts:
                _expand(v, p, deep=False)
        return
    tok = t.tokens[0]
    if not tok.isalpha():
        return
    t.stems = expand.same_stem(v, tok)
    forms = tuple(dict.fromkeys([*word_forms(tok), *(s for s in t.stems if len(s) >= 4)]))
    t.compounds = expand.compounds(v, tok, forms, part=not deep)
    t.synonyms = [_word_term(a) for a in expand.related((tok,))]
    if deep:
        parts = expand.split(v, tok)
        if parts:
            t.parts = [Term([p]) for p in parts]
            for p in t.parts:
                _expand(v, p, deep=False)
        t.similar = expand.similar(
            v, tok, lambda w: _term_matches(t, w, FORMS) or _synonym_matches(t, w)
        )


def _synonym_matches(t: Term, word: str) -> bool:
    return any(_term_matches(s, word, LITERAL) for s in t.synonyms)


def _found_anywhere(v: Vocab, t: Term) -> bool:
    """Whether any level of the term (short of similar spellings) occurs in the index."""
    if t.stems or t.compounds or t.parts:
        return True
    tok = t.tokens[0]
    if any(v.has_prefix(f) if _prefixable(f) else v.docs(f) for f in word_forms(tok)):
        return True
    return any(
        v.has_prefix(s.tokens[0]) if not s.exact and not s.phrase else v.docs(s.tokens[0])
        for s in t.synonyms
    )


def _prepare_terms(
    conn: sqlite3.Connection, terms: list[Term], stats: dict[str, Any]
) -> list[tuple[str, str]]:
    """Expand every term; a word that occurs nowhere, in no form, is replaced by the closest
    indexed word (a typo). Returns the corrections."""
    v = Vocab(conn)
    corrections: list[tuple[str, str]] = []
    t_f = time.perf_counter()
    for t in terms:
        _expand(v, t)
        if t.phrase or t.quoted:
            continue
        tok = t.tokens[0]
        if any(c.isdigit() for c in tok) or len(tok) < 4 or _found_anywhere(v, t):
            continue
        fixed = expand.correct(v, tok, stats)
        if fixed:
            corrections.append((tok, fixed))
            t.tokens = [fixed]
            _expand(v, t)
    stats["ms"] = round((time.perf_counter() - t_f) * 1000, 2)
    return corrections


# --- FTS query construction ---------------------------------------------------------------


def _token_expr(tok: str) -> str:
    # tokens are [^\W_]+ after folding -> safe inside FTS5 double quotes
    return f'"{tok}"*' if _prefixable(tok) else f'"{tok}"'


def _or(parts: list[str]) -> str:
    parts = list(dict.fromkeys(parts))
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _term_expr(t: Term, level: int = SIMILAR) -> str:
    """FTS5 expression for a term with everything it stands for up to `level`."""
    if t.phrase:
        alts = ['"' + " ".join(t.tokens) + '"']
        if all(any(c.isdigit() for c in tok) for tok in t.tokens):
            alts.append(f'"{"".join(t.tokens)}"')  # "8372 9381" also finds "83729381"
        elif level >= FORMS and not t.quoted and all(tok.isalpha() for tok in t.tokens):
            alts.append(_token_expr("".join(t.tokens)))  # "Kfz-Steuer" finds "Kfzsteuer"
    elif t.exact:
        alts = [f'"{t.tokens[0]}"']
    else:
        alts = [_token_expr(f) for f in _literal_forms(t)]
    if level >= FORMS:
        alts += [f'"{w}"' for w in (*t.stems, *t.compounds)]
    if level >= RELATED:
        alts += [_term_expr(s, LITERAL) for s in t.synonyms]
        if t.parts:
            alts.append("(" + " AND ".join(_term_expr(p, RELATED) for p in t.parts) + ")")
    if level >= SIMILAR:
        alts += [f'"{w}"' for w in t.similar]
    return _or(alts)


def _is_number(t: Term) -> bool:
    return not t.phrase and any(c.isdigit() for c in t.tokens[0])


def _merge_number_runs(terms: list[Term]) -> list[Term]:
    out: list[Term] = []
    for t in terms:
        if out and _is_number(t) and (_is_number(out[-1]) or out[-1].number_run):
            prev = out.pop()
            out.append(Term(prev.tokens + t.tokens, phrase=True, number_run=True))
        else:
            out.append(t)
    return out


def build_match(terms: list[Term], op: str = "AND", level: int = SIMILAR) -> str:
    return f" {op} ".join(_term_expr(t, level) for t in terms)


def _near_expr(terms: list[Term]) -> str | None:
    """The search words as typed, close together in one field (NEAR), or None for one word."""
    if len(terms) < 2:
        return None
    parts = []
    for t in terms:
        if t.phrase:
            parts.append('"' + " ".join(t.tokens) + '"')
        else:
            parts.append(_token_expr(t.tokens[0]) if not t.exact else f'"{t.tokens[0]}"')
    return f"NEAR({' '.join(parts)}, {NEAR_DISTANCE})"


def _relevance(
    conn: sqlite3.Connection,
    terms: list[Term],
    partial: bool,
    years: list[str],
    base: str,
    params: list[Any],
) -> list[tuple[str, float]]:
    """All documents of the search (the query `base` with its filters), best first, with their
    rank (lower = better).

    1. partial matches (OR fallback): more search words found first (as Meilisearch's "words")
    2. tiers: all words in the document's own description (title, sender, type, tags, fields,
       notes) before words only in the text; within each, the word as typed before its forms,
       before other words for it; similar spellings last
    3. BM25 with the column weights - of the words of the document's tier only, so a letter
       full of compounds of a word does not beat one with the word itself - improved when the
       title has all the words (x2), when they stand close together (x1.5) and when a year in
       the query is the document's year (document date or title, x1.5)
    4. received date and ingest order, newest first

    One FTS query per tier and boost, then sorted here: a BM25 per document and tier in SQL
    would evaluate the whole expression once per document.
    """
    rows = conn.execute(
        f"SELECT d.rowid, d.id, d.received_at, d.ingest_sequence, d.document_date, d.title {base}",
        params,
    ).fetchall()
    if not rows:
        return []
    weights = ", ".join(str(COLUMN_WEIGHTS[c]) for c in FTS_COLUMNS)

    def members(expr: str) -> set[int]:
        return {
            r[0] for r in conn.execute("SELECT rowid FROM doc_fts WHERE doc_fts MATCH ?", (expr,))
        }

    bm25_cache: dict[str, dict[int, float]] = {}

    def bm25(expr: str) -> dict[int, float]:
        if expr not in bm25_cache:
            bm25_cache[expr] = dict(
                conn.execute(
                    f"SELECT rowid, bm25(doc_fts, {weights}) FROM doc_fts WHERE doc_fts MATCH ?",
                    (expr,),
                ).fetchall()
            )
        return bm25_cache[expr]

    op = " OR " if partial else " AND "
    meta = "{" + " ".join(META_COLUMNS) + "} : "
    exprs = [op.join(_term_expr(t, lvl) for t in terms) for lvl in (LITERAL, FORMS, RELATED)]
    exprs.append(op.join(_term_expr(t) for t in terms))
    tiers: list[tuple[str, str]] = []  # (who is in the tier, what BM25 scores)
    for e in [meta + "(" + e + ")" for e in exprs[:-1]] + exprs:
        if e not in (t[0] for t in tiers):
            tiers.append((e, e.removeprefix(meta)))
    todo = {r[0] for r in rows}
    tier: dict[int, int] = {}
    score: dict[int, float] = {}
    for n, (who, what) in enumerate(tiers):
        found = todo if n == len(tiers) - 1 else todo & members(who)
        if found:
            scores = bm25(what)
            for rid in found:
                tier[rid], score[rid] = n, scores.get(rid, 0.0)
            todo -= found
        if not todo:
            break

    boost = dict.fromkeys(tier, 1.0)
    for rid in members("{title} : (" + exprs[-1] + ")") & boost.keys():
        boost[rid] += TITLE_BOOST
    near = _near_expr(terms)
    if near:
        for rid in members(near) & boost.keys():
            boost[rid] += PROXIMITY_BOOST
    if years:
        for r in rows:
            if (r["document_date"] or "")[:4] in years or any(
                y in (r["title"] or "") for y in years
            ):
                boost[r[0]] += YEAR_BOOST - 1
    missing = dict.fromkeys(tier, 0)
    if partial:
        for t in terms:
            has = members(_term_expr(t))
            for rid in missing:
                missing[rid] += rid not in has

    def rank(rid: int) -> float:
        return missing[rid] * TIER_STEP * 10 + tier[rid] * TIER_STEP + score[rid] * boost[rid]

    ordered = sorted(rows, key=lambda r: r["ingest_sequence"], reverse=True)
    ordered.sort(key=lambda r: r["received_at"] or "", reverse=True)
    ordered.sort(key=lambda r: rank(r[0]))
    return [(r["id"], rank(r[0])) for r in ordered]


# --- main entry --------------------------------------------------------------------------


@dataclass
class _Clause:
    facet: str  # which filter group it belongs to (facet counts ignore their own group)
    sql: str
    params: list[Any]


def _base(match: str | None, clauses: list[_Clause], exclude: str = "") -> tuple[str, list[Any]]:
    """FROM/WHERE for the documents matching the query and all filters except group `exclude`."""
    used = [c for c in clauses if c.facet != exclude]
    params: list[Any] = []
    where = [c.sql for c in used]
    for c in used:
        params += c.params
    if match is not None:
        sql = "FROM doc_fts JOIN documents d ON d.rowid = doc_fts.rowid WHERE doc_fts MATCH ?"
        return sql + "".join(" AND " + w for w in where), [match, *params]
    return "FROM documents d WHERE 1" + "".join(" AND " + w for w in where), params


def search(
    conn: sqlite3.Connection,
    p: SearchParams,
    *,
    with_facets: bool | str = False,
    today: date | None = None,
) -> SearchResult:
    t0 = time.perf_counter()
    per_page = max(1, min(int(p.per_page or 25), 100))
    page = max(1, int(p.page or 1))
    q = (p.q or "").strip()
    errors: list[str] = []
    notes: list[str] = []

    # 1. exact ID / hash
    exact_row = None
    if q:
        try:
            uid = str(uuid.UUID(q))
            exact_row = conn.execute("SELECT id FROM documents WHERE id=?", (uid,)).fetchone()
        except ValueError:
            if _HEX64.match(q):
                exact_row = conn.execute(
                    "SELECT id FROM documents WHERE sha256=?", (q.lower(),)
                ).fetchone()
            elif _HEX_PART.match(q) and re.search(r"\d", q) and re.search(r"[a-fA-F]", q):
                # the start of an ID or hash (e.g. copied from below the page view); IDs are
                # not in the word index, where their fragments would match words like "Bad"
                hx = q.lower().replace("-", "")
                found = conn.execute(
                    "SELECT id FROM documents WHERE replace(id, '-', '') LIKE ? OR sha256 LIKE ? "
                    "LIMIT 2",
                    (hx + "%", hx + "%"),
                ).fetchall()
                exact_row = found[0] if len(found) == 1 else None
    if exact_row:
        items = _hydrate(conn, [(exact_row["id"], None)], [])
        for it in items:
            it["reasons"] = [_("Exact ID/hash")]
        return SearchResult(
            items, 1, 1, per_page, "relevance", exact=True,
            took_ms=(time.perf_counter() - t0) * 1000,
        )  # fmt: skip

    # 2. "März 2025", "letztes Jahr", ... -> document date range
    date_phrase = None
    if q and not p.literal:
        q, date_phrase = datephrases.extract(q, today)

    parsed = parse_query(q)
    errors += parsed.errors

    try:
        clauses = _filters(conn, p, parsed.filters, notes)
    except SearchSyntaxError as e:
        errors.append(str(e))
        return SearchResult([], 0, page, per_page, p.sort or "received", errors=errors,
                            took_ms=(time.perf_counter() - t0) * 1000)  # fmt: skip
    if date_phrase:
        clauses.append(
            _Clause(
                "date",
                "d.document_date BETWEEN ? AND ?",
                [date_phrase.date_from, date_phrase.date_to],
            )
        )

    terms = parsed.terms
    fuzzy_stats: dict[str, Any] = {}
    corrections = _prepare_terms(conn, terms, fuzzy_stats) if terms else []

    sort = p.sort if p.sort in SORTS else ("relevance" if terms else "received")
    if sort == "relevance" and not terms:
        sort = "received"
    order = {
        "relevance": "d.received_at DESC, d.ingest_sequence DESC",  # ranked in _relevance
        "received": "d.received_at DESC, d.ingest_sequence DESC",
        "document_date": "d.document_date IS NULL, d.document_date DESC, "
        "d.received_at DESC, d.ingest_sequence DESC",
        "title": "d.title COLLATE NOCASE ASC, d.received_at DESC, d.ingest_sequence DESC",
    }[sort]

    partial = False
    match: str | None = None
    if terms:
        match = build_match(terms, "AND")
        base, params = _base(match, clauses)
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        merged = _merge_number_runs(terms)
        if total == 0 and merged != terms:
            # "8372 9381" typed with a space: try it as one number (and as a phrase)
            match = build_match(merged, "AND")
            base, params = _base(match, clauses)
            total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            if total:
                terms = merged
        if total == 0 and len(terms) > 1:
            match = build_match(terms, "OR")
            base, params = _base(match, clauses)
            total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            partial = total > 0
        # Years are too common for BM25's IDF: a year in the query that is the document's
        # year counts extra (see _rank_sql).
        years = sorted(
            {
                t.tokens[0]
                for t in terms
                if not t.phrase and re.fullmatch(r"(19|20)\d\d", t.tokens[0])
            }
        )
        if sort == "relevance":
            ranked = _relevance(conn, terms, partial, years, base, params)
            rows = ranked[(page - 1) * per_page : page * per_page]
        else:
            rows = [
                (r["id"], None)
                for r in conn.execute(
                    f"SELECT d.id {base} ORDER BY {order} LIMIT ? OFFSET ?",
                    [*params, per_page, (page - 1) * per_page],
                )
            ]
    else:
        base, params = _base(None, clauses)
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = [
            (r["id"], None)
            for r in conn.execute(
                f"SELECT d.id {base} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, per_page, (page - 1) * per_page],
            )
        ]

    items = _hydrate(conn, rows, terms)
    if partial:
        notes.append(_("Not all search terms occur together – showing partial matches."))
    result = SearchResult(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        sort=sort,
        corrections=corrections,
        partial=partial,
        errors=errors,
        notes=notes,
        took_ms=0.0,
        fuzzy_stats=fuzzy_stats,
        date_phrase=date_phrase,
        q_rest=q,
    )
    if with_facets:
        result.facets = _facets(conn, match, clauses, p, within=with_facets == "within")
    result.took_ms = (time.perf_counter() - t0) * 1000
    return result


def _term_ids(conn: sqlite3.Connection, kind: str, names: list[str], notes: list[str]) -> list[int]:
    ids = []
    for n in names:
        tid = tax.find_term(conn, kind, n)
        if tid is None:
            notes.append(_("“%(name)s” is unknown.", name=n))
            ids.append(-1)
        else:
            ids.append(tid)
    return ids


def _in(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def _filters(
    conn: sqlite3.Connection,
    p: SearchParams,
    syntax: dict[str, list[str]],
    notes: list[str],
) -> list[_Clause]:
    out: list[_Clause] = []
    add = lambda facet, sql, *params: out.append(_Clause(facet, sql, list(params)))  # noqa: E731
    corr = list(p.correspondent) + syntax.get("correspondent", [])
    if corr:
        ids = _term_ids(conn, "correspondent", corr, notes)
        add("correspondent", f"d.correspondent_id IN ({_in(ids)})", *ids)
    types = list(p.document_type) + syntax.get("document_type", [])
    if types:
        ids = _term_ids(conn, "document_type", types, notes)
        add("document_type", f"d.document_type_id IN ({_in(ids)})", *ids)
    tags = list(p.tags) + syntax.get("tag", [])
    if tags:
        ids = _term_ids(conn, "tag", tags, notes)
        if p.tag_mode == "any":
            add("tag", "EXISTS (SELECT 1 FROM document_tags dt WHERE dt.doc_id=d.id "
                f"AND dt.tag_id IN ({_in(ids)}))", *ids)  # fmt: skip
        else:
            for tid in ids:
                add("tag", "EXISTS (SELECT 1 FROM document_tags dt "
                    "WHERE dt.doc_id=d.id AND dt.tag_id=?)", tid)  # fmt: skip
    for y in syntax.get("year", []):
        add("date", "d.document_date BETWEEN ? AND ?", *_year_range(y))
    for r in syntax.get("received", []):
        add("received", "substr(d.received_at, 1, 10) BETWEEN ? AND ?", *_date_range(r, "received"))
    if p.date_from:
        add("date", "d.document_date >= ?", _date_range(p.date_from, _("Document date from"))[0])
    if p.date_to:
        add("date", "d.document_date <= ?", _date_range(p.date_to, _("Document date to"))[1])
    if p.received_from:
        add("received", "substr(d.received_at, 1, 10) >= ?",
            _date_range(p.received_from, _("Received from"))[0])  # fmt: skip
    if p.received_to:
        add("received", "substr(d.received_at, 1, 10) <= ?",
            _date_range(p.received_to, _("Received to"))[1])  # fmt: skip
    sources = list(p.source) + syntax.get("source", [])
    if sources:
        bad = [s for s in sources if s not in SOURCES]
        if bad:
            raise SearchSyntaxError(
                _(
                    "Unknown source “%(source)s”. Allowed: %(options)s.",
                    source=bad[0],
                    options=", ".join(SOURCES),
                )
            )
        add("source", f"d.source IN ({_in(sources)})", *sources)
    if p.status:
        add("status", f"d.status IN ({_in(p.status)})", *p.status)
    if p.filed == "yes":
        add("filed", "d.filing_sequence IS NOT NULL")
    elif p.filed == "no":  # paper not dealt with yet: not filed, not in a folder, not shredded
        add("filed", "d.paper = 1 AND d.filing_sequence IS NULL AND d.paper_location IS NULL "
            "AND d.paper_discarded_at IS NULL")  # fmt: skip
    if p.session:
        add("session", "d.scan_session_id = ?", p.session)
    if p.filing_section:
        add("filing_section", "d.filing_section = ?", p.filing_section)
    if p.filing_binder:
        add("filing_binder", "d.filing_binder = ?", p.filing_binder)
    for bound in (p.cf_min, p.cf_max):
        if isinstance(bound, str):
            raise SearchSyntaxError(
                _("Amount “%(value)s” not understood – e.g. 1234.56.", value=bound[:40])
            )
    if p.cf_key and (p.cf_min is not None or p.cf_max is not None):
        cond = "c.key = ?"
        cparams: list[Any] = [p.cf_key]
        if p.cf_min is not None:
            cond += " AND c.value_num >= ?"
            cparams.append(float(p.cf_min))
        if p.cf_max is not None:
            cond += " AND c.value_num <= ?"
            cparams.append(float(p.cf_max))
        add("custom", "EXISTS (SELECT 1 FROM custom_field_values c WHERE c.doc_id=d.id "
            f"AND {cond})", *cparams)  # fmt: skip
    return out


# --- facets ------------------------------------------------------------------------------


def _facets(
    conn: sqlite3.Connection,
    match: str | None,
    clauses: list[_Clause],
    p: SearchParams,
    within: bool = False,
) -> dict[str, Any]:
    """Result counts per correspondent, type, tag, source and month of the document date.

    For the search page, groups combined with OR (correspondent, type, source, "any" tags,
    date) are counted without their own filter, so the other values of the group stay visible
    and show how many documents selecting them would add. "All" tags are counted within the
    current results (narrowing). ``within``: every count within all filters (statistics, e.g.
    for Claude: "how many per year in 2025" must not list other years).
    """

    def term_counts(kind: str, column: str) -> list[dict[str, Any]]:
        base, params = _base(match, clauses, exclude="" if within else kind)
        rows = conn.execute(
            f"SELECT t.name, COUNT(*) AS n {base.replace('WHERE', f'JOIN taxonomy t ON t.id = d.{column} WHERE', 1)} "
            "GROUP BY t.id ORDER BY n DESC, t.norm",
            params,
        ).fetchall()
        return [{"name": r[0], "count": r[1]} for r in rows]

    out: dict[str, Any] = {
        "correspondent": term_counts("correspondent", "correspondent_id"),
        "document_type": term_counts("document_type", "document_type_id"),
    }
    base, params = _base(
        match, clauses, exclude="tag" if p.tag_mode == "any" and not within else ""
    )
    out["tag"] = [
        {"name": r[0], "count": r[1]}
        for r in conn.execute(
            "SELECT t.name, COUNT(*) AS n FROM document_tags dt JOIN taxonomy t ON t.id = dt.tag_id "
            f"WHERE dt.doc_id IN (SELECT d.id {base}) GROUP BY t.id ORDER BY n DESC, t.norm",
            params,
        )
    ]
    base, params = _base(match, clauses, exclude="" if within else "source")
    out["source"] = [
        {"value": r[0], "count": r[1]}
        for r in conn.execute(
            f"SELECT d.source, COUNT(*) AS n {base} GROUP BY d.source ORDER BY n DESC", params
        )
    ]
    base, params = _base(match, clauses, exclude="" if within else "date")
    months: dict[str, int] = {}
    undated = 0
    for r in conn.execute(
        f"SELECT substr(d.document_date, 1, 7) AS m, COUNT(*) {base} GROUP BY m", params
    ):
        if r[0]:
            months[r[0]] = r[1]
        else:
            undated = r[1]
    out["months"] = dict(sorted(months.items()))
    out["undated"] = undated
    return out


# --- suggestions while typing ------------------------------------------------------------

_KIND_LABELS = i18n.Labels(
    {"correspondent": N_("Sender"), "document_type": N_("Type"), "tag": N_("Tag")}
)
_KIND_PARAM = {"correspondent": "correspondent", "document_type": "document_type", "tag": "tag"}


def _taxonomy_matches(conn: sqlite3.Connection, norm: str) -> list[dict[str, Any]]:
    # norm consists of folded letters/digits and single spaces -> no LIKE wildcards inside
    rows = conn.execute(
        """
        SELECT t.id, t.kind, t.name, t.norm, NULL AS alias FROM taxonomy t
          WHERE t.norm LIKE ? OR t.norm LIKE ?
        UNION
        SELECT t.id, t.kind, t.name, t.norm, a.alias FROM taxonomy_alias a
          JOIN taxonomy t ON t.id = a.term_id
          WHERE a.alias_norm LIKE ? OR a.alias_norm LIKE ?
        """,
        (norm + "%", "% " + norm + "%") * 2,
    ).fetchall()
    seen: dict[int, dict[str, Any]] = {}
    for r in rows:
        if r["id"] in seen:
            continue
        if r["kind"] == "tag":
            n = conn.execute(
                "SELECT COUNT(*) FROM document_tags WHERE tag_id=?", (r["id"],)
            ).fetchone()[0]
        else:
            col = "correspondent_id" if r["kind"] == "correspondent" else "document_type_id"
            n = conn.execute(
                f"SELECT COUNT(*) FROM documents WHERE {col}=?", (r["id"],)
            ).fetchone()[0]
        if not n:
            continue
        seen[r["id"]] = {
            "kind": r["kind"],
            "group": _KIND_LABELS[r["kind"]],
            "label": r["name"],
            "detail": _("also “%(alias)s”", alias=r["alias"]) if r["alias"] else "",
            "count": n,
            "param": _KIND_PARAM[r["kind"]],
            "value": r["name"],
            "starts": r["norm"].startswith(norm),
        }
    return sorted(seen.values(), key=lambda x: (not x["starts"], -x["count"], x["label"].lower()))


SUGGEST_DOCS = 10  # documents in the list while typing: enough for an overview


def suggest(conn: sqlite3.Connection, text: str, limit: int = 18) -> dict[str, Any]:
    """Suggestions for the search box: filter values, numbers from fields, documents, dates.

    Taxonomy values are looked up for the whole input, else for its last two words, else the
    last word; ``replace`` says which part of the input a chosen filter replaces.
    """
    text = (text or "").strip()[:200]
    out: list[dict[str, Any]] = []
    if not text:
        return {"items": out, "replace": ""}
    words = text.split()
    replace = ""
    for n in dict.fromkeys(x for x in (len(words), 2, 1) if x <= len(words)):
        frag = " ".join(words[-n:])
        norm = normalize_name(frag)
        if len(norm) < 2:
            continue
        found = _taxonomy_matches(conn, norm)
        if not found and n == 1:  # "rechnungen" -> type "Rechnung"
            for form in word_forms(norm)[1:]:
                found = found or _taxonomy_matches(conn, form)
        if found:
            out += found[:4]
            replace = frag
            break

    # numbers: custom field values (contract, invoice, customer numbers) -> open the document
    key = re.sub(r"[^0-9a-z]", "", fold(words[-1]))
    if sum(c.isdigit() for c in key) >= 3 and not re.fullmatch(r"(19|20)\d\d", key):
        for r in conn.execute(
            """
            SELECT c.doc_id, c.key, c.value_text, d.title FROM custom_field_values c
              JOIN documents d ON d.id = c.doc_id
             WHERE c.value_text IS NOT NULL AND c.type != 'date'
               AND lower(replace(replace(replace(replace(c.value_text, ' ', ''), '-', ''),
                   '/', ''), '.', '')) LIKE ?
             ORDER BY d.received_at DESC LIMIT 3
            """,
            (f"%{key}%",),
        ):
            out.append({
                "kind": "number", "group": _("Number"), "label": f"{r['key']}: {r['value_text']}",
                "detail": r["title"], "href": f"/documents/{r['doc_id']}",
            })  # fmt: skip

    _rest, phrase = datephrases.extract(text)
    if phrase:
        out.append({"kind": "date", "group": _("Date range"), "label": phrase.label,
                    "detail": _("filters by document date")})  # fmt: skip

    docs = search(conn, SearchParams(q=text, per_page=SUGGEST_DOCS))
    if docs.items and not docs.partial:
        for it in docs.items:
            detail = " · ".join(
                x for x in (it.get("correspondent"), _date_text(it.get("document_date"))) if x
            )
            out.append({"kind": "document", "group": _("Documents"), "label": it["title"],
                        "detail": detail, "href": f"/documents/{it['id']}"})  # fmt: skip
    return {"items": out[:limit], "replace": replace}


def _date_text(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return i18n.format_date(date.fromisoformat(iso[:10]))
    except ValueError:
        return iso


# --- similar documents -------------------------------------------------------------------


def similar(conn: sqlite3.Connection, doc_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Documents with the most distinctive words of this one in common (TF-IDF -> BM25 OR query).

    Same correspondent / type rank higher. Purely local, no embeddings.
    """
    d = conn.execute(
        "SELECT rowid, correspondent_id, document_type_id, title, summary FROM documents "
        "WHERE id=?",
        (doc_id,),
    ).fetchone()
    if d is None:
        return []
    text_row = conn.execute(
        "SELECT content FROM document_text WHERE doc_id=?", (doc_id,)
    ).fetchone()
    body = (text_row[0] if text_row else "")[:30000]
    tf: dict[str, int] = {}
    for tok in TOKEN_RE.findall(fold(" ".join([d["title"] * 3, d["summary"], body]))):
        digits = sum(c.isdigit() for c in tok)
        if (digits == 0 and len(tok) >= 4) or (digits and len(tok) >= 6):
            tf[tok] = tf.get(tok, 0) + 1
    if not tf:
        return []
    n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if n_docs < 2:
        return []
    cand = sorted(tf, key=lambda t: -tf[t])[:400]
    df: dict[str, int] = {}
    for i in range(0, len(cand), 200):
        chunk = cand[i : i + 200]
        for r in conn.execute(
            f"SELECT term, doc FROM doc_vocab WHERE term IN ({_in(chunk)})", chunk
        ):
            df[r[0]] = r[1]
    # ignore words in more than half of all documents - unless the archive is still tiny
    max_df = n_docs if n_docs < 10 else int(0.5 * n_docs)
    scored = [
        (tf[t] ** 0.5 * math.log(n_docs / df[t]), t) for t in cand if 2 <= df.get(t, 0) <= max_df
    ]
    top = [t for _, t in sorted(scored, reverse=True)[:12]]
    if not top:
        return []
    weights = ", ".join(str(COLUMN_WEIGHTS[c]) for c in FTS_COLUMNS)
    match = " OR ".join(f'"{t}"' for t in top)
    rows = conn.execute(
        f"""
        SELECT d.id, d.correspondent_id = ? AS same_corr, d.document_type_id = ? AS same_type,
               bm25(doc_fts, {weights})
                 * (CASE WHEN d.correspondent_id = ? THEN 1.6 ELSE 1.0 END)
                 * (CASE WHEN d.document_type_id = ? THEN 1.2 ELSE 1.0 END) AS rank
          FROM doc_fts JOIN documents d ON d.rowid = doc_fts.rowid
         WHERE doc_fts MATCH ? AND d.id != ?
         ORDER BY rank LIMIT ?
        """,
        [d["correspondent_id"], d["document_type_id"]] * 2 + [match, doc_id, limit],
    ).fetchall()
    items = _hydrate(conn, [(r["id"], r["rank"]) for r in rows], [])
    for it, r in zip(items, rows, strict=True):
        why = []
        if r["same_corr"]:
            why.append(_("same sender"))
        if r["same_type"]:
            why.append(_("same type"))
        it["why"] = why
    return items


# --- hits inside a document (viewer) -----------------------------------------------------


def highlight_terms(conn: sqlite3.Connection, q: str) -> list[Term]:
    """The search terms of `q` as the search used them (date phrases removed, typos fixed)."""
    q = (q or "").strip()[:500]
    if not q:
        return []
    rest, _phrase = datephrases.extract(q)
    terms = parse_query(rest).terms
    _prepare_terms(conn, terms, {})
    return terms


def word_matches(word: str, terms: list[Term]) -> bool:
    toks = TOKEN_RE.findall(fold(word))
    return any(_term_matches(t, w) for t in terms for w in toks)


def count_hits(text: str, terms: list[Term]) -> int:
    return sum(
        1
        for m in TOKEN_RE.finditer(fold(text or ""))
        if any(_term_matches(t, m.group(0)) for t in terms)
    )


# --- result hydration, snippets and reasons ----------------------------------------------


def _hydrate(
    conn: sqlite3.Connection, rows: list[tuple[str, float | None]], terms: list[Term]
) -> list[dict[str, Any]]:
    out = []
    for doc_id, rank in rows:
        d = conn.execute("SELECT rowid, * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            continue
        meta = json.loads(d["metadata_json"])
        text_row = conn.execute(
            "SELECT content FROM document_text WHERE doc_id=?", (doc_id,)
        ).fetchone()
        text = text_row[0] if text_row else ""
        item = {
            "id": doc_id,
            "title": d["title"] or d["original_filename"],
            "document_date": d["document_date"],
            "document_date_status": meta.get("document_date_status"),
            "received_at": d["received_at"],
            "ingest_sequence": d["ingest_sequence"],
            "correspondent": meta.get("correspondent"),
            "document_type": meta.get("document_type"),
            "tags": meta.get("tags", []),
            "source": d["source"],
            "status": d["status"],
            "text_status": d["text_status"],
            "mime_type": d["mime_type"],
            "page_count": d["page_count"],
            "revision": d["revision"],  # in the thumbnail URL: a new cover page after edits
            "filing_section": d["filing_section"],
            "filing_binder": d["filing_binder"],
            "taken_out": d["filing_sequence"] is not None and d["paper_location"] is not None,
            "filed": d["filing_sequence"] is not None,
            "paper": bool(d["paper"]),
            "rank": rank,
        }
        if terms:
            fts_row = conn.execute("SELECT * FROM doc_fts WHERE rowid=?", (d["rowid"],)).fetchone()
            item["reasons"] = _reasons(fts_row, terms) if fts_row else []
            item["snippet_html"] = snippet_html(text or meta.get("summary", ""), terms)
        else:
            item["reasons"] = []
            item["snippet_html"] = html.escape((meta.get("summary") or text[:220]).strip()[:220])
        out.append(item)
    return out


def _literal_match(tok: str, word: str, forms: tuple[str, ...]) -> bool:
    if not _prefixable(tok):
        return word == tok
    return any(word.startswith(f) for f in forms)


def _term_matches(t: Term, word: str, level: int = SIMILAR) -> bool:
    """Whether an indexed (folded) word is one the term stands for, up to `level` - the same
    rule as the index query, for highlighting, "found in" and the hits on a page."""
    if t.phrase:
        if any(_literal_match(tok, word, (tok,)) for tok in t.tokens):
            return True
    elif t.exact:
        if word == t.tokens[0]:
            return True
    elif _literal_match(t.tokens[0], word, _literal_forms(t)):
        return True
    if level >= FORMS and (word in t.stems or word in t.compounds):
        return True
    if level >= RELATED and (
        any(_term_matches(s, word, LITERAL) for s in t.synonyms)
        or any(_term_matches(p, word, RELATED) for p in t.parts)
    ):
        return True
    return level >= SIMILAR and word in t.similar


def _reasons(fts_row: sqlite3.Row, terms: list[Term]) -> list[str]:
    found = []
    for col in FTS_COLUMNS[1:]:
        words = (fts_row[col] or "").split()
        wordset = set()
        for w in words:
            wordset.update(TOKEN_RE.findall(w))
        hit = False
        for t in terms:
            if t.phrase:
                if (
                    " ".join(t.tokens) in " ".join(TOKEN_RE.findall(fts_row[col] or ""))
                    or any(_term_matches(x, w, LITERAL) for x in t.synonyms for w in wordset)
                    or (t.parts and all(any(_term_matches(x, w) for w in wordset) for x in t.parts))
                ):
                    hit = True
            elif any(_term_matches(t, w) for w in wordset):
                hit = True
        if hit:
            found.append(COLUMN_LABELS[col])
    return found


def snippet_html(text: str, terms: list[Term], width: int = 220) -> str:
    """Escaped excerpt around the first match with <mark> highlighting."""
    if not text:
        return ""
    spans = []
    for m in TOKEN_RE.finditer(text):
        w = fold(m.group(0))
        if any(_term_matches(t, w) for t in terms):
            spans.append((m.start(), m.end()))
    if not spans:
        excerpt = text[:width].strip()
        return html.escape(excerpt) + ("…" if len(text) > width else "")
    start = max(0, spans[0][0] - width // 3)
    end = min(len(text), start + width)
    # snap to word boundaries
    if start > 0:
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < spans[0][0] else start
    out, pos = [], start
    for a, b in spans:
        if a < start or b > end:
            continue
        out.append(html.escape(text[pos:a]))
        out.append("<mark>" + html.escape(text[a:b]) + "</mark>")
        pos = b
    out.append(html.escape(text[pos:end]))
    res = re.sub(r"\s+", " ", "".join(out)).strip()
    return ("…" if start > 0 else "") + res + ("…" if end < len(text) else "")


# --- analysis helpers (API / MCP): text lines and field values across documents ----------

MAX_LINE = 400


def _scope(
    conn: sqlite3.Connection,
    p: SearchParams,
    extra: list[Term] | None = None,
    today: date | None = None,
) -> tuple[str, list[Any], list[str]]:
    """FROM/WHERE for all documents matching query + filters (+ extra required terms)."""
    notes: list[str] = []
    q = (p.q or "").strip()
    clauses: list[_Clause] = []
    if q and not p.literal:
        q, phrase = datephrases.extract(q, today)
        if phrase:
            clauses.append(
                _Clause(
                    "date", "d.document_date BETWEEN ? AND ?", [phrase.date_from, phrase.date_to]
                )
            )
            notes.append(_("Date range recognized: %(range)s", range=phrase.label))
    parsed = parse_query(q)
    notes += parsed.errors
    clauses = _filters(conn, p, parsed.filters, notes) + clauses
    terms = parsed.terms
    for a, b in _prepare_terms(conn, terms, {}):
        notes.append(_("Search term corrected: %(old)s -> %(new)s", old=a, new=b))
    terms = terms + (extra or [])
    return (*_base(build_match(terms, "AND") if terms else None, clauses), notes)


def find_lines(
    archive,
    p: SearchParams,
    pattern: str,
    *,
    regex: bool = False,
    context: int = 0,
    limit: int = 200,
    max_docs: int = 2000,
) -> dict[str, Any]:
    """Text lines matching `pattern` in all documents within query/filters, with page numbers.

    Plain patterns match case- and umlaut-insensitively as a substring of the line - also inside
    compound words ("miete" finds "Kaltmiete"); documents are preselected by their stored
    plain text. Regular expressions are applied to every document within the filters
    (case-insensitive).
    """
    from . import documents as docs

    conn = archive.conn
    pattern = (pattern or "").strip()
    if not pattern:
        raise SearchSyntaxError(_("Search pattern missing."))
    if len(pattern) > 200:
        raise SearchSyntaxError(_("Search pattern is too long (max. 200 characters)."))
    context = max(0, min(int(context), 3))
    limit = max(1, min(int(limit), 1000))
    if regex:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise SearchSyntaxError(_("Invalid regular expression: %(error)s", error=e)) from e
        extra: list[Term] = []
        hit = None  # matched in a separate process, see _regex_hits
    else:
        needle = re.sub(r"\s+", " ", fold(pattern))
        extra = []  # preselected below: the index query knows word prefixes only

        def hit(line: str) -> bool:
            return needle in re.sub(r"\s+", " ", fold(line))

    base, params, notes = _scope(conn, p, extra)
    rows = conn.execute(
        f"SELECT d.id, d.title, d.document_date, d.metadata_json {base} "
        "ORDER BY d.document_date IS NULL, d.document_date, d.ingest_sequence LIMIT ?",
        [*params, max_docs + 1],
    ).fetchall()
    truncated_docs = len(rows) > max_docs
    matches: list[dict[str, Any]] = []
    total = 0
    docs_matched = 0
    candidates = None if regex else _containing(conn, TOKEN_RE.findall(needle))
    loaded = []
    for r in rows[:max_docs]:
        if candidates is not None and r["id"] not in candidates:
            continue
        tp = docs.load_text_pages(archive, r["id"])
        if tp is not None:
            loaded.append((r, tp, [pg.text.splitlines() for pg in tp.pages]))
    if regex:
        flat = [
            ln[: MAX_LINE * 2] for _row, _tp, pages in loaded for lines in pages for ln in lines
        ]
        hits = _regex_hits(pattern, flat)
        n = 0
        regex_hit: dict[tuple[int, int, int], bool] = {}
        for di, (_row, _tp, pages) in enumerate(loaded):
            for pi, lines in enumerate(pages):
                for li in range(len(lines)):
                    regex_hit[(di, pi, li)] = n in hits
                    n += 1
    for di, (r, tp, pages) in enumerate(loaded):
        meta = json.loads(r["metadata_json"])
        found_here = False
        for pi, pg in enumerate(tp.pages):
            lines = pages[pi]
            for i, line in enumerate(lines):
                matched = regex_hit[(di, pi, i)] if regex else hit(line)  # type: ignore[misc]
                if not line.strip() or not matched:
                    continue
                total += 1
                found_here = True
                if len(matches) < limit:
                    m: dict[str, Any] = {
                        "doc_id": r["id"], "title": r["title"], "document_date": r["document_date"],
                        "correspondent": meta.get("correspondent"),
                        "document_type": meta.get("document_type"), "page": pg.page,
                        "line": line.strip()[:MAX_LINE],
                    }  # fmt: skip
                    if context:
                        m["before"] = [x.strip()[:MAX_LINE] for x in lines[max(0, i - context) : i]]
                        m["after"] = [x.strip()[:MAX_LINE] for x in lines[i + 1 : i + 1 + context]]
                    matches.append(m)
        docs_matched += found_here
    return {
        "matches": matches,
        "total_matches": total,
        "documents_matched": docs_matched,
        "documents_scanned": min(len(rows), max_docs),
        "truncated": total > len(matches) or truncated_docs,
        "notes": notes,
    }


REGEX_SECONDS = 5.0
_REGEX_CHILD = (
    "import json, re, sys\n"
    "d = json.load(sys.stdin)\n"
    "rx = re.compile(d['p'], re.IGNORECASE)\n"
    "json.dump([i for i, t in enumerate(d['l']) if rx.search(t)], sys.stdout)\n"
)


def _regex_hits(pattern: str, lines: list[str]) -> set[int]:
    """Indexes of the lines matching a user's regular expression - in a separate process with a
    time limit: Python's regex engine cannot be interrupted, and a pattern like "(.+)+#" would
    otherwise hold the whole server for hours."""
    import subprocess
    import sys

    try:
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _REGEX_CHILD],
            input=json.dumps({"p": pattern, "l": lines}), capture_output=True, text=True,
            timeout=REGEX_SECONDS, check=True,
        )  # fmt: skip
    except subprocess.TimeoutExpired as e:
        raise SearchSyntaxError(
            _(
                "The regular expression is too expensive (more than %(sec)s s) – please "
                "simplify it, e.g. without nested repetitions like (.+)+.",
                sec=f"{REGEX_SECONDS:.0f}",
            )
        ) from e
    except subprocess.CalledProcessError as e:
        raise SearchSyntaxError(_("The regular expression could not be evaluated.")) from e
    return set(json.loads(out.stdout or "[]"))


def _containing(conn: sqlite3.Connection, parts: list[str], max_terms: int = 2000) -> set | None:
    """Documents with every part somewhere inside an indexed word (compounds: "miete" ->
    "kaltmiete", "mieter"); None = no preselection possible (a part too common)."""
    out: set | None = None
    for part in dict.fromkeys(parts):
        terms = [
            r[0]
            for r in conn.execute(
                "SELECT term FROM doc_vocab WHERE instr(term, ?) > 0 LIMIT ?", (part, max_terms + 1)
            )
        ]
        if len(terms) > max_terms:
            continue
        ids: set = set()
        for i in range(0, len(terms), 200):
            match = " OR ".join(f'"{t}"' for t in terms[i : i + 200])
            ids.update(
                r[0]
                for r in conn.execute("SELECT doc_id FROM doc_fts WHERE doc_fts MATCH ?", (match,))
            )
        out = ids if out is None else out & ids
        if not out:
            break
    return out


def field_values(
    conn: sqlite3.Connection, p: SearchParams, key: str | None = None, limit: int = 500
) -> dict[str, Any]:
    """Custom field values (amounts, numbers, dates) of the documents within query/filters.

    Without `key`: which fields exist there, with counts. With `key` (case-insensitive): one row
    per document with the value, numeric value and currency.
    """
    base, params, notes = _scope(conn, p)
    scope = f"SELECT d.id {base}"
    if not key:
        rows = conn.execute(
            f"SELECT c.key, c.type, COUNT(*) AS n FROM custom_field_values c "
            f"WHERE c.doc_id IN ({scope}) GROUP BY c.key, c.type ORDER BY n DESC, c.key",
            params,
        ).fetchall()
        return {"fields": [dict(r) for r in rows], "notes": notes}
    limit = max(1, min(int(limit), 2000))
    rows = conn.execute(
        f"SELECT c.key, c.type, c.value_text, c.value_num, d.id, d.title, d.document_date, "
        f"d.metadata_json FROM custom_field_values c JOIN documents d ON d.id = c.doc_id "
        f"WHERE c.key = ? COLLATE NOCASE AND c.doc_id IN ({scope}) "
        "ORDER BY d.document_date IS NULL, d.document_date, d.ingest_sequence LIMIT ?",
        [key, *params, limit + 1],
    ).fetchall()
    values = []
    for r in rows[:limit]:
        meta = json.loads(r["metadata_json"])
        cf = (meta.get("custom_fields") or {}).get(r["key"]) or {}
        values.append({
            "doc_id": r["id"], "title": r["title"], "document_date": r["document_date"],
            "correspondent": meta.get("correspondent"), "document_type": meta.get("document_type"),
            "key": r["key"], "type": r["type"], "value": r["value_text"],
            "number": r["value_num"], "currency": cf.get("currency"),
        })  # fmt: skip
    return {"values": values, "truncated": len(rows) > limit, "notes": notes}


def matching_ids(conn: sqlite3.Connection, p: SearchParams, limit: int = 2000) -> list[str]:
    """IDs of every document matching query + filters (strict: all words), oldest first -
    for bulk actions. At most `limit` + 1 (so callers can tell that there are more)."""
    base, params, _notes = _scope(conn, p)
    return [
        r[0]
        for r in conn.execute(
            f"SELECT d.id {base} ORDER BY d.ingest_sequence LIMIT ?", [*params, limit + 1]
        )
    ]
