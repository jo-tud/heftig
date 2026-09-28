import json

from heftig import documents as docs
from heftig import jobs
from heftig.config import Settings
from heftig.processing import reprocess
from heftig.providers import registry
from heftig.providers.base import ProviderError
from heftig.search import SearchParams, search
from heftig.worker import run_until_idle

from .conftest import FakeExtractor, ScriptedClassifier, ingest_bytes, process_all
from .helpers import scan_pdf, text_pdf


def test_embedded_text_needs_no_ocr(archive):
    fake = FakeExtractor()
    registry.override(extractor=fake)
    r = ingest_bytes(
        archive, text_pdf(["Ein langer eingebetteter Text " * 3, "Seite zwei " * 5]), "t.pdf"
    )
    process_all(archive)
    tp = docs.load_text_pages(archive, r.doc_id)
    assert [p.method for p in tp.pages] == ["embedded", "embedded"]
    assert fake.calls == 0
    assert docs.load_meta(archive, r.doc_id).text_status == "ok"
    text_md = docs.files(archive, r.doc_id).text_md.read_text()
    assert "<!-- page 2 -->" in text_md


def test_partial_ocr_failure_is_visible_and_keeps_other_pages(archive):
    registry.override(
        extractor=FakeExtractor(pages={1: "Erste Seite", 3: "Dritte Seite"}, fail_pages={2})
    )
    r = ingest_bytes(archive, scan_pdf(["1", "2", "3"]), "scan.pdf", source="scanner")
    process_all(archive)
    meta = docs.load_meta(archive, r.doc_id)
    tp = docs.load_text_pages(archive, r.doc_id)
    assert meta.text_status == "partial"
    assert meta.status == "needs_review"
    assert any("1 of 3 pages" in x for x in meta.review_reasons)
    assert tp.pages[1].error and "Seite 2" in tp.pages[1].error
    assert tp.pages[0].text == "Erste Seite" and tp.pages[2].text == "Dritte Seite"
    assert r.doc_id in [i["id"] for i in search(archive.conn, SearchParams(q="Dritte")).items]


def test_without_any_ocr_the_document_stays_usable(tmp_path):
    from heftig.archive import Archive

    from .conftest import make_settings

    a = Archive(make_settings(tmp_path, ocr_provider="none", classify_provider="none"))
    r = ingest_bytes(a, scan_pdf(["Bild"]), "Kaufvertrag Auto.pdf")
    process_all(a)
    meta = docs.load_meta(a, r.doc_id)
    assert meta.text_status == "failed" and meta.status == "failed"
    assert "turned off" in docs.load_text_pages(a, r.doc_id).pages[0].error
    # still findable by filename and editable by hand
    assert [i["id"] for i in search(a.conn, SearchParams(q="Kaufvertrag")).items] == [r.doc_id]
    docs.update_fields(
        a, r.doc_id, {"title": "Kaufvertrag Golf", "correspondent": "Autohaus Beispiel"}
    )
    assert [i["id"] for i in search(a.conn, SearchParams(q="Autohaus")).items] == [r.doc_id]
    a.close()


def test_transient_errors_are_retried_with_backoff(archive):
    fake = FakeExtractor(transient_failures=2)
    registry.override(extractor=fake)
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    run_until_idle(archive)  # backoff 0 in tests -> retried immediately until success
    job = archive.conn.execute("SELECT * FROM jobs WHERE doc_id=?", (r.doc_id,)).fetchone()
    assert job["status"] == "done" and job["attempts"] == 3
    assert docs.load_meta(archive, r.doc_id).text_status == "ok"


def test_backoff_delays_next_attempt(archive):
    archive.settings.job_backoff_seconds = 60
    registry.override(extractor=FakeExtractor(transient_failures=1))
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    run_until_idle(archive)
    job = archive.conn.execute("SELECT * FROM jobs WHERE doc_id=?", (r.doc_id,)).fetchone()
    assert job["status"] == "queued" and job["next_run_at"] > job["updated_at"]
    assert "temporär" in job["error"]


def test_final_attempt_records_error_instead_of_looping(archive):
    archive.settings.job_max_attempts = 2
    registry.override(extractor=FakeExtractor(transient_failures=99))
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    run_until_idle(archive)
    meta = docs.load_meta(archive, r.doc_id)
    assert meta.text_status == "failed"
    job = archive.conn.execute("SELECT * FROM jobs WHERE doc_id=?", (r.doc_id,)).fetchone()
    assert job["status"] == "failed" and job["attempts"] == 2


