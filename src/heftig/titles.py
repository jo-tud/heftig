"""Consistent document titles.

Three parts:

- :func:`normalize_title` - deterministic clean-up of every AI title (period notation, spacing):
  "Kontoabrechnung 3. Quartal 2020" -> "Kontoabrechnung Q3 2020", "2023 Steuerbescheid" ->
  "Steuerbescheid 2023".
- the classification prompt gets a naming scheme and the titles of the most similar documents
  as examples (:func:`title_examples`, used by :mod:`heftig.classify`).
- a one-time "harmonise" job: per group of correspondent + document type, only the titles, dates
  and the two names go to the configured AI, which returns consistent titles. They are stored as
  proposals (``title_proposals``) and applied only when the user accepts them.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from . import documents as docs
from . import jobs
from .archive import Archive
from .db import now_iso, write_tx
from .i18n import _
from .providers.base import ProviderError
from .textnorm import clean_display_name, normalize_name

log = logging.getLogger("heftig.titles")

MAX_TITLE = 200
BATCH_DOCS = 80  # titles per AI request
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_YEAR = r"((?:19|20)\d\d)"

_QUARTER_PATTERNS = [
    # "3. Quartal 2020", "3.Quartal/2020", "III. Quartal 2020", "drittes Quartal 2020"
    re.compile(rf"\b([1-4]|I{{1,3}}|IV)\s*\.?\s*Quartal\s*[/ ,-]?\s*{_YEAR}\b", re.I),
    # "Quartal 3/2020", "Quartal 3 2020"
    re.compile(rf"\bQuartal\s+([1-4])\s*[/ .-]\s*{_YEAR}\b", re.I),
    # "Q3/2020", "Q3-2020", "Q3.2020", "Q3 2020"
    re.compile(rf"\bQ([1-4])\s*[/.-]?\s*{_YEAR}\b", re.I),
    # "2020 Q3", "2020/Q3"
    re.compile(rf"\b{_YEAR}\s*[/ -]\s*Q([1-4])\b", re.I),
]


def _quarter(m: re.Match[str], year_first: bool) -> str:
    q, y = (m.group(2), m.group(1)) if year_first else (m.group(1), m.group(2))
    q = str(_ROMAN.get(q.lower(), q))
    return f"Q{q} {y}"


def normalize_title(title: str | None) -> str | None:
    """Deterministic clean-up of a title; None/empty stays None."""
    if not title:
        return None
    t = clean_display_name(title)
    for i, rx in enumerate(_QUARTER_PATTERNS):
        t = rx.sub(lambda m, yf=(i == 3): _quarter(m, yf), t)
    # a leading year belongs at the end: "2023 Steuerbescheid" -> "Steuerbescheid 2023"
    m = re.fullmatch(rf"{_YEAR}\s*[-–:]?\s+(.+)", t)
    if m and not re.search(rf"\b{_YEAR}\b", m.group(2)):
        t = f"{m.group(2)} {m.group(1)}"
    t = re.sub(r"\s+", " ", t).strip(" -–,;:")
    return t[:MAX_TITLE] or None


# --- examples for the classifier -----------------------------------------------------------


def title_examples(conn: sqlite3.Connection, doc_id: str, limit: int = 8) -> list[dict[str, Any]]:
    """Titles of the most similar documents that already have a real (AI/user) title."""
    from .search import similar

    out = []
    for it in similar(conn, doc_id, limit=limit * 2):
        row = conn.execute(
            "SELECT json_extract(metadata_json, '$.field_sources.title') FROM documents WHERE id=?",
            (it["id"],),
        ).fetchone()
        if not row or row[0] not in ("ai", "user", "import"):
            continue
        out.append({
            "title": it["title"], "correspondent": it.get("correspondent"),
            "document_type": it.get("document_type"), "document_date": it.get("document_date"),
        })  # fmt: skip
        if len(out) >= limit:
            break
    return out


# --- harmonising existing titles -------------------------------------------------------------

_NOT_A_NAME = {
    "gmbh", "ag", "kg", "ohg", "ug", "se", "ev", "mbh", "inc", "ltd", "llc", "co", "und", "der",
    "die", "das", "the", "and", "of", "fuer", "for", "haftungsbeschraenkt", "deutschland",
}  # fmt: skip


def sender_words(conn: sqlite3.Connection, correspondent_id: int | None) -> set[str]:
    """Words that name the sender in a title: from its name and aliases ("TK", "atpar")."""
    if correspondent_id is None:
        return set()
    from . import taxonomy as tax

    names = [tax.term_name(conn, correspondent_id), *tax.aliases_for(conn, correspondent_id)]
    return {w for n in names if n for w in normalize_name(n).split()
            if w not in _NOT_A_NAME and len(w) >= 2}  # fmt: skip


def drops_sender(old: str, new: str, words: set[str]) -> bool:
    """The proposal loses the sender the current title names (the user wants to keep it)."""
    return bool(words & set(normalize_name(old).split())) and not (
        words & set(normalize_name(new).split())
    )



def _groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, title, document_date, correspondent_id, document_type_id, metadata_json
          FROM documents
         WHERE status IN ('done', 'needs_review') AND title != ''
           AND (correspondent_id IS NOT NULL OR document_type_id IS NOT NULL)
         ORDER BY correspondent_id, document_type_id, document_date, ingest_sequence
        """
    ).fetchall()
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM taxonomy")}
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        meta = json.loads(r["metadata_json"])
        key = (r["correspondent_id"], r["document_type_id"])
        g = groups.setdefault(key, {
            "correspondent": names.get(r["correspondent_id"]),
            "document_type": names.get(r["document_type_id"]), "documents": [],
            "sender_words": sender_words(conn, r["correspondent_id"]),
        })  # fmt: skip
        g["documents"].append({
            "id": r["id"], "title": r["title"], "date": r["document_date"],
            "locked": bool((meta.get("field_locks") or {}).get("title")),
        })  # fmt: skip
    return list(groups.values())


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM title_proposals").fetchone()[0]


