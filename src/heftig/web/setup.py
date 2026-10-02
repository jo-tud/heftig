"""First start and connections in the browser: account, AI, e-mail, scanner. No .env needed.

First start: as long as there is no user, ``<archive>/setup-token`` holds a random code (also
logged). ``/setup?token=<code>`` creates the account - whoever reaches the port first cannot
take over the archive without it. After that the same pages are part of the settings
(``/settings/ai``, ``/settings/mail``, ``/settings/scanner``); ``?setup=1`` shows them as the
steps of the first setup.

Everything saved here goes to :mod:`heftig.settings_store`. A setting given by an environment
variable is shown, but cannot be changed here.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import auth, connections, i18n
from .. import settings_store as store
from ..archive import Archive
from ..config import Settings, is_local_url, parse_headers
from ..i18n import _, translate_text
from . import api
from .deps import ApiError, Principal, client_address, get_archive, require_user, require_write
from .ui import _browser_only, redirect, render

log = logging.getLogger("heftig.web")
router = APIRouter(include_in_schema=False, dependencies=[Depends(_browser_only)])

TOKEN_FILE = "setup-token"  # noqa: S105 - a file name
STEPS = [
    ("ai", "/settings/ai"),
    ("search", "/settings/search"),
    ("mail", "/settings/mail"),
    ("scanner", "/settings/scanner"),
]

# what the AI choices mean; model defaults are a good balance of cost and quality
AI_MODES = ("offline", "anthropic", "openai", "local")
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-5-mini", "local": ""}
DEFAULT_LOCAL_URL = "http://localhost:11434/v1"  # see default_local_url()
ANTHROPIC_SEARCH_MODEL = "claude-haiku-4-5-20251001"
AI_KEYS = {
    "ocr_provider", "ocr_model", "ocr_base_url", "ocr_api_key", "allow_cloud_ocr",
    "classify_provider", "classify_model", "classify_base_url", "classify_api_key",
    "allow_cloud_classify", "ai_search_model",
}  # fmt: skip
MAIL_KEYS = {
    "imap_host", "imap_port", "imap_user", "imap_password", "imap_mailbox", "imap_move_to",
    "imap_delete_after_import", "imap_allowed_senders", "imap_mail_keyword",
}  # fmt: skip
# additional HTTP headers of an OpenAI-compatible server: given by the environment they stay
# as they are, without locking the rest of the page
HEADER_KEYS = {"classify_headers", "ocr_headers"}
SCANNER_KEYS = {"auto_file_sources", "consume_after"}
SEARCH_KEYS = {"semantic_search"}


# --- first start ----------------------------------------------------------------------------


def token_path(archive: Archive) -> Path:
    return archive.paths.root / TOKEN_FILE


def ensure_setup_token(archive: Archive) -> str | None:
    """Without a user: the setup code (created once); with users: none (file removed)."""
    path = token_path(archive)
    if auth.user_count(archive.conn) > 0:
        path.unlink(missing_ok=True)
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) < 16:
        token = secrets.token_urlsafe(18)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
    log.warning("No account yet. Set Heftig up at: http://<this computer>:<port>/setup?token=%s",
                token)  # fmt: skip
    return token


def _token_ok(archive: Archive, sent: str) -> bool:
    try:
        token = token_path(archive).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(sent and token) and hmac.compare_digest(sent.encode(), token.encode())


@router.get("/setup")
def setup_page(request: Request, token: str = "", lang: str = ""):
    a = get_archive(request)
    if auth.user_count(a.conn) > 0:
        return redirect("/settings/ai?setup=1")
    lang = (lang if lang in i18n.LANGUAGES else None) or (
        a.settings.language if "language" in store.fixed(a.base_settings) else None
    )
    with i18n.language(lang or i18n.pick(request.headers.get("accept-language"))):
        return render(request, "setup_account.html", token=token[:100],
                      token_ok=_token_ok(a, token), error=None, form={})  # fmt: skip


@router.post("/setup")
async def setup_submit(request: Request):
    form = await request.form()
    return await run_in_threadpool(_setup_account, request, form)


def _setup_account(request: Request, form) -> Any:
    a = get_archive(request)
    f = {k: str(form.get(k) or "") for k in ("token", "language", "username", "password",
                                             "password2")}  # fmt: skip
    lang = f["language"] if f["language"] in i18n.LANGUAGES else "en"
    if auth.user_count(a.conn) > 0:
        return redirect("/login")
    with i18n.language(lang):
        error = None
        if not _token_ok(a, f["token"]):
            error = _("The setup code is not correct.")
        elif not f["username"].strip():
            error = _("Please choose a user name.")
        elif f["password"] != f["password2"]:
            error = _("The two passwords are not the same.")
        else:
            try:
                auth.create_user(a.conn, f["username"].strip(), f["password"])
            except auth.AuthError as e:  # too short, or a second tab was faster
                error = str(e)
        if error:
            return render(request, "setup_account.html", http_status=400, token=f["token"],
                          token_ok=_token_ok(a, f["token"]), error=error, form=f)  # fmt: skip
    token_path(a).unlink(missing_ok=True)
    if "language" not in store.fixed(a.base_settings):
        a.settings = store.save(a.conn, a.base_settings, {"language": lang})
    s = a.settings
    session, _csrf = auth.login(
        a.conn, f["username"].strip(), f["password"], client_address(request),
        max_attempts=s.login_max_attempts, window=s.login_window_seconds,
        session_hours=s.session_hours,
    )  # fmt: skip
    resp = redirect("/settings/ai?setup=1")
    api.set_session_cookie(request, resp, session)
    return resp


# --- shared --------------------------------------------------------------------------------


def _ctx(request: Request, step: str, **kw: Any) -> dict[str, Any]:
    a = get_archive(request)
    wizard = request.query_params.get("setup") == "1" or kw.pop("wizard", False)
    names = [s for s, _url in STEPS]
    nxt = names.index(step) + 1
    return {
        "nav": "settings",
        "wizard": wizard,
        "step": step,
        "steps": STEPS,
        "next_url": (STEPS[nxt][1] + "?setup=1") if nxt < len(STEPS) else "/",
        "fixed": store.fixed(a.base_settings),
        "s": a.settings,
        **kw,
    }


def _save(request: Request, changes: dict[str, Any]) -> Settings:
    a = get_archive(request)
    a.settings = store.save(a.conn, a.base_settings, changes)
    a.refresh_settings()
    return a.settings


def saved_message(code: str | None, s: Settings) -> str | None:
    """The confirmation after saving (a code in the URL, never free text)."""
    return {
        "offline": _("Saved: everything stays on this computer."),
        "ai": _("Saved. The model %(model)s answered.", model=s.classify_model),
        "mail": _("Saved. Signed in to the mailbox; new e-mails are checked every few minutes."),
        "mail_off": _("E-mail import is off."),
        "ok": _("Saved."),
    }.get(code or "")


def _done(request: Request, step: str, code: str) -> Any:
    if request.query_params.get("setup") == "1":
        return redirect(_ctx(request, step)["next_url"])
    return redirect(f"/settings/{step}?saved={code}")


# --- AI ------------------------------------------------------------------------------------


def default_local_url() -> str:
    """Where Ollama on this computer is reached from here: from a container through the host's
    name (Docker: host.docker.internal, Podman: host.containers.internal), else localhost."""
    for name in ("host.docker.internal", "host.containers.internal"):
        try:
            socket.getaddrinfo(name, None)
            return f"http://{name}:11434/v1"
        except OSError:
            continue
    return DEFAULT_LOCAL_URL


def ai_mode(s: Settings) -> str:
    p = s.classify_provider
    if p == "anthropic":
        return "anthropic"
    if p == "openai":
        return "openai"
    if p == "openai_compatible":
        return "local"
    return "offline"


def _ai_view(request: Request, **kw: Any) -> Any:
    s = get_archive(request).settings
    mode = kw.pop("mode", None) or ai_mode(s)
    form = kw.pop("form", None) or {
        "mode": mode,
        "model": s.classify_model or DEFAULT_MODELS.get(mode, ""),
        "base_url": s.classify_base_url or default_local_url(),
        "ocr_ai": s.ocr_provider in ("anthropic", "openai", "openai_compatible"),
        "consent": s.allow_cloud_classify,
    }
    ctx = _ctx(request, "ai", form=form, has_key=store.has_secret(s, "classify_api_key"),
               has_headers=store.has_secret(s, "classify_headers"),
               defaults=DEFAULT_MODELS, default_url=default_local_url(), **kw)  # fmt: skip
    ctx["locked"] = bool(ctx["fixed"] & AI_KEYS)
    ctx["headers_fixed"] = "classify_headers" in ctx["fixed"]
    return render(request, "setup_ai.html", **ctx)


@router.get("/settings/ai")
def ai_page(request: Request, p: Principal = Depends(require_user)):
    q = request.query_params
    return _ai_view(request, message=saved_message(q.get("saved"), get_archive(request).settings))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower() if url else ""


def same_endpoint(current: Settings, provider: str, base_url: str) -> bool:
    """Does the stored classification key belong to this provider and server?"""
    return current.classify_provider == provider and _host(current.classify_base_url) == _host(
        base_url
    )


def ai_changes(form: dict[str, Any], current: Settings) -> tuple[dict[str, Any], str | None]:
    """The settings for an AI choice, or an error message."""
    mode = form.get("mode")
    if mode not in AI_MODES:
        return {}, _("Please choose one of the options.")
    if mode == "offline":
        return {"classify_provider": "rules", "ocr_provider": "tesseract",
                "allow_cloud_classify": False, "allow_cloud_ocr": False}, None  # fmt: skip
    key = str(form.get("api_key") or "").strip()
    model = str(form.get("model") or "").strip() or DEFAULT_MODELS[mode]
    base = str(form.get("base_url") or "").strip() if mode == "local" else ""
    if mode == "local" and not base:
        return {}, _("Please enter the address of the model server.")
    if not model:
        return {}, _("Please enter a model name.")
    headers = str(form.get("headers") or "").strip() if mode == "local" else ""
    try:
        parse_headers(headers)
    except ValueError as e:
        return {}, translate_text(str(e))
    provider = {"anthropic": "anthropic", "openai": "openai", "local": "openai_compatible"}[mode]
    # a stored key is only used again for the same provider AND the same server
    same = same_endpoint(current, provider, base)
    if (
        mode in ("anthropic", "openai")
        and not key
        and not (same and store.has_secret(current, "classify_api_key"))
    ):
        return {}, _("Please enter the API key.")
    cloud = mode != "local" or not is_local_url(base)
    consent = bool(form.get("consent"))
    if cloud and not consent:
        return {}, _("Please confirm that document texts may be sent to this service.")
    ocr_ai = bool(form.get("ocr_ai"))
    # the stored headers, like the key, only for the same server - unless they are removed
    keep_headers = same and mode == "local" and not form.get("clear_headers")
    changes: dict[str, Any] = {
        "classify_provider": provider,
        "classify_model": model,
        "classify_base_url": base,
        "classify_api_key": key if key else ("" if same else None),
        "classify_headers": headers or ("" if keep_headers else None),
        "allow_cloud_classify": cloud and consent,
        "ocr_provider": provider if ocr_ai else "tesseract",
        "ocr_model": model if ocr_ai else "",
        "ocr_base_url": base if ocr_ai else "",
        # the OCR key: the new one, or the stored one of the same provider - never another's
        "ocr_api_key": (key or (current.secret("classify_api_key") if same else None) or None)
        if ocr_ai
        else None,
        "ocr_headers": (
            headers or (current.secret("classify_headers") if keep_headers else None) or None
        )
        if ocr_ai
        else None,
        "allow_cloud_ocr": bool(ocr_ai and cloud and consent),
        "ai_search_model": ANTHROPIC_SEARCH_MODEL if mode == "anthropic" else "",
    }
    return changes, None


@router.post("/settings/ai")
async def ai_submit(request: Request, p: Principal = Depends(require_write)):
    form = dict(await request.form())
    return await run_in_threadpool(_ai_submit, request, form)


def _ai_submit(request: Request, form: dict[str, Any]) -> Any:
    a = get_archive(request)
    changes, error = ai_changes(form, a.settings)
    view = {"mode": form.get("mode"), "model": form.get("model", ""),
            "base_url": form.get("base_url", ""), "ocr_ai": bool(form.get("ocr_ai")),
            "consent": bool(form.get("consent"))}  # fmt: skip
    if error:
        return _ai_view(request, form=view, error=error)
    for name in HEADER_KEYS & store.fixed(a.base_settings):
        changes.pop(name, None)  # set by the environment
    try:
        s = _save(request, changes)
    except store.SettingsError as e:
        return _ai_view(request, form=view, error=str(e))
    if form.get("mode") == "offline":
        return _done(request, "ai", "offline")
    try:
        connections.test_ai(s)
    except connections.ConnectionProblem as e:
        return _ai_view(request, form=view,
                        error=_("Saved, but the test failed: %(error)s", error=str(e)))  # fmt: skip
    return _done(request, "ai", "ai")


@router.post("/settings/ai/models")
async def ai_models(request: Request, p: Principal = Depends(require_write)):
    form = dict(await request.form())
    return await run_in_threadpool(_ai_models, request, form)


def _ai_models(request: Request, form: dict[str, Any]) -> JSONResponse:
    s = get_archive(request).settings
    mode = str(form.get("mode") or "")
    if mode not in ("anthropic", "openai", "local"):
        return JSONResponse({"models": []})
    provider = {"anthropic": "anthropic", "openai": "openai", "local": "openai_compatible"}[mode]
    # the server address only counts for a local server (the field is hidden otherwise)
    base = str(form.get("base_url") or "").strip() if mode == "local" else ""
    key = str(form.get("api_key") or "").strip()
    same = same_endpoint(s, provider, base)
    if not key and same:
        key = s.secret("classify_api_key") or ""
    try:
        headers = parse_headers(str(form.get("headers") or "")) if mode == "local" else {}
        if not headers and same and mode == "local" and not form.get("clear_headers"):
            headers = s.provider_headers("classify")
    except (ValueError, OSError) as e:
        return JSONResponse({"models": [], "error": translate_text(str(e))})
    try:
        models = connections.list_models(provider, base, key or None, headers=headers)
    except connections.ConnectionProblem as e:
        return JSONResponse({"models": [], "error": str(e)})
    return JSONResponse({"models": models[:300]})


# --- e-mail ---------------------------------------------------------------------------------


def _mail_view(request: Request, **kw: Any) -> Any:
    s = get_archive(request).settings
    after = "delete" if s.imap_delete_after_import else ("move" if s.imap_move_to else "seen")
    form = kw.pop("form", None) or {
        "imap_user": s.imap_user, "imap_host": s.imap_host, "imap_port": s.imap_port,
        "imap_mailbox": s.imap_mailbox, "imap_move_to": s.imap_move_to, "after": after,
        "imap_allowed_senders": s.imap_allowed_senders.replace(",", "\n"),
        "imap_mail_keyword": s.imap_mail_keyword,
    }  # fmt: skip
    presets = {d: connections.imap_preset("x@" + d) for d in connections.IMAP_PRESETS}
    ctx = _ctx(request, "mail", form=form, has_password=store.has_secret(s, "imap_password"),
               presets_json=json.dumps(presets), **kw)  # fmt: skip
    ctx["locked"] = bool(ctx["fixed"] & MAIL_KEYS)
    return render(request, "setup_mail.html", **ctx)


@router.get("/settings/mail")
def mail_page(request: Request, p: Principal = Depends(require_user)):
    q = request.query_params
    return _mail_view(request, message=saved_message(q.get("saved"), get_archive(request).settings))


@router.post("/settings/mail")
async def mail_submit(request: Request, p: Principal = Depends(require_write)):
    form = dict(await request.form())
    return await run_in_threadpool(_mail_submit, request, form)


def mail_changes(form: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if form.get("action") == "off":
        return {"imap_host": ""}, None
    user = str(form.get("imap_user") or "").strip()
    host = str(form.get("imap_host") or "").strip()
    if not host and "@" in user:
        host = (connections.imap_preset(user) or {}).get("host", "")
    if not (user and host):
        return {}, _("Please enter the e-mail address (user name) and the server.")
    try:
        port = int(str(form.get("imap_port") or "993"))
    except ValueError:
        return {}, _("The port must be a number (usually 993).")
    senders = [x.strip() for x in str(form.get("imap_allowed_senders") or "").replace(
        ",", "\n").splitlines() if x.strip()]  # fmt: skip
    after = form.get("after")
    move_to = str(form.get("imap_move_to") or "").strip() if after == "move" else ""
    if after == "move" and not move_to:
        return {}, _("Please enter the folder the e-mails are moved to.")
    keyword = str(form.get("imap_mail_keyword") or "").strip()
    if len(keyword.split()) > 1 or len(keyword) > 40:
        return {}, _("The keyword must be a single word (e.g. #mail).")
    return {
        "imap_host": host, "imap_port": port, "imap_user": user,
        "imap_password": str(form.get("imap_password") or ""),
        "imap_mailbox": str(form.get("imap_mailbox") or "").strip() or "INBOX",
        "imap_move_to": move_to, "imap_delete_after_import": after == "delete",
        "imap_allowed_senders": ",".join(senders), "imap_mail_keyword": keyword,
    }, None  # fmt: skip


def _mail_submit(request: Request, form: dict[str, Any]) -> Any:
    a = get_archive(request)
    changes, error = mail_changes(form)
    view = {k: form.get(k, "") for k in ("imap_user", "imap_host", "imap_port", "imap_mailbox",
                                         "imap_move_to", "after", "imap_allowed_senders",
                                         "imap_mail_keyword")}  # fmt: skip
    if error:
        return _mail_view(request, form=view, error=error)
    cur = a.settings
    # the stored password only for the same server and user - never sent to another server
    same = (cur.imap_host.lower(), cur.imap_user.lower()) == (
        str(changes.get("imap_host", "")).lower(),
        str(changes.get("imap_user", "")).lower(),
    )
    if (
        changes.get("imap_host")
        and not changes.get("imap_password")
        and not (same and store.has_secret(cur, "imap_password"))
    ):
        return _mail_view(request, form=view, error=_("Please enter the password."))
    try:
        s = _save(request, changes)
    except store.SettingsError as e:
        return _mail_view(request, form=view, error=str(e))
    if not s.imap_host:
        return _done(request, "mail", "mail_off")
    try:
        folders = connections.test_imap(s.imap_host, s.imap_port, s.imap_user,
                                        s.secret("imap_password") or "")  # fmt: skip
    except connections.ConnectionProblem as e:
        return _mail_view(request, form=view,
                          error=_("Saved, but the test failed: %(error)s", error=str(e)))  # fmt: skip
    missing = [f for f in (s.imap_mailbox, s.imap_move_to) if f and f not in folders]
    if missing:
        return _mail_view(request, form=view, error=_(
            "Signed in, but the folder %(folder)s does not exist. Folders: %(folders)s",
            folder=missing[0], folders=", ".join(folders[:30])))  # fmt: skip
    return _done(request, "mail", "mail")


# --- search by meaning ---------------------------------------------------------------------


@router.get("/settings/search")
def search_page(request: Request, p: Principal = Depends(require_user)):
    from .. import semantic
    from ..db import get_meta
    from ..local_embed import DEFAULT

    a = get_archive(request)
    q = request.query_params
    ctx = _ctx(request, "search", message=saved_message(q.get("saved"), a.settings),
               model=DEFAULT, status=semantic.status(a.conn, a.settings),
               last_error=get_meta(a.conn, semantic.ERROR_KEY) or "")  # fmt: skip
    # the assistant suggests it; afterwards the page shows what is set
    ctx["checked"] = True if ctx["wizard"] else a.settings.semantic_search
    ctx["locked"] = bool(ctx["fixed"] & SEARCH_KEYS)
    return render(request, "setup_search.html", **ctx)


@router.get("/settings/search/progress")
def search_progress(request: Request, p: Principal = Depends(require_user)):
    """The number of prepared documents, for the settings pages to update in place."""
    from .. import semantic

    a = get_archive(request)
    return {"active": semantic.available(a.settings), **semantic.status(a.conn, a.settings)}


@router.post("/settings/search")
async def search_submit(request: Request, p: Principal = Depends(require_write)):
    form = dict(await request.form())
    return await run_in_threadpool(_search_submit, request, form)


def _search_submit(request: Request, form: dict[str, Any]) -> Any:
    try:
        _save(request, {"semantic_search": form.get("semantic") == "1"})
    except store.SettingsError as e:
        raise ApiError(400, "invalid", str(e)) from e
    return _done(request, "search", "ok")


# --- scanner --------------------------------------------------------------------------------


@router.get("/settings/scanner")
def scanner_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    q = request.query_params
    ctx = _ctx(request, "scanner", message=saved_message(q.get("saved"), a.settings),
               host_dir=a.settings.host_scanner_dir,
               consume_dir=str(a.settings.consume_path))  # fmt: skip
    ctx["locked"] = bool(ctx["fixed"] & SCANNER_KEYS)
    return render(request, "setup_scanner.html", **ctx)


@router.post("/settings/scanner")
async def scanner_submit(request: Request, p: Principal = Depends(require_write)):
    form = dict(await request.form())
    return await run_in_threadpool(_scanner_submit, request, form)


def _scanner_submit(request: Request, form: dict[str, Any]) -> Any:
    s = get_archive(request).settings
    sources = s.auto_file_source_set - {"scanner"}
    if form.get("auto_file"):
        sources.add("scanner")
    after = form.get("consume_after") if form.get("consume_after") in ("delete", "move") else None
    changes: dict[str, Any] = {"auto_file_sources": ",".join(sorted(sources))}
    if after:
        changes["consume_after"] = after
    try:
        _save(request, changes)
    except store.SettingsError as e:
        raise ApiError(400, "invalid", str(e)) from e
    if request.query_params.get("setup") == "1":
        return redirect("/?welcome=1")
    return redirect("/settings/scanner?saved=ok")
