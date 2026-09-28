"""Request helpers: authentication, CSRF protection, consistent errors."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import auth
from ..archive import Archive
from ..i18n import _

SESSION_COOKIE = "heftig_session"


@dataclass
class Principal:
    user: auth.User
    via: str  # "session" | "token"
    csrf: str | None
    scope: str = "full"  # API tokens may be "read" (only GET requests)


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status_code=status, detail={"code": code, "message": message})


def get_archive(request: Request) -> Archive:
    return request.app.state.archive


def client_address(request: Request) -> str:
    if request.app.state.archive.settings.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # the right-most entry was added by our (trusted) proxy; earlier ones are client-controlled
            return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    s = request.app.state.archive.settings
    if s.cookie_secure == "true":
        return True
    if s.cookie_secure == "false":
        return False
    if s.trust_proxy_headers and request.headers.get("x-forwarded-proto") == "https":
        return True
    return request.url.scheme == "https"


def principal(request: Request) -> Principal | None:
    conn = get_archive(request).conn
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        found = auth.token_principal(conn, authz[7:].strip())
        return Principal(found[0], "token", None, found[1]) if found else None
    found = auth.session_user(conn, request.cookies.get(SESSION_COOKIE))
    if found:
        return Principal(found[0], "session", found[1])
    return None


def require_user(request: Request) -> Principal:
    p = principal(request)
    if p is None:
        raise ApiError(401, "unauthorized", _("Sign-in required."))
    request.state.principal = p
    return p


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        # non-browser clients, privacy settings or sandboxed contexts send no usable origin;
        # the per-session CSRF token (checked separately) and SameSite=Strict still protect
        return True
    o = urlparse(origin)
    hosts = {request.headers.get("host", "")}
    if request.app.state.archive.settings.trust_proxy_headers:
        hosts.add(request.headers.get("x-forwarded-host", "").split(",")[0].strip())
    return o.netloc in hosts


async def require_write(request: Request) -> Principal:
    """Authenticated + CSRF-checked for cookie sessions (tokens are CSRF-immune)."""
    p = await run_in_threadpool(require_user, request)
    if p.via == "token":
        if p.scope != "full":
            raise ApiError(403, "read_only", _("This API token is read-only."))
        return p
    sent = request.headers.get("x-csrf-token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            sent = str(form.get("csrf_token") or "")
    token_ok = bool(sent and p.csrf) and hmac.compare_digest(
        sent.encode("utf-8", "replace"), p.csrf.encode("utf-8")
    )
    if not token_ok or not _same_origin(request):
        raise ApiError(403, "csrf", _("Security check failed (CSRF). Reload the page."))
    return p
