"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__, i18n
from ..archive import Archive
from ..config import Settings, get_settings
from ..documents import DocumentNotFound
from . import api, setup, ui

HERE = Path(__file__).parent
log = logging.getLogger("heftig.web")
SLOW_MS = 300

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "frame-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; "
    "form-action 'self'"
)


# the only requests that carry files; everything else (forms, JSON, login) is small, and a
# big body there would only be parsed - before authentication - to exhaust memory
UPLOAD_PATHS = re.compile(
    r"/(api/documents|upload|documents/[^/]+/notes|api/documents/[^/]+/attachments)"
)
SMALL_BODY = 1024 * 1024


class BodyLimit:
    """ASGI middleware: abort requests whose body exceeds the limit (413) - the upload limit
    for the upload routes, 1 MB for everything else."""

    def __init__(self, app, max_bytes: int, small_bytes: int = SMALL_BODY):
        self.app = app
        self.max_bytes = max_bytes
        self.small_bytes = small_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = (
            self.max_bytes
            if UPLOAD_PATHS.fullmatch(scope.get("path") or "")
            else min(self.small_bytes, self.max_bytes)
        )
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None and cl.isdigit() and int(cl) > limit:
            return await _too_large(send, limit, scope)
        seen = 0

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge()
            return msg

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(send, limit, scope)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send, limit: int, scope) -> None:
    # runs before the middleware that sets the request's language
    app = scope.get("app")
    lang = app.state.archive.settings.language if app is not None else None
    with i18n.language(lang):
        msg = i18n._("Request too large (max. %(mb)s MB).", mb=max(1, limit // (1024 * 1024)))
    body = json.dumps({"error": {"code": "too_large", "message": msg}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = FastAPI(
        title="Heftig",
        version=__version__,
        description="Local document archive – REST API. Authentication by session "
        "(browser, with CSRF token) or `Authorization: Bearer <API token>`.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.archive = Archive(settings)
    app.state.templates = ui.make_templates(HERE / "templates")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        archive = request.app.state.archive
        archive.refresh_settings()  # changed on the settings page or by the worker's process
        i18n.set_language(archive.settings.language)  # for this request's task
        response = await call_next(request)
        ms = (time.perf_counter() - started) * 1000
        if ms > SLOW_MS:  # the path only - query strings may contain search words
            log.info("slow request: %s %s %.0f ms", request.method, request.url.path, ms)
        response.headers["Server-Timing"] = f"app;dur={ms:.0f}"
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        ctype = h.get("content-type", "")
        path = request.url.path
        if path.startswith("/static/vendor/") or (
            path.startswith("/static/") and request.query_params.get("v")
        ):
            # versioned files (third-party ones, and ours with ?v=<content hash>): cache long
            h["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            h["Cache-Control"] = "no-cache"  # always revalidated (cheap: ETag -> 304)
        if path == "/scan":
            # document camera: camera access, and WebAssembly for OpenCV.js
            h["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(self)"
            h["Content-Security-Policy"] = CSP.replace(
                "script-src 'self'", "script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'"
            ).replace(
                "img-src 'self' data: blob:",
                # OpenCV.js instantiates its embedded WebAssembly from a data: URL
                "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' data:",
            )
        elif request.url.path == "/api/docs":
            # Swagger UI is served from the jsDelivr CDN by FastAPI; everything else is local
            h["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: "
                "https://fastapi.tiangolo.com; frame-ancestors 'self'"
            )
        elif ctype.startswith("text/html"):
            h.setdefault("Content-Security-Policy", CSP)
        if not path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            err = detail
        else:
            err = {"code": _code(exc.status_code), "message": str(detail)}
        if not request.url.path.startswith("/api/") and exc.status_code == 401:
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        if not request.url.path.startswith("/api/") and "text/html" in request.headers.get(
            "accept", ""
        ):
            return ui.error_page(request, exc.status_code, err["message"])
        return JSONResponse({"error": err}, status_code=exc.status_code)

    @app.exception_handler(DocumentNotFound)
    async def not_found(request: Request, exc: DocumentNotFound):
        if not request.url.path.startswith("/api/") and "text/html" in request.headers.get(
            "accept", ""
        ):
            return ui.error_page(request, 404, i18n._("Document not found."))
        return JSONResponse(
            {"error": {"code": "not_found", "message": i18n._("Document not found.")}},
            status_code=404,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errs = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(
            {"error": {"code": "validation", "message": i18n._("Invalid input"), "details": errs}},
            status_code=422,
        )

    app.include_router(api.health_router)
    app.include_router(api.router)
    app.include_router(ui.router)
    app.include_router(setup.router)
    setup.ensure_setup_token(app.state.archive)  # first start: the code for /setup
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(BodyLimit, max_bytes=settings.max_upload_bytes * 5 + 1024 * 1024)
    return app


def _code(status: int) -> str:
    return {
        400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
        405: "method_not_allowed", 409: "conflict", 413: "too_large", 422: "invalid",
        429: "rate_limited",
    }.get(status, "error")  # fmt: skip