def test_resume_after_crash_skips_finished_stage(archive):
    fake = FakeExtractor(pages={1: "Brief vom 01.02.2025"})
    clf = ScriptedClassifier(error=ProviderError("überlastet", transient=True))
    registry.override(extractor=fake, classifier=clf)
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    job = jobs.claim(archive.conn, 900)
    from heftig.worker import run_job

    run_job(archive, job)  # extract ok, classify fails transiently -> requeued
    payload = json.loads(
        archive.conn.execute("SELECT payload FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
    )
    assert payload["done"] == ["extract"]
    clf.error = None
    clf.default = {"title": "Fortgesetzt"}
    run_until_idle(archive)
    assert fake.calls == 1  # extraction not repeated
    assert docs.load_meta(archive, r.doc_id).title == "Fortgesetzt"


def test_worker_restart_requeues_interrupted_jobs(archive):
    registry.override(extractor=FakeExtractor())
    r = ingest_bytes(archive, scan_pdf(["x"]), "s.pdf")
    job = jobs.claim(archive.conn, 900)  # worker "dies" while processing
    assert job["status"] == "processing"
    assert run_until_idle(archive) == 0  # lease still valid, nothing to do
    assert jobs.requeue_all_processing(archive.conn) == 1  # what a new worker does on start
    run_until_idle(archive)
    assert docs.load_meta(archive, r.doc_id).status == "done"


def test_provider_failure_keeps_metadata_and_original(archive):
    registry.override(classifier=ScriptedClassifier(default={"title": "KI-Titel"}))
    r = ingest_bytes(archive, text_pdf(["Inhalt eines Briefes " * 5]), "b.pdf")
    process_all(archive)
    docs.update_fields(archive, r.doc_id, {"correspondent": "Beispiel GmbH"})
    before = docs.load_meta(archive, r.doc_id)
    registry.override(classifier=ScriptedClassifier(error=ProviderError("HTTP 400 kaputt")))
    reprocess(archive, [r.doc_id], ["classify"])
    process_all(archive)
    after = docs.load_meta(archive, r.doc_id)
    assert after.title == before.title and after.correspondent == "Beispiel GmbH"
    assert after.processing_history[-1].status == "failed"
    assert any("Classification failed" in x for x in after.review_reasons)
    assert archive.paths.resolve(after.original_relpath).exists()


def test_locked_fields_and_tag_decisions_survive_reprocessing(archive):
    text = "Versicherung Beispiel AG\nBeitragsrechnung\nDatum: 03.03.2025"
    first = {
        "title": "KI Titel 1",
        "correspondent": "Versicherung Beispiel AG",
        "correspondent_confidence": 0.9,
        "document_type": "Rechnung",
        "document_type_confidence": 0.9,
        "tags": ["Versicherung", "Haus"],
        "summary": "S1",
    }
    registry.override(classifier=ScriptedClassifier(default=first))
    r = ingest_bytes(archive, text_pdf([text]), "r.pdf")
    process_all(archive)
    docs.update_fields(
        archive, r.doc_id, {"title": "Mein Titel", "tags": ["Versicherung", "Wichtig"]}
    )
    m = docs.load_meta(archive, r.doc_id)
    assert m.field_locks == {"title": True}
    assert m.tag_overrides.added == ["Wichtig"] and m.tag_overrides.removed == ["Haus"]
    second = dict(first, title="KI Titel 2", tags=["Versicherung", "Haus", "Neu"], summary="S2",
                  document_type="Beitragsrechnung")  # fmt: skip
    registry.override(classifier=ScriptedClassifier(default=second))
    reprocess(archive, [r.doc_id], ["extract", "classify"])
    process_all(archive)
    m = docs.load_meta(archive, r.doc_id)
    assert m.title == "Mein Titel"  # locked
    assert m.summary == "S2" and m.document_type == "Beitragsrechnung"  # unlocked -> updated
    assert sorted(m.tags) == ["Neu", "Versicherung", "Wichtig"]  # "Haus" stays removed


def test_cloud_provider_needs_explicit_permission(tmp_path):
    from heftig.archive import Archive

    s = Settings(_env_file=None, archive_dir=tmp_path / "a", classify_provider="openai",
                 classify_model="x", classify_api_key="sk-test-not-real", ocr_provider="mock")  # fmt: skip
    a = Archive(s)
    r = ingest_bytes(a, text_pdf(["Ein Text " * 10]), "t.pdf")
    process_all(a)
    meta = docs.load_meta(a, r.doc_id)
    assert any("not allowed" in x for x in meta.review_reasons)
    raw = docs.files(a, r.doc_id).metadata.read_text()
    assert "sk-test-not-real" not in raw
    a.close()
