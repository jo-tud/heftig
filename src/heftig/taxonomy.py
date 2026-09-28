"""Correspondents, document types and tags with aliases.

Names are compared in normalised form (:func:`heftig.textnorm.normalize_name`), so
``"Telekom Deutschland GmbH"``/``"TELEKOM DEUTSCHLAND GMBH"`` are the same term. Aliases map
alternative spellings onto one canonical term. The complete taxonomy is mirrored into
``taxonomy.json`` so it survives a database rebuild and is part of every export.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import now_iso, write_tx
from .i18n import _
from .models import TaxonomyFile, TaxonomyTerm
from .storage import ArchivePaths, atomic_write_json, read_json
from .textnorm import clean_display_name, levenshtein, normalize_name

KINDS = ("correspondent", "document_type", "tag")

# Legal-form suffixes ignored when looking for near-duplicate correspondents.
_LEGAL_SUFFIXES = {
    "gmbh", "ag", "kg", "ohg", "ug", "se", "ev", "e", "v", "co", "mbh", "inc", "ltd", "llc",
    "haftungsbeschraenkt", "deutschland", "germany",
}  # fmt: skip


class TaxonomyError(ValueError):
    pass


@dataclass
class Term:
    id: int
    kind: str
    name: str
    norm: str
    origin: str
    aliases: list[str]
    doc_count: int = 0


def _core(norm: str) -> str:
    return " ".join(t for t in norm.split() if t not in _LEGAL_SUFFIXES) or norm


def find_term(conn: sqlite3.Connection, kind: str, name: str) -> int | None:
    norm = normalize_name(name)
    if not norm:
        return None
    row = conn.execute("SELECT id FROM taxonomy WHERE kind=? AND norm=?", (kind, norm)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT term_id FROM taxonomy_alias WHERE kind=? AND alias_norm=?", (kind, norm)
    ).fetchone()
    return row[0] if row else None


def get_or_create(conn: sqlite3.Connection, kind: str, name: str, origin: str = "user") -> int:
    if kind not in KINDS:
        raise TaxonomyError(kind)
    display = clean_display_name(name)
    if not normalize_name(display):
        raise TaxonomyError(_("Empty name"))
    existing = find_term(conn, kind, display)
    if existing is not None:
        return existing
    cur = conn.execute(
        "INSERT INTO taxonomy(kind, name, norm, created_at, origin) VALUES(?,?,?,?,?)",
        (kind, display[:200], normalize_name(display), now_iso(), origin),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


def canonical_name(conn: sqlite3.Connection, kind: str, name: str) -> str | None:
    tid = find_term(conn, kind, name)
    if tid is None:
        return None
    return conn.execute("SELECT name FROM taxonomy WHERE id=?", (tid,)).fetchone()[0]


def term_name(conn: sqlite3.Connection, term_id: int | None) -> str | None:
    if term_id is None:
        return None
    row = conn.execute("SELECT name FROM taxonomy WHERE id=?", (term_id,)).fetchone()
    return row[0] if row else None


def aliases_for(conn: sqlite3.Connection, term_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT alias FROM taxonomy_alias WHERE term_id=? ORDER BY alias", (term_id,)
    ).fetchall()
    return [r[0] for r in rows]


def list_terms(conn: sqlite3.Connection, kind: str | None = None) -> list[Term]:
    sql = """
        SELECT t.id, t.kind, t.name, t.norm, t.origin,
          CASE t.kind
            WHEN 'correspondent' THEN (SELECT COUNT(*) FROM documents d
                                       WHERE d.correspondent_id = t.id)
            WHEN 'document_type' THEN (SELECT COUNT(*) FROM documents d
                                       WHERE d.document_type_id = t.id)
            ELSE (SELECT COUNT(*) FROM document_tags dt WHERE dt.tag_id = t.id)
          END AS doc_count
        FROM taxonomy t
    """
    params: tuple = ()
    if kind:
        sql += " WHERE t.kind = ?"
        params = (kind,)
    sql += " ORDER BY t.kind, t.norm"
    out = []
    for r in conn.execute(sql, params).fetchall():
        out.append(
            Term(
                id=r["id"],
                kind=r["kind"],
                name=r["name"],
                norm=r["norm"],
                origin=r["origin"],
                aliases=aliases_for(conn, r["id"]),
                doc_count=r["doc_count"],
            )
        )
    return out


def similar_terms(conn: sqlite3.Connection, kind: str, name: str, limit: int = 3) -> list[str]:
    """Near-duplicates of a proposed new term (legal-form-insensitive, small edit distance)."""
    norm = normalize_name(name)
    core = _core(norm)
    hits: list[tuple[int, str]] = []
    rows = conn.execute("SELECT name, norm FROM taxonomy WHERE kind=?", (kind,)).fetchall()
    for r in rows:
        other_core = _core(r["norm"])
        if other_core == core:
            hits.append((0, r["name"]))
            continue
        if core and (other_core.startswith(core + " ") or core.startswith(other_core + " ")):
            hits.append((1, r["name"]))
            continue
        max_d = 1 if len(core) <= 6 else 2
        d = levenshtein(core, other_core, max_d)
        if d <= max_d:
            hits.append((1 + d, r["name"]))
    return [n for _, n in sorted(hits)[:limit]]


def add_alias(conn: sqlite3.Connection, term_id: int, alias: str) -> None:
    row = conn.execute("SELECT kind, norm FROM taxonomy WHERE id=?", (term_id,)).fetchone()
    if not row:
        raise TaxonomyError(_("Unknown entry"))
    alias = clean_display_name(alias)
    norm = normalize_name(alias)
    if not norm or norm == row["norm"]:
        return
    clash = conn.execute(
        "SELECT id FROM taxonomy WHERE kind=? AND norm=?", (row["kind"], norm)
    ).fetchone()
    if clash:
        raise TaxonomyError(
            _("“%(alias)s” is already an entry of its own – please merge instead.", alias=alias)
        )
    conn.execute(
        "INSERT INTO taxonomy_alias(kind, alias_norm, alias, term_id) VALUES(?,?,?,?) "
        "ON CONFLICT(kind, alias_norm) DO UPDATE SET term_id=excluded.term_id, alias=excluded.alias",
        (row["kind"], norm, alias, term_id),
    )


def remove_alias(conn: sqlite3.Connection, term_id: int, alias: str) -> None:
    conn.execute(
        "DELETE FROM taxonomy_alias WHERE term_id=? AND alias_norm=?",
        (term_id, normalize_name(alias)),
    )


def affected_documents(conn: sqlite3.Connection, term_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT id FROM documents WHERE correspondent_id=?1 OR document_type_id=?1
           UNION SELECT doc_id FROM document_tags WHERE tag_id=?1""",
        (term_id,),
    ).fetchall()
    return sorted(r[0] for r in rows)


