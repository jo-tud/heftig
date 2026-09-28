"""Watched input folder (scanner share / local folder).

Polling instead of file system events: works on network shares (SMB/NFS) where inotify does not,
and picks up files that arrived while the machine was off. A file is only taken when it is
complete - its size and mtime stayed unchanged over several polls, it is not named like a
temporary file, and a PDF/JPEG ends like one (``%%EOF`` / end-of-image marker): scanners pause
between the pages of a multi-page scan longer than the stability window, and a half-written PDF
would otherwise be quarantined as broken. Such a file is waited for up to
``consume_incomplete_wait_seconds`` after its last change, then taken anyway (the import
decides). The source is removed (or moved) only after the original, metadata and
processing job are durably committed. Failing files end up in ``quarantine/`` with a reason
file; they are never retried forever and never silently dropped.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .archive import Archive
from .db import now_iso, write_tx
from .i18n import N_, _
from .ingest import ingest_stream
from .storage import atomic_write_json, safe_fs_name

log = logging.getLogger("heftig.consume")

TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", ".filepart", ".swp")
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
DONE_DIR = ".heftig-verarbeitet"
REJECTED_DIR = ".heftig-abgelehnt"  # too large for the archive: left here, moved aside


def is_temporary_name(name: str) -> bool:
    low = name.lower()
    return (
        low.startswith(".")
        or low.startswith("~")
        or low.endswith("~")
        or low.endswith(TEMP_SUFFIXES)
        or low in IGNORED_NAMES
    )


def open_regular(path: Path):
    """Open a file for reading without following a symbolic link and without blocking on a
    FIFO; raises OSError unless it is a regular file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def looks_complete(path: Path, size: int) -> bool:
    """Does the file end like a finished PDF / JPEG? Other types: no check."""
    try:
        with open_regular(path) as f:
            head = f.read(8)
            f.seek(max(0, size - 4096))
            tail = f.read()
    except OSError:
        return True  # unreadable: the import reports it (and gives up after a few tries)
    if head.startswith(b"%PDF"):
        return b"%%EOF" in tail
    if head.startswith(b"\xff\xd8\xff"):
        return b"\xff\xd9" in tail[-1024:]
    return True


@dataclass
class _Seen:
    size: int
    mtime: float
    stable: int