def estimate(conn: sqlite3.Connection, model: str | None = None) -> dict[str, Any]:
    groups = _groups(conn)
    n = sum(len(g["documents"]) for g in groups)
    chars = sum(len(d["title"]) + 40 for g in groups for d in g["documents"])
    requests = max(1, -(-n // BATCH_DOCS)) if n else 0
    # rough token count: ~3.5 characters per token, prompt ~900 tokens per request
    tokens_in = chars / 3.5 + 900 * requests
    tokens_out = chars / 3.5 * 1.1
    from .providers.pricing import price as list_price

    price = list_price(model)
    usd = tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1] if price else None
    return {"documents": n, "groups": len(groups), "requests": requests,
            "tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "usd": usd}  # fmt: skip


def _batches(groups: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    size = 0
    for g in groups:
        n = len(g["documents"])
        if cur and size + n > BATCH_DOCS:
            out.append(cur)
            cur, size = [], 0
        cur.append(g)
        size += n
    if cur:
        out.append(cur)
    return out


def generate(archive: Archive, progress=None) -> dict[str, Any]:
    """Ask the AI for consistent titles and store them as proposals. Returns a report."""
    from .providers import registry
    from .providers.prompt import HARMONIZE_SCHEMA, harmonize_system, harmonize_user_message

    classifier = registry.get_classifier(archive.settings)
    if classifier is None or not hasattr(classifier, "complete_json"):
        raise ValueError(_("This needs an AI provider for the classification."))
    conn = archive.conn
    groups = _groups(conn)
    report: dict[str, Any] = {"documents": 0, "proposals": 0, "requests": 0, "skipped": 0,
                              "errors": []}  # fmt: skip
    batches = _batches(groups)
    with write_tx(conn):
        conn.execute("DELETE FROM title_proposals")
    for bi, batch in enumerate(batches):
        # short ids keep the request small and the answer easy to check
        ids: dict[str, dict[str, Any]] = {}
        payload = []
        for gi, g in enumerate(batch):
            docs_out = []
            for di, d in enumerate(g["documents"]):
                sid = f"{gi}.{di}"
                ids[sid] = d
                docs_out.append({"id": sid, "title": d["title"], "date": d["date"],
                                 "fixed": d["locked"]})  # fmt: skip
            payload.append({"correspondent": g["correspondent"],
                            "document_type": g["document_type"], "documents": docs_out})  # fmt: skip
        try:
            data = classifier.complete_json(
                harmonize_system(archive.settings.language),
                harmonize_user_message(payload),
                HARMONIZE_SCHEMA,
                8000,
            )
        except ProviderError as e:
            # one failed batch (e.g. a slow local model timing out) does not end the run
            if e.rate_limited or len(report["errors"]) >= 3:
                raise
            report["errors"].append(str(e))
            continue
        finally:
            report["requests"] += 1
        label_of = {}
        for gi, g in enumerate(batch):
            label_of[gi] = " · ".join(x for x in (g["correspondent"], g["document_type"]) if x)
        rows = []
        for item in data.get("titles", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            d = ids.get(str(item.get("id", "")))
            new = normalize_title(item.get("title") if isinstance(item.get("title"), str) else "")
            gi = int(str(item["id"]).split(".")[0]) if d is not None else 0
            if d is None or d["locked"] or not new or drops_sender(
                d["title"], new, batch[gi]["sender_words"]
            ):
                report["skipped"] += 1
                continue
            report["documents"] += 1
            if normalize_name(new) == normalize_name(d["title"]) and new == d["title"]:
                continue
            gi = int(str(item["id"]).split(".")[0])
            rows.append((d["id"], d["title"], new, label_of[gi], now_iso()))
        with write_tx(conn):
            conn.executemany(
                "INSERT OR REPLACE INTO title_proposals(doc_id, old_title, new_title, group_label, "
                "created_at) VALUES(?,?,?,?,?)",
                rows,
            )
        report["proposals"] += len(rows)
        if progress:
            progress((bi + 1) / len(batches))
    return report


def proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open proposals grouped by label, groups sorted by name. Proposals that would drop the
    sender from a title (made before that rule) are not shown."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    words: dict[int | None, set[str]] = {}
    for r in conn.execute(
        "SELECT p.*, d.document_date, d.correspondent_id FROM title_proposals p "
        "JOIN documents d ON d.id = p.doc_id ORDER BY p.group_label, d.document_date, p.doc_id"
    ):
        cid = r["correspondent_id"]
        if cid not in words:
            words[cid] = sender_words(conn, cid)
        if drops_sender(r["old_title"], r["new_title"], words[cid]):
            continue
        grouped.setdefault(r["group_label"], []).append(dict(r))
    return [{"label": k, "items": v} for k, v in sorted(grouped.items())]


def accept(archive: Archive, doc_ids: list[str]) -> dict[str, int]:
    """Apply the chosen proposals (as user edits: the title is then locked)."""
    done = stale = 0
    for doc_id in doc_ids:
        row = archive.conn.execute(
            "SELECT old_title, new_title FROM title_proposals WHERE doc_id=?", (doc_id,)
        ).fetchone()
        if row is None:
            continue
        try:
            meta = docs.load_meta(archive, doc_id)
        except docs.DocumentNotFound:
            meta = None
        if meta is None or meta.title != row["old_title"] or meta.locked("title"):
            stale += 1  # changed in the meantime: never overwrite a newer title
        else:
            docs.update_fields(archive, doc_id, {"title": row["new_title"]})
            done += 1
        with write_tx(archive.conn):
            archive.conn.execute("DELETE FROM title_proposals WHERE doc_id=?", (doc_id,))
    return {"accepted": done, "stale": stale}


def dismiss(archive: Archive, doc_ids: list[str] | None = None) -> int:
    with write_tx(archive.conn):
        if doc_ids is None:
            return archive.conn.execute("DELETE FROM title_proposals").rowcount
        return sum(
            archive.conn.execute("DELETE FROM title_proposals WHERE doc_id=?", (d,)).rowcount
            for d in doc_ids
        )


def enqueue_job(archive: Archive) -> int:
    return jobs.enqueue(archive.conn, "titles", max_attempts=2)


def job_handler(archive: Archive, job) -> str:
    def progress(frac: float) -> None:
        jobs.set_stage(archive.conn, job["id"], "titles", frac, archive.settings.job_lease_seconds)

    report = generate(archive, progress=progress)
    jobs.finish(archive.conn, job["id"], "done", result=report)
    return "done"
