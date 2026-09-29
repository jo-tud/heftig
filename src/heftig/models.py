"""Portable data formats (sidecar files). These are the long-term contract of the archive.

``metadata.json`` is validated by :class:`DocumentMetadata`; ``heftig schema`` prints the JSON
schema, a copy lives in ``docs/metadata.schema.json``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

METADATA_SCHEMA_VERSION = 1

Source = Literal["scanner", "folder", "web", "api", "email", "import"]
FieldSource = Literal["ai", "user", "import", "rule"]
DocStatus = Literal["queued", "processing", "done", "needs_review", "failed"]
TextStatus = Literal["pending", "ok", "partial", "failed", "empty"]
DateStatus = Literal["unknown", "ai", "ai_uncertain", "user", "import", "none_found", "as_of"]
CustomFieldType = Literal["string", "number", "monetary", "date", "boolean"]

LOCKABLE_FIELDS = (
    "title",
    "document_date",
    "correspondent",
    "document_type",
    "tags",
    "summary",
    "custom_fields",
)


class CustomField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: CustomFieldType
    value: str | float | bool | None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class IngestEvent(BaseModel):
    """One arrival of this file. A duplicate upload adds an event instead of a new original."""

    model_config = ConfigDict(extra="forbid")

    at: str
    source: Source
    source_details: dict[str, Any] = Field(default_factory=dict)
    original_filename: str = ""
    result: Literal["created", "duplicate", "imported"]


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Literal["extract", "classify", "edit", "filing", "import", "merge", "note", "attachment"]
    at: str
    status: str
    provider: str = ""
    model: str = ""
    target: str = ""
    adapter_version: str = ""
    prompt_version: str = ""
    fields: list[str] = Field(default_factory=list)
    error: str | None = None
    by: FieldSource | Literal["system"] = "system"


class Suggestion(BaseModel):
    """An AI proposal that was not applied automatically (locked, uncertain or ambiguous)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: Any
    reason: str
    confidence: float | None = None


class TagOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class Note(BaseModel):
    """A user comment on a document. Never changed by processing or AI."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    at: str
    text: str = Field(max_length=20000)
    updated_at: str | None = None


class Attachment(BaseModel):
    """An additional file belonging to a document. Stored byte-identical next to the originals,
    content-addressed: originals/attachments/<sha256[:2]>/<sha256>.<ext> (shared if the same file
    is attached to several documents)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relpath: str
    added_at: str
    description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _relpath_matches_hash(self) -> Attachment:
        m = re.fullmatch(
            r"originals/attachments/([0-9a-f]{2})/([0-9a-f]{64})\.[a-z0-9]{1,5}", self.relpath
        )
        if not m or m.group(2) != self.sha256 or m.group(1) != self.sha256[:2]:
            raise ValueError("attachment relpath does not match sha256")
        return self


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = METADATA_SCHEMA_VERSION
    # canonical lowercase UUID: it names the sidecar folder, "urn:uuid:…" or upper case would
    # reach another document's folder
    id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_filename: str
    original_relpath: str
    mime_type: str
    size_bytes: int
    page_count: int | None = None
    source: Source
    source_details: dict[str, Any] = Field(default_factory=dict)
    received_at: str
    ingest_sequence: int
    paper: bool = False
    document_date: str | None = None
    document_date_status: DateStatus = "unknown"
    document_date_reason: str | None = None
    filed_at: str | None = None
    filing_sequence: int | None = None
    filing_section: str | None = None
    # the binder the paper went into (name on its spine, see binders.json)
    filing_binder: str | None = None
    title: str = ""
    correspondent: str | None = None
    document_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    custom_fields: dict[str, CustomField] = Field(default_factory=dict)
    field_locks: dict[str, bool] = Field(default_factory=dict)
    field_sources: dict[str, FieldSource] = Field(default_factory=dict)
    tag_overrides: TagOverrides = Field(default_factory=TagOverrides)
    suggestions: list[Suggestion] = Field(default_factory=list)
    status: DocStatus = "queued"
    text_status: TextStatus = "pending"
    review_reasons: list[str] = Field(default_factory=list)
    ingest_events: list[IngestEvent] = Field(default_factory=list)
    # documents the user confirmed as NOT being duplicates of this one ("keep both")
    not_duplicate_of: list[str] = Field(default_factory=list)
    # AI stages that ran with the local fallback and are redone automatically later
    ai_pending: list[Literal["extract", "classify"]] = Field(default_factory=list)
    # the user asked for AI text recognition of every page (beyond HEFTIG_OCR_AI_MAX_PAGES)
    ocr_all_pages: bool = False
    # pages (1-based) the user marked as blank (True: hidden in the viewer) or as not blank
    # (False: always shown) - overrides the detection in text_pages.json
    page_blank: dict[int, bool] = Field(default_factory=dict)
    # scan session the paper came in with, and where the paper is now (besides Heftig's own
    # filing): in an existing folder (paper_location) or shredded (paper_discarded_at)
    scan_session: ScanSessionRef | None = None
    paper_location: str | None = None
    paper_discarded_at: str | None = None
    # keep the paper original? suggested by the classifier (or a rule), decided by the user
    keep_original: bool | None = None
    keep_original_reason: str | None = None
    keep_original_source: Literal["ai", "rule", "user"] | None = None
    # set while the document is in the Papierkorb (archive/trash/<id>/)
    trashed_at: str | None = None
    trash_reason: str | None = None
    trash_batch: str | None = None
    notes: list[Note] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    processing_history: list[HistoryEntry] = Field(default_factory=list)
    revision: int = 1
    updated_at: str

    @field_validator("document_date")
    @classmethod
    def _valid_date(cls, v: str | None) -> str | None:
        if v is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError("document_date must be YYYY-MM-DD")
            date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _relpath_matches_hash(self) -> DocumentMetadata:
        # the original's location is derived from its hash - never trust another path
        m = re.fullmatch(
            r"originals/([0-9a-f]{2})/([0-9a-f]{64})\.[a-z0-9]{1,5}", self.original_relpath
        )
        if not m or m.group(2) != self.sha256 or m.group(1) != self.sha256[:2]:
            raise ValueError("original_relpath does not match sha256")
        return self

    def locked(self, field: str) -> bool:
        return bool(self.field_locks.get(field))


class ScanSessionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    mode: Literal["folder", "refile", "sort"]


class PageText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int  # 1-based
    method: Literal["embedded", "ocr", "none", "user"]
    provider: str = ""
    text: str = ""
    chars: int = 0
    error: str | None = None
    # (almost) no ink, e.g. the empty back of a duplex scan: not sent to a paid AI, hidden in the
    # page viewer (unless the user decided otherwise, see DocumentMetadata.page_blank)
    blank: bool = False


class TextPages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    page_count: int
    pages: list[PageText]
    extracted_at: str
    # True once the user edited the text; re-extraction then keeps the confirmed text.
    user_confirmed: bool = False


class TaxonomyTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["correspondent", "document_type", "tag"]
    name: str
    aliases: list[str] = Field(default_factory=list)
    origin: str = "user"
    created_at: str


class TaxonomyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    terms: list[TaxonomyTerm] = Field(default_factory=list)
