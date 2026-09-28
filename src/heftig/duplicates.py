"""Detection of possible content duplicates (same document, different file).

Byte-identical files never become a second document (SHA-256 dedup at ingest). This module finds
the other case - e.g. an invoice received as PDF by e-mail and also photographed from paper - and
lists it for review. Nothing is merged or deleted automatically.

Signals (all local, no AI calls):
- text similarity (Jaccard over folded word sets)
- same document date, same correspondent
- same identifier (string custom field, e.g. invoice/contract number) or same amount

Differences are strong evidence *against* a duplicate: two monthly invoices of the same sender
share most of their words but differ in date, amount and invoice number. Therefore:
- the numbers in the text (dates, amounts, invoice numbers) must match, not just the words -
  template letters are "almost identical" except exactly there
- identifiers that recur on several documents (IBAN, customer number, mandate reference,
  tax number of the sender relationship) are no evidence *for* a duplicate
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import documents as docs
from .archive import Archive
from .db import now_iso, write_tx
from .i18n import N_
from .textnorm import TOKEN_RE, fold, normalize_name

log = logging.getLogger("heftig.duplicates")

TEXT_MIN = 0.55  # Jaccard similarity that counts as "very similar"
IDENTICAL_MIN_WORDS = 20  # automatic resolution needs at least this much identical text
RECURRING = 2  # an identifier on more documents than this belongs to the sender, not the letter
NUMBERS_MAX_DIFF = 0.1  # share of differing number tokens that still counts as "same numbers"
# month/quarter words (folded): template letters often name their period only in words. Each
# maps to its period, so "Sep" and "September" (a stamp on one copy) are the same month.
_MONTH_WORDS = [
    ("januar", "jaenner", "january", "jan"), ("februar", "february", "feb"),
    ("maerz", "mrz", "march", "mar"), ("april", "apr"), ("mai", "may"),
    ("juni", "june", "jun"), ("juli", "july", "jul"), ("august", "aug"),
    ("september", "sep", "sept"), ("oktober", "october", "okt", "oct"), ("november", "nov"),
    ("dezember", "december", "dez", "dec"),
]  # fmt: skip
PERIOD_WORDS = {w: f"m{i}" for i, words in enumerate(_MONTH_WORDS, 1) for w in words} | {
    "q1": "q1", "q2": "q2", "q3": "q3", "q4": "q4", "quartal": "quartal",
}  # fmt: skip
TEXT_NEAR_IDENTICAL = 0.98  # overrides weak structural conflicts
POOL_LIMIT = 300
MAX_TOKENS = 6000


@dataclass
class Profile:
    id: str
    title: str
    correspondent_id: int | None
    document_date: str | None
    tokens: set[str]
    idents: dict[str, str] = field(default_factory=dict)  # normalized key -> compact value
    amounts: dict[str, float] = field(default_factory=dict)
    recurring: set[str] = field(default_factory=set)  # ident values found on many documents

    @property
    def periods(self) -> set[str]:
        return {PERIOD_WORDS[t] for t in self.tokens if t in PERIOD_WORDS}

    @property
    def numbers(self) -> set[str]:
        """Number tokens of the text (dates, amounts, invoice numbers) - not words with a digit."""
        return {t for t in self.tokens if sum(c.isdigit() for c in t) * 2 >= len(t)}


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in TOKEN_RE.findall(fold(text)):
        if len(t) >= 2:
            out.add(t)
            if len(out) >= MAX_TOKENS:
                break
    return out


def _compact(v: str) -> str:
    return re.sub(r"[\s./-]", "", fold(v))


def profile(conn: sqlite3.Connection, doc_id: str) -> Profile | None:
    row = conn.execute(
        "SELECT id, title, correspondent_id, document_date FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    if row is None:
        return None
    text_row = conn.execute(
        "SELECT content FROM document_text WHERE doc_id=?", (doc_id,)
    ).fetchone()
    p = Profile(
        id=row["id"],
        title=row["title"],
        correspondent_id=row["correspondent_id"],
        document_date=row["document_date"],
        tokens=_tokens(text_row[0] if text_row else ""),
    )
    for r in conn.execute(
        "SELECT key, type, value_text, value_num FROM custom_field_values WHERE doc_id=?", (doc_id,)
    ):
        key = normalize_name(r["key"])
        if r["type"] == "string" and r["value_text"] and any(c.isdigit() for c in r["value_text"]):
            p.idents[key] = _compact(r["value_text"])
            n = conn.execute(
                "SELECT COUNT(DISTINCT doc_id) FROM custom_field_values WHERE type='string' AND "
                "replace(replace(replace(replace(lower(value_text),' ',''),'.',''),'/',''),'-','')=?",
                (p.idents[key],),
            ).fetchone()[0]
            if n > RECURRING:
                p.recurring.add(p.idents[key])
        elif r["type"] in ("monetary", "number") and r["value_num"] is not None:
            p.amounts[key] = round(float(r["value_num"]), 2)
    return p


def _pool(conn: sqlite3.Connection, p: Profile) -> set[str]:
    ids: set[str] = set()

    def add(sql: str, params: tuple) -> None:
        ids.update(r[0] for r in conn.execute(sql, params))

    if p.correspondent_id is not None:
        add(
            "SELECT id FROM documents WHERE correspondent_id=? AND id!=? "
            "ORDER BY received_at DESC LIMIT ?",
            (p.correspondent_id, p.id, POOL_LIMIT),
        )
    if p.document_date:
        add("SELECT id FROM documents WHERE document_date=? AND id!=? LIMIT ?",
            (p.document_date, p.id, POOL_LIMIT))  # fmt: skip
    for value in p.idents.values():
        add(
            "SELECT doc_id FROM custom_field_values WHERE type='string' AND doc_id!=? AND "
            "replace(replace(replace(replace(lower(value_text),' ',''),'.',''),'/',''),'-','')=? "
            "LIMIT ?",
            (p.id, value, POOL_LIMIT),
        )
    for value in p.amounts.values():
        add(
            "SELECT doc_id FROM custom_field_values WHERE value_num BETWEEN ? AND ? AND doc_id!=? "
            "LIMIT ?",
            (value - 0.005, value + 0.005, p.id, POOL_LIMIT),
        )
    # text: the rarest words of this document that also occur elsewhere
    rare: list[tuple[int, str]] = []
    for tok in p.tokens:
        if len(tok) < 4:
            continue
        row = conn.execute("SELECT doc FROM doc_vocab WHERE term=?", (tok,)).fetchone()
        if row and row[0] >= 2:
            rare.append((row[0], tok))
    rare.sort()
    words = [t for _, t in rare[:10]]
    if words:
        match = " OR ".join(f'body:"{w}"' for w in words)
        try:
            add(
                "SELECT d.id FROM doc_fts JOIN documents d ON d.rowid = doc_fts.rowid "
                "WHERE doc_fts MATCH ? AND d.id != ? ORDER BY bm25(doc_fts) LIMIT 30",
                (match, p.id),
            )
        except sqlite3.OperationalError:
            log.debug("duplicate text lookup failed", exc_info=True)
    return ids


# reasons and conflicts are stored in English (translated when shown)
_OTHER_NUMBERS = N_("different numbers in the text (date, amount, number)")


def compare(a: Profile, b: Profile) -> tuple[bool, float, list[str]]:
    """(is_candidate, score 0..1, human readable reasons)."""
    reasons: list[str] = []
    conflicts: list[str] = []
    union = a.tokens | b.tokens
    sim = len(a.tokens & b.tokens) / len(union) if union else 0.0
    if sim >= 0.9:
        reasons.append(N_("Text nearly identical"))
    elif sim >= TEXT_MIN:
        reasons.append(N_("Text very similar (%(percent)s)") % {"percent": f"{round(sim * 100)}%"})

    date_eq = bool(a.document_date and a.document_date == b.document_date)
    if a.document_date and b.document_date and not date_eq:
        conflicts.append(N_("different document date"))
    elif date_eq:
        reasons.append(N_("same document date"))

    corr_eq = a.correspondent_id is not None and a.correspondent_id == b.correspondent_id
    if a.correspondent_id and b.correspondent_id and not corr_eq:
        conflicts.append(N_("different sender"))
    elif corr_eq:
        reasons.append(N_("same sender"))

    # numbers in the text: template letters differ exactly there (date, amount, invoice no.)
    na, nb = a.numbers, b.numbers
    numbers_diff = len(na ^ nb)
    if na and nb and numbers_diff >= 3 and numbers_diff / len(na | nb) > NUMBERS_MAX_DIFF:
        conflicts.append(_OTHER_NUMBERS)

    # period in words ("Rechnung April 2022" vs "... Juni 2022"): another month, another letter
    pa, pb = a.periods - {"quartal"}, b.periods - {"quartal"}
    if pa and pb and pa != pb:
        conflicts.append(N_("different period in the text"))
        numbers_diff = max(numbers_diff, 2)  # never outweighed by near-identical text

    ident_eq = False
    recurring = a.recurring | b.recurring
    for key in a.idents.keys() & b.idents.keys():
        if a.idents[key] == b.idents[key]:
            if a.idents[key] not in recurring:
                ident_eq = True
                reasons.append(N_("same number (%(key)s)") % {"key": key})
        else:
            conflicts.append(N_("different number (%(key)s)") % {"key": key})
    shared_values = (set(a.idents.values()) & set(b.idents.values())) - recurring
    if shared_values and not ident_eq:
        ident_eq = True
        reasons.append(N_("same number"))

    amount_eq = False
    for key in a.amounts.keys() & b.amounts.keys():
        if abs(a.amounts[key] - b.amounts[key]) < 0.005:
            amount_eq = True
        else:
            conflicts.append(N_("different amount (%(key)s)") % {"key": key})
    if not amount_eq and set(a.amounts.values()) & set(b.amounts.values()):
        amount_eq = True
    if amount_eq:
        reasons.append(N_("same amount"))

    # same date, same (non-recurring) number and same amount: the same letter, even if the
    # text recognition misread a few other numbers (a phone photo against the PDF)
    if ident_eq and date_eq and amount_eq:
        conflicts = [c for c in conflicts if c != _OTHER_NUMBERS]
    # near-identical text outweighs structural differences (e.g. a misread date) - but only
    # when the numbers in the text agree as well (one deviation allowed for OCR misreads)
    if conflicts and (sim < TEXT_NEAR_IDENTICAL or numbers_diff > 1):
        return False, 0.0, conflicts
    candidate = sim >= TEXT_MIN or ident_eq or (date_eq and corr_eq and amount_eq)
    structural = sum([date_eq, corr_eq, ident_eq, amount_eq])
    score = round(min(1.0, 0.6 * sim + 0.12 * structural + (0.2 if ident_eq else 0)), 2)
    return candidate, score, reasons


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def check_document(archive: Archive, doc_id: str) -> list[dict[str, Any]]:
    """Look for duplicates of one document and record open candidates."""
    conn = archive.conn
    me = profile(conn, doc_id)
    if me is None:
        return []
    try:
        confirmed = set(docs.load_meta(archive, doc_id).not_duplicate_of)
    except docs.DocumentNotFound:
        return []
    found = []
    results = []
    for other_id in sorted(_pool(conn, me)):
        if other_id in confirmed:
            continue
        other = profile(conn, other_id)
        if other is None:
            continue
        results.append((other_id, *compare(me, other)))
    with write_tx(conn):  # one transaction per document, not per compared pair
        for other_id, is_dup, score, reasons in results:
            a, b = _pair(doc_id, other_id)
            if is_dup:
                conn.execute(
                    "INSERT INTO duplicate_candidates(doc_a, doc_b, score, reasons, status, "
                    "created_at) VALUES(?,?,?,?,'open',?) ON CONFLICT(doc_a, doc_b) DO UPDATE SET "
                    "score=excluded.score, reasons=excluded.reasons "
                    "WHERE duplicate_candidates.status='open'",
                    (a, b, score, json.dumps(reasons, ensure_ascii=False), now_iso()),
                )
                found.append({"other": other_id, "score": score, "reasons": reasons})
            else:
                # e.g. metadata was corrected meanwhile: no longer a candidate
                conn.execute(
                    "DELETE FROM duplicate_candidates WHERE doc_a=? AND doc_b=? AND status='open'",
                    (a, b),
                )
    return found


def scan_all(archive: Archive) -> int:
    ids = [r[0] for r in archive.conn.execute("SELECT id FROM documents ORDER BY ingest_sequence")]
    for doc_id in ids:
        check_document(archive, doc_id)
    return archive.conn.execute(
        "SELECT COUNT(*) FROM duplicate_candidates WHERE status='open'"
    ).fetchone()[0]


def keep_both(archive: Archive, a: str, b: str) -> None:
    """User decision: not duplicates. Stored in both sidecars so it survives rebuild/export."""
    conn = archive.conn
    with write_tx(conn):
        for x, y in ((a, b), (b, a)):
            meta = docs.load_meta(archive, x)
            if y not in meta.not_duplicate_of:
                meta.not_duplicate_of.append(y)
                docs.persist(archive, meta)
        pa, pb = _pair(a, b)
        conn.execute(
            "UPDATE duplicate_candidates SET status='kept_both', decided_at=? "
            "WHERE doc_a=? AND doc_b=?",
            (now_iso(), pa, pb),
        )


def forget_document(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("DELETE FROM duplicate_candidates WHERE doc_a=? OR doc_b=?", (doc_id, doc_id))


def open_pairs(conn: sqlite3.Connection, doc_id: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT c.doc_a, c.doc_b, c.score, c.reasons, c.created_at FROM duplicate_candidates c "
        "JOIN documents a ON a.id=c.doc_a JOIN documents b ON b.id=c.doc_b WHERE c.status='open'"
    )
    params: tuple = ()
    if doc_id:
        sql += " AND (c.doc_a=? OR c.doc_b=?)"
        params = (doc_id, doc_id)
    sql += " ORDER BY c.score DESC, c.created_at DESC"
    out = []
    for r in conn.execute(sql, params):
        out.append(
            {
                "a": _summary(conn, r["doc_a"]),
                "b": _summary(conn, r["doc_b"]),
                "score": r["score"],
                "reasons": json.loads(r["reasons"]),
                "created_at": r["created_at"],
            }
        )
    return out


def _summary(conn: sqlite3.Connection, doc_id: str) -> dict[str, Any]:
    r = conn.execute(
        "SELECT id, title, original_filename, document_date, received_at, source, mime_type, "
        "page_count, size_bytes, filing_section, filing_sequence, paper, text_status "
        "FROM documents WHERE id=?",
        (doc_id,),
    ).fetchone()
    return dict(r) if r else {"id": doc_id}


# --- automatic resolution of truly identical documents -------------------------------------


def _holds_user_data(meta) -> bool:
    """Anything the user did with this copy: notes, attachments, where the paper is, edits
    (locked fields, tag decisions, accepted suggestions), the keep-the-paper decision."""
    return bool(
        meta.notes or meta.attachments or meta.filing_sequence is not None
        or meta.paper_location or meta.paper_discarded_at or any(meta.field_locks.values())
        or meta.tag_overrides.added or meta.tag_overrides.removed
        or "user" in meta.field_sources.values() or meta.keep_original_source == "user"
        or any(h.by == "user" for h in meta.processing_history)
    )  # fmt: skip


def identical(archive: Archive, a, b) -> bool:
    """Same page count, word-for-word the same text and every page visually the same."""
    from .pagediff import compare_documents

    if (a.page_count or 1) != (b.page_count or 1):
        return False
    ta = TOKEN_RE.findall(fold(docs.get_text(archive, a.id)))
    tb = TOKEN_RE.findall(fold(docs.get_text(archive, b.id)))
    # enough real text that matches word for word: without text (failed OCR, photos of
    # receipts) the coarse page comparison alone must not decide
    if ta != tb or len(ta) < IDENTICAL_MIN_WORDS:
        return False
    diff = compare_documents(archive, a, b)
    return bool(diff["identical"] and diff["comparable"])


def resolve_identical(archive: Archive, doc_id: str | None = None) -> list[dict[str, str]]:
    """Move the second copy of truly identical documents to the Papierkorb (restorable).

    Kept is the copy with the user's data (notes, attachments, filing, corrections), else the
    older one; if both carry user data nothing happens automatically.
    """
    from datetime import date

    from . import trash

    if not archive.settings.auto_resolve_identical:
        return []
    done: list[dict[str, str]] = []
    for pair in open_pairs(archive.conn, doc_id):
        try:
            a = docs.load_meta(archive, pair["a"]["id"])
            b = docs.load_meta(archive, pair["b"]["id"])
        except docs.DocumentNotFound:
            continue
        if a.status in ("queued", "processing") or b.status in ("queued", "processing"):
            continue
        ua, ub = _holds_user_data(a), _holds_user_data(b)
        if ua and ub:
            continue
        if not identical(archive, a, b):
            continue
        keep, drop = (
            (a, b) if (ua or (not ub and a.ingest_sequence <= b.ingest_sequence)) else (b, a)
        )
        with write_tx(archive.conn):
            # the page comparison takes a while: the user may have edited either copy
            try:
                now_keep, now_drop = (
                    docs.load_meta(archive, keep.id),
                    docs.load_meta(archive, drop.id),
                )
            except docs.DocumentNotFound:
                continue
            if (now_keep.revision, now_drop.revision) != (keep.revision, drop.revision):
                continue
            trash.trash_document(
                archive, drop.id,
                reason=N_("identical to “%(title)s”") % {"title": keep.title or keep.original_filename},
                batch=f"auto-{date.today().isoformat()}", by="web",
            )  # fmt: skip
        done.append({"kept": keep.id, "trashed": drop.id})
        log.info("identical duplicate %s moved to the trash (kept %s)", drop.id, keep.id)
    return done