def export_file(conn: sqlite3.Connection) -> TaxonomyFile:
    rows = conn.execute(
        "SELECT id, kind, name, origin, created_at FROM taxonomy ORDER BY kind, norm"
    ).fetchall()
    return TaxonomyFile(
        terms=[
            TaxonomyTerm(
                kind=r["kind"],
                name=r["name"],
                aliases=aliases_for(conn, r["id"]),
                origin=r["origin"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


def write_sidecar(conn: sqlite3.Connection, paths: ArchivePaths) -> None:
    atomic_write_json(paths.taxonomy, export_file(conn).model_dump())


def load_file(conn: sqlite3.Connection, data: TaxonomyFile) -> dict[str, int]:
    """Merge terms and aliases from a taxonomy file. Returns counters."""
    stats = {"terms_created": 0, "aliases_added": 0, "alias_conflicts": 0}
    with write_tx(conn):
        for t in data.terms:
            if find_term(conn, t.kind, t.name) is None:
                conn.execute(
                    "INSERT INTO taxonomy(kind, name, norm, created_at, origin) VALUES(?,?,?,?,?)",
                    (t.kind, t.name, normalize_name(t.name), t.created_at, t.origin),
                )
                stats["terms_created"] += 1
        for t in data.terms:
            tid = find_term(conn, t.kind, t.name)
            for a in t.aliases:
                existing = find_term(conn, t.kind, a)
                if existing == tid:
                    continue
                if existing is not None:
                    stats["alias_conflicts"] += 1
                    continue
                add_alias(conn, tid, a)  # type: ignore[arg-type]
                stats["aliases_added"] += 1
    return stats


def read_sidecar(paths: ArchivePaths) -> TaxonomyFile | None:
    if not paths.taxonomy.exists():
        return None
    return TaxonomyFile.model_validate(read_json(paths.taxonomy))
