import base64
import json
import shutil
import subprocess

import httpx
import pytest

from heftig.config import Settings, is_local_url
from heftig.providers import registry
from heftig.providers.base import ClassifyRequest, ProviderError, ProviderUnavailable
from heftig.providers.openai_compat import (
    OpenAICompatClassifier,
    OpenAICompatExtractor,
    parse_json_object,
)

from .helpers import image_bytes, text_image


def _mock_httpx(monkeypatch, handler):
    real = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "Client", factory)


def test_openai_compatible_ocr_sends_inline_image_only(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Seitentext"}, "finish_reason": "stop"}]}
        )

    _mock_httpx(monkeypatch, handler)
    ex = OpenAICompatExtractor("openai_compatible", "http://localhost:11434/v1", None, "llava", 10)
    png = image_bytes(text_image("x"))
    assert ex.extract_page(png, 1, "deu") == "Seitentext"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions" and seen["auth"] is None
    url = seen["body"]["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,") and base64.b64decode(url.split(",")[1]) == png


def test_classifier_uses_json_schema_and_hides_key_in_errors(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert request.headers["authorization"] == "Bearer sk-geheim"
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    _mock_httpx(monkeypatch, handler)
    c = OpenAICompatClassifier("openai", "https://api.openai.com/v1", "sk-geheim", "m", 10)
    req = ClassifyRequest(text="t", filename="f", page_count=1, taxonomy={})
    with pytest.raises(ProviderError) as e:
        c.classify(req)
    assert "sk-geheim" not in str(e.value) and not e.value.transient


def test_rate_limit_is_transient(monkeypatch):
    _mock_httpx(monkeypatch, lambda r: httpx.Response(429))
    c = OpenAICompatClassifier("openai", "https://api.openai.com/v1", "k", "m", 10)
    with pytest.raises(ProviderError) as e:
        c.classify(ClassifyRequest(text="t", filename="f", page_count=1, taxonomy={}))
    assert e.value.transient


def test_capability_check_for_text_only_endpoint():
    ex = OpenAICompatExtractor("openai_compatible", "http://localhost:1234/v1", None, "m", 10,
                               supports_images=False)  # fmt: skip
    with pytest.raises(ProviderUnavailable, match="not accepting images"):
        ex.extract_page(b"", 1, "deu")


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('```json\n{"a": 1}\n```', "x") == {"a": 1}
    with pytest.raises(ProviderError):
        parse_json_object("kein json", "x")


def test_cloud_detection_and_gating(tmp_path):
    assert is_local_url("http://localhost:11434/v1") and is_local_url("http://127.0.0.1:8080")
    assert is_local_url("http://192.168.1.20:8000/v1")
    assert is_local_url("http://host.docker.internal:11434/v1")
    assert is_local_url("http://100.101.102.103:11434/v1")  # Tailscale / CGNAT
    s = Settings(_env_file=None, archive_dir=tmp_path, ocr_provider="openai_compatible",
                 ocr_base_url="https://llm.example.com/v1", ocr_model="m")  # fmt: skip
    with pytest.raises(ProviderUnavailable, match="not allowed"):
        registry.get_extractor(s)
    s2 = Settings(_env_file=None, archive_dir=tmp_path, ocr_provider="openai_compatible",
                  ocr_base_url="http://localhost:11434/v1", ocr_model="m")  # fmt: skip
    assert registry.get_extractor(s2).target == "localhost"
    d = registry.describe(Settings(_env_file=None, archive_dir=tmp_path, classify_provider="anthropic",
                                   classify_api_key="sk-x"))  # fmt: skip
    assert d["classify"]["cloud"] and d["classify"]["blocked"] and "sk-x" not in json.dumps(d)


def test_default_config_sends_nothing_to_the_cloud(tmp_path):
    s = Settings(_env_file=None, archive_dir=tmp_path)
    assert s.ocr_provider == "tesseract" and s.classify_provider == "rules"
    assert not s.allow_cloud_ocr and not s.allow_cloud_classify


def _tesseract_ok() -> bool:
    exe = shutil.which("tesseract")
    if not exe:
        return False
    out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, check=False)
    return "deu" in out.stdout


