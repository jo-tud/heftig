"""Maintenance of the FTS5 search index (one row per document, rowid = documents.rowid)."""

from __future__ import annotations

import json
import sqlite3

from .taxonomy import aliases_for
from .textnorm import TOKEN_RE, fold, identifiers, index_text

# BM25 column weights, in column order of doc_fts (doc_id is unindexed -> 0).
# Documented in docs/search.md. Higher = more important.
COLUMN_WEIGHTS = {
    "doc_id": 0.0,
    "ident": 12.0,  # IDs, hashes, contract/invoice numbers (separator-free)
    "title": 10.0,
    "correspondent": 8.0,  # incl. aliases
    "doctype": 6.0,
    "tags": 6.0,
    "custom": 7.0,  # custom field values (e.g. contract number)
    "filename": 3.0,
    "dates": 4.0,  # document date / received date as year, year-month, date
    "summary": 2.5,
    "body": 1.0,
    "notes": 5.0,  # the user's own notes, attachment names and descriptions
}
FTS_COLUMNS = list(COLUMN_WEIGHTS)


def _dates(meta: dict) -> str:
    out = []
    for key in ("document_date", "received_at", "filed_at"):
        v = meta.get(key)
        if v:
            out += [v[:4], v[:7], v[:10]]
    return " ".join(dict.fromkeys(out))


def build_row(conn: sqlite3.Connection, doc_row: sqlite3.Row, text: str) -> dict[str, str]:
    meta = json.loads(doc_row["metadata_json"])
    corr = meta.get("correspondent") or ""
    if doc_row["correspondent_id"]:
        corr = " ".join([corr, *aliases_for(conn, doc_row["correspondent_id"])])
    dtype = meta.get("document_type") or ""
    if doc_row["document_type_id"]:
        dtype = " ".join([dtype, *aliases_for(conn, doc_row["document_type_id"])])
    tags = " ".join(meta.get("tags") or [])
    custom_parts = []
    for key, cf in (meta.get("custom_fields") or {}).items():
        val = cf.get("value")
        if val is not None:
            custom_parts.append(f"{key} {val}")
    custom = " ".join(custom_parts)
    # "ident": numbers from structured metadata (custom fields, title, filename). Document IDs
    # and hashes are not indexed (their hex fragments would match words such as "Bad" or
    # "Café"); the search looks them up directly. Numbers that only occur in the body text
    # stay in the (low-weight) body column, with separator-free variants ("8372 9381" ->
    # "83729381") appended there.
    ident_src = " ".join([custom, meta.get("title") or "", meta["original_filename"]])
    ident_tokens: set[str] = set()
    ident_tokens.update(t for t in TOKEN_RE.findall(fold(ident_src)) if any(c.isdigit() for c in t))
    ident_tokens.update(identifiers(ident_src))
    body_extra = " ".join(identifiers(text))
    return {
        "doc_id": meta["id"],
        "ident": " ".join(sorted(ident_tokens)),
        "title": fold(meta.get("title") or ""),
        "correspondent": fold(corr),
        "doctype": fold(dtype),
        "tags": fold(tags),
        "custom": fold(custom),
        "filename": fold(meta["original_filename"]),
        "dates": _dates(meta),
        "summary": fold(meta.get("summary") or ""),
        "body": index_text(text) + ("\n" + body_extra if body_extra else ""),
        "notes": fold(
            " ".join(
                [n.get("text", "") for n in meta.get("notes") or []]
                + [
                    f"{a.get('filename', '')} {a.get('description', '')}"
                    for a in meta.get("attachments") or []
                ]
            )
        ),
    }


def index_document(conn: sqlite3.Connection, doc_id: str) -> None:
    row = conn.execute("SELECT rowid, * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        return
    trow = conn.execute("SELECT content FROM document_text WHERE doc_id=?", (doc_id,)).fetchone()
    data = build_row(conn, row, trow[0] if trow else "")
    conn.execute("DELETE FROM doc_fts WHERE rowid=?", (row["rowid"],))
    cols = ", ".join(FTS_COLUMNS)
    marks = ", ".join("?" for _ in FTS_COLUMNS)
    conn.execute(
        f"INSERT INTO doc_fts(rowid, {cols}) VALUES(?, {marks})",
        (row["rowid"], *[data[c] for c in FTS_COLUMNS]),
    )


def remove_document(conn: sqlite3.Connection, rowid: int) -> None:
    conn.execute("DELETE FROM doc_fts WHERE rowid=?", (rowid,))


def rebuild(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM doc_fts")
    ids = [r[0] for r in conn.execute("SELECT id FROM documents ORDER BY ingest_sequence")]
    for doc_id in ids:
        index_document(conn, doc_id)
    conn.execute("INSERT INTO doc_fts(doc_fts) VALUES('optimize')")
    return len(ids)
