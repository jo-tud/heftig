"""SQLite access: connections, migrations, small helpers.

One connection per thread. WAL mode lets the web process and the worker read concurrently;
writers serialise through ``BEGIN IMMEDIATE`` (see :func:`write_tx`).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 3


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def connect(path: Path) -> sqlite3.Connection:
    new = not path.exists()
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # deleted rows (purged documents, pruned AI responses) are overwritten, not left behind
    conn.execute("PRAGMA secure_delete=ON")
    if new:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return conn


@contextlib.contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Exclusive write transaction (serialised across processes)."""
    if conn.in_transaction:
        # nested use: piggyback on the outer transaction
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migrations() -> list[tuple[int, str]]:
    out = []
    for entry in resources.files("heftig.migrations").iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            out.append((int(name[:4]), entry.read_text(encoding="utf-8")))
    return sorted(out)


def pending_migrations(conn: sqlite3.Connection) -> list[int]:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    return [v for v, _ in _migrations() if v > current]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _migrations():
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # web and worker start together after an upgrade: the other process may have
            # applied this migration while we waited for the lock
            if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                conn.execute("COMMIT")
                current = version
                continue
            # executescript would COMMIT implicitly; run statements one by one instead
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    statements, buf = [], []
    for ln in lines:
        buf.append(ln)
        candidate = "\n".join(buf)
        if ln.rstrip().endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            buf = []
    if "".join(buf).strip():
        statements.append("\n".join(buf).strip())
    return statements


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def backup_to(conn: sqlite3.Connection, dest: Path) -> None:
    """Consistent online copy using SQLite's backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # own temporary name per call: the worker's automatic snapshot and one started by a
    # backup script (heftig db-snapshot) may run at the same time
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            conn.backup(target)
        finally:
            target.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