@pytest.mark.tesseract
@pytest.mark.skipif(not _tesseract_ok(), reason="tesseract mit deutschen Sprachdaten fehlt")
def test_tesseract_offline_ocr_reads_german():
    from heftig.providers.tesseract import TesseractExtractor

    img = text_image("Rechnung für Müller\nBetrag 123,45 EUR")
    text = TesseractExtractor().extract_page(image_bytes(img), 1, "deu+eng")
    assert "Rechnung" in text and "123,45" in text


def test_anthropic_classifier_caches_the_archive_wide_part():
    pytest.importorskip("anthropic")
    from types import SimpleNamespace

    from heftig.providers.anthropic_provider import AnthropicClassifier
    from heftig.providers.pricing import cost

    c = AnthropicClassifier("sk-test", "claude-opus-5-5", "", 10)
    sent = []

    def create(**kw):
        sent.append(kw)
        usage = SimpleNamespace(input_tokens=2000, cache_creation_input_tokens=0,
                                cache_read_input_tokens=5000, output_tokens=400)  # fmt: skip
        return SimpleNamespace(stop_reason="end_turn", usage=usage,
                               content=[SimpleNamespace(type="text", text='{"title": "x"}')])  # fmt: skip

    c._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    tax = {"correspondent": [{"name": "Stadtwerke"}], "document_type": [], "tag": []}
    for text in ("Rechnung eins", "Brief zwei"):
        c.classify(ClassifyRequest(text=text, filename="f.pdf", page_count=1, taxonomy=tax,
                                   title_examples=[text[:5]]))  # fmt: skip
    first, second = (kw["messages"][0]["content"] for kw in sent)
    # the categories are the identical, cached first block; the document follows uncached
    assert first[0] == second[0] and first[0]["cache_control"] == {"type": "ephemeral"}
    assert "Stadtwerke" in first[0]["text"] and "Rechnung eins" in first[1]["text"]
    assert "cache_control" not in first[1] and "Rechnung" not in first[0]["text"]
    # cached input is priced at a tenth
    assert c.usage.input_tokens == 2 * 7000 and c.usage.cache_read_tokens == 10000
    assert abs(c.usage.cost_usd - 2 * cost("claude-opus-5-5", 2000, 400, 0, 5000)) < 1e-9
    assert cost("claude-opus-5-5", 0, 0, 0, 1_000_000) == pytest.approx(0.4)


def _answer(content, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content},
                                                  "finish_reason": finish}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})  # fmt: skip


def test_open_models_reasoning_parts_and_one_retry(monkeypatch):
    """What local and reasoning models send back: thinking blocks, content parts, a first
    answer that is not JSON."""
    from heftig.providers.openai_compat import OpenAICompatClassifier, strip_reasoning

    answers = [
        _answer("<think>Let me see {not json}</think>\nSure! Here it is"),  # no JSON: retried
        _answer([{"type": "text", "text": '<think>hm</think>{"ok": true}'}]),
    ]
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return answers.pop(0)

    _mock_httpx(monkeypatch, handler)
    c = OpenAICompatClassifier("openai_compatible", "http://127.0.0.1:1/v1", None, "qwen", 5)
    assert c.complete_json("sys", "user", {"type": "object"}, 300) == {"ok": True}
    assert len(bodies) == 2 and bodies[0]["max_tokens"] == 300
    assert strip_reasoning("reasoning without start</think> answer") == "answer"


def test_openai_gets_max_completion_tokens(monkeypatch):
    from heftig.providers.openai_compat import OpenAICompatClassifier

    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _answer('{"ok": true}')

    _mock_httpx(monkeypatch, handler)
    c = OpenAICompatClassifier("openai", "https://api.openai.com/v1", "sk", "gpt-5-mini", 5)
    c.complete_json("sys", "user", {"type": "object"}, 300)
    assert bodies[0]["max_completion_tokens"] == 300 and "max_tokens" not in bodies[0]
