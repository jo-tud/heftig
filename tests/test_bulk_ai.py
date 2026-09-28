"""Bulk processing with a paid AI: image size, parallel pages, page cache, rate limits, costs,
and a catch-up that never pays twice."""

import json
import random
import shutil

import pytest
from PIL import Image

from heftig import documents as docs
from heftig import jobs, maintenance
from heftig.archive import Archive
from heftig.media import MAX_OCR_IMAGE_BYTES, ocr_image_bytes
from heftig.processing import catch_up_ai
from heftig.providers import registry
from heftig.providers.base import ProviderError
from heftig.providers.pricing import UsageMeter, cost
from heftig.worker import run_until_idle

from .conftest import ScriptedClassifier, ingest_bytes, make_settings
from .helpers import scan_pdf


def pages(*labels):
    """Image-only PDF with realistic text pages (a single letter would count as blank)."""
    return scan_pdf(
        ["\n".join(f"{lab} Zeile {n}: Sehr geehrte Damen und Herren, anbei" for n in range(14))
         for lab in labels]
    )  # fmt: skip


needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract fehlt")


class CloudOCR:
    """Remote OCR fake: metered, per-page scripted errors, records what it was sent."""

    name = "cloud-ocr"
    model = "claude-sonnet-5"
    target = "api.example"
    adapter_version = "cloud-v1"

    def __init__(self, errors=None):
        from heftig.providers.base import ExtractCapabilities

        self.capabilities = ExtractCapabilities(images=True)
        self.errors = errors or {}  # page -> list of exceptions to raise, one per call
        self.calls = []
        self.usage = UsageMeter()

    def extract_page(self, image, page_number, languages, media_type="image/png"):
        self.calls.append((page_number, len(image), media_type))
        queue = self.errors.get(page_number)
        if queue:
            raise queue.pop(0)
        self.usage.add(self.model, 2000, 500)
        return f"Cloud-Text Seite {page_number}"


@pytest.fixture
def cloud(tmp_path):
    a = Archive(make_settings(tmp_path, ai_fallback=True, rate_limit_pause_seconds=10))
    yield a
    a.close()


def _job(archive, doc_id):
    return archive.conn.execute(
        "SELECT * FROM jobs WHERE doc_id=? ORDER BY id DESC LIMIT 1", (doc_id,)
    ).fetchone()


def _make_runnable(archive):
    archive.conn.execute("UPDATE jobs SET next_run_at='2000-01-01T00:00:00Z'")


def test_ocr_images_stay_below_the_provider_limit():
    rnd = random.Random(1)
    noisy = Image.frombytes("RGB", (2481, 3508), rnd.randbytes(2481 * 3508 * 3))
    data, mt = ocr_image_bytes(noisy, 2000)
    assert mt == "image/jpeg" and len(data) <= MAX_OCR_IMAGE_BYTES
    clean = Image.new("RGB", (2481, 3508), "white")
    data, mt = ocr_image_bytes(clean, 2000)
    assert mt == "image/png"
    assert max(Image.open(__import__("io").BytesIO(data)).size) == 2000


def test_pages_in_parallel_cost_recorded_and_cached(cloud):
    ocr = CloudOCR()
    registry.override(extractor=ocr, classifier=ScriptedClassifier(default={"title": "x"}))
    r = ingest_bytes(cloud, pages("A", "B", "C", "D"), "s.pdf")
    run_until_idle(cloud)
    assert sorted(p for p, _, _ in ocr.calls) == [1, 2, 3, 4]
    tp = docs.load_text_pages(cloud, r.doc_id)
    assert [p.text for p in tp.pages] == [f"Cloud-Text Seite {n}" for n in range(1, 5)]
    run = cloud.conn.execute(
        "SELECT input_tokens, output_tokens, cost_usd FROM processing_runs WHERE task='extract'"
    ).fetchone()
    assert tuple(run[:2]) == (8000, 2000) and run[2] == pytest.approx(
        cost("claude-sonnet-5", 8000, 2000)
    )
    costs = maintenance.ai_costs(cloud.conn)
    assert costs["today"]["usd"] == round(run[2], 2) and costs["since"]


def test_rate_limit_postpones_without_fallback_and_pays_pages_once(cloud):
    rl = ProviderError("Rate-Limit", rate_limited=True, retry_after=30)
    ocr = CloudOCR(errors={3: [rl]})
    registry.override(extractor=ocr, classifier=ScriptedClassifier(default={"title": "x"}))
    r = ingest_bytes(cloud, pages("A", "B", "C"), "s.pdf")
    run_until_idle(cloud)
    job = _job(cloud, r.doc_id)
    # postponed, the attempt does not count, nothing fell back to Tesseract
    assert job["status"] == "queued" and job["attempts"] == 0 and "Rate-Limit" in job["error"]
    assert docs.load_meta(cloud, r.doc_id).ai_pending == []
    _make_runnable(cloud)
    run_until_idle(cloud)
    # pages 1 and 2 came from the cache: each page was paid exactly once
    paid = [p for p, _, _ in ocr.calls]
    assert sorted(paid) == [1, 2, 3, 3]
    assert docs.load_meta(cloud, r.doc_id).status == "done"


