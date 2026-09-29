"""The Archive object ties settings, paths and per-thread database connections together."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from .config import Settings
from .db import connect, migrate
from .storage import ArchivePaths

log = logging.getLogger("heftig")


class Archive:
    def __init__(self, settings: Settings):
        # base_settings: from the environment; settings: plus what was set in the web interface
        self.base_settings = self.settings = settings
        self.paths = ArchivePaths(Path(settings.archive_dir))
        self.paths.ensure()
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._snapshot_before_update()
        migrate(self.conn)
        self._settings_revision = -1
        self.refresh_settings()
        self._rebuild_index_if_required()
        self._adopt_unassigned_filings()

    def _adopt_unassigned_filings(self) -> None:
        """Sheets filed before binders existed belong to the first binder."""
        from .binders import adopt_unassigned

        try:
            n = adopt_unassigned(self)
        except Exception:  # noqa: BLE001 - never block the start over this
            log.exception("assigning filed documents to a binder failed")
            return
        if n:
            log.info("%s filed document(s) assigned to the first binder", n)

    def refresh_settings(self) -> bool:
        """Pick up settings changed in the web interface (cheap: one small query)."""
        from . import settings_store

        rev = settings_store.revision(self.conn)
        if rev == self._settings_revision:
            return False
        self.settings = settings_store.effective(self.base_settings, self.conn)
        self._settings_revision = rev
        return True

    def _snapshot_before_update(self) -> None:
        """An update with database migrations: first a copy of the database as it was
        (backup/vor-update-v<N>.sqlite, the last three are kept)."""
        from .db import backup_to, pending_migrations

        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current == 0 or not pending_migrations(self.conn):
            return  # new archive, or nothing to migrate
        dest = self.paths.backup / f"vor-update-v{current}.sqlite"
        try:
            backup_to(self.conn, dest)
            log.info("database copied to %s before the update", dest.name)
        except (OSError, sqlite3.Error):
            log.exception("copy of the database before the update failed")
            return
        old = sorted(
            self.paths.backup.glob("vor-update-v*.sqlite"), key=lambda f: f.stat().st_mtime
        )
        for f in old[:-3]:
            f.unlink(missing_ok=True)

    def _rebuild_index_if_required(self) -> None:
        """A migration that recreated the search index sets this flag."""
        from .db import get_meta, set_meta, write_tx
        from .index import rebuild

        if get_meta(self.conn, "index_rebuild_required") == "1":
            log.info("rebuilding search index after schema migration")
            with write_tx(self.conn):
                rebuild(self.conn)
                set_meta(self.conn, "index_rebuild_required", "0")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.paths.db)
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()
