"""Saved searches: a name plus the query string of the search page.

Stored in ``saved_searches.json`` at the archive root (not in SQLite), so they survive a
database rebuild and travel with backups and exports like ``taxonomy.json``.
"""

from __future__ import annotations

import secrets
import threading
from typing import Any
from urllib.parse import parse_qsl, urlencode

from .db import now_iso
from .i18n import _
from .storage import ArchivePaths, atomic_write_json, read_json

FILENAME = "saved_searches.json"
MAX_ENTRIES = 200
# query parameters of the search page that a saved search may contain
KEYS = (
    "q", "correspondent", "document_type", "tag", "tag_mode", "date_from", "date_to",
    "received_from", "received_to", "source", "status", "filed", "filing_section", "cf_key",
    "cf_min", "cf_max", "sort", "literal", "session",
)  # fmt: skip
_LOCK = threading.Lock()


class SavedSearchError(ValueError):
    pass


def clean_query(query: str) -> str:
    """Keep only known search parameters (drops paging and anything else)."""
    pairs = [(k, v[:500]) for k, v in parse_qsl(query.lstrip("?"), keep_blank_values=False)]
    return urlencode([(k, v) for k, v in pairs if k in KEYS and v][:50])


def _path(paths: ArchivePaths):
    return paths.root / FILENAME


def load(paths: ArchivePaths) -> list[dict[str, Any]]:
    p = _path(paths)
    if not p.exists():
        return []
    try:
        data = read_json(p)
    except (OSError, ValueError):
        return []
    out = []
    for e in data.get("searches", []) if isinstance(data, dict) else []:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("name"), str):
            out.append({
                "id": e["id"], "name": e["name"][:80], "query": clean_query(str(e.get("query", ""))),
                "created_at": str(e.get("created_at", "")),
            })  # fmt: skip
    return out


def _write(paths: ArchivePaths, entries: list[dict[str, Any]]) -> None:
    atomic_write_json(_path(paths), {"version": 1, "searches": entries})


def add(paths: ArchivePaths, name: str, query: str) -> dict[str, Any]:
    name = " ".join((name or "").split())[:80]
    query = clean_query(query)
    if not name:
        raise SavedSearchError(_("Please enter a name."))
    if not query:
        raise SavedSearchError(_("Empty search – nothing to save."))
    with _LOCK:
        entries = load(paths)
        for e in entries:
            if e["query"] == query:  # same search again: just rename
                e["name"] = name
                _write(paths, entries)
                return e
        if len(entries) >= MAX_ENTRIES:
            raise SavedSearchError(_("At most %(num)s saved searches.", num=MAX_ENTRIES))
        entry = {"id": secrets.token_hex(8), "name": name, "query": query, "created_at": now_iso()}
        entries.append(entry)
        _write(paths, entries)
        return entry


def delete(paths: ArchivePaths, search_id: str) -> bool:
    with _LOCK:
        entries = load(paths)
        keep = [e for e in entries if e["id"] != search_id]
        if len(keep) == len(entries):
            return False
        _write(paths, keep)
        return True


def merge(paths: ArchivePaths, incoming: list[dict[str, Any]]) -> int:
    """Add entries from an import that are not present yet (by query). Returns the count."""
    with _LOCK:
        entries = load(paths)
        known = {e["query"] for e in entries}
        added = 0
        for e in incoming:
            q = clean_query(str(e.get("query", "")))
            name = " ".join(str(e.get("name", "")).split())[:80]
            if q and name and q not in known and len(entries) < MAX_ENTRIES:
                entries.append({"id": secrets.token_hex(8), "name": name, "query": q,
                                "created_at": str(e.get("created_at") or now_iso())})  # fmt: skip
                known.add(q)
                added += 1
        if added:
            _write(paths, entries)
        return added
