"""Papierkorb: deleting a document moves it here; it can be restored until it is purged.

Layout: the document's sidecar folder moves from ``documents/<id>/`` to ``trash/<id>/``
(metadata.json then carries ``trashed_at``, ``trash_reason``, ``trash_batch``); the original and
attachments stay where they are. The ``trash`` table mirrors the folder and is rebuilt from it.
A trashed document is completely out of the documents table, so search, facets, duplicates,
exports and the MCP connection only ever see live documents.

Purging - after ``trash_retention_days`` automatically, or explicitly - deletes the folder and
the original / attachments that no other document (live or trashed) references.

Source documents work the same way, but are never purged: the documents that were combined
into another one, and the original of a split document, move to ``sources/<id>/``
(metadata.json then carries ``replaced_at`` and ``replaced_by``) and get a row in the same
table with ``kind = 'source'``. They stay until the user restores them (undoing the combining
or splitting) or deletes them on purpose - which moves them into the Papierkorb like any
deleted document.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from datetime import timedelta
from typing import Any

from . import documents as docs
from . import index as fts
from . import jobs
from .archive import Archive
from .db import iso, now_iso, parse_iso, utcnow, write_tx
from .i18n import N_, _
from .models import DocumentMetadata, TextPages
from .storage import atomic_write_bytes, atomic_write_json, fsync_dir, read_json

log = logging.getLogger("heftig.trash")


class TrashError(ValueError):
    pass


def new_batch() -> str:
    return secrets.token_hex(6)


TRASH, SOURCE = "trash", "source"


def _dir(archive: Archive, doc_id: str, kind: str = TRASH):
    base = archive.paths.sources if kind == SOURCE else archive.paths.trash
    return base / docs.files(archive, doc_id).dir.name


def _kind(conn, doc_id: str) -> str:
    row = conn.execute("SELECT kind FROM trash WHERE id=?", (doc_id,)).fetchone()
    return row[0] if row else TRASH


def _merge_leftover(src, leftover) -> None:
    """A trash folder of this id already exists (an interrupted earlier attempt): keep what
    only it has (e.g. the text of a half-deleted document), then remove it."""
    for f in leftover.rglob("*"):
        if f.is_file():
            target = src / f.relative_to(leftover)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(f, target)
    shutil.rmtree(leftover)


def _move(src, dst) -> None:
    os.replace(src, dst)  # same file system: atomic
    fsync_dir(dst.parent)
    fsync_dir(src.parent)


def trash_document(
    archive: Archive,
    doc_id: str,
    reason: str = "",
    batch: str | None = None,
    by: str = "web",
    replaced_by: list[str] | None = None,
) -> dict[str, Any]:
    """Database first, the folder move last (inside the transaction); if anything fails after
    the move, the folder goes back, so a document is never half in the Papierkorb.

    With ``replaced_by`` (the documents it was combined into) the document is kept as a source
    document instead: in sources/, never purged."""
    conn = archive.conn
    kind = SOURCE if replaced_by else TRASH
    src, dst = docs.files(archive, doc_id).dir, _dir(archive, doc_id, kind)
    moved = False
    original_sidecar: bytes | None = None
    try:
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            original_sidecar = (src / "metadata.json").read_bytes()
            row = conn.execute(
                "SELECT rowid, sha256, original_relpath, title, original_filename FROM documents "
                "WHERE id=?",
                (doc_id,),
            ).fetchone()
            if row is None:
                raise docs.DocumentNotFound(doc_id)
            moved_at = now_iso()
            if kind == SOURCE:
                meta.replaced_at = moved_at
                meta.replaced_by = list(replaced_by or [])
            else:
                meta.trashed_at = moved_at
            meta.trash_reason = (reason or "")[:300]
            meta.trash_batch = batch
            data = meta.model_dump(mode="json")
            fts.remove_document(conn, row["rowid"])
            conn.execute(
                "DELETE FROM duplicate_candidates WHERE doc_a=? OR doc_b=?", (doc_id, doc_id)
            )
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.execute(
                "DELETE FROM jobs WHERE doc_id=? AND status IN ('queued','failed')", (doc_id,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO trash(id, sha256, original_relpath, title, trashed_at, "
                "reason, batch, metadata_json, kind) VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, row["sha256"], row["original_relpath"],
                 row["title"] or row["original_filename"], moved_at, meta.trash_reason,
                 batch, json.dumps(data, ensure_ascii=False), kind),
            )  # fmt: skip
            title = row["title"] or row["original_filename"]
            if kind == SOURCE:
                message = N_("“%(title)s” kept as a source document (%(reason)s)") % {
                    "title": title, "reason": reason or "-"
                }  # fmt: skip
            elif reason:
                message = N_("“%(title)s” moved to the trash (%(reason)s)") % {
                    "title": title, "reason": reason
                }  # fmt: skip
            else:
                message = N_("“%(title)s” moved to the trash") % {"title": title}
            result = "replaced" if kind == SOURCE else "deleted"
            _event(conn, doc_id, row["sha256"], row["original_filename"], by, result, message)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                _merge_leftover(src, dst)
            _move(src, dst)
            moved = True
            atomic_write_json(dst / "metadata.json", data)
    except BaseException:
        if moved:
            _move_back(dst, src)
            if original_sidecar is not None:
                atomic_write_bytes(src / "metadata.json", original_sidecar)
        raise
    return {"id": doc_id, "sha256": row["sha256"], "batch": batch}


def _move_back(moved_to, original) -> None:
    """Undo a folder move after a failed transaction (the caller restores the sidecar)."""
    try:
        _move(moved_to, original)
    except OSError:
        log.exception("could not move %s back to %s", moved_to, original)


def _conflicts(conn, meta: DocumentMetadata) -> list[str]:
    """Numbers that another document took meanwhile (e.g. the combined document holds the
    filing position of its parts). The internal arrival number is renumbered silently; a
    taken filing position is released and the user asked to check where the paper is."""
    notes = []
    if conn.execute(
        "SELECT 1 FROM documents WHERE ingest_sequence=?", (meta.ingest_sequence,)
    ).fetchone():
        meta.ingest_sequence = docs.next_sequence(conn, "ingest_sequence")
    if meta.filing_sequence is not None:
        holder = conn.execute(
            "SELECT title, original_filename FROM documents WHERE filing_sequence=?",
            (meta.filing_sequence,),
        ).fetchone()
        if holder:
            meta.filed_at = meta.filing_sequence = meta.filing_section = None
            notes.append(
                N_(
                    "Filing: The filing position now belongs to “%(title)s” – please check "
                    "where the paper is."
                )
                % {"title": holder[0] or holder[1]}
            )
    return notes


def restore(archive: Archive, doc_id: str) -> DocumentMetadata:
    """Back into the archive - from the Papierkorb or from the source documents."""
    conn = archive.conn
    src, dst = _dir(archive, doc_id, _kind(conn, doc_id)), docs.files(archive, doc_id).dir
    moved = False
    original_sidecar: bytes | None = None
    try:
        with write_tx(conn):
            row = conn.execute("SELECT * FROM trash WHERE id=?", (doc_id,)).fetchone()
            if row is None or not (src / "metadata.json").exists():
                raise TrashError(_("No longer in the trash."))
            original_sidecar = (src / "metadata.json").read_bytes()
            meta = DocumentMetadata.model_validate(read_json(src / "metadata.json"))
            other = conn.execute(
                "SELECT title FROM documents WHERE sha256=?", (meta.sha256,)
            ).fetchone()
            if other:
                raise TrashError(
                    _(
                        "The same file is back in the archive (“%(title)s”) – not restored.",
                        title=other[0],
                    )
                )
            if not archive.paths.resolve(meta.original_relpath).exists():
                raise TrashError(_("The original file is missing – cannot be restored."))
            if dst.exists():
                if (dst / "metadata.json").exists():
                    raise TrashError(_("A document with this ID already exists."))
                shutil.rmtree(dst)  # only caches written after the deletion: regenerable
            meta.trashed_at = meta.trash_reason = meta.trash_batch = meta.replaced_at = None
            meta.replaced_by = []
            for note in _conflicts(conn, meta):
                if note not in meta.review_reasons:
                    meta.review_reasons.append(note)
                    meta.status = "needs_review"
            _move(src, dst)
            moved = True
            requeue = _needs_processing(conn, meta)
            if requeue:
                meta.status = "queued"
            docs.persist(archive, meta, create=True)  # the row first: document_text refers to it
            tp_path = dst / "text_pages.json"
            if tp_path.exists():
                tp = TextPages.model_validate(read_json(tp_path))
                text = "\n\n".join(p.text.strip() for p in tp.pages if p.text.strip())
                conn.execute(
                    "INSERT OR REPLACE INTO document_text(doc_id, content) VALUES(?,?)",
                    (doc_id, text),
                )
                fts.index_document(conn, doc_id)
            if requeue:
                jobs.enqueue(
                    conn, "process", doc_id, {"stages": ["extract", "classify"]},
                    max_attempts=archive.settings.job_max_attempts,
                )  # fmt: skip
            conn.execute("DELETE FROM trash WHERE id=?", (doc_id,))
            _event(conn, doc_id, meta.sha256, meta.original_filename, "web", "restored",
                   N_("“%(title)s” restored") % {"title": meta.title or meta.original_filename})  # fmt: skip
    except BaseException:
        if moved:
            _move_back(dst, src)
            if original_sidecar is not None:
                atomic_write_bytes(src / "metadata.json", original_sidecar)
        raise
    # back in the archive: if its paper had stayed in the binder, the document holds the place
    from . import binders

    binders.sheet_taken_out(archive, doc_id)
    return meta


def _needs_processing(conn, meta: DocumentMetadata) -> bool:
    """Deleted while it was waiting or being processed: its job is gone (or failed)."""
    if meta.status not in ("queued", "processing") and meta.text_status != "pending":
        return False
    return not conn.execute(
        "SELECT 1 FROM jobs WHERE doc_id=? AND status IN ('queued','processing')", (meta.id,)
    ).fetchone()


def restore_batch(archive: Archive, batch: str) -> dict[str, Any]:
    ids = [r[0] for r in archive.conn.execute("SELECT id FROM trash WHERE batch=?", (batch,))]
    done, failed = [], []
    for doc_id in ids:
        try:
            restore(archive, doc_id)
            done.append(doc_id)
        except TrashError as e:
            failed.append(str(e))
    return {"restored": len(done), "ids": done, "failed": failed}


def purge(archive: Archive, doc_id: str) -> None:
    """Delete for good: folder, and original/attachments nothing else references. The check
    and the deletion happen under the write lock, so an upload of the same file in between
    (which reuses the existing original) cannot lose it."""
    conn = archive.conn
    with write_tx(conn):
        row = conn.execute("SELECT * FROM trash WHERE id=?", (doc_id,)).fetchone()
        if row is None or row["kind"] != TRASH:  # a source document is never purged
            raise TrashError(_("Not in the trash."))
        meta = json.loads(row["metadata_json"])
        conn.execute("DELETE FROM trash WHERE id=?", (doc_id,))
        paths = [row["original_relpath"]] + [a["relpath"] for a in meta.get("attachments") or []]
        for rel in dict.fromkeys(paths):
            if not is_referenced(archive, rel):
                p = archive.paths.resolve(rel)
                if p.exists():
                    p.unlink()
    shutil.rmtree(_dir(archive, doc_id), ignore_errors=True)


def purge_expired(archive: Archive) -> int:
    adopt_combined(archive)  # archives from before the source documents: first keep those
    cutoff = iso(utcnow() - timedelta(days=archive.settings.trash_retention_days))
    ids = [
        r[0]
        for r in archive.conn.execute(
            "SELECT id FROM trash WHERE kind='trash' AND trashed_at < ?", (cutoff,)
        )
    ]
    for doc_id in ids:
        purge(archive, doc_id)
    return len(ids)


def empty(archive: Archive) -> int:
    adopt_combined(archive)
    ids = [r[0] for r in archive.conn.execute("SELECT id FROM trash WHERE kind='trash'")]
    for doc_id in ids:
        purge(archive, doc_id)
    return len(ids)


def is_referenced(archive: Archive, relpath: str) -> bool:
    conn = archive.conn
    pattern = f'%"{relpath}"%'
    return bool(
        conn.execute("SELECT 1 FROM documents WHERE original_relpath=?", (relpath,)).fetchone()
        or conn.execute("SELECT 1 FROM trash WHERE original_relpath=?", (relpath,)).fetchone()
        or conn.execute(
            "SELECT 1 FROM documents WHERE metadata_json LIKE ? LIMIT 1", (pattern,)
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM trash WHERE metadata_json LIKE ? LIMIT 1", (pattern,)
        ).fetchone()
    )


def referenced_files(archive: Archive) -> set[str]:
    """Originals and attachments held by trashed and source documents (integrity check)."""
    out: set[str] = set()
    for rel, mj in archive.conn.execute("SELECT original_relpath, metadata_json FROM trash"):
        out.add(rel)
        out.update(a["relpath"] for a in json.loads(mj).get("attachments") or [])
    return out


def listing(archive: Archive) -> list[dict[str, Any]]:
    """Trash grouped: one entry per bulk batch, single deletions on their own; newest first."""
    keep = archive.settings.trash_retention_days
    groups: dict[str, dict[str, Any]] = {}
    for r in archive.conn.execute(
        "SELECT * FROM trash WHERE kind='trash' ORDER BY trashed_at DESC, title"
    ):
        key = r["batch"] or r["id"]
        g = groups.setdefault(key, {"batch": r["batch"], "trashed_at": r["trashed_at"],
                                    "reason": r["reason"], "items": []})  # fmt: skip
        g["items"].append({"id": r["id"], "title": r["title"], "reason": r["reason"]})
    for g in groups.values():
        started = parse_iso(g["trashed_at"]) or utcnow()
        g["purge_at"] = iso(started + timedelta(days=keep))
    return list(groups.values())


def rebuild(archive: Archive) -> int:
    """Recreate the trash table from the trash and sources folders (rebuild-db)."""
    conn = archive.conn
    n = 0
    with write_tx(conn):
        conn.execute("DELETE FROM trash")
        for kind, folder in ((TRASH, archive.paths.trash), (SOURCE, archive.paths.sources)):
            if not folder.exists():
                continue
            for d in sorted(folder.iterdir()):
                f = d / "metadata.json"
                if not f.exists():
                    continue
                add_row(conn, read_json(f), kind)
                n += 1
    return n


def add_row(conn, data: dict[str, Any], kind: str) -> None:
    """The table row of a folder in trash/ or sources/ (from its metadata.json)."""
    moved_at = data.get("replaced_at" if kind == SOURCE else "trashed_at")
    conn.execute(
        "INSERT OR REPLACE INTO trash(id, sha256, original_relpath, title, trashed_at, reason, "
        "batch, metadata_json, kind) VALUES(?,?,?,?,?,?,?,?,?)",
        (data["id"], data["sha256"], data["original_relpath"],
         data.get("title") or data.get("original_filename", ""), moved_at or now_iso(),
         data.get("trash_reason") or "", data.get("trash_batch"),
         json.dumps(data, ensure_ascii=False), kind),
    )  # fmt: skip


# --- source documents --------------------------------------------------------------------


def discard_source(archive: Archive, doc_id: str, by: str = "web") -> None:
    """A source document deleted on purpose: into the Papierkorb (purged after the usual time).
    The only way a source document can ever be purged."""
    conn = archive.conn
    src, dst = _dir(archive, doc_id, SOURCE), _dir(archive, doc_id, TRASH)
    moved = False
    original_sidecar: bytes | None = None
    try:
        with write_tx(conn):
            row = conn.execute(
                "SELECT * FROM trash WHERE id=? AND kind='source'", (doc_id,)
            ).fetchone()
            if row is None or not (src / "metadata.json").exists():
                raise TrashError(_("No longer among the source documents."))
            original_sidecar = (src / "metadata.json").read_bytes()
            meta = DocumentMetadata.model_validate(read_json(src / "metadata.json"))
            meta.trashed_at = now_iso()
            meta.trash_reason = N_("deleted from the source documents")
            meta.trash_batch = meta.replaced_at = None  # replaced_by stays: where it went
            data = meta.model_dump(mode="json")
            conn.execute(
                "UPDATE trash SET kind='trash', trashed_at=?, reason=?, batch=NULL, "
                "metadata_json=? WHERE id=?",
                (meta.trashed_at, meta.trash_reason, json.dumps(data, ensure_ascii=False), doc_id),
            )
            title = row["title"]
            _event(conn, doc_id, row["sha256"], meta.original_filename, by, "deleted",
                   N_("“%(title)s” moved to the trash") % {"title": title})  # fmt: skip
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                _merge_leftover(src, dst)
            _move(src, dst)
            moved = True
            atomic_write_json(dst / "metadata.json", data)
    except BaseException:
        if moved:
            _move_back(dst, src)
            if original_sidecar is not None:
                atomic_write_bytes(src / "metadata.json", original_sidecar)
        raise


def sources_listing(archive: Archive) -> list[dict[str, Any]]:
    """Source documents grouped by what was made from them (``action``: ``combine`` - the
    parts of one combined document - or ``split`` - the original of the parts); newest first.
    ``targets``: those documents still in the archive (empty: deleted meanwhile)."""
    conn = archive.conn
    groups: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM trash WHERE kind='source' ORDER BY trashed_at DESC"):
        meta = json.loads(r["metadata_json"])
        key = r["batch"] or r["id"]
        action = "split" if (r["batch"] or "").startswith("split-") else "combine"
        g = groups.setdefault(key, {"batch": r["batch"], "action": action,
                                    "replaced_at": r["trashed_at"],
                                    "reason": r["reason"], "replaced_by": meta.get("replaced_by") or [],
                                    "items": []})  # fmt: skip
        g["items"].append(
            {"id": r["id"], "title": r["title"], "pages": meta.get("page_count") or 1,
             "document_date": meta.get("document_date"),
             "filename": meta.get("original_filename") or ""}
        )  # fmt: skip
    for g in groups.values():
        g["targets"], order = [], []
        for i in g["replaced_by"]:
            t = conn.execute(
                "SELECT id, title, original_filename, metadata_json FROM documents WHERE id=?", (i,)
            ).fetchone()
            if t is None:
                continue
            g["targets"].append({"id": t["id"], "title": t["title"] or t["original_filename"]})
            details = json.loads(t["metadata_json"]).get("source_details") or {}
            order += [c.get("id") for c in details.get("combined_from") or []]
        # in the order of their pages in the combined document
        g["items"].sort(key=lambda it: order.index(it["id"]) if it["id"] in order else len(order))
    return list(groups.values())


def source_meta(archive: Archive, doc_id: str) -> DocumentMetadata:
    row = archive.conn.execute(
        "SELECT metadata_json FROM trash WHERE id=? AND kind='source'", (doc_id,)
    ).fetchone()
    if row is None:
        raise docs.DocumentNotFound(doc_id)
    return DocumentMetadata.model_validate_json(row[0])


SOURCES_ADOPTED_KEY = "sources_adopted"


def adopt_combined(archive: Archive) -> int:
    """Once, for archives from before the source documents: the parts of combined documents
    that are still in the Papierkorb (batch combine-<id>, the combined document still in the
    archive) become source documents instead of being purged."""
    from .combine import BATCH_PREFIX
    from .db import get_meta, set_meta

    conn = archive.conn
    if get_meta(conn, SOURCES_ADOPTED_KEY):
        return 0
    n = 0
    rows = conn.execute(
        "SELECT id, batch, trashed_at FROM trash WHERE kind='trash' AND batch LIKE ?",
        (BATCH_PREFIX + "%",),
    ).fetchall()
    for r in rows:
        combined = r["batch"][len(BATCH_PREFIX) :]
        if not conn.execute("SELECT 1 FROM documents WHERE id=?", (combined,)).fetchone():
            continue  # the combined document is gone too: the parts stay ordinary trash
        try:
            _to_source(archive, r["id"], combined, r["trashed_at"])
            n += 1
        except Exception:  # noqa: BLE001 - one damaged folder must not stop the others
            log.exception("could not keep %s as a source document", r["id"])
    with write_tx(conn):
        set_meta(conn, SOURCES_ADOPTED_KEY, now_iso())
    return n


def _to_source(archive: Archive, doc_id: str, combined_id: str, moved_at: str) -> None:
    conn = archive.conn
    src, dst = _dir(archive, doc_id, TRASH), _dir(archive, doc_id, SOURCE)
    moved = False
    original_sidecar: bytes | None = None
    try:
        with write_tx(conn):
            original_sidecar = (src / "metadata.json").read_bytes()
            meta = DocumentMetadata.model_validate(read_json(src / "metadata.json"))
            meta.replaced_at, meta.replaced_by, meta.trashed_at = moved_at, [combined_id], None
            data = meta.model_dump(mode="json")
            conn.execute(
                "UPDATE trash SET kind='source', metadata_json=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), doc_id),
            )
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                _merge_leftover(src, dst)
            _move(src, dst)
            moved = True
            atomic_write_json(dst / "metadata.json", data)
    except BaseException:
        if moved:
            _move_back(dst, src)
            if original_sidecar is not None:
                atomic_write_bytes(src / "metadata.json", original_sidecar)
        raise


def _event(conn, doc_id, sha, filename, source, result, message) -> None:
    conn.execute(
        "INSERT INTO ingest_events(doc_id, sha256, source, filename, result, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (doc_id, sha, source if source in ("web", "api") else "web", filename, result, message,
         now_iso()),
    )  # fmt: skip
