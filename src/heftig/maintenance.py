"""Integrity check, repair, rebuilds, export/import and backup.

Export format (directory or ZIP), see docs/data-format.md::

    manifest.json            format/schema versions, export time, every file with SHA-256
    README.txt               short human-readable description
    originals/ab/<sha>.<ext> unchanged originals
    documents/<uuid>/        metadata.json, text.md, text_pages.json
    metadata.jsonl           one metadata object per line, in ingest order
    taxonomy.json            correspondents, document types, tags incl. aliases
    saved_searches.json      saved searches (optional)
    state/                   sequences, ingest event log, IMAP cursors, user/token names
                             (never passwords, password hashes, tokens or API keys)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from . import __version__, jobs, saved_searches
from . import documents as docs
from . import index as fts
from . import taxonomy as tax
from .archive import Archive
from .db import backup_to, get_meta, now_iso, parse_iso, set_meta, utcnow, write_tx
from .i18n import N_, _, ngettext
from .models import METADATA_SCHEMA_VERSION, DocumentMetadata, TaxonomyFile, TextPages
from .storage import atomic_write_json, iter_files, read_json, sha256_file

log = logging.getLogger("heftig.maintenance")

EXPORT_FORMAT = "heftig-export"
EXPORT_FORMAT_VERSION = 1
MAX_ZIP_RATIO = 200

EXPORT_README = """Heftig archive export
=====================

originals/        byte-identical original files, named by SHA-256
documents/<id>/   metadata.json (see metadata.schema.json), text.md, text_pages.json
metadata.jsonl    all metadata objects, one per line, in ingest order
taxonomy.json     correspondents, document types and tags with aliases
saved_searches.json  saved searches of the search page (optional)
state/            sequences, ingest events, IMAP cursors, user names (no secrets)
manifest.json     list of all files with SHA-256 for verification

