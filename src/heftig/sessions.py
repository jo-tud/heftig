"""Scan sessions: digitising a stack of paper from one existing folder.

While a session is active, every paper document that arrives (scanner, phone camera, upload
marked as paper) belongs to it. What happens to the paper is chosen once when starting:

- ``folder``: the paper goes back into the old folder - each document records it
  (``paper_location``), nothing to file afterwards
- ``refile``: the paper moves into Heftig's own filing - at the end, all of them are marked
  filed with one action, as the stack comes out of the scanner: a document feeder keeps the
  order, so the first scanned sheet lies on top
- ``sort``: sort out - the classifier suggests per document whether to keep the original
  (contracts, certificates, assessments ...); at the end the kept ones are filed and the rest
  marked as shredded, again with one action

A session ends by itself after ``IDLE_HOURS`` without a new document, so the next day's mail
does not join it by accident. A finished session with open paper decisions stays visible in
the inbox until they are taken or dismissed.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from . import documents as docs
from .archive import Archive
from .db import iso, now_iso, parse_iso, utcnow, write_tx
from .i18n import N_, Labels, _
from .models import DocumentMetadata, ScanSessionRef
from .textnorm import fold

MODES = Labels({
    "folder": N_("back into the binder"),
    "refile": N_("into the Heftig filing"),
    "sort": N_("sort out"),
})  # fmt: skip
IDLE_HOURS = 8
# document types whose paper original is usually worth keeping (fallback when the classifier
# gave no suggestion); matched as word stems of the folded type name
KEEP_STEMS = (
    "vertrag", "urkunde", "zeugnis", "bescheid", "police", "versicherungsschein", "vollmacht",
    "bescheinigung", "garantie", "testament", "zertifikat", "notar", "grundbuch", "ausweis",
    "kuendigung", "darlehen", "diplom", "nachweis",
)  # fmt: skip


class SessionError(ValueError):
    pass


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["mode_label"] = MODES.get(d["mode"], d["mode"])
    return d


def active(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM scan_sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    )


def get(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM scan_sessions WHERE id=?", (session_id,)).fetchone())


def start(archive: Archive, name: str, mode: str) -> dict[str, Any]:
    name = " ".join((name or "").split())[:80]
    if not name:
        raise SessionError(_("Please name the binder, e.g. “Insurance”."))
    if mode not in MODES:
        raise SessionError(_("Unknown paper handling."))
    now = now_iso()
    sid = secrets.token_hex(6)
    with write_tx(archive.conn):
        archive.conn.execute("UPDATE scan_sessions SET ended_at=? WHERE ended_at IS NULL", (now,))
        archive.conn.execute(
            "INSERT INTO scan_sessions(id, name, mode, started_at, last_activity_at) "
            "VALUES(?,?,?,?,?)",
            (sid, name, mode, now, now),
        )
    return get(archive.conn, sid)  # type: ignore[return-value]


def end(archive: Archive, session_id: str) -> None:
    with write_tx(archive.conn):
        archive.conn.execute(
            "UPDATE scan_sessions SET ended_at=COALESCE(ended_at, ?) WHERE id=?",
            (now_iso(), session_id),
        )


def close(archive: Archive, session_id: str) -> None:
    """Hide a finished session; its open paper shows up in the normal "to file" list."""
    now = now_iso()
    with write_tx(archive.conn):
        archive.conn.execute(
            "UPDATE scan_sessions SET ended_at=COALESCE(ended_at, ?), closed_at=? WHERE id=?",
            (now, now, session_id),
        )


def end_idle(archive: Archive, hours: int = IDLE_HOURS) -> int:
    cutoff = iso(utcnow() - timedelta(hours=hours))
    with write_tx(archive.conn):
        return archive.conn.execute(
            "UPDATE scan_sessions SET ended_at=? WHERE ended_at IS NULL AND last_activity_at < ?",
            (now_iso(), cutoff),
        ).rowcount


def attach(archive: Archive, meta: DocumentMetadata) -> None:
    """Called at ingest (inside the write transaction) for new paper documents."""
    if not meta.paper:
        return
    s = active(archive.conn)
    if s is None:
        return
    last = parse_iso(s["last_activity_at"])
    if last and last < utcnow() - timedelta(hours=IDLE_HOURS):
        return  # forgotten session: the worker ends it at its next round
    meta.scan_session = ScanSessionRef(id=s["id"], name=s["name"], mode=s["mode"])
    if s["mode"] == "folder":
        meta.paper_location = N_("Binder %(name)s") % {"name": s["name"]}  # stored
    archive.conn.execute(
        "UPDATE scan_sessions SET last_activity_at=? WHERE id=?", (now_iso(), s["id"])
    )


def keep_suggestion(meta: dict[str, Any]) -> tuple[bool, str]:
    """(keep the paper?, why) - the classifier's or the user's decision, else a type rule."""
    if meta.get("keep_original") is not None:
        return bool(meta["keep_original"]), meta.get("keep_original_reason") or ""
    t = fold(meta.get("document_type") or "")
    if any(stem in t for stem in KEEP_STEMS):
        return True, _("Document type %(name)s", name=meta.get("document_type"))
    return False, ""


def summary(archive: Archive, session_id: str) -> dict[str, Any] | None:
    """What the inbox card shows: progress and the open paper decisions."""
    import json

    conn = archive.conn
    s = get(conn, session_id)
    if s is None:
        return None
    rows = conn.execute(
        "SELECT id, title, original_filename, status, filing_sequence, paper_location, "
        "paper_discarded_at, metadata_json FROM documents WHERE scan_session_id=? "
        "ORDER BY ingest_sequence",
        (session_id,),
    ).fetchall()
    keep, discard, pending = [], [], []
    busy = review = 0
    for n, r in enumerate(rows, 1):
        busy += r["status"] in ("queued", "processing")
        review += r["status"] in ("needs_review", "failed")
        if r["filing_sequence"] is not None or r["paper_location"] or r["paper_discarded_at"]:
            continue
        meta = json.loads(r["metadata_json"])
        k, why = keep_suggestion(meta)
        item = {"id": r["id"], "n": n, "title": r["title"] or r["original_filename"],
                "why": why, "decided": meta.get("keep_original_source") == "user"}  # fmt: skip
        pending.append(item)
        (keep if k else discard).append(item)
    s.update(
        total=len(rows), busy=busy, review=review, pending=pending, keep=keep, discard=discard,
        done=not pending,
    )  # fmt: skip
    return s


def card(archive: Archive) -> dict[str, Any] | None:
    """The session for the inbox card: the active one, else the latest finished one that
    still has open paper decisions."""
    s = active(archive.conn)
    if s is None:
        r = archive.conn.execute(
            "SELECT id FROM scan_sessions WHERE closed_at IS NULL AND ended_at IS NOT NULL "
            "AND mode != 'folder' ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        if r is None:
            return None
        sm = summary(archive, r[0])
        return sm if sm and sm["pending"] else None
    return summary(archive, s["id"])


def _shown(items: list[dict[str, Any]], shown: set[str] | None) -> list[dict[str, Any]]:
    """Only what the user saw when deciding: a page scanned meanwhile (not yet classified)
    must not be filed or marked as shredded along with the rest."""
    return [i for i in items if shown is None or i["id"] in shown]


def file_all(archive: Archive, session_id: str, shown: set[str] | None = None) -> int:
    """refile: mark all open paper of the session as filed - the stack from the scanner goes
    into the binder as it is, the first scanned sheet on top (so it is filed last)."""
    sm = summary(archive, session_id)
    items = _shown(sm["pending"], shown) if sm else []
    for item in reversed(items):
        docs.mark_filed(archive, item["id"])
    return len(items)


def apply_sort(archive: Archive, session_id: str, shown: set[str] | None = None) -> tuple[int, int]:
    """sort: file the originals to keep (the first scanned on top, see file_all), mark the
    rest as shredded."""
    sm = summary(archive, session_id)
    if sm is None:
        return 0, 0
    if sm["busy"]:
        raise SessionError(_("Still being processed – please wait until the suggestion is ready."))
    keep, discard = _shown(sm["keep"], shown), _shown(sm["discard"], shown)
    for item in reversed(keep):
        docs.mark_filed(archive, item["id"])
    for item in discard:
        docs.set_paper_state(archive, item["id"], discarded=True)
    return len(keep), len(discard)
