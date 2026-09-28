import os

import pytest

from heftig import documents as docs
from heftig import maintenance, storage
from heftig.archive import Archive
from heftig.db import write_tx
from heftig.search import SearchParams, search

from .conftest import ingest_bytes, make_settings, process_all
from .corpus import load_corpus
from .helpers import text_pdf


def search_ids(a, q):
    return [i["id"] for i in search(a.conn, SearchParams(q=q, per_page=50)).items]


def test_check_clean_and_detects_corruption(archive):
    r = ingest_bytes(archive, text_pdf(["x"]), "x.pdf")
    process_all(archive)
    assert maintenance.check(archive)["ok"]
    p = archive.paths.resolve(docs.load_meta(archive, r.doc_id).original_relpath)
    os.chmod(p, 0o600)
    p.write_bytes(p.read_bytes() + b"manipuliert")
    kinds = {i["kind"] for i in maintenance.check(archive)["issues"]}
    assert "hash_mismatch" in kinds


def test_repair_after_crash_between_sidecar_and_db(archive):
    """Simulates a crash after metadata.json was written but before the DB commit."""
    r = ingest_bytes(archive, text_pdf(["Rechnung"]), "r.pdf")
    process_all(archive)
    meta = docs.load_meta(archive, r.doc_id)
    meta.title = "Nach dem Absturz"
    meta.revision += 1
    storage.atomic_write_json(docs.files(archive, r.doc_id).metadata, meta.model_dump(mode="json"))
    kinds = {i["kind"] for i in maintenance.check(archive)["issues"]}
    assert kinds == {"revision_mismatch"}
    rep = maintenance.repair(archive)
    assert not rep["remaining_issues"]
    assert search_ids(archive, "Absturz") == [r.doc_id]


def test_repair_adopts_orphan_sidecar_and_requeues(archive):
    r = ingest_bytes(archive, text_pdf(["Verwaist"]), "v.pdf")
    # crash right after the sidecar: DB transaction never committed
    with write_tx(archive.conn):
        archive.conn.execute("DELETE FROM doc_fts")
        archive.conn.execute("DELETE FROM documents WHERE id=?", (r.doc_id,))
        archive.conn.execute("DELETE FROM jobs")
    assert "orphan_sidecar" in {i["kind"] for i in maintenance.check(archive)["issues"]}
    maintenance.repair(archive)
    process_all(archive)
    assert docs.load_meta(archive, r.doc_id).status == "done"
    assert search_ids(archive, "Verwaist") == [r.doc_id]
    assert maintenance.check(archive)["ok"]


def test_repair_requeues_unfinished_processing(archive):
    r = ingest_bytes(archive, text_pdf(["x"]), "x.pdf")
    with write_tx(archive.conn):
        archive.conn.execute("DELETE FROM jobs")
    assert "unfinished_processing" in {i["kind"] for i in maintenance.check(archive)["issues"]}
    maintenance.repair(archive)
    process_all(archive)
    assert docs.load_meta(archive, r.doc_id).status == "done"


def test_repair_reports_and_adopts_orphan_original(archive):
    data = text_pdf(["Original ohne Dokument"])
    import hashlib

    sha = hashlib.sha256(data).hexdigest()
    p = archive.paths.resolve(archive.paths.original_relpath(sha, "pdf"))
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    assert "orphan_original" in {i["kind"] for i in maintenance.check(archive)["issues"]}
    maintenance.repair(archive, adopt_orphans=True)
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_rebuild_db_from_sidecars_after_losing_database(tmp_path):
    s = make_settings(tmp_path)
    a = Archive(s)
    ids = load_corpus(a)
    docs.mark_filed(a, ids["tickets.pdf"])
    before = {q: search_ids(a, q) for q in ("Telekomm Rechnung", "83729381", "")}
    a.close()
    for f in a.paths.root.glob("index.sqlite*"):
        f.unlink()
    b = Archive(s)
    rep = maintenance.rebuild_db(b)
    assert rep["documents"] == len(ids) and not rep["errors"]
    assert {q: search_ids(b, q) for q in before} == before
    assert docs.load_meta(b, ids["tickets.pdf"]).filing_sequence is not None
    new = ingest_bytes(b, text_pdf(["neu"]), "neu.pdf")
    seqs = [r[0] for r in b.conn.execute("SELECT ingest_sequence FROM documents")]
    assert len(seqs) == len(set(seqs)) and docs.load_meta(b, new.doc_id).ingest_sequence == max(
        seqs
    )
    b.close()


