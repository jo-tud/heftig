"""Build the configured providers. Tests can inject fakes via :func:`override`."""

from __future__ import annotations

from ..config import Settings, with_headers
from ..i18n import N_, _, translate_text
from .base import Classifier, ProviderUnavailable, TextExtractor
from .openai_compat import OPENAI_BASE

_override: dict[str, object] = {}


def override(
    *,
    extractor: object = "keep",
    classifier: object = "keep",
    search_planner: object = "keep",
    embedder: object = "keep",
) -> None:
    """Replace providers (tests). Pass None to disable a task, "keep" to leave as is."""
    if embedder != "keep":
        _override["embedder"] = embedder
    if extractor != "keep":
        _override["extractor"] = extractor
    if classifier != "keep":
        _override["classifier"] = classifier
    if search_planner != "keep":
        _override["search_planner"] = search_planner


def clear_overrides() -> None:
    _override.clear()


def headers(s: Settings, task: str) -> dict[str, str]:
    """The additional HTTP headers of a task's OpenAI-compatible endpoint."""
    try:
        return s.provider_headers(task)  # type: ignore[arg-type]
    except OSError as e:
        raise ProviderUnavailable(
            N_("The file with the additional headers cannot be read (%(error)s).")
            % {"error": type(e).__name__}
        ) from e
    except ValueError as e:
        raise ProviderUnavailable(str(e)) from e


def get_extractor(s: Settings) -> TextExtractor | None:
    """OCR provider for image pages, or None if OCR is switched off."""
    if "extractor" in _override:
        return _override["extractor"]  # type: ignore[return-value]
    name = s.ocr_provider
    if name == "none":
        return None
    blocked = s.ocr_blocked_reason()
    if blocked:
        raise ProviderUnavailable(blocked)
    if name == "tesseract":
        from .tesseract import TesseractExtractor

        return TesseractExtractor(timeout=s.ocr_page_timeout_seconds)
    if name == "mock":
        from .rules import MockExtractor, parse_mock_fail

        return MockExtractor(parse_mock_fail(s.mock_fail)[0])
    if name in ("openai", "openai_compatible"):
        from .openai_compat import OpenAICompatExtractor

        base = s.ocr_base_url or (OPENAI_BASE if name == "openai" else "")
        return OpenAICompatExtractor(
            name, base, s.secret("ocr_api_key"), s.ocr_model, s.provider_timeout_seconds,
            supports_images=s.ocr_supports_images, headers=headers(s, "ocr"),
        )  # fmt: skip
    if name == "anthropic":
        from .anthropic_provider import AnthropicExtractor

        return AnthropicExtractor(
            s.secret("ocr_api_key"), s.ocr_model, s.ocr_base_url, s.provider_timeout_seconds,
            s.anthropic_ocr_effort, s.ocr_first_page_model,
        )  # fmt: skip
    raise ProviderUnavailable(N_("Unknown OCR provider %(name)s") % {"name": name})


def get_classifier(s: Settings) -> Classifier | None:
    if "classifier" in _override:
        return _override["classifier"]  # type: ignore[return-value]
    name = s.classify_provider
    if name == "none":
        return None
    blocked = s.classify_blocked_reason()
    if blocked:
        raise ProviderUnavailable(blocked)
    if name in ("rules", "mock"):
        from .rules import RulesClassifier, parse_mock_fail

        fail = name == "mock" and parse_mock_fail(s.mock_fail)[1]
        c = RulesClassifier(fail=fail)
        c.name = name
        return c
    if name in ("openai", "openai_compatible"):
        from .openai_compat import OpenAICompatClassifier

        base = s.classify_base_url or (OPENAI_BASE if name == "openai" else "")
        return OpenAICompatClassifier(
            name, base, s.secret("classify_api_key"), s.classify_model,
            s.provider_timeout_seconds, s.classify_json_mode, headers(s, "classify"),
        )  # fmt: skip
    if name == "anthropic":
        from .anthropic_provider import AnthropicClassifier

        return AnthropicClassifier(
            s.secret("classify_api_key"), s.classify_model, s.classify_base_url,
            s.provider_timeout_seconds,
        )  # fmt: skip
    raise ProviderUnavailable(N_("Unknown classification provider %(name)s") % {"name": name})


_EMBEDDERS: dict = {}