@needs_tesseract
def test_rejected_image_falls_back_locally_for_that_page_only(cloud):
    ocr = CloudOCR(errors={1: [ProviderError("anthropic: HTTP 400")]})
    registry.override(extractor=ocr, classifier=ScriptedClassifier(default={"title": "x"}))
    r = ingest_bytes(cloud, pages("Beitragsrechnung Hausrat", "B"), "s.pdf")
    run_until_idle(cloud)
    m = docs.load_meta(cloud, r.doc_id)
    tp = docs.load_text_pages(cloud, r.doc_id)
    assert tp.pages[0].provider == "tesseract (fallback)" and "Hausrat" in tp.pages[0].text
    assert tp.pages[1].provider == "cloud-ocr"
    assert m.ai_pending == []  # a rejected image would be rejected again: no paid catch-up loop
    assert any("read locally" in x for x in m.review_reasons)


def test_catch_up_never_queues_twice_and_waits_for_big_imports(cloud, monkeypatch):
    monkeypatch.setattr(registry, "probe_ai", lambda s: True)
    registry.override(classifier=ScriptedClassifier(error=ProviderError("offline", transient=True)))
    a = ingest_bytes(cloud, scan_pdf(["A"]), "a.pdf").doc_id
    run_until_idle(cloud)
    assert docs.load_meta(cloud, a).ai_pending == ["classify"]
    first = catch_up_ai(cloud)
    assert first["queued"] == 1
    assert catch_up_ai(cloud)["queued"] == 0  # its job is still queued: not again
    run_until_idle(cloud)
    # a permanent AI error removes the pending mark: the next cycle does not pay again
    registry.override(classifier=ScriptedClassifier(error=ProviderError("HTTP 400")))
    jobs.enqueue(cloud.conn, "process", a, {"stages": ["classify"]}, max_attempts=1)
    run_until_idle(cloud)
    assert docs.load_meta(cloud, a).ai_pending == []
    assert catch_up_ai(cloud) == {"pending": 0, "queued": 0}
    # with a large backlog the catch-up waits
    registry.override(classifier=ScriptedClassifier(error=ProviderError("offline", transient=True)))
    b = ingest_bytes(cloud, scan_pdf(["B"]), "b.pdf").doc_id
    run_until_idle(cloud)
    cloud.settings.ai_catch_up_batch = 1
    ingest_bytes(cloud, scan_pdf(["C"]), "c.pdf")  # queued, not processed
    res = catch_up_ai(cloud)
    assert res["queued"] == 0 and res["deferred"] == 1 and docs.load_meta(cloud, b).ai_pending


def test_postpone_does_not_use_up_attempts(cloud):
    registry.override(classifier=ScriptedClassifier(default={}))
    jid = jobs.enqueue(cloud.conn, "export", payload={}, max_attempts=1)
    cloud.conn.execute("UPDATE jobs SET attempts=1, status='processing' WHERE id=?", (jid,))
    assert jobs.postpone(cloud.conn, jid, "Rate-Limit", 30) == "queued"
    row = cloud.conn.execute("SELECT attempts, status FROM jobs WHERE id=?", (jid,)).fetchone()
    assert tuple(row) == (0, "queued")


def test_first_page_model_is_used_for_page_one_only():
    pytest.importorskip("anthropic")
    from heftig.providers.anthropic_provider import AnthropicExtractor

    ex = AnthropicExtractor("sk-test", "claude-sonnet-5", "", 30, "low", "claude-opus-5-5")
    assert ex.page_model(1) == "claude-opus-5-5" and ex.page_model(2) == "claude-sonnet-5"
    assert ex.model == "claude-sonnet-5 (page 1: claude-opus-5-5)"
    same = AnthropicExtractor("sk-test", "claude-opus-5-5", "", 30, "low", "claude-opus-5-5")
    assert same.page_model(1) == "claude-opus-5-5" and same.model == "claude-opus-5-5"
    assert json.dumps(ex.page_model(3))


@needs_tesseract
def test_blank_pages_and_page_budget_stay_local(cloud):
    ocr = CloudOCR()
    registry.override(extractor=ocr, classifier=ScriptedClassifier(default={"title": "x"}))
    cloud.settings.ocr_ai_max_pages = 2
    # pages: text, blank (duplex back side), text, text  -> AI for 1 and 3, 4 over budget
    pdf = scan_pdf([
        "\n".join(f"Vorderseite Zeile {n} mit Text" for n in range(14)), "",
        "\n".join(f"Seite drei Zeile {n} mit Text" for n in range(14)),
        "\n".join(f"Seite vier Zeile {n} Hausrat" for n in range(14)),
    ])  # fmt: skip
    r = ingest_bytes(cloud, pdf, "s.pdf")
    run_until_idle(cloud)
    assert sorted(p for p, _, _ in ocr.calls) == [1, 3]
    tp = docs.load_text_pages(cloud, r.doc_id)
    assert tp.pages[1].blank and tp.pages[1].provider == "tesseract (blank page)"
    assert tp.pages[3].provider == "tesseract (page budget)" and "Hausrat" in tp.pages[3].text
    m = docs.load_meta(cloud, r.doc_id)
    assert any(
        x.startswith("Text: Large document – 1 pages read without AI") for x in m.review_reasons
    )
    # the user asks for all pages: now page 4 goes to the AI as well
    from heftig.processing import ocr_all_pages

    ocr_all_pages(cloud, r.doc_id)
    run_until_idle(cloud)
    assert 4 in [p for p, _, _ in ocr.calls]
    assert not any(
        x.startswith("Text: Großes Dokument")
        for x in docs.load_meta(cloud, r.doc_id).review_reasons
    )
