from __future__ import annotations

import io

import pytest

from heftig.archive import Archive
from heftig.config import Settings
from heftig.ingest import ingest_stream
from heftig.providers import registry
from heftig.worker import run_until_idle


class FakeExtractor:
    """OCR fake: text per page number, optional failing pages / transient errors."""

    name = "fake-ocr"
    model = "fake"
    target = "lokal"
    adapter_version = "fake-v1"

    def __init__(self, pages=None, fail_pages=(), transient_failures=0):
        from heftig.providers.base import ExtractCapabilities

        self.capabilities = ExtractCapabilities(images=True)
        self.pages = pages or {}
        self.fail_pages = set(fail_pages)
        self.transient_failures = transient_failures
        self.calls = 0
        self.media_types = []

    def extract_page(self, image_png, page_number, languages, media_type="image/png"):
        from heftig.providers.base import ProviderError

        self.calls += 1
        self.media_types.append(media_type)
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise ProviderError("temporärer Fehler", transient=True)
        if page_number in self.fail_pages:
            raise ProviderError(f"kaputt auf Seite {page_number}")
        return self.pages.get(page_number, f"Seite {page_number} OCR")


class ScriptedClassifier:
    """Classifier fake returning prepared data (by filename) or a default."""

    name = "fake-ai"
    model = "fake-model"
    target = "lokal"
    adapter_version = "fake-v1"
    prompt_version = "p1"

    def __init__(self, by_filename=None, default=None, error=None):
        self.by_filename = by_filename or {}
        self.default = default
        self.error = error
        self.requests = []

    def classify(self, request):
        from heftig.providers.base import ClassifyResponse

        self.requests.append(request)
        if self.error:
            raise self.error
        data = self.by_filename.get(request.filename, self.default)
        if data is None:
            data = {}
        full = {
            "title": "",
            "document_date": None,
            "document_date_evidence": None,
            "document_date_confidence": 0.0,
            "correspondent": None,
            "correspondent_confidence": 0.0,
            "document_type": None,
            "document_type_confidence": 0.0,
            "tags": [],
            "summary": "",
            "custom_fields": [],
        }
        full.update(data)
        return ClassifyResponse(data=full, raw=str(full))


def make_settings(tmp_path, **kw) -> Settings:
    base = dict(
        archive_dir=tmp_path / "archive",
        ocr_provider="mock",
        classify_provider="rules",
        consume_stable_polls=2,
        consume_min_age_seconds=0,
        job_backoff_seconds=0,
        worker_concurrency=1,
        ai_fallback=False,  # tests of the fallback enable it explicitly
        ocr_blank_max_ink=0.0,  # test pages are tiny; only truly empty pages count as blank
        language="de",  # most web tests check the German texts (English: test_i18n.py)
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def archive(settings):
    a = Archive(settings)
    yield a
    a.close()
    registry.clear_overrides()


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    registry.clear_overrides()


def ingest_bytes(archive, data: bytes, filename: str, source: str = "web", **kw):
    return ingest_stream(archive, io.BytesIO(data), filename, source, **kw)


def process_all(archive):
    return run_until_idle(archive)
