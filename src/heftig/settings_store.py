"""Settings made in the web interface (setup and settings page), stored in the archive.

Priority: an environment variable (``HEFTIG_*``, ``.env``, ``*_FILE``) wins over a stored value,
which wins over the default. A setting given by the environment is shown as fixed in the web
interface. Stored values live in the database's ``meta`` table (keys ``setting.<name>``, JSON
values), so they travel with the archive and its database backups. Secrets (API keys, the mail
password) are stored there too - the database is readable by its owner only - and are never
shown again, only replaced. Exports do not contain them.

Every change increases ``settings_revision``; web server and worker compare it and reload.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import SecretStr, ValidationError

from .config import Settings
from .db import get_meta, set_meta, write_tx

PREFIX = "setting."
REVISION = "settings_revision"

# what the web interface may change; everything else stays an environment/.env setting
EDITABLE = {
    "language",
    # AI
    "ocr_provider", "ocr_model", "ocr_base_url", "ocr_api_key", "allow_cloud_ocr",
    "classify_provider", "classify_model", "classify_base_url", "classify_api_key",
    "allow_cloud_classify", "ai_search_model", "ocr_languages",
    "embed_provider", "embed_model", "embed_base_url", "embed_api_key", "allow_cloud_embed",
    # e-mail
    "imap_host", "imap_port", "imap_user", "imap_password", "imap_mailbox", "imap_move_to",
    "imap_delete_after_import", "imap_allowed_senders",
    # paper, scanner, trash
    "auto_file_sources", "consume_after", "filing_granularity", "trash_retention_days",
}  # fmt: skip
SECRETS = {"ocr_api_key", "classify_api_key", "embed_api_key", "imap_password"}


class SettingsError(ValueError):
    pass


def stored(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'setting.%'")
    out = {}
    for key, value in rows:
        name = key[len(PREFIX) :]
        if name in EDITABLE:
            try:
                out[name] = json.loads(value)
            except ValueError:
                continue
    return out


def revision(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, REVISION, "0") or 0)


def fixed(base: Settings) -> set[str]:
    """Editable settings given by the environment (or by the code that built ``base``);
    their ``*_file`` twins count too."""
    given = set(base.model_fields_set)
    given |= {n[: -len("_file")] for n in given if n.endswith("_file")}
    return given & EDITABLE


def effective(base: Settings, conn: sqlite3.Connection) -> Settings:
    """``base`` (from the environment) with the stored values for everything it leaves open."""
    values = {k: v for k, v in stored(conn).items() if k not in fixed(base)}
    if not values:
        return base
    try:
        return Settings.model_validate({**dict(base), **values})
    except ValidationError:
        # one broken stored value must not stop Heftig: use the valid ones
        good = {}
        for k, v in values.items():
            try:
                Settings.model_validate({**dict(base), k: v})
                good[k] = v
            except ValidationError:
                continue
        return Settings.model_validate({**dict(base), **good})


def save(conn: sqlite3.Connection, base: Settings, changes: dict[str, Any]) -> Settings:
    """Validate and store changes; returns the new effective settings.

    Empty secrets mean "keep the stored one"; ``None`` removes a stored value (default again).
    Raises SettingsError for unknown, fixed or invalid settings.
    """
    blocked = fixed(base)
    clean: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in EDITABLE:
            raise SettingsError(f"not editable: {name}")
        if name in blocked:
            raise SettingsError(f"set by the environment: {name}")
        if name in SECRETS and value == "":
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        clean[name] = value
    current = effective(base, conn)
    candidate = {**dict(current), **{k: v for k, v in clean.items() if v is not None}}
    try:
        Settings.model_validate(candidate)
    except ValidationError as e:
        err = e.errors()[0]
        raise SettingsError(f"{'.'.join(map(str, err['loc']))}: {err['msg']}") from e
    with write_tx(conn):
        for name, value in clean.items():
            if value is None:
                conn.execute("DELETE FROM meta WHERE key = ?", (PREFIX + name,))
            else:
                set_meta(conn, PREFIX + name, json.dumps(value, ensure_ascii=False))
        set_meta(conn, REVISION, str(revision(conn) + 1))
    return effective(base, conn)


def has_secret(settings: Settings, name: str) -> bool:
    try:
        return bool(settings.secret(name))
    except OSError:
        return False