def get_embedder(s: Settings):
    """The built-in embedding model (search by meaning), or None if it is switched off. One
    instance per archive and process: the model is loaded once, on first use."""
    if "embedder" in _override:
        return _override["embedder"]
    if not s.semantic_search:
        return None
    from ..local_embed import DEFAULT, LocalEmbedder

    key = (str(s.archive_dir), DEFAULT.name, s.semantic_threads)
    if key not in _EMBEDDERS:
        _EMBEDDERS[key] = LocalEmbedder(
            DEFAULT, s.archive_dir / "models", s.semantic_threads or None
        )
    return _EMBEDDERS[key]


def get_search_planner(s: Settings):
    """The model behind the AI search (question -> filters): the classification provider,
    with Anthropic the small fast model ``HEFTIG_AI_SEARCH_MODEL``. None: not available."""
    if "search_planner" in _override:
        return _override["search_planner"]
    name = s.classify_provider
    if name not in ("anthropic", "openai", "openai_compatible") or s.classify_blocked_reason():
        return None
    if name == "anthropic":
        from .anthropic_provider import AnthropicClassifier

        return AnthropicClassifier(
            s.secret("classify_api_key"), s.ai_search_model or s.classify_model,
            s.classify_base_url, min(s.provider_timeout_seconds, 30),
        )  # fmt: skip
    from .openai_compat import OpenAICompatClassifier

    base = s.classify_base_url or (OPENAI_BASE if name == "openai" else "")
    return OpenAICompatClassifier(
        name, base, s.secret("classify_api_key"), s.classify_model,
        # a local model may need a while; a cloud service answers quickly or not at all
        s.provider_timeout_seconds if not s.provider_is_cloud(name, base) else
        min(s.provider_timeout_seconds, 30), s.classify_json_mode, headers(s, "classify"),
    )  # fmt: skip


def _host(url: str) -> str:
    """Scheme, host and port of a base URL - never credentials, paths or query keys in it."""
    from urllib.parse import urlsplit

    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname}{f':{u.port}' if u.port else ''}" if u.hostname else "(URL)"


def describe(s: Settings) -> dict[str, dict]:
    """Human-readable provider status for the settings page (no secrets)."""
    out: dict[str, dict] = {}
    for task, name, base, model, blocked, is_cloud in (
        ("ocr", s.ocr_provider, s.ocr_base_url, s.ocr_model, s.ocr_blocked_reason(),
         s.provider_is_cloud(s.ocr_provider, s.ocr_base_url)),
        ("classify", s.classify_provider, s.classify_base_url, s.classify_model,
         s.classify_blocked_reason(), s.provider_is_cloud(s.classify_provider, s.classify_base_url)),
    ):  # fmt: skip
        target = _("local")
        if name == "openai":
            target = _host(base or OPENAI_BASE)
        elif name == "anthropic":
            target = _host(base or "https://api.anthropic.com")
        elif name == "openai_compatible":
            target = _host(base) if base else _("(no URL)")
        elif name == "none":
            target = "–"
        key_set = (
            bool(s.secret(f"{task}_api_key"))
            if name not in ("none", "rules", "mock", "tesseract")
            else None
        )
        has_headers = None
        if name in ("openai", "openai_compatible"):
            try:
                has_headers = bool(s.secret(f"{task}_headers"))
            except OSError:
                has_headers = True  # a file is given, it just cannot be read here
        out[task] = {
            "provider": name,
            "model": model,
            "target": target,
            "cloud": is_cloud,
            "blocked": translate_text(blocked) or None,
            "api_key_configured": key_set,
            "headers_configured": has_headers,
        }
    return out


def probe_ai(s: Settings) -> bool:
    """Cheap reachability check of the configured remote AI providers (no document data sent).

    True when every remote provider answers (or none is configured)."""
    import httpx

    checks = []
    for task, name, base, _model in (
        ("ocr", s.ocr_provider, s.ocr_base_url, s.ocr_model),
        ("classify", s.classify_provider, s.classify_base_url, s.classify_model),
    ):
        if name in ("none", "tesseract", "mock", "rules"):
            continue
        key = s.secret(f"{task}_api_key")
        if name == "anthropic":
            url = (base or "https://api.anthropic.com").rstrip("/") + "/v1/models"
            sent = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
        else:
            url = (base or OPENAI_BASE).rstrip("/") + "/models"
            sent = {"Authorization": f"Bearer {key}"} if key else {}
            try:
                sent = with_headers(sent, headers(s, task))
            except ProviderUnavailable:
                return False
        checks.append((url, sent))
    for url, sent in dict.fromkeys((u, tuple(h.items())) for u, h in checks):
        try:
            r = httpx.get(url, headers=dict(sent), timeout=10, follow_redirects=False)
        except httpx.HTTPError:
            return False
        if r.status_code >= 500 or r.status_code in (408, 429):
            return False
    return True