Import into Heftig: `heftig import <this directory or zip>`.
Field mapping to Paperless-ngx: docs/paperless.md in the Heftig repository.
"""


class MaintenanceError(Exception):
    pass


# --- integrity -------------------------------------------------------------------------


def check(archive: Archive, *, verify_hashes: bool = True) -> dict[str, Any]:
    conn = archive.conn
    paths = archive.paths
    issues: list[dict[str, str]] = []
    rows = conn.execute(
        "SELECT id, sha256, original_relpath, revision FROM documents ORDER BY ingest_sequence"
    ).fetchall()
    known_ids = {r["id"] for r in rows}
    known_originals = set()
    for r in rows:
        known_originals.add(r["original_relpath"])
        orig = paths.resolve(r["original_relpath"])
        if not orig.exists():
            issues.append(
                {"kind": "missing_original", "doc": r["id"], "detail": r["original_relpath"]}
            )
        elif verify_hashes and sha256_file(orig) != r["sha256"]:
            issues.append(
                {"kind": "hash_mismatch", "doc": r["id"], "detail": r["original_relpath"]}
            )
        f = docs.files(archive, r["id"])
        if not f.metadata.exists():
            issues.append({"kind": "missing_sidecar", "doc": r["id"], "detail": "metadata.json"})
            continue
        try:
            meta = DocumentMetadata.model_validate(read_json(f.metadata))
        except Exception as e:
            issues.append({"kind": "invalid_sidecar", "doc": r["id"], "detail": str(e)[:200]})
            continue
        if meta.revision != r["revision"]:
            issues.append(
                {
                    "kind": "revision_mismatch",
                    "doc": r["id"],
                    "detail": f"sidecar {meta.revision} / db {r['revision']}",
                }
            )
        if meta.sha256 != r["sha256"]:
            issues.append({"kind": "sidecar_hash_mismatch", "doc": r["id"], "detail": ""})
        for att in meta.attachments:
            known_originals.add(att.relpath)
            ap = paths.resolve(att.relpath)
            if not ap.exists():
                issues.append(
                    {"kind": "missing_attachment", "doc": r["id"], "detail": att.filename}
                )
            elif verify_hashes and sha256_file(ap) != att.sha256:
                issues.append(
                    {"kind": "attachment_hash_mismatch", "doc": r["id"], "detail": att.filename}
                )
    if paths.documents.exists():
        for d in sorted(paths.documents.iterdir()):
            if d.is_dir() and d.name not in known_ids:
                issues.append({"kind": "orphan_sidecar", "doc": d.name, "detail": str(d.name)})
    from .trash import referenced_files

    known_originals |= referenced_files(archive)  # still held by documents in the Papierkorb
    if paths.originals.exists():
        for p in iter_files(paths.originals):
            rel = str(p.relative_to(paths.root))
            if rel not in known_originals and not p.name.startswith("."):
                kind = (
                    "orphan_attachment"
                    if rel.startswith("originals/attachments/")
                    else "orphan_original"
                )
                issues.append({"kind": kind, "doc": "", "detail": rel})
    stuck = conn.execute(
        "SELECT d.id FROM documents d WHERE d.status IN ('queued','processing') AND NOT EXISTS "
        "(SELECT 1 FROM jobs j WHERE j.doc_id=d.id AND j.status IN ('queued','processing'))"
    ).fetchall()
    for r in stuck:
        issues.append({"kind": "unfinished_processing", "doc": r["id"], "detail": ""})
    fts_count = conn.execute("SELECT COUNT(*) FROM doc_fts").fetchone()[0]
    if fts_count != len(rows):
        issues.append(
            {
                "kind": "index_mismatch",
                "doc": "",
                "detail": f"{fts_count} index rows, {len(rows)} docs",
            }
        )
    return {"documents": len(rows), "issues": issues, "ok": not issues}


def repair(archive: Archive, *, adopt_orphans: bool = False) -> dict[str, Any]:
    """Finish interrupted operations. Safe to run repeatedly."""
    report = check(archive, verify_hashes=False)
    actions: list[str] = []
    for issue in report["issues"]:
        try:
            _repair_issue(archive, issue, actions, adopt_orphans)
        except Exception as e:  # one broken item must not stop the rest
            log.exception("repair of %s failed", issue)
            actions.append(
                _(
                    "%(item)s: cannot be repaired (%(error)s)",
                    item=issue["doc"] or issue["detail"],
                    error=f"{type(e).__name__}: {e}",
                )
            )
    _finish_repair(archive, report, actions)
    after = check(archive, verify_hashes=False)
    return {"actions": actions, "remaining_issues": after["issues"]}


def _park_sidecar(archive: Archive, doc_id: str, reason: str) -> str:
    """Move an unusable sidecar directory to quarantine (reversible, nothing deleted)."""
    src = docs.files(archive, doc_id).dir
    dest = archive.paths.quarantine / f"sidecar-{doc_id}"
    shutil.move(str(src), dest)
    atomic_write_json(dest / "reason.json", {"reason": reason, "at": now_iso()})
    return f"quarantine/{dest.name}"


def _repair_issue(archive: Archive, issue: dict, actions: list[str], adopt_orphans: bool) -> None:
    conn = archive.conn
    kind, doc_id = issue["kind"], issue["doc"]
    if kind in ("orphan_sidecar", "revision_mismatch"):
        # sidecars are written before the DB commit -> a newer sidecar wins
        f = docs.files(archive, doc_id)
        if not f.metadata.exists():
            return
        try:
            meta = DocumentMetadata.model_validate(read_json(f.metadata))
        except ValueError as e:  # pydantic ValidationError / broken JSON
            if kind != "orphan_sidecar":
                raise
            where = _park_sidecar(
                archive, doc_id, N_("Invalid sidecar: %(error)s") % {"error": str(e)[:300]}
            )
            actions.append(_("%(id)s: invalid sidecar moved to %(path)s", id=doc_id, path=where))
            return
        if kind == "orphan_sidecar":
            clash = conn.execute(
                "SELECT id FROM documents WHERE sha256=?", (meta.sha256,)
            ).fetchone()
            if clash:
                where = _park_sidecar(
                    archive,
                    doc_id,
                    N_("Original already archived as document %(id)s") % {"id": clash["id"]},
                )
                actions.append(
                    _("%(id)s: duplicate sidecar moved to %(path)s", id=doc_id, path=where)
                )
                return
            if conn.execute(
                "SELECT 1 FROM documents WHERE ingest_sequence=?", (meta.ingest_sequence,)
            ).fetchone():
                with write_tx(conn):
                    old = meta.ingest_sequence
                    meta.ingest_sequence = docs.next_sequence(conn, "ingest_sequence")
                actions.append(
                    _(
                        "%(id)s: arrival number %(old)s → %(new)s",
                        id=doc_id,
                        old=old,
                        new=meta.ingest_sequence,
                    )
                )
        if kind == "revision_mismatch":
            row = conn.execute(
                "SELECT revision, metadata_json FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            if row and row["revision"] > meta.revision:
                newer = DocumentMetadata.model_validate_json(row["metadata_json"])
                with write_tx(conn):
                    docs.persist(archive, newer, bump=False)
                actions.append(
                    _("%(id)s: sidecar written from the newer database version", id=doc_id)
                )
                return
        orig = archive.paths.resolve(meta.original_relpath)
        if not orig.exists():
            actions.append(_("%(id)s: sidecar without original – not taken over", id=doc_id))
            return
        if kind == "orphan_sidecar" and sha256_file(orig) != meta.sha256:
            where = _park_sidecar(
                archive, doc_id, N_("SHA-256 in the sidecar does not match the original")
            )
            actions.append(
                _(
                    "%(id)s: sidecar does not match the original, moved to %(path)s",
                    id=doc_id,
                    path=where,
                )
            )
            return
        _load_sidecar_into_db(archive, meta)
        if kind == "orphan_sidecar" and meta.text_status == "pending":
            jobs.enqueue(conn, "process", doc_id, {"stages": ["extract", "classify"]})
        actions.append(_("%(id)s: loaded from the sidecar", id=doc_id))
    elif kind == "missing_sidecar":
        row = conn.execute("SELECT metadata_json FROM documents WHERE id=?", (doc_id,)).fetchone()
        meta = DocumentMetadata.model_validate_json(row[0])
        with write_tx(conn):
            docs.persist(archive, meta, bump=False)
        actions.append(_("%(id)s: sidecar rewritten from the database", id=doc_id))
    elif kind == "unfinished_processing":
        jobs.enqueue(conn, "process", doc_id, {"stages": ["extract", "classify"]})
        actions.append(_("%(id)s: processing queued again", id=doc_id))
    elif kind == "orphan_original" and adopt_orphans:
        from .ingest import ingest_path

        p = archive.paths.resolve(issue["detail"])
        res = ingest_path(archive, p, "import", source_details={"repair": True})
        actions.append(
            _("%(path)s: added as a document (%(status)s)", path=issue["detail"], status=res.status)
        )


def _finish_repair(archive: Archive, report: dict, actions: list[str]) -> None:
    conn = archive.conn
    if any(i["kind"] == "index_mismatch" for i in report["issues"]) or actions:
        with write_tx(conn):
            fts.rebuild(conn)
        actions.append(_("Search index rebuilt"))
    # stale temp files from interrupted uploads
    cutoff = time.time() - 3600
    for p in archive.paths.tmp.glob("*"):
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                actions.append(_("tmp/%(name)s removed", name=p.name))
    n = jobs.requeue_expired(conn)
    if n:
        actions.append(
            ngettext(
                "%(num)d interrupted job queued again", "%(num)d interrupted jobs queued again", n
            )
        )


def _load_sidecar_into_db(archive: Archive, meta: DocumentMetadata) -> None:
    conn = archive.conn
    f = docs.files(archive, meta.id)
    with write_tx(conn):
        docs.persist(archive, meta, bump=False, create=True)
        if f.text_pages.exists():
            docs.write_text(archive, meta.id, TextPages.model_validate(read_json(f.text_pages)))
            fts.index_document(conn, meta.id)
        for col in ("ingest_sequence", "filing_sequence"):
            top = conn.execute(f"SELECT COALESCE(MAX({col}),0) FROM documents").fetchone()[0]
            cur = int(get_meta(conn, f"last_{col}", "0") or 0)
            set_meta(conn, f"last_{col}", str(max(top, cur)))


def reindex(archive: Archive) -> int:
    with write_tx(archive.conn):
        return fts.rebuild(archive.conn)


def rebuild_db(archive: Archive) -> dict[str, Any]:
    """Recreate all document data in SQLite from sidecar files (+ taxonomy.json).

    Operational state (users, API tokens, jobs, IMAP cursors, the global ingest log) is kept if
    the database still exists; it cannot be recovered from the sidecars alone.
    """
    conn = archive.conn
    loaded, errors = 0, []
    with write_tx(conn):
        for table in (
            "doc_fts", "document_tags", "custom_field_values", "document_text", "title_proposals",
            "documents", "taxonomy_alias", "taxonomy",
        ):  # fmt: skip
            conn.execute(f"DELETE FROM {table}")
    tf = tax.read_sidecar(archive.paths)
    if tf:
        tax.load_file(conn, tf)
    metas: list[DocumentMetadata] = []
    for d in sorted(archive.paths.documents.glob("*/metadata.json")):
        try:
            metas.append(DocumentMetadata.model_validate(read_json(d)))
        except Exception as e:
            errors.append(f"{d.parent.name}: {str(e)[:200]}")
    metas.sort(key=lambda m: m.ingest_sequence)
    for meta in metas:
        try:
            _load_sidecar_into_db(archive, meta)
            loaded += 1
        except Exception as e:
            errors.append(f"{meta.id}: {type(e).__name__}: {str(e)[:200]}")
    with write_tx(conn):
        tax.write_sidecar(conn, archive.paths)
    from .trash import rebuild as rebuild_trash

    trashed = rebuild_trash(archive)
    return {"documents": loaded, "errors": errors, "trash": trashed}


# --- export ----------------------------------------------------------------------------


def _unique_name(parent: Path, prefix: str) -> str:
    """Timestamped name that does not exist yet (two runs within one second)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name, n = f"{prefix}-{stamp}", 1
    while any(parent.glob(name + "*")) or any(parent.glob("." + name + "*")):
        name = f"{prefix}-{stamp}-{n}"
        n += 1
    return name


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_archive(
    archive: Archive, dest_parent: Path, *, as_zip: bool = False, progress=None
) -> Path:
    conn = archive.conn
    dest_parent.mkdir(parents=True, exist_ok=True)
    name = _unique_name(dest_parent, "heftig-export")
    work = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=dest_parent))
    try:
        conn.execute("BEGIN")  # read snapshot (WAL)
        try:
            rows = conn.execute(
                "SELECT id, metadata_json, original_relpath FROM documents ORDER BY ingest_sequence"
            ).fetchall()
            taxonomy = tax.export_file(conn).model_dump()
            events = [dict(r) for r in conn.execute("SELECT * FROM ingest_events ORDER BY id")]
            imap_state = [dict(r) for r in conn.execute("SELECT * FROM imap_state")]
            imap_items = [dict(r) for r in conn.execute("SELECT * FROM imap_items")]
            open_jobs = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, kind, doc_id, payload, status, stage, attempts, error, created_at "
                    "FROM jobs WHERE status != 'done' ORDER BY id"
                )
            ]
            users = [dict(r) for r in conn.execute("SELECT username, created_at FROM users")]
            tokens = [
                dict(r)
                for r in conn.execute(
                    "SELECT name, token_prefix, created_at, last_used_at, revoked_at "
                    "FROM api_tokens"
                )
            ]
            sequences = {
                "last_ingest_sequence": int(get_meta(conn, "last_ingest_sequence", "0") or 0),
                "last_filing_sequence": int(get_meta(conn, "last_filing_sequence", "0") or 0),
            }
        finally:
            conn.execute("COMMIT")

        missing: list[dict[str, str]] = []
        exported = 0
        with open(work / "metadata.jsonl", "w", encoding="utf-8") as jl:
            for i, r in enumerate(rows):
                meta = json.loads(r["metadata_json"])
                src_orig = archive.paths.resolve(r["original_relpath"])
                if not src_orig.exists():
                    # one damaged document must not block the export of all others
                    missing.append({"id": r["id"], "original_relpath": r["original_relpath"]})
                    continue
                jl.write(json.dumps(meta, ensure_ascii=False) + "\n")
                exported += 1
                dst_orig = work / r["original_relpath"]
                dst_orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_orig, dst_orig)
                ddir = work / "documents" / r["id"]
                _write_json(ddir / "metadata.json", meta)
                f = docs.files(archive, r["id"])
                for src in (f.text_md, f.text_pages):
                    if src.exists():
                        shutil.copyfile(src, ddir / src.name)
                for att in meta.get("attachments") or []:
                    src = archive.paths.resolve(att["relpath"])
                    dst = work / att["relpath"]
                    if src.exists() and not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
                if progress:
                    progress(i / max(1, len(rows)))
        _write_json(work / "taxonomy.json", taxonomy)
        saved = saved_searches.load(archive.paths)
        if saved:
            _write_json(work / saved_searches.FILENAME, {"version": 1, "searches": saved})
        _write_json(work / "state" / "sequences.json", sequences)
        _write_json(work / "state" / "imap_state.json", imap_state)
        _write_json(work / "state" / "users.json", {"users": users, "api_tokens": tokens})
        with open(work / "state" / "ingest_events.jsonl", "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with open(work / "state" / "imap_items.jsonl", "w", encoding="utf-8") as f:
            for e in imap_items:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with open(work / "state" / "open_jobs.jsonl", "w", encoding="utf-8") as f:
            for e in open_jobs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        (work / "README.txt").write_text(EXPORT_README, encoding="utf-8")
        from .models import DocumentMetadata as _DM

        _write_json(work / "metadata.schema.json", _DM.model_json_schema())

        files = []
        for p in iter_files(work):
            rel = str(p.relative_to(work))
            files.append({"path": rel, "sha256": sha256_file(p), "size": p.stat().st_size})
        manifest = {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "metadata_schema_version": METADATA_SCHEMA_VERSION,
            "app_version": __version__,
            "created_at": now_iso(),
            "document_count": exported,
            "missing_originals": missing,
            "files": files,
        }
        _write_json(work / "manifest.json", manifest)

        if as_zip:
            final = dest_parent / f"{name}.zip"
            tmpzip = dest_parent / f".{name}.zip.tmp"
            with zipfile.ZipFile(tmpzip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in iter_files(work):
                    rel = str(p.relative_to(work))
                    ctype = (
                        zipfile.ZIP_STORED if rel.startswith("originals/") else zipfile.ZIP_DEFLATED
                    )
                    zf.write(p, f"{name}/{rel}", compress_type=ctype)
            os.chmod(tmpzip, 0o600)  # a full copy of the archive: for the owner only
            os.replace(tmpzip, final)
            shutil.rmtree(work)
        else:
            final = dest_parent / name
            os.replace(work, final)
        with write_tx(conn):
            set_meta(conn, "last_export_at", now_iso())
            set_meta(conn, "last_export_path", final.name)
        return final
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


# --- import ----------------------------------------------------------------------------


def _safe_extract(zip_path: Path, dest: Path, max_total: int) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        total = 0
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise MaintenanceError(N_("Unsafe path in the ZIP: %(path)s") % {"path": name})
            if info.flag_bits & 0x1:
                raise MaintenanceError(N_("Encrypted ZIP files are not supported"))
            total += info.file_size
            if total > max_total:
                raise MaintenanceError(N_("The ZIP is too large when unpacked"))
            if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
                raise MaintenanceError(
                    N_("Suspicious compression ratio for %(path)s (zip bomb?)") % {"path": name}
                )
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise MaintenanceError(
                    N_("Unsafe path in the ZIP: %(path)s") % {"path": info.filename}
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                written = 0
                while chunk := src.read(1024 * 1024):
                    written += len(chunk)
                    if written > info.file_size:
                        raise MaintenanceError(N_("ZIP entry larger than stated"))
                    out.write(chunk)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if (dest / "manifest.json").exists():
        return dest
    if len(roots) == 1 and (roots[0] / "manifest.json").exists():
        return roots[0]
    raise MaintenanceError(N_("manifest.json not found"))


def import_archive(archive: Archive, src: Path, progress=None) -> dict[str, Any]:
    conn = archive.conn
    report: dict[str, Any] = {
        "imported": 0,
        "unchanged": 0,
        "conflicts": [],
        "renumbered": [],
        "warnings": [],
    }
    tmpdir = None
    try:
        if src.is_file() and zipfile.is_zipfile(src):
            tmpdir = Path(tempfile.mkdtemp(prefix="import-", dir=archive.paths.tmp))
            root = _safe_extract(src, tmpdir, max_total=500 * 1024**3)
        elif (src / "manifest.json").exists():
            root = src
        else:
            raise MaintenanceError(N_("Not a Heftig export (manifest.json missing)"))
        manifest = read_json(root / "manifest.json")
        if manifest.get("format") != EXPORT_FORMAT:
            raise MaintenanceError(N_("Unknown export format"))
        if manifest.get("format_version", 0) > EXPORT_FORMAT_VERSION:
            raise MaintenanceError(N_("The export comes from a newer Heftig version"))
        # verify every file before touching the archive
        for entry in manifest["files"]:
            p = (root / entry["path"]).resolve()
            if root.resolve() not in p.parents:
                raise MaintenanceError(
                    N_("Unsafe path in the manifest: %(path)s") % {"path": entry["path"]}
                )
            if not p.exists() or sha256_file(p) != entry["sha256"]:
                raise MaintenanceError(N_("Checksum mismatch: %(path)s") % {"path": entry["path"]})

        target_was_empty = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        tf = TaxonomyFile.model_validate(read_json(root / "taxonomy.json"))
        report["taxonomy"] = tax.load_file(conn, tf)
        with write_tx(conn):
            tax.write_sidecar(conn, archive.paths)
        if (root / saved_searches.FILENAME).exists():
            data = read_json(root / saved_searches.FILENAME)
            report["saved_searches"] = saved_searches.merge(
                archive.paths, data.get("searches", []) if isinstance(data, dict) else []
            )

        lines = (root / "metadata.jsonl").read_text(encoding="utf-8").splitlines()
        metas = [DocumentMetadata.model_validate_json(ln) for ln in lines if ln.strip()]
        metas.sort(key=lambda m: m.ingest_sequence)
        for i, meta in enumerate(metas):
            _import_one(archive, root, meta, report)
            if progress:
                progress(i / max(1, len(metas)))

        state = root / "state"
        with write_tx(conn):
            seq = read_json(state / "sequences.json") if (state / "sequences.json").exists() else {}
            for key in ("last_ingest_sequence", "last_filing_sequence"):
                cur = int(get_meta(conn, key, "0") or 0)
                set_meta(conn, key, str(max(cur, int(seq.get(key, 0)))))
            if target_was_empty and (state / "ingest_events.jsonl").exists():
                for ln in (state / "ingest_events.jsonl").read_text(encoding="utf-8").splitlines():
                    if not ln.strip():
                        continue
                    e = json.loads(ln)
                    conn.execute(
                        "INSERT INTO ingest_events(doc_id, sha256, source, source_details, "
                        "filename, result, message, import_ref, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (e.get("doc_id"), e.get("sha256"), e["source"], e.get("source_details", "{}"),
                         e.get("filename", ""), e["result"], e.get("message", ""),
                         e.get("import_ref"), e["created_at"]),
                    )  # fmt: skip
            if (state / "imap_state.json").exists():
                for st in read_json(state / "imap_state.json"):
                    conn.execute(
                        "INSERT OR IGNORE INTO imap_state(account, mailbox, uidvalidity, last_uid, "
                        "last_poll_at, last_error) VALUES(?,?,?,?,?,?)",
                        (st["account"], st["mailbox"], st["uidvalidity"], st["last_uid"],
                         st.get("last_poll_at"), st.get("last_error")),
                    )  # fmt: skip
            if (state / "imap_items.jsonl").exists():
                for ln in (state / "imap_items.jsonl").read_text(encoding="utf-8").splitlines():
                    if ln.strip():
                        it = json.loads(ln)
                        conn.execute(
                            "INSERT OR IGNORE INTO imap_items(account, message_key, part_sha256, "
                            "doc_id, result, created_at) VALUES(?,?,?,?,?,?)",
                            (it["account"], it["message_key"], it["part_sha256"], it.get("doc_id"),
                             it["result"], it["created_at"]),
                        )  # fmt: skip
        users_file = state / "users.json"
        if users_file.exists() and read_json(users_file).get("users"):
            report["warnings"].append(
                N_(
                    "Users and API tokens are not imported for security reasons – please create "
                    "them again with `heftig init` or `heftig token create`."
                )
            )
        from .duplicates import check_document

        for doc_id in report.get("imported_ids", []):
            check_document(archive, doc_id)
        return report
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _import_one(archive: Archive, root: Path, meta: DocumentMetadata, report: dict) -> None:
    conn = archive.conn
    by_id = conn.execute(
        "SELECT sha256, metadata_json FROM documents WHERE id=?", (meta.id,)
    ).fetchone()
    if by_id:
        if by_id["sha256"] != meta.sha256:
            report["conflicts"].append({"id": meta.id, "reason": N_("same ID, different original")})
            return
        existing = DocumentMetadata.model_validate_json(by_id["metadata_json"])
        if _comparable(existing) == _comparable(meta):
            report["unchanged"] += 1
        else:
            report["conflicts"].append(
                {
                    "id": meta.id,
                    "reason": N_("Document exists with different metadata – not overwritten"),
                }  # fmt: skip
            )
        return
    by_sha = conn.execute("SELECT id FROM documents WHERE sha256=?", (meta.sha256,)).fetchone()
    if by_sha:
        report["conflicts"].append(
            {
                "id": meta.id,
                "reason": N_("Original already present as document %(id)s") % {"id": by_sha["id"]},
            }
        )
        return
    src_orig = root / meta.original_relpath
    if sha256_file(src_orig) != meta.sha256:
        report["conflicts"].append({"id": meta.id, "reason": N_("Original checksum wrong")})
        return
    with write_tx(conn):
        taken = conn.execute(
            "SELECT id FROM documents WHERE ingest_sequence=?", (meta.ingest_sequence,)
        ).fetchone()
        if taken:
            old = meta.ingest_sequence
            meta.ingest_sequence = docs.next_sequence(conn, "ingest_sequence")
            report["renumbered"].append(
                {"id": meta.id, "ingest_sequence": [old, meta.ingest_sequence]}
            )
        if meta.filing_sequence is not None:
            taken = conn.execute(
                "SELECT id FROM documents WHERE filing_sequence=?", (meta.filing_sequence,)
            ).fetchone()
            if taken:
                old = meta.filing_sequence
                meta.filing_sequence = docs.next_sequence(conn, "filing_sequence")
                report["renumbered"].append(
                    {"id": meta.id, "filing_sequence": [old, meta.filing_sequence]}
                )
        dest = archive.paths.resolve(meta.original_relpath)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not dest.exists():
            tmp = dest.with_name(f".{dest.name}.import")
            shutil.copyfile(src_orig, tmp)
            os.chmod(tmp, 0o400)
            os.replace(tmp, dest)
        ddir = root / "documents" / meta.id
        f = docs.files(archive, meta.id)
        f.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for att in meta.attachments:
            src_att = root / att.relpath
            if not src_att.exists() or sha256_file(src_att) != att.sha256:
                report["warnings"].append(
                    N_("%(id)s: attachment %(filename)s is missing or damaged")
                    % {"id": meta.id, "filename": att.filename}
                )
                continue
            dst_att = archive.paths.resolve(att.relpath)
            if not dst_att.exists():
                dst_att.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                tmp_att = dst_att.with_name(f".{dst_att.name}.import")
                shutil.copyfile(src_att, tmp_att)
                os.chmod(tmp_att, 0o400)
                os.replace(tmp_att, dst_att)
        docs.persist(archive, meta, bump=False, create=True)
        if (ddir / "text_pages.json").exists():
            pages = TextPages.model_validate(read_json(ddir / "text_pages.json"))
            docs.write_text(archive, meta.id, pages)
            fts.index_document(conn, meta.id)
        conn.execute(
            "INSERT INTO ingest_events(doc_id, sha256, source, source_details, filename, result, "
            "message, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (meta.id, meta.sha256, "import", "{}", meta.original_filename, "imported",
             N_("Imported from an export"), now_iso()),
        )  # fmt: skip
    report["imported"] += 1
    report.setdefault("imported_ids", []).append(meta.id)
    if meta.status in ("queued", "processing") or meta.text_status == "pending":
        jobs.enqueue(conn, "process", meta.id, {"stages": ["extract", "classify"]})
        report.setdefault("requeued", []).append(meta.id)
    try:
        from .media import make_preview

        with write_tx(conn):
            docs.write_preview(
                archive, meta.id,
                make_preview(dest, meta.mime_type, archive.settings.max_image_megapixels),
            )  # fmt: skip
    except Exception:
        log.warning("preview for imported %s failed", meta.id)


def _comparable(meta: DocumentMetadata) -> dict:
    d = meta.model_dump(mode="json")
    # sequences may have been renumbered on import into a non-empty archive
    for k in ("revision", "updated_at", "status", "ingest_sequence", "filing_sequence"):
        d.pop(k, None)
    return d


# --- backup ----------------------------------------------------------------------------


def db_snapshot(archive: Archive) -> Path:
    """Consistent copy of the database inside the archive (for file-level backup tools)."""
    dest = archive.paths.backup / "index-snapshot.sqlite"
    backup_to(archive.conn, dest)
    with write_tx(archive.conn):
        set_meta(archive.conn, "last_db_snapshot_at", now_iso())
    return dest


def snapshot_if_due(archive: Archive) -> Path | None:
    """The worker's automatic snapshot (``db_snapshot_hours``)."""
    hours = archive.settings.db_snapshot_hours
    if hours <= 0:
        return None
    last = parse_iso(get_meta(archive.conn, "last_db_snapshot_at") or "")
    if last and last > utcnow() - timedelta(hours=hours):
        return None
    try:
        return db_snapshot(archive)
    except (OSError, sqlite3.Error):
        log.exception("automatic database snapshot failed")
        return None


def snapshot_overdue(archive: Archive) -> str | None:
    """A warning when the automatic snapshot has not happened for a while (disk full, worker
    not running) - None when all is well or snapshots are switched off."""
    hours = archive.settings.db_snapshot_hours
    if hours <= 0 or not archive.conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
        return None
    last = parse_iso(get_meta(archive.conn, "last_db_snapshot_at") or "")
    if last is None:
        return _("There is no backup copy of the database yet.")
    age = utcnow() - last
    if age < timedelta(hours=2 * hours + 1):
        return None
    if age.days >= 1:
        return ngettext(
            "The last backup copy of the database is %(num)d day old.",
            "The last backup copy of the database is %(num)d days old.",
            age.days,
        )
    return ngettext(
        "The last backup copy of the database is %(num)d hour old.",
        "The last backup copy of the database is %(num)d hours old.",
        int(age.total_seconds() // 3600),
    )


def backup(archive: Archive, dest_parent: Path) -> Path:
    """Full, consistent backup: DB via SQLite backup API + originals + sidecars."""
    dest_parent.mkdir(parents=True, exist_ok=True)
    name = _unique_name(dest_parent, "heftig-backup")
    final = dest_parent / name
    work = dest_parent / f".{name}.tmp"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700)
    try:
        backup_to(archive.conn, work / "index.sqlite")
        root = archive.paths.root
        for sub in ("originals", "documents", "trash", "email", "quarantine"):
            src = root / sub
            if src.exists():
                shutil.copytree(src, work / sub)
        if archive.paths.taxonomy.exists():
            shutil.copyfile(archive.paths.taxonomy, work / "taxonomy.json")
        if (root / saved_searches.FILENAME).exists():
            shutil.copyfile(root / saved_searches.FILENAME, work / saved_searches.FILENAME)
        _write_json(
            work / "backup.json",
            {"created_at": now_iso(), "app_version": __version__, "source": "heftig backup"},
        )
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    with write_tx(archive.conn):
        set_meta(archive.conn, "last_backup_at", now_iso())
    return final


def restore(backup_dir: Path, target: Path) -> None:
    """Restore a `heftig backup` into an empty archive directory."""
    if not (backup_dir / "index.sqlite").exists():
        raise MaintenanceError(_("Not a Heftig backup (index.sqlite missing)"))
    if target.exists() and any(target.iterdir()):
        raise MaintenanceError(_("The target directory is not empty"))
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in backup_dir.iterdir():
        if item.name == "backup.json":
            continue
        dst = target / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copyfile(item, dst)
    os.chmod(target / "index.sqlite", 0o600)


# --- job handlers ----------------------------------------------------------------------


def _job_progress(archive: Archive, job_id: int, stage: str):
    def cb(frac: float) -> None:
        jobs.set_stage(archive.conn, job_id, stage, frac, archive.settings.job_lease_seconds)

    return cb


def _export_job(archive: Archive, job) -> str:
    payload = json.loads(job["payload"] or "{}")
    dest = Path(payload.get("dest") or archive.paths.root / "exports")
    path = export_archive(
        archive, dest, as_zip=bool(payload.get("zip")),
        progress=_job_progress(archive, job["id"], "export"),
    )  # fmt: skip
    jobs.finish(archive.conn, job["id"], "done", result={"path": str(path)})
    return "done"


def _import_job(archive: Archive, job) -> str:
    payload = json.loads(job["payload"] or "{}")
    report = import_archive(
        archive, Path(payload["path"]), progress=_job_progress(archive, job["id"], "import")
    )
    status = "needs_review" if report["conflicts"] else "done"
    jobs.finish(archive.conn, job["id"], status, result=report)
    return status


def _reindex_job(archive: Archive, job) -> str:
    n = reindex(archive)
    jobs.finish(archive.conn, job["id"], "done", result={"documents": n})
    return "done"


def _rebuild_job(archive: Archive, job) -> str:
    report = rebuild_db(archive)
    status = "needs_review" if report["errors"] else "done"
    jobs.finish(archive.conn, job["id"], status, result=report)
    return status


def _titles_job(archive: Archive, job) -> str:
    from .titles import job_handler

    return job_handler(archive, job)


JOB_HANDLERS = {
    "titles": _titles_job,
    "export": _export_job,
    "import": _import_job,
    "reindex": _reindex_job,
    "rebuild": _rebuild_job,
}


def _ai_since(conn) -> str | None:
    from .processing import ai_unreachable_since

    return ai_unreachable_since(conn)


def ai_costs(conn) -> dict[str, Any]:
    """Recorded AI usage (list-price estimate): today, last 7 days, total, and since when.

    Only calls since the usage recording was introduced are counted.
    """
    from datetime import datetime

    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = local_midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    week = (local_midnight - timedelta(days=6)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def total(since: str | None = None) -> dict[str, Any]:
        where = "input_tokens IS NOT NULL" + (" AND finished_at >= ?" if since else "")
        r = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(input_tokens), 0), "
            f"COALESCE(SUM(output_tokens), 0), COUNT(DISTINCT doc_id), "
            f"COALESCE(SUM(cost_usd IS NULL), 0) FROM processing_runs "
            f"WHERE {where}",
            (since,) if since else (),
        ).fetchone()
        # unpriced: calls to models without a known price (local models, other providers)
        return {"calls": r[0], "usd": round(r[1], 2), "input_tokens": r[2],
                "output_tokens": r[3], "documents": r[4], "unpriced": r[5]}  # fmt: skip

    first = conn.execute(
        "SELECT MIN(finished_at) FROM processing_runs WHERE input_tokens IS NOT NULL"
    ).fetchone()[0]
    return {"today": total(today), "week": total(week), "total": total(), "since": first}


def status(archive: Archive) -> dict[str, Any]:
    conn = archive.conn
    hb = get_meta(conn, "worker_heartbeat")
    alive = False
    if hb:
        from .db import parse_iso

        alive = parse_iso(hb) > utcnow() - timedelta(seconds=90)  # type: ignore[operator]
    return {
        "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "jobs": jobs.counts(conn),
        "worker_heartbeat": hb,
        "worker_alive": alive,
        "consume_status": get_meta(conn, "consume_status"),
        "folder_status": get_meta(conn, "folder_status"),
        "ai_unreachable_since": _ai_since(conn),
        "ai_pending": conn.execute(
            'SELECT COUNT(*) FROM documents WHERE metadata_json LIKE \'%"ai_pending": ["%\''
        ).fetchone()[0],
        "last_export_at": get_meta(conn, "last_export_at"),
        "last_export_path": get_meta(conn, "last_export_path"),
        "last_backup_at": get_meta(conn, "last_backup_at"),
        "last_db_snapshot_at": get_meta(conn, "last_db_snapshot_at"),
        "imap": [dict(r) for r in conn.execute("SELECT * FROM imap_state")],
        "ai_costs": ai_costs(conn),
    }
