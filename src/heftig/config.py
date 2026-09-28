"""Runtime configuration.

All settings come from environment variables with the ``HEFTIG_`` prefix (or a ``.env`` file).
Secrets can alternatively be read from files (``*_FILE`` variants) so that container secrets
work without putting passwords into the environment.
"""

from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .i18n import N_

OcrProviderName = Literal["none", "tesseract", "openai", "anthropic", "openai_compatible", "mock"]
ClassifyProviderName = Literal["none", "rules", "openai", "anthropic", "openai_compatible", "mock"]

CLOUD_PROVIDERS = {"openai", "anthropic"}


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8").strip()


# host names that always mean "this machine" (container -> host) and the CGNAT range used by
# Tailscale and similar VPNs
LOCAL_HOSTNAMES = {"localhost", "host.docker.internal", "host.containers.internal"}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_local_url(url: str) -> bool:
    """True if the URL points to this machine or a private network address."""
    host = urlparse(url).hostname or ""
    if host in LOCAL_HOSTNAMES or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not (addr.is_loopback or addr.is_private or addr.is_link_local or addr in _CGNAT):
            return False
    return bool(infos)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEFTIG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # "HEFTIG_X=" (empty) means "not set": it must not lock the setting on the settings page
        env_ignore_empty=True,
    )

    # --- storage -----------------------------------------------------------------------
    archive_dir: Path = Path("./archive")
    consume_dir: Path | None = None  # default: <archive>/consume
    # optional second watched folder for digital files (source "folder", no paper filing)
    folder_dir: Path | None = None

    # --- language ----------------------------------------------------------------------
    # interface language, and the language of AI-written titles, summaries and explanations
    language: Literal["en", "de"] = "en"

    # --- web ---------------------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8765
    # "auto": Secure cookie flag when the request came in via HTTPS (incl. X-Forwarded-Proto
    # from a trusted reverse proxy). "true"/"false" force it.
    cookie_secure: Literal["auto", "true", "false"] = "auto"
    trust_proxy_headers: bool = False
    session_hours: int = 24 * 14
    login_max_attempts: int = 5
    login_window_seconds: int = 300

    # --- limits ------------------------------------------------------------------------
    max_upload_mb: int = 100
    max_pages: int = 500
    max_image_megapixels: int = 150
    ocr_dpi: int = 300
    # cloud OCR: long side of the page image sent (providers downscale bigger images anyway;
    # also keeps each image below their size limit) and pages recognised in parallel per document
    ocr_max_side: int = Field(default=2000, ge=800, le=4000)
    ocr_page_concurrency: int = Field(default=3, ge=1, le=8)
    # optional stronger model for page 1 (letterhead, sender, date, subject); Anthropic only
    ocr_first_page_model: str = ""
    # cost guards for cloud OCR: pages with less ink than this share of the page (after
    # trimming the margins) count as blank and are read locally; at most this many pages per
    # document go to the AI, further pages are read locally (0 = no limit)
    ocr_blank_max_ink: float = Field(default=0.001, ge=0, le=0.05)
    ocr_ai_max_pages: int = Field(default=30, ge=0, le=5000)
    # wait this long after an AI rate limit when the provider does not say (seconds)
    rate_limit_pause_seconds: int = Field(default=90, ge=10, le=3600)
    ocr_page_timeout_seconds: int = 180
    min_text_chars_per_page: int = 40  # below this a PDF page counts as image-only -> OCR

    # --- worker ------------------------------------------------------------------------
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    job_max_attempts: int = 5
    job_backoff_seconds: int = 30
    job_lease_seconds: int = 900
    consume_poll_seconds: int = 10
    consume_stable_polls: int = 2
    consume_min_age_seconds: int = 5
    # a PDF/JPEG without its end marker is waited for this long after its last change
    consume_incomplete_wait_seconds: int = 900
    consume_after: Literal["delete", "move"] = "delete"
    consume_max_failures: int = 3
    auto_file_sources: str = ""  # comma list, e.g. "scanner": mark paper as filed on ingest
    filing_granularity: Literal["month", "year"] = "month"
    raw_response_retention_days: int = 30
    trash_retention_days: int = Field(default=30, ge=1, le=3650)  # Papierkorb, then purged
    # automatic consistent copy of the database (backup/index-snapshot.sqlite) every n hours;
    # a file backup tool (Kopia, restic, ...) then always finds a recent one. 0 = off
    db_snapshot_hours: int = Field(default=6, ge=0, le=24 * 30)
    # truly identical documents (same text, every page visually the same): second copy -> trash
    auto_resolve_identical: bool = True
    raw_response_max_kb: int = 64

    # --- OCR / text --------------------------------------------------------------------
    ocr_provider: OcrProviderName = "tesseract"
    ocr_languages: str = "deu+eng"
    ocr_model: str = ""
    ocr_base_url: str = ""
    ocr_api_key: SecretStr | None = None
    ocr_api_key_file: str | None = None
    ocr_supports_images: bool = True  # capability of an openai_compatible endpoint
    anthropic_ocr_effort: str = "low"  # output_config.effort for OCR calls ("" = model default)
    allow_cloud_ocr: bool = False

    # --- classification ----------------------------------------------------------------
    classify_provider: ClassifyProviderName = "rules"
    classify_model: str = ""
    classify_base_url: str = ""
    classify_api_key: SecretStr | None = None
    classify_api_key_file: str | None = None
    classify_json_mode: Literal["schema", "object", "none"] = "schema"
    classify_max_chars: int = 24000
    classify_min_confidence: float = 0.6
    # AI search (question -> filters) with Anthropic: a small, fast model; empty = classify model
    ai_search_model: str = "claude-haiku-4-5-20251001"
    allow_cloud_classify: bool = False
    provider_timeout_seconds: int = 120
    # when a remote AI provider is unreachable: process locally right away (Tesseract / rules)
    # and redo the AI steps automatically once it answers again
    ai_fallback: bool = True
    ai_retry_minutes: int = Field(default=60, ge=5)
    ai_catch_up_batch: int = Field(default=25, ge=1, le=500)

    # --- IMAP --------------------------------------------------------------------------
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: SecretStr | None = None
    imap_password_file: str | None = None
    imap_mailbox: str = "INBOX"
    imap_move_to: str = ""  # optional folder for processed mails; else just mark as seen
    # delete mails whose documents are all safely archived (dedicated mailbox only). Goes to the
    # server's trash folder if there is one (auto-detected or imap_trash_mailbox), else expunged.
    imap_delete_after_import: bool = False
    imap_trash_mailbox: str = ""
    imap_poll_seconds: int = 300
    imap_max_attachment_mb: int = 50
    imap_skip_inline_images_below_kb: int = 30
    imap_skip_filename_patterns: str = "logo*,image0*,signature*"
    imap_archive_eml: bool = False
    # who may send documents: comma list of addresses and/or "@domain" - empty = everyone (the
    # address is then effectively public input). Mail from others is refused and recorded.
    imap_allowed_senders: str = ""
    imap_max_attachments: int = Field(default=20, ge=1, le=500)  # per message

    @property
    def imap_allowed_sender_set(self) -> set[str]:
        return {x.strip().lower() for x in self.imap_allowed_senders.split(",") if x.strip()}

    # --- misc --------------------------------------------------------------------------
    mock_fail: str = ""  # test hook for the mock provider, e.g. "ocr:2" fails OCR on page 2
    log_level: str = "INFO"

    @field_validator("archive_dir", mode="after")
    @classmethod
    def _abs_archive(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    # -- derived -------------------------------------------------------------------------
    @property
    def consume_path(self) -> Path:
        return (self.consume_dir or self.archive_dir / "consume").expanduser().resolve()

    @property
    def folder_path(self) -> Path | None:
        return self.folder_dir.expanduser().resolve() if self.folder_dir else None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def auto_file_source_set(self) -> set[str]:
        return {s.strip() for s in self.auto_file_sources.split(",") if s.strip()}

    def secret(self, name: str) -> str | None:
        """Resolve a secret from the value or its ``*_file`` twin. Never logged."""
        value = getattr(self, name, None)
        if isinstance(value, SecretStr) and value.get_secret_value():
            return value.get_secret_value()
        return _read_secret_file(getattr(self, f"{name}_file", None))

    def provider_is_cloud(self, provider: str, base_url: str) -> bool:
        if provider in CLOUD_PROVIDERS:
            return True
        if provider == "openai_compatible":
            return not (base_url and is_local_url(base_url))
        return False

    def ocr_blocked_reason(self) -> str | None:
        if (
            self.provider_is_cloud(self.ocr_provider, self.ocr_base_url)
            and not self.allow_cloud_ocr
        ):
            return N_(
                "Cloud OCR is not allowed (set HEFTIG_ALLOW_CLOUD_OCR=true if documents may be "
                "sent to this provider)."
            )
        return None

    def classify_blocked_reason(self) -> str | None:
        if (
            self.provider_is_cloud(self.classify_provider, self.classify_base_url)
            and not self.allow_cloud_classify
        ):
            return N_(
                "Cloud classification is not allowed (set HEFTIG_ALLOW_CLOUD_CLASSIFY=true if "
                "document texts may be sent to this provider)."
            )
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
