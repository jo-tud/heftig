"""Provider protocols. Text extraction, classification and (later) embeddings are separate
capabilities; one backend may implement several of them, but each is configured on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """A provider call failed. ``transient`` errors are retried with backoff."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        rate_limited: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        # a rate limit is transient, but no reason to fall back to local processing
        self.transient = transient or rate_limited
        self.rate_limited = rate_limited
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """Provider is not usable with the current configuration (not retried)."""


@dataclass(frozen=True)
class ExtractCapabilities:
    images: bool = True
    pdf: bool = False  # not used by the page-wise pipeline yet, reported for transparency


@runtime_checkable
class TextExtractor(Protocol):
    name: str
    model: str
    target: str  # where data goes: "local", host name of the API, ...
    adapter_version: str
    capabilities: ExtractCapabilities

    def extract_page(
        self, image: bytes, page_number: int, languages: str, media_type: str = "image/png"
    ) -> str:
        """Return the text of one page image (PNG or JPEG)."""
        ...


@dataclass
class ClassifyRequest:
    text: str
    filename: str
    page_count: int | None
    taxonomy: dict[str, list[dict[str, Any]]]  # kind -> [{name, aliases}]
    custom_field_keys: list[str] = field(default_factory=list)
    truncated: bool = False
    # titles (+ correspondent, type, date) of the most similar documents, as naming pattern
    title_examples: list[dict[str, Any]] = field(default_factory=list)
    paper: bool = False  # a paper original exists (keep_original is asked for)
    paper_folder: str | None = None  # label of the folder the paper came from (scan session)
    language: str = "en"  # the installation's language: summary and reasons are written in it


@dataclass
class ClassifyResponse:
    data: dict[str, Any]
    raw: str


@runtime_checkable
class Classifier(Protocol):
    name: str
    model: str
    target: str
    adapter_version: str
    prompt_version: str

    def classify(self, request: ClassifyRequest) -> ClassifyResponse: ...


@runtime_checkable
class Embedder(Protocol):
    """Extension point for a later semantic search mode. Not implemented in V1."""

    name: str
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...
