"""Anthropic Messages API adapter (official ``anthropic`` SDK, optional dependency).

Install with ``pip install 'heftig[anthropic]'`` (included in the container image).
"""

from __future__ import annotations

import base64
import json

from ..i18n import N_
from .base import (
    ClassifyRequest,
    ClassifyResponse,
    ExtractCapabilities,
    ProviderError,
    ProviderUnavailable,
)
from .openai_compat import parse_json_object
from .pricing import UsageMeter
from .prompt import (
    CLASSIFY_SCHEMA,
    OCR_PROMPT_VERSION,
    OCR_SYSTEM,
    OCR_USER,
    PROMPT_VERSION,
    classify_system,
    classify_user_parts,
)

DEFAULT_MODEL = "claude-opus-5-5"

# process-wide token counter (shown by `heftig status` users via logs, used by the live test)
USAGE = {"requests": 0, "input_tokens": 0, "output_tokens": 0}


def _client(api_key: str | None, base_url: str, timeout: int):
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - depends on installation
        raise ProviderUnavailable(
            N_(
                "Package “anthropic” is missing – install it with `pip install 'heftig[anthropic]'`."
            )
        ) from e
    if not api_key:
        raise ProviderUnavailable(N_("No Anthropic API key configured."))
    kwargs: dict = {"api_key": api_key, "timeout": float(timeout), "max_retries": 2}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic, anthropic.Anthropic(**kwargs)


def _call(anthropic, fn, name: str):
    try:
        return fn()
    except anthropic.RateLimitError as e:
        retry_after = None
        try:
            retry_after = float(e.response.headers.get("retry-after"))
        except (AttributeError, TypeError, ValueError):
            pass
        raise ProviderError(
            N_("%(provider)s: rate limit reached") % {"provider": name},
            rate_limited=True,
            retry_after=retry_after,
        ) from e
    except anthropic.APIConnectionError as e:
        raise ProviderError(
            N_("%(provider)s: connection error") % {"provider": name}, transient=True
        ) from e
    except anthropic.APIStatusError as e:
        transient = e.status_code >= 500 or e.status_code in (408, 409, 529)
        raise ProviderError(f"{name}: HTTP {e.status_code}", transient=transient) from e


def _text(resp, name: str, meter: UsageMeter | None = None, model: str = "") -> str:
    usage = getattr(resp, "usage", None)
    USAGE["requests"] += 1
    if usage is not None:
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        USAGE["input_tokens"] += tin + cw + cr
        USAGE["output_tokens"] += tout
        if meter is not None:
            meter.add(model, tin, tout, cw, cr)
    if resp.stop_reason == "refusal":
        raise ProviderError(
            N_("%(provider)s: request refused by the model (refusal)") % {"provider": name}
        )
    if resp.stop_reason == "max_tokens":
        raise ProviderError(N_("%(provider)s: response cut off (max_tokens)") % {"provider": name})
    return "".join(b.text for b in resp.content if b.type == "text")


class AnthropicExtractor:
    name = "anthropic"
    target = "api.anthropic.com"
    adapter_version = f"anthropic-messages-v1/{OCR_PROMPT_VERSION}"
    capabilities = ExtractCapabilities(images=True, pdf=True)

    def __init__(
        self,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout: int,
        effort: str,
        first_page_model: str = "",
    ):
        self._anthropic, self._client = _client(api_key, base_url, timeout)
        self.base_model = model or DEFAULT_MODEL
        # the first page (letterhead, sender, date, subject) may use a stronger model
        self.first_page_model = first_page_model if first_page_model != self.base_model else ""
        self.model = (
            N_("%(model)s (page 1: %(first)s)")
            % {"model": self.base_model, "first": self.first_page_model}
            if self.first_page_model
            else self.base_model
        )
        self.effort = effort
        self.usage = UsageMeter()
        if base_url:
            from urllib.parse import urlparse

            self.target = urlparse(base_url).hostname or self.target

    def page_model(self, page_number: int) -> str:
        return (
            self.first_page_model if page_number == 1 and self.first_page_model else self.base_model
        )

    def extract_page(
        self, image: bytes, page_number: int, languages: str, media_type: str = "image/png"
    ) -> str:
        b64 = base64.standard_b64encode(image).decode("ascii")
        model = self.page_model(page_number)
        kwargs: dict = {
            "model": model,
            "max_tokens": 16000,
            "system": OCR_SYSTEM,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {
                            "type": "text",
                            "text": OCR_USER.format(page=page_number, languages=languages),
                        },
                    ],
                }
            ],
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        resp = _call(self._anthropic, lambda: self._client.messages.create(**kwargs), self.name)
        return _text(resp, self.name, self.usage, model)


class AnthropicClassifier:
    name = "anthropic"
    target = "api.anthropic.com"
    adapter_version = "anthropic-messages-v1"
    prompt_version = PROMPT_VERSION

    def __init__(self, api_key: str | None, model: str, base_url: str, timeout: int):
        self._anthropic, self._client = _client(api_key, base_url, timeout)
        self.model = model or DEFAULT_MODEL
        self.usage = UsageMeter()
        if base_url:
            from urllib.parse import urlparse

            self.target = urlparse(base_url).hostname or self.target

    def _json(
        self, system: str, user: str | list[dict], schema: dict, max_tokens: int
    ) -> tuple[dict, str]:
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        resp = _call(self._anthropic, lambda: self._client.messages.create(**kwargs), self.name)
        raw = _text(resp, self.name, self.usage, self.model)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = parse_json_object(raw, self.name)
        return data, raw

    def classify(self, request: ClassifyRequest) -> ClassifyResponse:
        # instructions + categories are the same for every document: cached by the API for a
        # few minutes (reads cost a tenth), so a batch of documents pays for them about once
        static, own = classify_user_parts(request)
        content = [
            {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": own},
        ]
        data, raw = self._json(classify_system(request.language), content, CLASSIFY_SCHEMA, 16000)
        return ClassifyResponse(data=data, raw=raw)

    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 8000, prefix: str = ""
    ) -> dict:
        """One structured request (title harmonisation, AI search). `prefix`: a part that is
        the same across calls (categories) - sent first and cached."""
        if not prefix:
            return self._json(system, user, schema, max_tokens)[0]
        content = [
            {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user},
        ]
        return self._json(system, content, schema, max_tokens)[0]
