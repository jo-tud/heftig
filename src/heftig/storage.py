"""Archive directory layout and crash-safe file primitives.

Everything persistent lives below one archive root::

    archive/
      originals/ab/<sha256>.<ext>          byte-identical originals, never modified
      documents/<uuid>/metadata.json       authoritative per-document metadata (sidecar)
      documents/<uuid>/text.md             extracted text of the whole document
      documents/<uuid>/text_pages.json     text per page incl. method/errors
      documents/<uuid>/preview.webp        regenerable thumbnail
      taxonomy.json                        correspondents/types/tags + aliases (sidecar)
      index.sqlite                         database: index, jobs, auth, import state
      consume/                             watched input folder (may be mounted elsewhere)
      quarantine/                          rejected inputs with a reason file
      email/                               optional archived .eml sources
      backup/                              consistent database snapshots
      tmp/                                 staging area on the same filesystem
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .i18n import N_

HASH_CHUNK = 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class UnsafePathError(ValueError):
    pass


@dataclass(frozen=True)
class ArchivePaths:
    root: Path

    @property
    def originals(self) -> Path:
        return self.root / "originals"

    @property
    def documents(self) -> Path:
        return self.root / "documents"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"

    @property
    def trash(self) -> Path:
        return self.root / "trash"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def email(self) -> Path:
        return self.root / "email"

    @property
    def backup(self) -> Path:
        return self.root / "backup"

    @property
    def db(self) -> Path:
        return self.root / "index.sqlite"

    @property
    def taxonomy(self) -> Path:
        return self.root / "taxonomy.json"

    def ensure(self) -> None:
        old = os.umask(0o077)
        try:
            for d in (
                self.root,
                self.originals,
                self.documents,
                self.quarantine,
                self.trash,
                self.tmp,
                self.email,
                self.backup,
            ):
                d.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old)

    def original_relpath(self, sha256: str, ext: str) -> str:
        if not _SHA_RE.match(sha256):
            raise UnsafePathError("invalid sha256")
        if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
            raise UnsafePathError("invalid extension")
        return f"originals/{sha256[:2]}/{sha256}.{ext}"

    def attachment_relpath(self, sha256: str, filename: str) -> str:
        if not _SHA_RE.match(sha256):
            raise UnsafePathError("invalid sha256")
        ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
            ext = "bin"
        return f"originals/attachments/{sha256[:2]}/{sha256}.{ext}"

    def doc_dir(self, doc_id: str) -> Path:
        return self.documents / str(uuid.UUID(doc_id))

    def resolve(self, relpath: str) -> Path:
        """Resolve a relative path and make sure it stays inside the archive root."""
        if os.path.isabs(relpath) or "\x00" in relpath:
            raise UnsafePathError(relpath)
        p = (self.root / relpath).resolve()
        if p != self.root and self.root not in p.parents:
            raise UnsafePathError(relpath)
        return p


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write via temp file + fsync + rename so readers never see partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class TooLargeError(ValueError):
    pass


def stream_to_tmp(src: BinaryIO, tmp_dir: Path, max_bytes: int) -> tuple[Path, str, int]:
    """Copy a stream into a private temp file, hashing on the fly and enforcing a size cap."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="in-", suffix=".bin", dir=tmp_dir)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = src.read(HASH_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise TooLargeError(
                        N_("File is larger than %(mb)s MB") % {"mb": max_bytes // (1024 * 1024)}
                    )
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return Path(tmp), h.hexdigest(), size


def iter_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            yield Path(dirpath) / name


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def display_filename(name: str | None, limit: int = 200) -> str:
    """Filename for display/metadata only: strip directories and control characters."""
    if not name:
        return "untitled"
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return (name or "untitled")[:limit]


def safe_fs_name(name: str, limit: int = 80) -> str:
    """Conservative ASCII name for files we create ourselves (e.g. quarantine copies)."""
    cleaned = _FILENAME_SAFE.sub("_", display_filename(name)).strip(" .") or "file"
    return cleaned[:limit]
