"""Persistent job queue in SQLite with leases, retry/backoff and crash recovery.

A worker claims a job by setting a lease. If the worker dies, the lease expires and the job is
queued again (:func:`requeue_expired`). Job handlers must be idempotent: every stage writes its
results atomically, so re-running a half-finished job is safe.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from .db import iso, now_iso, utcnow, write_tx
from .i18n import N_

JOB_STATUSES = ("queued", "processing", "done", "needs_review", "failed")


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    doc_id: str | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 5,
    delay_seconds: int = 0,
) -> int:
    payload = payload or {}
    now = now_iso()
    with write_tx(conn):
        if doc_id and kind == "process":
            # coalesce with a job that has not started yet
            row = conn.execute(
                "SELECT id, payload FROM jobs WHERE doc_id=? AND kind='process' AND status='queued'",
                (doc_id,),
            ).fetchone()
            if row:
                old = json.loads(row["payload"])
                requested = payload.get("stages", [])
                stages = list(dict.fromkeys([*old.get("stages", []), *requested]))
                order = ["extract", "classify"]
                old["stages"] = sorted(stages, key=lambda s: order.index(s) if s in order else 9)
                # explicitly requested stages run again, now, with a fresh attempt budget
                old["done"] = [st for st in old.get("done", []) if st not in requested]
                conn.execute(
                    "UPDATE jobs SET payload=?, attempts=0, next_run_at=?, updated_at=? WHERE id=?",
                    (json.dumps(old), now, now, row["id"]),
                )
                return int(row["id"])
        cur = conn.execute(
            "INSERT INTO jobs(kind, doc_id, payload, status, max_attempts, next_run_at, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                kind,
                doc_id,
                json.dumps(payload),
                "queued",
                max_attempts,
                iso(utcnow() + timedelta(seconds=delay_seconds)),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]


def claim(conn: sqlite3.Connection, lease_seconds: int, kinds: tuple[str, ...] | None = None):
    now = now_iso()
    lease = iso(utcnow() + timedelta(seconds=lease_seconds))
    kind_sql = ""
    params: list[Any] = [now]
    if kinds:
        kind_sql = f" AND kind IN ({','.join('?' for _ in kinds)})"
        params += list(kinds)
    with write_tx(conn):
        # never hand out a second job for a document that is being processed right now
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND next_run_at<=?"
            + kind_sql
            + " AND (doc_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs p WHERE "
            "p.doc_id = jobs.doc_id AND p.status = 'processing'))"
            " ORDER BY CASE kind WHEN 'process' THEN 1 ELSE 0 END, id LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status='processing', lease_until=?, attempts=attempts+1, "
            "updated_at=? WHERE id=?",
            (lease, now, row["id"]),
        )
        return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def extend_lease(conn: sqlite3.Connection, job_id: int, lease_seconds: int) -> None:
    conn.execute(
        "UPDATE jobs SET lease_until=? WHERE id=?",
        (iso(utcnow() + timedelta(seconds=lease_seconds)), job_id),
    )


def set_stage(
    conn: sqlite3.Connection, job_id: int, stage: str, progress: float, lease_seconds: int
) -> None:
    with write_tx(conn):
        conn.execute(
            "UPDATE jobs SET stage=?, progress=?, lease_until=?, updated_at=? WHERE id=?",
            (
                stage,
                progress,
                iso(utcnow() + timedelta(seconds=lease_seconds)),
                now_iso(),
                job_id,
            ),
        )


def update_payload(conn: sqlite3.Connection, job_id: int, payload: dict[str, Any]) -> None:
    with write_tx(conn):
        conn.execute(
            "UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
            (json.dumps(payload), now_iso(), job_id),
        )


def finish(
    conn: sqlite3.Connection,
    job_id: int,
    status: str = "done",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    assert status in ("done", "needs_review", "failed")
    with write_tx(conn):
        conn.execute(
            "UPDATE jobs SET status=?, progress=1, lease_until=NULL, result=?, error=?, "
            "updated_at=? WHERE id=?",
            (status, json.dumps(result) if result else None, error, now_iso(), job_id),
        )


def fail(conn: sqlite3.Connection, job_id: int, error: str, backoff_seconds: int) -> str:
    """Record a failed attempt. Requeues with exponential backoff until max_attempts."""
    with write_tx(conn):
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return "failed"
        if row["attempts"] >= row["max_attempts"]:
            status, next_run = "failed", now_iso()
        else:
            delay = backoff_seconds * (2 ** max(0, row["attempts"] - 1))
            status, next_run = "queued", iso(utcnow() + timedelta(seconds=min(delay, 6 * 3600)))
        conn.execute(
            "UPDATE jobs SET status=?, error=?, lease_until=NULL, next_run_at=?, updated_at=? "
            "WHERE id=?",
            (status, error[:2000], next_run, now_iso(), job_id),
        )
        return status


def postpone(conn: sqlite3.Connection, job_id: int, error: str, delay_seconds: float) -> str:
    """Put a job back without counting the attempt (e.g. the AI provider's rate limit)."""
    with write_tx(conn):
        next_run = iso(utcnow() + timedelta(seconds=max(10.0, min(delay_seconds, 3600.0))))
        conn.execute(
            "UPDATE jobs SET status='queued', attempts=MAX(0, attempts - 1), error=?, "
            "lease_until=NULL, next_run_at=?, updated_at=? WHERE id=?",
            (error[:2000], next_run, now_iso(), job_id),
        )
    return "queued"


def requeue_expired(conn: sqlite3.Connection) -> int:
    """Jobs whose worker died (lease expired) go back to the queue."""
    now = now_iso()
    with write_tx(conn):
        cur = conn.execute(
            "UPDATE jobs SET status='queued', lease_until=NULL, next_run_at=?, updated_at=?, "
            "error=COALESCE(error, ?) WHERE status='processing' AND lease_until < ?",
            (now, now, N_("The worker was interrupted – will continue"), now),
        )
        return cur.rowcount


def requeue_all_processing(conn: sqlite3.Connection) -> int:
    """On worker start: this is the only worker, so every 'processing' job is orphaned."""
    now = now_iso()
    with write_tx(conn):
        cur = conn.execute(
            "UPDATE jobs SET status='queued', lease_until=NULL, next_run_at=?, updated_at=? "
            "WHERE status='processing'",
            (now, now),
        )
        return cur.rowcount


def retry(conn: sqlite3.Connection, job_id: int) -> bool:
    """Run a failed job again from the start (all stages)."""
    with write_tx(conn):
        row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return False
        payload = json.loads(row["payload"] or "{}")
        payload.pop("done", None)
        cur = conn.execute(
            "UPDATE jobs SET status='queued', attempts=0, error=NULL, next_run_at=?, payload=?, "
            "updated_at=? WHERE id=? AND status IN ('failed', 'needs_review')",
            (now_iso(), json.dumps(payload), now_iso(), job_id),
        )
        return cur.rowcount > 0


def get(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = dict.fromkeys(JOB_STATUSES, 0)
    for r in conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"):
        out[r[0]] = r[1]
    return out


def as_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d.get("payload") or "{}")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d
