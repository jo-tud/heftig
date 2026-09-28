"""Local fallback when the remote AI provider is unreachable, and automatic catch-up."""

import shutil

import pytest

from heftig import documents as docs
from heftig import maintenance
from heftig.archive import Archive
from heftig.db import get_meta
from heftig.processing import catch_up_ai
from heftig.providers import registry
from heftig.providers.base import ProviderError
from heftig.search import SearchParams, search
from heftig.worker import run_until_idle

from .conftest import FakeExtractor, ScriptedClassifier, ingest_bytes, make_settings
from .helpers import scan_pdf, text_pdf

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract fehlt")


@pytest.fixture
def fb(tmp_path):
    a = Archive(make_settings(tmp_path, ai_fallback=True))
    yield a
    a.close()


class Offline(FakeExtractor):
    name = "anthropic"

    def __init__(self):
        super().__init__(transient_failures=10**6)


@needs_tesseract
def test_ocr_falls_back_to_tesseract_and_is_searchable_at_once(fb):
    registry.override(extractor=Offline(), classifier=ScriptedClassifier(default={}))
    r = ingest_bytes(fb, scan_pdf(["Stadtwerke Beispielstadt\nJahresabrechnung Strom"]), "s.pdf")
    run_until_idle(fb)
    m = docs.load_meta(fb, r.doc_id)
    tp = docs.load_text_pages(fb, r.doc_id)
    assert m.text_status == "ok" and m.ai_pending == ["extract"]
    assert tp.pages[0].provider == "tesseract (fallback)"
    assert [i["id"] for i in search(fb.conn, SearchParams(q="Jahresabrechnung")).items] == [
        r.doc_id
    ]
    assert get_meta(fb.conn, "ai_unreachable_since_ocr")
    # nothing waits in the queue: no retry storm while offline
    assert fb.conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 0


def test_classification_falls_back_to_rules(fb):
    registry.override(
        classifier=ScriptedClassifier(error=ProviderError("keine Verbindung", transient=True))
    )
    r = ingest_bytes(fb, text_pdf(["Rechnung vom 03.09.2026 über 39,95 EUR " * 2]), "r.pdf")
    run_until_idle(fb)
    m = docs.load_meta(fb, r.doc_id)
    assert m.document_date == "2026-09-03" and m.document_type == "Rechnung"
    assert m.ai_pending == ["classify"] and m.status == "done"
    assert m.processing_history[-1].provider == "rules (fallback)"


def test_permanent_errors_do_not_fall_back(fb):
    registry.override(classifier=ScriptedClassifier(error=ProviderError("HTTP 400 ungültig")))
    r = ingest_bytes(fb, text_pdf(["Ein Brief " * 10]), "b.pdf")
    run_until_idle(fb)
    m = docs.load_meta(fb, r.doc_id)
    assert m.ai_pending == [] and any("Classification failed" in x for x in m.review_reasons)


def test_catch_up_when_ai_is_back(fb, monkeypatch):
    registry.override(classifier=ScriptedClassifier(error=ProviderError("offline", transient=True)))
    r = ingest_bytes(fb, text_pdf(["Rechnung vom 03.09.2026 " * 3]), "r.pdf")
    run_until_idle(fb)
    docs.update_fields(fb, r.doc_id, {"title": "Mein Titel"})
    st = maintenance.status(fb)
    assert st["ai_pending"] == 1 and st["ai_unreachable_since"]

    monkeypatch.setattr(registry, "probe_ai", lambda s: False)
    assert catch_up_ai(fb) == {"pending": 1, "queued": 0}

    monkeypatch.setattr(registry, "probe_ai", lambda s: True)
    registry.override(
        classifier=ScriptedClassifier(default={"title": "KI-Titel", "summary": "von der KI"})
    )
    assert catch_up_ai(fb)["queued"] == 1
    run_until_idle(fb)
    m = docs.load_meta(fb, r.doc_id)
    assert m.ai_pending == [] and m.summary == "von der KI"
    assert m.title == "Mein Titel"  # the user's correction survives
    st = maintenance.status(fb)
    assert st["ai_pending"] == 0 and not st["ai_unreachable_since"]


def test_probe_ai_without_remote_providers_is_true(tmp_path):
    from heftig.config import Settings

    assert registry.probe_ai(Settings(_env_file=None, archive_dir=tmp_path))


def test_inbox_and_document_show_the_state(fb):
    from fastapi.testclient import TestClient

    from heftig import auth
    from heftig.web.app import create_app

    registry.override(classifier=ScriptedClassifier(error=ProviderError("offline", transient=True)))
    r = ingest_bytes(fb, text_pdf(["Brief " * 20]), "b.pdf")
    run_until_idle(fb)
    app = create_app(fb.settings)
    auth.create_user(app.state.archive.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"})
    assert "nicht erreichbar seit" in c.get("/inbox").text
    assert "Vorläufig offline verarbeitet" in c.get(f"/documents/{r.doc_id}").text
    app.state.archive.close()