class ConsumeWatcher:
    def __init__(
        self,
        archive: Archive,
        source: str = "scanner",
        root: Path | None = None,
        paper: bool | None = None,
    ):
        """Watch `root` (default: the scanner folder). `paper` None = derive from source."""
        self.archive = archive
        self.source = source
        self._root = root
        self.paper = paper
        self._seen: dict[str, _Seen] = {}
        self._waiting: set[str] = set()  # files that do not end like a complete file yet
        # files already archived whose source could not be removed (read-only share):
        # skipped while unchanged instead of being ingested (as duplicate) on every poll
        self._undeletable: dict[str, tuple[int, float]] = {}
        self._undeletable_msg = ""
        self.last_error: str | None = None
        self.last_poll_at: str | None = None

    @property
    def root(self) -> Path:
        return self._root or self.archive.settings.consume_path

    def poll(self) -> list[dict]:
        """One pass over the folder. Returns the results of files taken this pass."""
        s = self.archive.settings
        self.last_poll_at = now_iso()
        root = self.root
        if not root.is_dir():
            self.last_error = N_("Input folder %(path)s cannot be reached") % {"path": root}
            return []
        # keep reporting sources that are archived but cannot be removed
        self.last_error = self._undeletable_msg if self._undeletable else None
        results = []
        present: set[str] = set()
        now = time.time()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d
                for d in dirnames
                if not is_temporary_name(d) and d not in (DONE_DIR, REJECTED_DIR)
            ]
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if is_temporary_name(name) or not path.is_file() or path.is_symlink():
                    continue
                key = str(path)
                present.add(key)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if self._undeletable.get(key) == (st.st_size, st.st_mtime):
                    continue
                if self._given_up(key):
                    continue
                prev = self._seen.get(key)
                if prev and prev.size == st.st_size and prev.mtime == st.st_mtime:
                    prev.stable += 1
                else:
                    self._seen[key] = _Seen(st.st_size, st.st_mtime, 0)
                    continue
                if prev.stable < s.consume_stable_polls - 1:
                    continue
                if now - st.st_mtime < s.consume_min_age_seconds:
                    continue
                if now - st.st_mtime < s.consume_incomplete_wait_seconds and not looks_complete(
                    path, st.st_size
                ):
                    if key not in self._waiting:
                        self._waiting.add(key)
                        log.info("consume: %s not complete yet (scanner still writing?)", name)
                    continue
                self._waiting.discard(key)
                results.append(self._take(path))
                self._seen.pop(key, None)
        for key in list(self._seen):
            if key not in present:
                self._seen.pop(key)
        self._waiting &= present
        for key in list(self._undeletable):
            if key not in present:
                self._undeletable.pop(key)
        return results

    def _given_up(self, key: str) -> bool:
        row = self.archive.conn.execute(
            "SELECT failures FROM consume_failures WHERE path=?", (key,)
        ).fetchone()
        return bool(row) and row[0] >= self.archive.settings.consume_max_failures

    def _take(self, path: Path) -> dict:
        rel = str(path.relative_to(self.root))
        details = {"path": rel}
        # opened once, without following links, and only if it is a regular file: a file
        # swapped for a link (to a private file elsewhere) or a FIFO after the checks above
        # is never read
        try:
            src = open_regular(path)
        except OSError as e:
            if e.errno not in (errno.ELOOP, errno.EINVAL, errno.ENXIO):
                # e.g. no permission: a real problem, reported (and given up after a few tries)
                return self._failure(path, rel, f"{type(e).__name__}: {e.strerror or e}")
            log.warning("consume: %s skipped: not a regular file (%s)", rel, e.strerror or e)
            try:
                st = path.lstat()
                self._undeletable[str(path)] = (st.st_size, st.st_mtime)
            except OSError:
                pass
            return {"path": rel, "status": "skipped", "message": N_("not a regular file")}
        with src:
            try:
                res = ingest_stream(
                    self.archive, src, path.name, self.source, source_details=details,
                    paper=self.paper,
                )  # fmt: skip
            except Exception as e:
                log.exception("consume: ingest of %s failed", rel)
                return self._failure(path, rel, f"{type(e).__name__}: {e}")
            return self._after_ingest(path, rel, res, src)

    def _after_ingest(self, path: Path, rel: str, res, src) -> dict:
        try:
            if res.status == "rejected":
                self._quarantine(path, res.message, src)
            else:
                self._dispose(path)
        except OSError as e:
            # archived (or rejected and recorded) but the source cannot be removed
            log.warning("consume: could not remove %s: %s", rel, e)
            self._undeletable_msg = N_(
                "Source file archived but not removed (%(path)s): %(error)s – check the write "
                "permissions of the input folder"
            ) % {"path": rel, "error": e.strerror}
            self.last_error = self._undeletable_msg
            try:
                st = path.stat()
                self._undeletable[str(path)] = (st.st_size, st.st_mtime)
            except OSError:
                pass
        self._clear_failures(str(path))
        return {"path": rel, **res.as_dict()}

    def _dispose(self, path: Path) -> None:
        # the archive commit is durable at this point
        if self.archive.settings.consume_after == "move":
            dest = _free_name(self.root / DONE_DIR, path.name)
            shutil.move(str(path), dest)
        else:
            path.unlink(missing_ok=True)

    def _quarantine(self, path: Path, reason: str, src=None) -> Path:
        info = {
            "original_name": path.name,
            "reason": reason,
            "at": now_iso(),
            "source": self.source,
        }
        size = os.fstat(src.fileno()).st_size if src is not None else path.lstat().st_size
        if size > self.archive.settings.max_upload_bytes:
            # too large: it stays in the consume folder (moved aside), a copy would only fill
            # the archive's disk
            kept = _free_name(self.root / REJECTED_DIR, path.name)
            os.replace(path, kept)
            info["kept_in"] = str(kept.relative_to(self.root))
            stub = _free_name(self.archive.paths.quarantine, path.name)
            atomic_write_json(stub.with_name(stub.name + ".reason.json"), info)
            log.warning("consume: %s too large, left in the consume folder", path.name)
            return stub
        dest = _free_name(self.archive.paths.quarantine, path.name)
        own = src if src is not None else open_regular(path)
        try:
            own.seek(0)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(own, out)
                out.flush()
                os.fsync(out.fileno())  # durable before the source is removed
        finally:
            if src is None:
                own.close()
        atomic_write_json(dest.with_name(dest.name + ".reason.json"), info)
        path.unlink(missing_ok=True)
        log.warning("consume: %s quarantined: %s", path.name, reason)
        return dest

    def _failure(self, path: Path, rel: str, error: str) -> dict:
        conn = self.archive.conn
        with write_tx(conn):
            conn.execute(
                "INSERT INTO consume_failures(path, failures, last_error, updated_at) "
                "VALUES(?, 1, ?, ?) ON CONFLICT(path) DO UPDATE SET failures=failures+1, "
                "last_error=excluded.last_error, updated_at=excluded.updated_at",
                (str(path), error[:500], now_iso()),
            )
            n = conn.execute(
                "SELECT failures FROM consume_failures WHERE path=?", (str(path),)
            ).fetchone()[0]
        if n == self.archive.settings.consume_max_failures:
            reason = N_("Could not be imported after %(num)s attempts: %(error)s") % {
                "num": n,
                "error": error,
            }
            quarantined = True
            try:
                self._quarantine(path, reason)
            except OSError:
                log.exception("consume: could not quarantine %s", rel)
                quarantined = False
                reason = N_("%(reason)s – the file stays in the input folder and is ignored") % {
                    "reason": reason
                }
            from .ingest import record_rejection

            record_rejection(
                self.archive, source=self.source, filename=path.name, message=reason,
                details={"path": rel},
            )  # fmt: skip
            if quarantined:
                self._clear_failures(str(path))
            return {"path": rel, "status": "rejected", "message": reason}
        return {"path": rel, "status": "error", "message": error}

    def _clear_failures(self, key: str) -> None:
        with write_tx(self.archive.conn):
            self.archive.conn.execute("DELETE FROM consume_failures WHERE path=?", (key,))


