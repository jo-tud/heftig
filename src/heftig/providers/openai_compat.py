"""OpenAI and OpenAI-compatible endpoints (Chat Completions API).

Used for the OpenAI API itself and for local servers that speak the same protocol (Ollama,
LM Studio, llama.cpp server, vLLM, ...). Images are sent inline as ``data:`` URLs, never as
external URLs, so the provider never fetches anything on our behalf.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlparse

import httpx

from ..config import with_headers
from ..i18n import N_
from .base import (
    ClassifyRequest,
    ClassifyResponse,
    ExtractCapabilities,
    ProviderError,
    ProviderUnavailable,
)
from .pricing import UsageMeter
from .prompt import (
    CLASSIFY_SCHEMA,
    OCR_PROMPT_VERSION,
    OCR_SYSTEM,
    OCR_USER,
    PROMPT_VERSION,
    classify_system,
    classify_user_message,
)

OPENAI_BASE = "https://api.openai.com/v1"


class _Client:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: int,
        headers: dict[str, str] | None = None,
    ):
        if not model:
            raise ProviderUnavailable(
                N_("No model configured for %(provider)s.") % {"provider": name}
            )
        if not base_url:
            raise ProviderUnavailable(
                N_("No base URL configured for %(provider)s.") % {"provider": name}
            )
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ProviderUnavailable(N_("Invalid base URL for %(provider)s.") % {"provider": name})
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.target = parsed.hostname
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        # additional headers of the endpoint (they may replace Authorization); never logged
        self._headers = with_headers(self._headers, headers or {})
        self._timeout = timeout
        self.usage = UsageMeter()

    def post(self, path: str, body: dict) -> dict:
        """POST to the API, errors as ProviderError; the parsed JSON answer."""
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                r = client.post(f"{self.base_url}{path}", headers=self._headers, json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(
                N_("%(provider)s: timeout") % {"provider": self.name}, transient=True
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                N_("%(provider)s: connection error (%(error)s)")
                % {"provider": self.name, "error": type(e).__name__},
                transient=True,
            ) from e
        if r.status_code == 429:
            try:
                retry_after = float(r.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
            raise ProviderError(
                N_("%(provider)s: rate limit reached") % {"provider": self.name},
                rate_limited=True,
                retry_after=retry_after,
            )
        if r.status_code in (408, 409) or r.status_code >= 500:
            raise ProviderError(f"{self.name}: HTTP {r.status_code}", transient=True)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json().get("error", {}).get("message", ""))[:300]
            except (ValueError, AttributeError):
                pass
            raise ProviderError(f"{self.name}: HTTP {r.status_code} {detail}".strip())
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError(
                N_("%(provider)s: unexpected response") % {"provider": self.name}
            ) from e
        if not isinstance(data, dict):
            raise ProviderError(N_("%(provider)s: unexpected response") % {"provider": self.name})
        return data

    def chat(self, body: dict) -> str:
        data = self.post("/chat/completions", body)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            if isinstance(content, list):  # some servers answer with content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            usage = data.get("usage") or {}
            self.usage.add(
                str(body.get("model") or ""),
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(
                N_("%(provider)s: unexpected response") % {"provider": self.name}
            ) from e
        if choice.get("finish_reason") == "length":
            raise ProviderError(
                N_("%(provider)s: response cut off (max_tokens)") % {"provider": self.name}
            )
        return strip_reasoning(content)

    def limit(self, body: dict, max_tokens: int) -> dict:
        """The output limit: OpenAI's current models only take ``max_completion_tokens`` (it
        includes their hidden reasoning); other servers know ``max_tokens``."""
        body["max_completion_tokens" if self.name == "openai" else "max_tokens"] = max_tokens
        return body


# reasoning models served locally (Qwen, DeepSeek, ...) put their thinking into the answer
_REASONING = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    text = _REASONING.sub("", text)
    for tag in ("</think>", "</thinking>", "</reasoning>"):  # opening tag cut off by the server
        if tag in text:
            text = text.rsplit(tag, 1)[1]
    return text.strip()


class OpenAICompatExtractor:
    adapter_version = f"openai-chat-v1/{OCR_PROMPT_VERSION}"

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: int,
        supports_images: bool = True,
        headers: dict[str, str] | None = None,
    ):
        self._c = _Client(name, base_url, api_key, model, timeout, headers)
        self.usage = self._c.usage
        self.name, self.model, self.target = name, model, self._c.target
        self.capabilities = ExtractCapabilities(images=supports_images, pdf=False)

    def extract_page(
        self, image: bytes, page_number: int, languages: str, media_type: str = "image/png"
    ) -> str:
        if not self.capabilities.images:
            raise ProviderUnavailable(
                N_(
                    "Endpoint %(target)s is configured as not accepting images "
                    "(HEFTIG_OCR_SUPPORTS_IMAGES=false) – OCR not possible."
                )
                % {"target": self.target}
            )
        b64 = base64.b64encode(image).decode("ascii")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": OCR_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": OCR_USER.format(page=page_number, languages=languages),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"},
                        },
                    ],
                },
            ],
        }
        return self._c.chat(self._c.limit(body, 16000 if self.name == "openai" else 8000))


class OpenAICompatClassifier:
    adapter_version = "openai-chat-v1"
    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: int,
        json_mode: str = "schema",
        headers: dict[str, str] | None = None,
    ):
        self._c = _Client(name, base_url, api_key, model, timeout, headers)
        self.usage = self._c.usage
        self.name, self.model, self.target = name, model, self._c.target
        self.json_mode = json_mode

    def _json(
        self, system: str, user: str, schema: dict, max_tokens: int, name: str
    ) -> tuple[dict, str]:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        self._c.limit(body, max_tokens)
        if self.json_mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            }
        elif self.json_mode == "object":
            body["response_format"] = {"type": "json_object"}
            body["messages"][0]["content"] = system + "\n\nJSON schema:\n" + json.dumps(schema)
        else:
            body["messages"][0]["content"] = (
                system
                + "\n\nAnswer with a single JSON object only. JSON schema:\n"
                + json.dumps(schema)
            )
        raw = self._c.chat(body)
        try:
            return parse_json_object(raw, self.name), raw
        except ProviderError:
            # smaller models get the format wrong now and then: one more try
            raw = self._c.chat(body)
            return parse_json_object(raw, self.name), raw

    def classify(self, request: ClassifyRequest) -> ClassifyResponse:
        data, raw = self._json(
            classify_system(request.language), classify_user_message(request), CLASSIFY_SCHEMA,
            16000 if self.name == "openai" else 6000, "classification",
        )  # fmt: skip
        return ClassifyResponse(data=data, raw=raw)

    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 8000, prefix: str = ""
    ) -> dict:
        """One structured request (title harmonisation, AI search); `prefix` is sent first."""
        text = f"{prefix}\n{user}" if prefix else user
        return self._json(system, text, schema, max_tokens, "result")[0]


def parse_json_object(raw: str, name: str) -> dict:
    text = strip_reasoning(raw)
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ProviderError(
            N_("%(provider)s: response contains no JSON object") % {"provider": name}
        )
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ProviderError(
            N_("%(provider)s: invalid JSON (%(error)s)") % {"provider": name, "error": e.msg}
        ) from e
    if not isinstance(data, dict):
        raise ProviderError(N_("%(provider)s: JSON is not an object") % {"provider": name})
    return data
