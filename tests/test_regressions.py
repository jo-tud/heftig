"""Regression tests for defects found in the code review of 2026-09-28."""

import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from heftig import auth, i18n, jobs, maintenance
from heftig import documents as docs
from heftig import taxonomy as tax
from heftig.archive import Archive
from heftig.consume import ConsumeWatcher
from heftig.db import write_tx
from heftig.models import DocumentMetadata
from heftig.processing import reprocess
from heftig.providers import registry
from heftig.providers.base import ProviderError
from heftig.search import SearchParams, search
from heftig.web.app import create_app
from heftig.worker import Worker, run_job, run_until_idle

from .conftest import FakeExtractor, ScriptedClassifier, ingest_bytes, make_settings, process_all
from .helpers import scan_pdf, text_pdf

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(make_settings(tmp_path))
    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    yield app, c, csrf
    app.state.archive.close()


# 1 ------------------------------------------------------------------------------------------
def test_form_post_from_browser_with_null_origin_is_accepted(web):
    app, c, csrf = web
    r = c.get("/settings")
    assert r.headers["referrer-policy"] == "same-origin"
    # a browser under a no-referrer policy sends "Origin: null" for form posts
    r = c.post("/settings/action", data={"csrf_token": csrf, "action": "reindex"},
               headers={"Origin": "null"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    r = c.post("/settings/action", data={"csrf_token": csrf, "action": "reindex"},
               headers={"Origin": "http://testserver"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    # still rejected: wrong origin, or null origin without a valid token
    r = c.post("/settings/action", data={"csrf_token": csrf, "action": "reindex"},
               headers={"Origin": "http://evil.example"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 403
    r = c.post("/settings/action", data={"csrf_token": "x", "action": "reindex"},
               headers={"Origin": "null"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 403
    r = c.post("/settings/action", data={"csrf_token": "äöü", "action": "reindex"},
               headers={"Origin": "null"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 403  # non-ASCII token: rejected, not a 500


# 2 ------------------------------------------------------------------------------------------
def _upload(c, csrf, text="Hallo Welt Brief"):
    r = c.post("/api/documents", files=[("files", ("a.pdf", text_pdf([text]), "application/pdf"))],
               headers={"X-CSRF-Token": csrf})  # fmt: skip
    return r.json()["results"][0]["document_id"]


def test_stale_edit_form_does_not_wipe_ai_results(web):
    app, c, csrf = web
    doc_id = _upload(c, csrf)
    rendered = docs.load_meta(app.state.archive, doc_id)  # page shown while queued
    registry.override(classifier=ScriptedClassifier(default={
        "correspondent": "Beispiel GmbH", "correspondent_confidence": 0.9, "tags": ["Internet"],
        "summary": "KI"}))  # fmt: skip
    process_all(app.state.archive)
    form = {"csrf_token": csrf, "revision": str(rendered.revision), "title": "Neu",
            "correspondent": "", "document_type": "", "tags": "", "summary": ""}  # fmt: skip
    r = c.post(f"/documents/{doc_id}/edit", data=form, follow_redirects=False)
    assert r.status_code == 303 and "saved" not in r.headers["location"]
    m = docs.load_meta(app.state.archive, doc_id)
    assert m.correspondent == "Beispiel GmbH" and m.tags == ["Internet"] and not m.field_locks


def test_edit_keeps_comma_tags_and_crlf_summary(web):
    app, c, csrf = web
    doc_id = _upload(c, csrf)
    docs.update_fields(app.state.archive, doc_id, {"tags": ["Müller, Hans"], "summary": "a\nb"},
                       locks={"tags": False, "summary": False})  # fmt: skip
    m = docs.load_meta(app.state.archive, doc_id)
    form = {"csrf_token": csrf, "revision": str(m.revision), "title": "Titel neu",
            "correspondent": "", "document_type": "", "tags": "Müller, Hans",
            "summary": "a\r\nb"}  # fmt: skip
    c.post(f"/documents/{doc_id}/edit", data=form, follow_redirects=False)
    m = docs.load_meta(app.state.archive, doc_id)
    assert m.tags == ["Müller, Hans"] and m.summary == "a\nb"
    assert m.field_locks == {"title": True}


# 3 ------------------------------------------------------------------------------------------
def test_rename_to_empty_name_is_refused(web):
    app, c, csrf = web
    doc_id = _upload(c, csrf)
    docs.update_fields(app.state.archive, doc_id, {"correspondent": "Telekom"})
    tid = tax.find_term(app.state.archive.conn, "correspondent", "Telekom")
    r = c.patch(f"/api/taxonomy/{tid}", json={"name": " "}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    r = c.patch(f"/api/taxonomy/{tid}", json={"name": "!!!"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert docs.load_meta(app.state.archive, doc_id).correspondent == "Telekom"


# 4 ------------------------------------------------------------------------------------------
def test_import_refuses_foreign_original_paths(archive, tmp_path):
    r = ingest_bytes(archive, text_pdf(["Opfer"]), "opfer.pdf")
    exp = maintenance.export_archive(archive, tmp_path / "exp")
    line = json.loads((exp / "metadata.jsonl").read_text())
    for evil in ("index.sqlite", "originals/00/" + "0" * 64 + ".pdf"):
        bad = dict(line, id="11111111-1111-4111-8111-111111111111", original_relpath=evil)
        with pytest.raises(ValueError):
            DocumentMetadata.model_validate(bad)
    assert docs.load_meta(archive, r.doc_id)


# 5 ------------------------------------------------------------------------------------------
def test_placeholder_names_and_crashing_jobs_do_not_leave_processing(archive):
    registry.override(classifier=ScriptedClassifier(default={
        "correspondent": "–", "correspondent_confidence": 0.9}))  # fmt: skip
    r = ingest_bytes(archive, text_pdf(["Brief " * 20]), "b.pdf")
    process_all(archive)
    m = docs.load_meta(archive, r.doc_id)
    assert m.correspondent is None and m.status == "done"

    class Boom:
        name = model = target = adapter_version = prompt_version = "boom"

        def classify(self, request):
            raise RuntimeError("unerwartet")

    registry.override(classifier=Boom())
    archive.settings.job_max_attempts = 2
    reprocess(archive, [r.doc_id], ["classify"])
    run_until_idle(archive)
    m = docs.load_meta(archive, r.doc_id)
    assert m.status in ("failed", "needs_review")
    assert any("Processing failed" in x for x in m.review_reasons)


# 6 + 7 + 8 ----------------------------------------------------------------------------------
def test_no_parallel_jobs_for_one_document(archive):
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    j1 = jobs.claim(archive.conn, 900)
    reprocess(archive, [r.doc_id], ["classify"])
    assert jobs.claim(archive.conn, 900) is None  # must wait for j1
    run_job(archive, j1)
    assert jobs.claim(archive.conn, 900) is not None


def test_requested_stage_reruns_when_merged_into_retry_job(archive):
    fake = FakeExtractor()
    registry.override(
        extractor=fake, classifier=ScriptedClassifier(error=ProviderError("x", transient=True))
    )
    archive.settings.job_backoff_seconds = 3600
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    run_until_idle(archive)  # extract ok, classify -> requeued with backoff
    assert fake.calls == 1
    registry.override(classifier=ScriptedClassifier(default={"title": "ok"}))
    reprocess(archive, [r.doc_id], ["extract"])
    run_until_idle(archive)  # runs immediately, re-extracts
    assert fake.calls == 2 and docs.load_meta(archive, r.doc_id).title == "ok"


def test_retry_runs_all_stages_again(archive):
    fake = FakeExtractor(fail_pages={1})
    registry.override(extractor=fake)
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    run_until_idle(archive)
    job = archive.conn.execute("SELECT id, status FROM jobs WHERE doc_id=?", (r.doc_id,)).fetchone()
    assert job["status"] == "failed"
    fake.fail_pages = set()
    assert jobs.retry(archive.conn, job["id"])
    run_until_idle(archive)
    assert fake.calls == 2 and docs.load_meta(archive, r.doc_id).text_status == "ok"


# 9 ------------------------------------------------------------------------------------------
def test_worker_survives_locked_database(archive, monkeypatch):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    w = Worker(archive)

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jobs, "claim", locked)
    with ThreadPoolExecutor(1) as pool:
        w.tick(pool, {})  # must not raise


# 10 -----------------------------------------------------------------------------------------
def test_tag_decisions_follow_rename_and_delete(archive):
    registry.override(classifier=ScriptedClassifier(default={"tags": ["Rechnung", "Strom"]}))
    r = ingest_bytes(archive, text_pdf(["x " * 30]), "a.pdf")
    process_all(archive)
    docs.update_fields(archive, r.doc_id, {"tags": ["Strom"]})  # user removes "Rechnung"
    tid = tax.find_term(archive.conn, "tag", "Rechnung")
    docs.rename_term(archive, tid, "Invoice")
    registry.override(classifier=ScriptedClassifier(default={"tags": ["Invoice", "Strom"]}))
    reprocess(archive, [r.doc_id], ["classify"])
    process_all(archive)
    assert docs.load_meta(archive, r.doc_id).tags == ["Strom"]
    docs.update_fields(archive, r.doc_id, {"tags": ["Strom", "Extra"]})  # user adds "Extra"
    docs.delete_term(archive, tax.find_term(archive.conn, "tag", "Extra"))
    reprocess(archive, [r.doc_id], ["classify"])
    process_all(archive)
    assert docs.load_meta(archive, r.doc_id).tags == ["Strom"]
    assert tax.find_term(archive.conn, "tag", "Extra") is None


# 11 -----------------------------------------------------------------------------------------
def test_import_resumes_unfinished_processing_and_is_idempotent_after_renumbering(
    archive, tmp_path
):
    ingest_bytes(archive, text_pdf(["noch nicht verarbeitet " * 5]), "q.pdf")  # queued
    exp = maintenance.export_archive(archive, tmp_path / "exp")
    target = Archive(make_settings(tmp_path / "t"))
    ingest_bytes(target, text_pdf(["schon da"]), "x.pdf")
    rep = maintenance.import_archive(target, exp)
    assert rep["renumbered"] and rep.get("requeued")
    again = maintenance.import_archive(target, exp)
    assert again["unchanged"] == 1 and not again["conflicts"]
    process_all(target)
    assert all(r[0] == "done" for r in target.conn.execute("SELECT status FROM documents"))
    target.close()


def test_export_skips_document_with_missing_original(archive, tmp_path):
    a = ingest_bytes(archive, text_pdf(["a"]), "a.pdf")
    ingest_bytes(archive, text_pdf(["b"]), "b.pdf")
    p = archive.paths.resolve(docs.load_meta(archive, a.doc_id).original_relpath)
    os.chmod(p, 0o600)
    p.unlink()
    exp = maintenance.export_archive(archive, tmp_path / "exp")
    manifest = json.loads((exp / "manifest.json").read_text())
    assert manifest["document_count"] == 1 and manifest["missing_originals"][0]["id"] == a.doc_id


# 12 -----------------------------------------------------------------------------------------
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_is_given_up_once(archive):
    archive.settings.consume_path.mkdir(parents=True)
    p = archive.settings.consume_path / "gesperrt.pdf"
    p.write_bytes(text_pdf(["x"]))
    os.chmod(p, 0o000)
    os.chmod(archive.paths.quarantine, 0o500)  # quarantine not writable either
    try:
        w = ConsumeWatcher(archive)
        for _ in range(12):
            w.poll()
    finally:
        os.chmod(archive.paths.quarantine, 0o700)
        os.chmod(p, 0o600)
    n = archive.conn.execute(
        "SELECT COUNT(*) FROM ingest_events WHERE result='rejected'"
    ).fetchone()[0]
    assert n == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_undeletable_source_does_not_block_or_duplicate(archive):
    root = archive.settings.consume_path
    (root / "ro").mkdir(parents=True)
    (root / "ro" / "a.pdf").write_bytes(text_pdf(["a"]))
    os.chmod(root / "ro", 0o500)  # read-only share folder
    (root / "b.pdf").write_bytes(text_pdf(["b"]))
    try:
        w = ConsumeWatcher(archive)
        for _ in range(6):
            w.poll()
    finally:
        os.chmod(root / "ro", 0o700)
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    dups = archive.conn.execute(
        "SELECT COUNT(*) FROM ingest_events WHERE result='duplicate'"
    ).fetchone()[0]
    assert dups == 0 and not (root / "b.pdf").exists()
    assert "not removed" in w.last_error


def test_move_mode_never_overwrites(tmp_path):
    a = Archive(make_settings(tmp_path, consume_after="move"))
    root = a.settings.consume_path
    (root / "x").mkdir(parents=True)
    (root / "scan.pdf").write_bytes(text_pdf(["eins"]))
    (root / "x" / "scan.pdf").write_bytes(text_pdf(["zwei"]))
    w = ConsumeWatcher(a)
    w.poll()
    w.poll()
    assert len(list((root / ".heftig-verarbeitet").iterdir())) == 2
    a.close()


# 13 + 14 ------------------------------------------------------------------------------------
def test_non_standard_iso_dates_are_rejected(archive):
    r = ingest_bytes(archive, text_pdf(["x"]), "x.pdf")
    for bad in ("20240115", "2024-W03-1", "2024-01-15T10:00"):
        with pytest.raises(docs.EditError):
            docs.update_fields(archive, r.doc_id, {"document_date": bad})


def test_odd_input_gives_clean_errors(web):
    app, c, csrf = web
    assert c.get("/api/documents", params={"page": "99999999999999999999"}).status_code == 422
    assert c.get("/?page=99999999999999999999").status_code == 200
    assert c.get("/?page=²").status_code == 200
    h = {"accept": "text/html"}
    bogus = "22222222-2222-4222-8222-222222222222"
    assert (
        c.post(f"/documents/{bogus}/edit", data={"csrf_token": csrf}, headers=h).status_code == 404
    )
    assert c.post(f"/documents/{bogus}/action", data={"csrf_token": csrf, "action": "file"},
                  headers=h).status_code == 404  # fmt: skip
    assert c.post("/inbox/action", data={"csrf_token": csrf, "action": "retry_abc"},
                  follow_redirects=False).status_code == 303  # fmt: skip
    r = c.post("/inbox/action", data={"csrf_token": csrf, "action": "reprocess_selected", "doc": bogus},
               headers=h)  # fmt: skip
    assert r.status_code == 404
    assert app.state.archive.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# unverified items that were fixed as well ------------------------------------------------
def test_deleted_document_is_not_resurrected_by_worker(archive):
    r = ingest_bytes(archive, text_pdf(["x " * 30]), "x.pdf")
    meta = docs.load_meta(archive, r.doc_id)
    docs.delete_document(archive, r.doc_id)
    with pytest.raises(docs.DocumentNotFound), write_tx(archive.conn):
        docs.persist(archive, meta)
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_number_typed_with_space_finds_joined_number(archive):
    registry.override(classifier=ScriptedClassifier(default={}))
    r = ingest_bytes(archive, text_pdf(["Vertragsnummer 83729381 " * 3]), "v.pdf")
    process_all(archive)
    assert [i["id"] for i in search(archive.conn, SearchParams(q="8372 9381")).items] == [r.doc_id]


def test_rate_limit_uses_proxy_added_address(tmp_path):
    app = create_app(make_settings(tmp_path, trust_proxy_headers=True))
    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    c = TestClient(app)
    for i in range(5):
        c.post("/api/auth/login", json={"username": "x", "password": "y"},
               headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"})  # fmt: skip
    r = c.post("/api/auth/login", json={"username": "z", "password": "y"},
               headers={"X-Forwarded-For": "10.0.0.99, 203.0.113.9"})  # fmt: skip
    assert r.status_code == 429  # spoofed left-most entries do not reset the per-IP limit
    app.state.archive.close()


def test_automatic_snapshot_and_copy_before_an_update(tmp_path):
    from datetime import timedelta

    from heftig import maintenance
    from heftig.db import iso, set_meta, utcnow, write_tx

    from .conftest import ingest_bytes, make_settings

    a = Archive(make_settings(tmp_path))
    assert maintenance.snapshot_overdue(a) is None  # empty archive: nothing to warn about
    ingest_bytes(a, text_pdf(["Sicherung"]), "s.pdf")
    assert "no backup copy" in maintenance.snapshot_overdue(a)
    snap = maintenance.snapshot_if_due(a)
    assert snap.exists() and maintenance.snapshot_if_due(a) is None  # not due again yet
    assert maintenance.snapshot_overdue(a) is None
    with write_tx(a.conn):
        set_meta(a.conn, "last_db_snapshot_at", iso(utcnow() - timedelta(days=3)))
    assert "3 days old" in maintenance.snapshot_overdue(a)
    with i18n.language("de"):
        assert "vor 3 Tagen" in maintenance.snapshot_overdue(a)
    assert maintenance.snapshot_if_due(a) is not None
    # an update with migrations first copies the database as it was
    # (the database as version 10 left it: without the binder column of migration 11, the
    # embedding tables of migration 12 and the source documents of migration 13)
    a.conn.execute("DROP INDEX idx_trash_kind")
    a.conn.execute("ALTER TABLE trash DROP COLUMN kind")
    a.conn.execute("DROP INDEX idx_documents_binder")
    a.conn.execute("ALTER TABLE documents DROP COLUMN filing_binder")
    a.conn.execute("DROP TABLE doc_embeddings")
    a.conn.execute("DROP TABLE doc_embed_state")
    a.conn.execute("PRAGMA user_version = 10")
    a.close()
    a = Archive(make_settings(tmp_path))
    assert (a.paths.backup / "vor-update-v10.sqlite").exists()
    assert not list(a.paths.backup.glob(".*.tmp"))
    a.close()