def test_backup_and_restore_on_fresh_volume(tmp_path):
    s = make_settings(tmp_path / "src")
    a = Archive(s)
    ids = load_corpus(a)
    before = {q: search_ids(a, q) for q in ("Allianz Versicherung 2025", "Krankenversicherung")}
    backup_dir = maintenance.backup(a, tmp_path / "backups")
    a.close()
    target = tmp_path / "restored" / "archive"
    maintenance.restore(backup_dir, target)
    b = Archive(make_settings(tmp_path / "restored"))
    assert b.paths.root == target
    assert maintenance.check(b)["ok"]
    assert b.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == len(ids)
    assert {q: search_ids(b, q) for q in before} == before
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.restore(backup_dir, target)  # never over a non-empty target
    b.close()


def test_db_snapshot_is_consistent(archive):
    import sqlite3

    ingest_bytes(archive, text_pdf(["x"]), "x.pdf")
    path = maintenance.db_snapshot(archive)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    c.close()


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "meta.json"
    storage.atomic_write_json(target, {"v": 1})

    def boom(*a):
        raise OSError("Stromausfall")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        storage.atomic_write_json(target, {"v": 2})
    monkeypatch.undo()
    assert storage.read_json(target) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["meta.json"]


def test_paths_cannot_escape_archive(archive):
    for bad in ("../x", "/etc/passwd", "originals/../../x"):
        with pytest.raises(storage.UnsafePathError):
            archive.paths.resolve(bad)
    with pytest.raises(ValueError):
        archive.paths.doc_dir("../../etc")
    with pytest.raises(storage.UnsafePathError):
        archive.paths.original_relpath("../" + "a" * 62, "pdf")


def test_archive_permissions_are_restrictive(archive):
    assert oct(archive.paths.root.stat().st_mode & 0o777) == "0o700"
    assert oct(archive.paths.db.stat().st_mode & 0o777) == "0o600"


def test_raw_response_retention(archive):
    from heftig.processing import prune_raw_responses

    with write_tx(archive.conn):
        archive.conn.execute(
            "INSERT INTO processing_runs(doc_id, task, provider, status, raw_response, started_at) "
            "VALUES('x','classify','p','ok','{...}','2000-01-01T00:00:00Z')"
        )
    assert prune_raw_responses(archive) == 1


def test_repair_after_crash_and_reingest_does_not_abort(archive):
    """Crash after metadata.json: the DB commit (incl. sequence counter) is lost."""
    import shutil

    r1 = ingest_bytes(archive, text_pdf(["Doppelt"]), "a.pdf")
    seq1 = docs.load_meta(archive, r1.doc_id).ingest_sequence
    # (1) leftover sidecar of the same file that was later ingested again
    ghost = archive.paths.documents / "00000000-0000-4000-8000-000000000001"
    shutil.copytree(docs.files(archive, r1.doc_id).dir, ghost)
    meta = storage.read_json(ghost / "metadata.json")
    meta["id"] = ghost.name
    storage.atomic_write_json(ghost / "metadata.json", meta)
    # (2) a new document whose commit was lost; its sequence number was reused meanwhile
    r2 = ingest_bytes(archive, text_pdf(["Verloren"]), "b.pdf")
    with write_tx(archive.conn):
        archive.conn.execute("DELETE FROM doc_fts")
        archive.conn.execute("DELETE FROM documents WHERE id=?", (r2.doc_id,))
    m2 = storage.read_json(docs.files(archive, r2.doc_id).metadata)
    m2["ingest_sequence"] = seq1
    storage.atomic_write_json(docs.files(archive, r2.doc_id).metadata, m2)
    # (3) a sidecar whose hash does not match the original path it claims
    ghost3 = archive.paths.documents / "00000000-0000-4000-8000-000000000003"
    shutil.copytree(docs.files(archive, r1.doc_id).dir, ghost3)
    m3 = storage.read_json(ghost3 / "metadata.json")
    m3.update(id=ghost3.name, sha256="f" * 64)
    storage.atomic_write_json(ghost3 / "metadata.json", m3)

    rep = maintenance.repair(archive)
    text = " ".join(rep["actions"])
    assert "duplicate sidecar" in text and "invalid sidecar" in text
    assert f"arrival number {seq1} →" in text
    assert not rep["remaining_issues"]
    assert (archive.paths.quarantine / f"sidecar-{ghost.name}" / "reason.json").exists()
    seqs = [r[0] for r in archive.conn.execute("SELECT ingest_sequence FROM documents")]
    assert len(seqs) == 2 and len(set(seqs)) == 2
    process_all(archive)
    assert search_ids(archive, "Verloren") == [r2.doc_id]