def _free_name(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = directory / f"{stamp}_{safe_fs_name(name)}"
    n = 1
    while dest.exists() or dest.with_name(dest.name + ".reason.json").exists():
        dest = directory / f"{stamp}-{n}_{safe_fs_name(name)}"
        n += 1
    return dest


def list_quarantine(archive: Archive) -> list[dict]:
    out = []
    for p in sorted(archive.paths.quarantine.glob("*.reason.json"), reverse=True):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        info["file"] = p.name[: -len(".reason.json")]
        out.append(info)
    return out


HIDDEN_DIR = "ausgeblendet"


class QuarantineError(ValueError):
    pass


def _quarantined(archive: Archive, name: str) -> tuple[Path, Path, dict]:
    qdir = archive.paths.quarantine
    path = qdir / name
    if "/" in name or "\\" in name or name.startswith(".") or path.parent != qdir:
        raise QuarantineError(_("Invalid file name."))
    reason = path.with_name(path.name + ".reason.json")
    if not reason.is_file():
        raise QuarantineError(_("No longer in quarantine."))
    try:
        info = json.loads(reason.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    return path, reason, info


def retry_quarantined(archive: Archive, name: str) -> dict:
    """Import a quarantined file again (e.g. after an update); on success it leaves the
    quarantine, otherwise the new reason is recorded."""
    from .ingest import ingest_stream

    path, reason, info = _quarantined(archive, name)
    if not path.is_file():
        raise QuarantineError(
            _(
                "The file is not in the quarantine but in the input folder at %(path)s.",
                path=info["kept_in"],
            )
            if info.get("kept_in")
            else _("The file is not in the quarantine.")
        )
    source = info.get("source") if info.get("source") in ("scanner", "folder") else "folder"
    with open(path, "rb") as f:
        res = ingest_stream(
            archive, f, info.get("original_name") or name, source,
            {"path": info.get("original_name") or name, "from": "quarantine"},
        )  # fmt: skip
    if res.status == "rejected":
        atomic_write_json(reason, {**info, "reason": res.message, "at": now_iso()})
    else:
        path.unlink(missing_ok=True)
        reason.unlink(missing_ok=True)
    return res.as_dict()


def hide_quarantined(archive: Archive, name: str) -> None:
    """Out of the list, kept on disk (quarantine/ausgeblendet/) - nothing is deleted."""
    path, reason, _ = _quarantined(archive, name)
    dest = archive.paths.quarantine / HIDDEN_DIR
    dest.mkdir(parents=True, exist_ok=True)
    os.replace(reason, dest / reason.name)
    if path.is_file():
        os.replace(path, dest / path.name)
