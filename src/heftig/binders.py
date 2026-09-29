"""Binders: the physical folders Heftig's paper filing goes into.

There is always one *current* binder. Paper marked as filed goes on top of the current month's
section in it; when the binder is full, the next one is started and everything after that goes
there. The positions in the full binder stay as they are. A binder's name is what is written on
its spine ("Heftig 1"), so the archive can say "binder Heftig 1, section 2026-09, 3rd from the
top".

Stored in ``binders.json`` at the archive root (like ``saved_searches.json``): it survives a
database rebuild and travels with backups and exports. Each filed document also carries the
binder's name in its sidecar (``filing_binder``), so the sidecars alone say where the paper is.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from . import i18n
from .db import now_iso, write_tx
from .storage import ArchivePaths, atomic_write_json, read_json

FILENAME = "binders.json"
_LOCK = threading.Lock()


class BinderError(ValueError):
    pass


def _path(paths: ArchivePaths):
    return paths.root / FILENAME


def load(paths: ArchivePaths) -> list[dict[str, Any]]:
    """All binders, oldest first: {name, started_at, full_at}."""
    p = _path(paths)
    if not p.exists():
        return []
    try:
        data = read_json(p)
    except (OSError, ValueError):
        return []
    out = []
    for b in data.get("binders", []) if isinstance(data, dict) else []:
        if isinstance(b, dict) and isinstance(b.get("name"), str) and b["name"].strip():
            out.append({"name": b["name"].strip()[:60], "started_at": str(b.get("started_at") or ""),
                        "full_at": b.get("full_at") or None})  # fmt: skip
    return out


def _write(paths: ArchivePaths, binders: list[dict[str, Any]]) -> None:
    atomic_write_json(_path(paths), {"version": 1, "binders": binders})


def default_name(language: str, number: int = 1) -> str:
    with i18n.language(language):
        return i18n._("Binder %(num)s", num=number)


def current(archive) -> str:
    """The name of the binder new filings go into (created on first use)."""
    with _LOCK:
        binders = load(archive.paths)
        open_ = [b for b in binders if not b["full_at"]]
        if open_:
            return open_[-1]["name"]
        name = next_name(binders, archive.settings.language)
        binders.append({"name": name, "started_at": now_iso(), "full_at": None})
        _write(archive.paths, binders)
        return name


def next_name(binders: list[dict[str, Any]], language: str) -> str:
    """ "Heftig 1" -> "Heftig 2"; without a number at the end: "<name> 2"."""
    if not binders:
        return default_name(language)
    last = binders[-1]["name"]
    m = re.match(r"^(.*?)(\d+)\s*$", last)
    base, n = (m.group(1), int(m.group(2)) + 1) if m else (last.rstrip() + " ", 2)
    names = {b["name"].casefold() for b in binders}
    while f"{base}{n}".casefold() in names:
        n += 1
    return f"{base}{n}"


def _clean(name: str) -> str:
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise BinderError(i18n._("Please enter a name for the binder."))
    return name


def start_next(archive, name: str | None = None) -> str:
    """The current binder is full: close it and start the next one."""
    with _LOCK:
        binders = load(archive.paths)
        new = _clean(name) if name else next_name(binders, archive.settings.language)
        if any(b["name"].casefold() == new.casefold() for b in binders):
            raise BinderError(i18n._("There already is a binder named “%(name)s”.", name=new))
        stamp = now_iso()
        for b in binders:
            if not b["full_at"]:
                b["full_at"] = stamp
        binders.append({"name": new, "started_at": stamp, "full_at": None})
        _write(archive.paths, binders)
        return new


def rename(archive, old: str, new: str) -> int:
    """Rename a binder (e.g. to what is written on it); updates its documents."""
    from . import documents as docs

    new = _clean(new)
    with _LOCK:
        binders = load(archive.paths)
        if not any(b["name"] == old for b in binders):
            raise BinderError(i18n._("Unknown binder."))
        if new != old and any(b["name"].casefold() == new.casefold() for b in binders):
            raise BinderError(i18n._("There already is a binder named “%(name)s”.", name=new))
        for b in binders:
            if b["name"] == old:
                b["name"] = new
        _write(archive.paths, binders)
    ids = [r[0] for r in archive.conn.execute(
        "SELECT id FROM documents WHERE filing_binder=?", (old,))]  # fmt: skip
    for doc_id in ids:
        with write_tx(archive.conn):
            meta = docs.load_meta(archive, doc_id)
            meta.filing_binder = new
            docs.persist(archive, meta, bump=False)
    return len(ids)


def overview(archive) -> list[dict[str, Any]]:
    """Binders with their number of sheets and sections, newest first."""
    counts = {
        r[0]: (r[1], r[2], r[3])
        for r in archive.conn.execute(
            "SELECT filing_binder, COUNT(*), MIN(filing_section), MAX(filing_section) "
            "FROM documents WHERE filing_sequence IS NOT NULL AND paper_discarded_at IS NULL "
            "GROUP BY filing_binder"
        )
    }
    out = []
    binders = load(archive.paths)
    for b in reversed(binders):
        n, first, last = counts.get(b["name"], (0, None, None))
        out.append({**b, "sheets": n, "first_section": first, "last_section": last,
                    "current": not b["full_at"] and b is binders[-1]})  # fmt: skip
    return out


def adopt_unassigned(archive) -> int:
    """Sheets filed before binders existed belong to the first binder."""
    from . import documents as docs

    ids = [r[0] for r in archive.conn.execute(
        "SELECT id FROM documents WHERE filing_sequence IS NOT NULL AND filing_binder IS NULL")]  # fmt: skip
    if not ids:
        return 0
    binders = load(archive.paths)
    name = binders[0]["name"] if binders else current(archive)
    for doc_id in ids:
        with write_tx(archive.conn):
            meta = docs.load_meta(archive, doc_id)
            meta.filing_binder = name
            docs.persist(archive, meta, bump=False)
    return len(ids)


def merge(paths: ArchivePaths, incoming: list[dict[str, Any]]) -> int:
    """Add binders of an import that are not known yet (by name)."""
    with _LOCK:
        binders = load(paths)
        known = {b["name"].casefold() for b in binders}
        added = 0
        for b in incoming:
            name = " ".join(str(b.get("name", "")).split())[:60] if isinstance(b, dict) else ""
            if name and name.casefold() not in known:
                # imported binders are complete: filing continues in this archive's current one
                binders.insert(0, {"name": name, "started_at": str(b.get("started_at") or ""),
                                   "full_at": b.get("full_at") or now_iso()})  # fmt: skip
                known.add(name.casefold())
                added += 1
        if added:
            _write(paths, binders)
        return added
