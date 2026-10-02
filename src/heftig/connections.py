"""Checks behind the "Test" buttons of the setup and settings pages: does the AI answer, which
models does an endpoint offer, does the mailbox accept the login? Each returns quickly and turns
failures into one understandable sentence (ConnectionProblem)."""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from typing import Any

import httpx

from .config import Settings, with_headers
from .i18n import _
from .providers import registry
from .providers.base import ProviderError

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
OPENAI_BASE = "https://api.openai.com/v1"
TIMEOUT = 20

# mail providers people use most: IMAP server and what to know about the password
IMAP_PRESETS: dict[str, dict[str, Any]] = {
    "gmail.com": {"host": "imap.gmail.com", "app_password": True},
    "googlemail.com": {"host": "imap.gmail.com", "app_password": True},
    "outlook.com": {"host": "outlook.office365.com"},
    "hotmail.com": {"host": "outlook.office365.com"},
    "live.com": {"host": "outlook.office365.com"},
    "icloud.com": {"host": "imap.mail.me.com", "app_password": True},
    "me.com": {"host": "imap.mail.me.com", "app_password": True},
    "yahoo.com": {"host": "imap.mail.yahoo.com", "app_password": True},
    "fastmail.com": {"host": "imap.fastmail.com", "app_password": True},
    "posteo.de": {"host": "posteo.de"},
    "posteo.net": {"host": "posteo.de"},
    "mailbox.org": {"host": "imap.mailbox.org"},
    "gmx.de": {"host": "imap.gmx.net"},
    "gmx.net": {"host": "imap.gmx.net"},
    "gmx.com": {"host": "imap.gmx.com"},
    "web.de": {"host": "imap.web.de"},
    "t-online.de": {"host": "secureimap.t-online.de"},
    # Proton's IMAP only runs through its Bridge (STARTTLS with its own certificate), which the
    # verified TLS connection here does not accept
    "proton.me": {"host": "", "unsupported": True},
    "protonmail.com": {"host": "", "unsupported": True},
}


# one line of the IMAP LIST answer: (flags) "delimiter" name
_LIST_LINE = re.compile(r'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$')


class ConnectionProblem(Exception):
    pass


def imap_preset(address: str) -> dict[str, Any] | None:
    domain = address.rpartition("@")[2].strip().lower()
    preset = IMAP_PRESETS.get(domain)
    return {"port": 993, **preset} if preset else None


# --- AI -------------------------------------------------------------------------------------


def test_ai(settings: Settings) -> str:
    """One tiny request to the classification model; returns the model that answered."""
    try:
        classifier = registry.get_classifier(settings)
    except ProviderError as e:
        raise ConnectionProblem(str(e)) from e
    ask = getattr(classifier, "complete_json", None)
    if ask is None:
        return ""  # offline rules: nothing to test
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    try:
        # room for models that think first (reasoning tokens count against the limit)
        data = ask('Answer with the JSON object {"ok": true}.', "Test", schema, 2000)
    except ProviderError as e:
        if re.search(r"\b(401|403)\b", str(e)):
            raise ConnectionProblem(_("The key was not accepted.") + f" ({e})") from e
        raise ConnectionProblem(str(e)) from e
    except Exception as e:  # noqa: BLE001 - SDK errors of all kinds: one sentence for the page
        raise ConnectionProblem(f"{type(e).__name__}: {str(e)[:200]}") from e
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ConnectionProblem(_("The model answered, but not in the expected form."))
    return getattr(classifier, "model", "")


def list_models(
    provider: str, base_url: str, api_key: str | None, headers: dict[str, str] | None = None
) -> list[str]:
    """Model names the endpoint offers (for a selection list); ``headers``: the additional
    headers of an OpenAI-compatible endpoint."""
    sent: dict[str, str] = {}
    if provider == "anthropic":
        base = (base_url or ANTHROPIC_BASE).rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        sent = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
    else:
        base = (base_url or (OPENAI_BASE if provider == "openai" else "")).rstrip("/")
        if not base:
            raise ConnectionProblem(_("Enter the address of the server first."))
        if api_key:
            sent["Authorization"] = f"Bearer {api_key}"
        sent = with_headers(sent, headers or {})
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as c:
            r = c.get(f"{base}/models", headers=sent, params={"limit": 1000})
    except httpx.HTTPError as e:
        raise ConnectionProblem(
            _("No connection to %(host)s (%(error)s).", host=base, error=type(e).__name__)
        ) from e
    if r.status_code in (401, 403):
        raise ConnectionProblem(_("The key was not accepted."))
    if r.status_code >= 400:
        raise ConnectionProblem(f"HTTP {r.status_code}")
    try:
        items = r.json().get("data") or r.json().get("models") or []
    except (ValueError, AttributeError) as e:
        raise ConnectionProblem(_("Unexpected answer from the server.")) from e
    names = [str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict)]
    return sorted({n for n in names if n and n != "None"})


# --- e-mail ---------------------------------------------------------------------------------


def test_imap(host: str, port: int, user: str, password: str) -> list[str]:
    """Log in and list the mailbox folders."""
    if not (host and user and password):
        raise ConnectionProblem(_("Server, user name and password are needed."))
    try:
        c = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    except (socket.gaierror, ConnectionRefusedError) as e:
        raise ConnectionProblem(_("Server %(host)s not found or not reachable.", host=host)) from e
    except ssl.SSLError as e:
        raise ConnectionProblem(
            _("Secure connection to %(host)s failed (%(error)s).", host=host, error=e.reason)
        ) from e
    except OSError as e:
        raise ConnectionProblem(_("No connection to %(host)s (%(error)s).", host=host,
                                  error=type(e).__name__)) from e  # fmt: skip
    try:
        try:
            c.login(user, password)
        except imaplib.IMAP4.error as e:
            raise ConnectionProblem(
                _("The server refused the login: %(error)s", error=str(e)[:200])
            ) from e
        _typ, data = c.list()
        folders = []
        for line in data or []:
            m = _LIST_LINE.match(line.decode("ascii", "replace") if isinstance(line, bytes) else "")
            if m and "\\noselect" not in m.group(1).lower():
                name = m.group(2).strip()
                folders.append(name[1:-1] if name.startswith('"') else name)
        return folders
    finally:
        try:
            c.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
