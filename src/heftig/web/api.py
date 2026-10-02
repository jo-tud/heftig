"""REST API (JSON). Documented via OpenAPI at /api/docs."""

from __future__ import annotations

import json
import mimetypes
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import auth, jobs, maintenance, saved_searches, trash
from .. import documents as docs
from .. import taxonomy as tax
from ..db import write_tx
from ..i18n import _
from ..ingest import ingest_stream
from ..models import DocumentMetadata
from ..processing import reprocess
from ..providers.registry import describe
from ..search import (
    SearchParams,
    SearchSyntaxError,
    field_values,
    find_lines,
    search,
    similar,
    suggest,
)
from .deps import (
    SESSION_COOKIE,
    ApiError,
    Principal,
    client_address,
    get_archive,
    is_https,
    require_user,
    require_write,
)

router = APIRouter(prefix="/api")


# --- auth --------------------------------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


def set_session_cookie(request: Request, response, token: str) -> None:
    s = get_archive(request).settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=s.session_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=is_https(request),
        path="/",
    )


def do_login(request: Request, username: str, password: str) -> tuple[str, str]:
    a = get_archive(request)
    s = a.settings
    try:
        return auth.login(
            a.conn, username, password, client_address(request),
            max_attempts=s.login_max_attempts, window=s.login_window_seconds,
            session_hours=s.session_hours,
        )  # fmt: skip
    except auth.RateLimited as e:
        raise ApiError(429, "rate_limited", str(e)) from e
    except auth.AuthError as e:
        raise ApiError(401, "invalid_credentials", str(e)) from e


@router.post("/auth/login", tags=["auth"])
def api_login(request: Request, body: LoginBody):
    token, csrf = do_login(request, body.username, body.password)
    resp = JSONResponse({"ok": True, "csrf_token": csrf})
    set_session_cookie(request, resp, token)
    return resp


@router.post("/auth/logout", tags=["auth"])
def api_logout(request: Request, p: Principal = Depends(require_write)):
    auth.logout(get_archive(request).conn, request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/me", tags=["auth"])
def api_me(p: Principal = Depends(require_user)):
    return {"username": p.user.username, "via": p.via, "csrf_token": p.csrf}


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scope: Literal["full", "read"] = "full"


@router.get("/tokens", tags=["auth"])
def api_tokens(request: Request, p: Principal = Depends(require_user)):
    return auth.list_tokens(get_archive(request).conn)


@router.post("/tokens", tags=["auth"], status_code=201)
def api_token_create(request: Request, body: TokenBody, p: Principal = Depends(require_write)):
    if p.via != "session":
        raise ApiError(403, "forbidden", _("API tokens can only be created in the browser."))
    tid, token = auth.create_api_token(get_archive(request).conn, p.user.id, body.name, body.scope)
    return {"id": tid, "token": token, "note": _("Shown only now.")}


@router.delete("/tokens/{token_id}", tags=["auth"])
def api_token_revoke(request: Request, token_id: int, p: Principal = Depends(require_write)):
    if not auth.revoke_token(get_archive(request).conn, token_id):
        raise ApiError(404, "not_found", _("Token not found or already revoked."))
    return {"ok": True}


# --- documents -----------------------------------------------------------------------


@router.post("/documents", tags=["documents"])
async def api_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    kind: Literal["digital", "paper"] = Form("digital"),
    p: Principal = Depends(require_write),
):
    a = get_archive(request)
    source = "api" if p.via == "token" else "web"
    results = []
    for f in files:
        res = await run_in_threadpool(
            ingest_stream, a, f.file, f.filename, source,
            {"client": "api" if p.via == "token" else "browser", "kind": kind},
            paper=(kind == "paper"),
        )  # fmt: skip
        results.append(res.as_dict())
        await f.close()
    status = 200
    if results and all(r["status"] == "rejected" for r in results):
        status = 422
    return JSONResponse({"results": results}, status_code=status)


def _search_params(
    q: str = "",
    correspondent: list[str] = Query(default=[]),
    document_type: list[str] = Query(default=[]),
    tag: list[str] = Query(default=[]),
    date_from: str | None = None,
    date_to: str | None = None,
    received_from: str | None = None,
    received_to: str | None = None,
    source: list[str] = Query(default=[]),
    email_from: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    filed: Literal["yes", "no"] | None = None,
    filing_section: str | None = None,
    filing_binder: str | None = None,
    cf_key: str | None = None,
    cf_min: float | None = None,
    cf_max: float | None = None,
    sort: str | None = None,
    page: int = Query(1, ge=1, le=10_000),
    per_page: int = Query(25, ge=1, le=100),
    tag_mode: Literal["all", "any"] = "all",
    literal: bool = False,
    session: str | None = None,
    meaning: bool = True,
) -> SearchParams:
    return SearchParams(
        q=q, correspondent=correspondent, document_type=document_type, tags=tag,
        date_from=date_from or None, date_to=date_to or None,
        received_from=received_from or None, received_to=received_to or None,
        source=source, email_from=email_from, status=status, filed=filed,
        filing_section=filing_section or None,
        filing_binder=filing_binder or None,
        cf_key=cf_key or None, cf_min=cf_min, cf_max=cf_max, sort=sort, page=page,
        per_page=per_page, tag_mode=tag_mode, literal=literal, session=session or None,
        meaning=meaning,
    )  # fmt: skip


@router.get("/documents", tags=["documents"])
def api_search(
    request: Request,
    params: SearchParams = Depends(_search_params),
    facets: Literal["false", "true", "0", "1", "within"] = "false",
    p: Principal = Depends(require_user),
):
    """``facets=true``: counts as on the search page (each group without its own filter);
    ``facets=within``: every count within all filters (statistics). ``meaning=false``: words
    only, without the search by meaning (which is used when it is switched on and documents
    are embedded)."""
    from .ui import meaning_embedder

    a = get_archive(request)
    mode = {"false": False, "0": False, "true": True, "1": True, "within": "within"}[facets]
    return search(a.conn, params, with_facets=mode, embedder=meaning_embedder(a, params)).as_dict()


@router.get("/suggest", tags=["search"])
def api_suggest(request: Request, q: str = "", p: Principal = Depends(require_user)):
    """Suggestions while typing: correspondents/types/tags (as filters), numbers, documents."""
    return suggest(get_archive(request).conn, q)


@router.get("/lines", tags=["search"])
def api_lines(
    request: Request,
    pattern: str = Query(..., min_length=1, max_length=200),
    regex: bool = False,
    context: int = Query(0, ge=0, le=3),
    limit: int = Query(200, ge=1, le=1000),
    params: SearchParams = Depends(_search_params),
    p: Principal = Depends(require_user),
):
    """Text lines matching `pattern` (with page numbers) in all documents within the filters."""
    try:
        return find_lines(
            get_archive(request), params, pattern, regex=regex, context=context, limit=limit
        )
    except SearchSyntaxError as e:
        raise ApiError(422, "invalid", str(e)) from e


@router.get("/fields", tags=["search"])
def api_fields(
    request: Request,
    key: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
    params: SearchParams = Depends(_search_params),
    p: Principal = Depends(require_user),
):
    """Custom field values of the documents within the filters (or the available fields)."""
    try:
        return field_values(get_archive(request).conn, params, key, limit)
    except SearchSyntaxError as e:
        raise ApiError(422, "invalid", str(e)) from e


@router.get("/documents/{doc_id}/similar", tags=["search"])
def api_similar(request: Request, doc_id: str, p: Principal = Depends(require_user)):
    _load(request, doc_id)
    return {"items": similar(get_archive(request).conn, doc_id)}


class SavedSearchIn(BaseModel):
    name: str
    query: str


@router.get("/searches", tags=["search"])
def api_saved_searches(request: Request, p: Principal = Depends(require_user)):
    return {"searches": saved_searches.load(get_archive(request).paths)}


@router.post("/searches", tags=["search"], status_code=201)
def api_save_search(request: Request, body: SavedSearchIn, p: Principal = Depends(require_write)):
    try:
        return saved_searches.add(get_archive(request).paths, body.name, body.query)
    except saved_searches.SavedSearchError as e:
        raise ApiError(422, "invalid", str(e)) from e


@router.delete("/searches/{search_id}", tags=["search"])
def api_delete_search(request: Request, search_id: str, p: Principal = Depends(require_write)):
    if not saved_searches.delete(get_archive(request).paths, search_id):
        raise ApiError(404, "not_found", _("Saved search not found."))
    return {"deleted": search_id}


def _load(request: Request, doc_id: str):
    try:
        return docs.load_meta(get_archive(request), doc_id)
    except docs.DocumentNotFound as e:
        raise ApiError(404, "not_found", _("Document not found.")) from e


def document_detail(request: Request, doc_id: str) -> dict[str, Any]:
    a = get_archive(request)
    meta = _load(request, doc_id)
    pos = docs.filing_position(a, meta)
    doc_jobs = [
        jobs.as_dict(r)
        for r in a.conn.execute(
            "SELECT * FROM jobs WHERE doc_id=? ORDER BY id DESC LIMIT 10", (doc_id,)
        )
    ]
    runs = [
        {k: r[k] for k in r.keys() if k != "raw_response"}  # noqa: SIM118 - sqlite3.Row
        for r in a.conn.execute(
            "SELECT * FROM processing_runs WHERE doc_id=? ORDER BY id DESC LIMIT 20", (doc_id,)
        )
    ]
    for r in runs:
        r["suggestions"] = json.loads(r["suggestions"]) if r.get("suggestions") else None
    tp = docs.load_text_pages(a, doc_id)
    return {
        "metadata": meta.model_dump(mode="json"),
        "filing_position": pos.__dict__ if pos else None,
        "pages": [
            {
                "page": pg.page,
                "method": pg.method,
                "provider": pg.provider,
                "chars": pg.chars,
                "error": pg.error,
            }  # fmt: skip
            for pg in (tp.pages if tp else [])
        ],
        "jobs": doc_jobs,
        "processing_runs": runs,
        "providers": describe(a.settings),
    }


@router.get("/documents/{doc_id}", tags=["documents"])
def api_document(request: Request, doc_id: str, p: Principal = Depends(require_user)):
    return document_detail(request, doc_id)


def original_response(
    request: Request, doc_id: str, inline: bool, meta: DocumentMetadata | None = None
) -> FileResponse:
    """The original file; ``meta`` for a document outside the archive (a source document)."""
    a = get_archive(request)
    meta = meta or _load(request, doc_id)
    path = a.paths.resolve(meta.original_relpath)
    if not path.exists():
        raise ApiError(410, "original_missing", _("Original file is missing – run `heftig check`."))
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "X-Heftig-SHA256": meta.sha256,
    }
    if inline and meta.mime_type != "application/pdf":
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    ext = mimetypes.guess_extension(meta.mime_type) or ""
    filename = (
        meta.original_filename
        if meta.original_filename.lower().endswith(ext)
        else (meta.original_filename + ext)
    )
    return FileResponse(
        path,
        media_type=meta.mime_type,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


def mail_attachment_response(request: Request, doc_id: str, index: int) -> Response:
    """One attachment of an archived e-mail, taken from the original - always as a download."""
    from urllib.parse import quote

    from .. import mail

    a = get_archive(request)
    meta = _load(request, doc_id)
    path = a.paths.resolve(meta.original_relpath)
    if meta.mime_type != mail.MIME:
        raise ApiError(404, "not_found", _("Attachment not found."))
    if not path.exists():
        raise ApiError(410, "original_missing", _("Original file is missing – run `heftig check`."))
    try:
        part, data = mail.attachment(path.read_bytes(), index)
    except IndexError as e:
        raise ApiError(404, "not_found", _("Attachment not found.")) from e
    ascii_name = part.filename.encode("ascii", "replace").decode().replace('"', "'")
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(part.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/documents/{doc_id}/mail-attachments/{index}", tags=["documents"])
def api_mail_attachment(
    request: Request, doc_id: str, index: int, p: Principal = Depends(require_user)
):
    return mail_attachment_response(request, doc_id, index)


@router.get("/documents/{doc_id}/original", tags=["documents"])
def api_original(
    request: Request, doc_id: str, inline: bool = False, p: Principal = Depends(require_user)
):
    return original_response(request, doc_id, inline)


@router.get("/documents/{doc_id}/text", tags=["documents"])
def api_text(
    request: Request,
    doc_id: str,
    format: Literal["plain", "pages"] = "plain",
    p: Principal = Depends(require_user),
):
    a = get_archive(request)
    _load(request, doc_id)
    if format == "pages":
        tp = docs.load_text_pages(a, doc_id)
        return tp.model_dump(mode="json") if tp else {"pages": []}
    return PlainTextResponse(docs.get_text(a, doc_id))


class PatchBody(BaseModel):
    title: str | None = None
    document_date: str | None = None
    correspondent: str | None = None
    document_type: str | None = None
    tags: list[str] | None = None
    summary: str | None = None
    custom_fields: dict[str, dict[str, Any]] | None = None
    locks: dict[str, bool] | None = None


@router.patch("/documents/{doc_id}", tags=["documents"])
def api_patch(
    request: Request, doc_id: str, body: PatchBody, p: Principal = Depends(require_write)
):
    changes = body.model_dump(exclude_unset=True)
    locks = changes.pop("locks", None)
    _load(request, doc_id)
    try:
        meta = docs.update_fields(get_archive(request), doc_id, changes, locks)
    except docs.EditError as e:
        raise ApiError(422, "invalid", str(e)) from e
    except ValueError as e:
        raise ApiError(422, "invalid", str(e)) from e
    return meta.model_dump(mode="json")


@router.delete("/documents/{doc_id}", tags=["documents"])
def api_delete(
    request: Request, doc_id: str, confirm: str = "", p: Principal = Depends(require_write)
):
    """Move to the trash (restorable, purged after HEFTIG_TRASH_RETENTION_DAYS)."""
    if confirm != doc_id:
        raise ApiError(422, "confirm_required", _("To confirm, pass ?confirm=<id>."))
    try:
        return {"trashed": True, **trash.trash_document(get_archive(request), doc_id, by="api")}
    except docs.DocumentNotFound as e:
        raise ApiError(404, "not_found", _("Document not found.")) from e


@router.get("/trash", tags=["documents"])
def api_trash(request: Request, p: Principal = Depends(require_user)):
    return {"groups": trash.listing(get_archive(request))}


@router.post("/trash/{doc_id}/restore", tags=["documents"])
def api_trash_restore(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    try:
        meta = trash.restore(get_archive(request), doc_id)
    except trash.TrashError as e:
        raise ApiError(409, "not_restorable", str(e)) from e
    return {"restored": meta.id}


@router.delete("/trash/{doc_id}", tags=["documents"])
def api_trash_purge(
    request: Request, doc_id: str, confirm: str = "", p: Principal = Depends(require_write)
):
    if confirm != doc_id:
        raise ApiError(422, "confirm_required", _("Delete permanently: pass ?confirm=<id>."))
    try:
        trash.purge(get_archive(request), doc_id)
    except trash.TrashError as e:
        raise ApiError(404, "not_found", str(e)) from e
    return {"purged": doc_id}


@router.get("/sources", tags=["documents"])
def api_sources(request: Request, p: Principal = Depends(require_user)):
    """Source documents: the documents that were combined into another one, kept for good."""
    return {"groups": trash.sources_listing(get_archive(request))}


@router.get("/sources/{doc_id}/original", tags=["documents"])
def api_source_original(
    request: Request, doc_id: str, inline: bool = False, p: Principal = Depends(require_user)
):
    try:
        meta = trash.source_meta(get_archive(request), doc_id)
    except docs.DocumentNotFound as e:
        raise ApiError(404, "not_found", _("Document not found.")) from e
    return original_response(request, doc_id, inline, meta=meta)


@router.delete("/sources/{doc_id}", tags=["documents"])
def api_source_delete(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    """Move a source document to the trash (purged after HEFTIG_TRASH_RETENTION_DAYS)."""
    try:
        trash.discard_source(get_archive(request), doc_id, by="api")
    except trash.TrashError as e:
        raise ApiError(404, "not_found", str(e)) from e
    return {"trashed": doc_id}


class CombineBody(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=20)


@router.post("/documents/combine", tags=["documents"], status_code=201)
def api_combine(request: Request, body: CombineBody, p: Principal = Depends(require_write)):
    """Combine documents (pages in this order) into a new one; the parts are kept as source
    documents (GET /api/sources, never purged), batch ``combine-<new id>``. Undo:
    POST /api/documents/{new id}/uncombine."""
    from .. import combine

    for i in body.ids:
        _load(request, i)
    try:
        meta = combine.combine(get_archive(request), body.ids, by="api")
    except combine.CombineError as e:
        raise ApiError(409, "not_combinable", str(e)) from e
    batch = combine.batch_for(meta.id)
    return {"document_id": meta.id, "batch": batch, "trash_batch": batch}  # trash_batch: older name


@router.post("/documents/{doc_id}/uncombine", tags=["documents"])
def api_uncombine(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    from .. import combine

    r = combine.undo(get_archive(request), doc_id)
    if not r["restored"]:
        raise ApiError(
            409, "not_restorable", "; ".join(sorted(set(r["failed"]))) or _("Nothing to do.")
        )
    return {"restored": r["ids"]}


class SplitBody(BaseModel):
    parts: list[list[int]] = Field(min_length=1, max_length=500)
    rotation: dict[int, int] = Field(default_factory=dict)


@router.post("/documents/{doc_id}/split", tags=["documents"], status_code=201)
def api_split(
    request: Request, doc_id: str, body: SplitBody, p: Principal = Depends(require_write)
):
    """New documents from this document's pages: ``parts`` lists the page numbers (1-based) of
    each new document in order - pages in no part are left out; ``rotation`` turns pages
    (page -> 0/90/180/270 degrees clockwise, absolute; missing pages keep their turn). The
    first part keeps the metadata. The original goes to the trash as batch
    ``split-<id>``. Undo: POST /api/documents/{id}/unsplit."""
    from .. import split

    _load(request, doc_id)
    try:
        metas = split.split(get_archive(request), doc_id, body.parts, body.rotation, by="api")
    except split.SplitError as e:
        raise ApiError(409, "not_splittable", str(e)) from e
    return {"document_ids": [m.id for m in metas], "trash_batch": split.batch_for(doc_id)}


@router.post("/documents/{doc_id}/unsplit", tags=["documents"])
def api_unsplit(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    """Undo a split: the original (``doc_id``) comes back, its parts go to the trash."""
    from .. import split

    r = split.undo(get_archive(request), doc_id)
    if not r["restored"]:
        raise ApiError(
            409, "not_restorable", "; ".join(sorted(set(r["failed"]))) or _("Nothing to do.")
        )
    return {"restored": r["ids"]}


class FilingBody(BaseModel):
    action: Literal["file", "unfile"] = "file"


@router.post("/documents/{doc_id}/filing", tags=["documents"])
def api_filing(
    request: Request, doc_id: str, body: FilingBody, p: Principal = Depends(require_write)
):
    _load(request, doc_id)
    a = get_archive(request)
    meta = docs.mark_filed(a, doc_id) if body.action == "file" else docs.unmark_filed(a, doc_id)
    pos = docs.filing_position(a, meta)
    return {
        "metadata": meta.model_dump(mode="json"),
        "filing_position": pos.__dict__ if pos else None,
    }


class ReprocessBody(BaseModel):
    stages: list[Literal["extract", "classify"]] = ["extract", "classify"]
    ids: list[str] | None = None
    all: bool = False


@router.post("/documents/{doc_id}/reprocess", tags=["jobs"])
def api_reprocess_one(
    request: Request, doc_id: str, body: ReprocessBody, p: Principal = Depends(require_write)
):
    _load(request, doc_id)
    ids = reprocess(get_archive(request), [doc_id], list(body.stages))
    return {"job_ids": ids, "providers": describe(get_archive(request).settings)}


@router.post("/reprocess", tags=["jobs"])
def api_reprocess(request: Request, body: ReprocessBody, p: Principal = Depends(require_write)):
    a = get_archive(request)
    if body.all:
        ids = [r[0] for r in a.conn.execute("SELECT id FROM documents ORDER BY ingest_sequence")]
    else:
        ids = body.ids or []
    for i in ids:
        _load(request, i)
    return {"job_ids": reprocess(a, ids, list(body.stages)), "documents": len(ids)}


@router.post("/documents/{doc_id}/suggestions/{index}/{action}", tags=["documents"])
def api_suggestion(
    request: Request,
    doc_id: str,
    index: int,
    action: Literal["accept", "dismiss"],
    p: Principal = Depends(require_write),
):
    a = get_archive(request)
    _load(request, doc_id)
    try:
        meta = (docs.accept_suggestion if action == "accept" else docs.dismiss_suggestion)(
            a, doc_id, index
        )
    except docs.EditError as e:
        raise ApiError(422, "invalid", str(e)) from e
    return meta.model_dump(mode="json")


# --- jobs & inbox ----------------------------------------------------------------------


@router.get("/jobs", tags=["jobs"])
def api_jobs(
    request: Request,
    status: list[str] = Query(default=[]),
    limit: int = Query(100, le=500),
    p: Principal = Depends(require_user),
):
    conn = get_archive(request).conn
    sql, params = "SELECT * FROM jobs", []
    if status:
        sql += f" WHERE status IN ({','.join('?' for _s in status)})"
        params = list(status)
    sql += " ORDER BY id DESC LIMIT ?"
    return {
        "counts": jobs.counts(conn),
        "jobs": [jobs.as_dict(r) for r in conn.execute(sql, [*params, limit])],
    }


@router.post("/jobs/{job_id}/retry", tags=["jobs"])
def api_retry(request: Request, job_id: int, p: Principal = Depends(require_write)):
    if not jobs.retry(get_archive(request).conn, job_id):
        raise ApiError(409, "not_retryable", _("The job has not failed."))
    return {"ok": True}


@router.get("/ingest-events", tags=["jobs"])
def api_events(
    request: Request,
    result: list[str] = Query(default=[]),
    limit: int = Query(100, le=500),
    p: Principal = Depends(require_user),
):
    conn = get_archive(request).conn
    sql, params = "SELECT * FROM ingest_events", []
    if result:
        sql += f" WHERE result IN ({','.join('?' for _r in result)})"
        params = list(result)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = []
    for r in conn.execute(sql, [*params, limit]):
        d = dict(r)
        d["source_details"] = json.loads(d["source_details"] or "{}")
        rows.append(d)
    return rows


# --- notes & attachments ----------------------------------------------------------------


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


def _edit_call(fn, *args):
    try:
        return fn(*args)
    except docs.EditError as e:
        raise ApiError(422, "invalid", str(e)) from e


@router.post("/documents/{doc_id}/notes", tags=["documents"], status_code=201)
def api_note_add(
    request: Request, doc_id: str, body: NoteBody, p: Principal = Depends(require_write)
):
    _load(request, doc_id)
    meta = _edit_call(docs.add_note, get_archive(request), doc_id, body.text)
    return meta.notes[-1].model_dump()


@router.patch("/documents/{doc_id}/notes/{note_id}", tags=["documents"])
def api_note_edit(
    request: Request,
    doc_id: str,
    note_id: str,
    body: NoteBody,
    p: Principal = Depends(require_write),
):
    _load(request, doc_id)
    meta = _edit_call(docs.edit_note, get_archive(request), doc_id, note_id, body.text)
    return [n.model_dump() for n in meta.notes]


@router.delete("/documents/{doc_id}/notes/{note_id}", tags=["documents"])
def api_note_delete(
    request: Request, doc_id: str, note_id: str, p: Principal = Depends(require_write)
):
    _load(request, doc_id)
    _edit_call(docs.delete_note, get_archive(request), doc_id, note_id)
    return {"ok": True}


@router.post("/documents/{doc_id}/attachments", tags=["documents"], status_code=201)
async def api_attachment_add(
    request: Request,
    doc_id: str,
    files: list[UploadFile] = File(...),
    description: str = Form(""),
    p: Principal = Depends(require_write),
):
    a = get_archive(request)
    _load(request, doc_id)
    added = []
    for f in files:
        meta = await run_in_threadpool(
            _edit_call, docs.add_attachment, a, doc_id, f.file, f.filename, description
        )
        added.append(meta.attachments[-1].model_dump())
        await f.close()
    return {"attachments": added}


INLINE_ATTACHMENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}


def attachment_response(request: Request, doc_id: str, att_id: str, inline: bool) -> FileResponse:
    a = get_archive(request)
    meta = _load(request, doc_id)
    att = next((x for x in meta.attachments if x.id == att_id), None)
    if att is None:
        raise ApiError(404, "not_found", _("Attachment not found."))
    path = _edit_call(docs.attachment_path, a, doc_id, att_id)
    if not path.exists():
        raise ApiError(
            410, "attachment_missing", _("Attachment file is missing – run `heftig check`.")
        )
    inline = inline and att.mime_type in INLINE_ATTACHMENT_TYPES
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}
    if att.mime_type != "application/pdf":
        # uploaded files are never allowed to run anything in the browser
        headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    return FileResponse(
        path,
        media_type=att.mime_type if inline else "application/octet-stream",
        filename=att.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


@router.get("/documents/{doc_id}/attachments/{att_id}", tags=["documents"])
def api_attachment_get(
    request: Request,
    doc_id: str,
    att_id: str,
    inline: bool = False,
    p: Principal = Depends(require_user),
):
    return attachment_response(request, doc_id, att_id, inline)


@router.delete("/documents/{doc_id}/attachments/{att_id}", tags=["documents"])
def api_attachment_delete(
    request: Request, doc_id: str, att_id: str, p: Principal = Depends(require_write)
):
    _load(request, doc_id)
    _edit_call(docs.delete_attachment, get_archive(request), doc_id, att_id)
    return {"ok": True}


# --- duplicates ------------------------------------------------------------------------


class KeepBody(BaseModel):
    a: str
    b: str


@router.get("/duplicates", tags=["duplicates"])
def api_duplicates(request: Request, p: Principal = Depends(require_user)):
    from ..duplicates import open_pairs

    return open_pairs(get_archive(request).conn)


@router.post("/duplicates/keep", tags=["duplicates"])
def api_duplicates_keep(request: Request, body: KeepBody, p: Principal = Depends(require_write)):
    from ..duplicates import keep_both

    _load(request, body.a)
    _load(request, body.b)
    keep_both(get_archive(request), body.a, body.b)
    return {"ok": True}


@router.post("/duplicates/scan", tags=["duplicates"])
def api_duplicates_scan(request: Request, p: Principal = Depends(require_write)):
    from ..duplicates import scan_all

    return {"open": scan_all(get_archive(request))}


# --- taxonomy --------------------------------------------------------------------------


class TermBody(BaseModel):
    kind: Literal["correspondent", "document_type", "tag"]
    name: str = Field(min_length=1, max_length=200)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    keep_alias: bool = True


class MergeBody(BaseModel):
    into: int


class AliasBody(BaseModel):
    alias: str = Field(min_length=1, max_length=200)


def _tax_call(fn, *args):
    try:
        return fn(*args)
    except tax.TaxonomyError as e:
        raise ApiError(409, "taxonomy_conflict", str(e)) from e


@router.get("/taxonomy", tags=["taxonomy"])
def api_taxonomy(request: Request, kind: str | None = None, p: Principal = Depends(require_user)):
    return [t.__dict__ for t in tax.list_terms(get_archive(request).conn, kind)]


@router.post("/taxonomy", tags=["taxonomy"], status_code=201)
def api_term_create(request: Request, body: TermBody, p: Principal = Depends(require_write)):
    a = get_archive(request)
    with write_tx(a.conn):
        tid = _tax_call(tax.get_or_create, a.conn, body.kind, body.name, "user")
        tax.write_sidecar(a.conn, a.paths)
    return {"id": tid}


@router.patch("/taxonomy/{term_id}", tags=["taxonomy"])
def api_term_rename(
    request: Request, term_id: int, body: RenameBody, p: Principal = Depends(require_write)
):
    n = _tax_call(docs.rename_term, get_archive(request), term_id, body.name, body.keep_alias)
    return {"documents_updated": n}


@router.post("/taxonomy/{term_id}/merge", tags=["taxonomy"])
def api_term_merge(
    request: Request, term_id: int, body: MergeBody, p: Principal = Depends(require_write)
):
    n = _tax_call(docs.merge_terms, get_archive(request), term_id, body.into)
    return {"documents_updated": n}


@router.post("/taxonomy/{term_id}/aliases", tags=["taxonomy"])
def api_alias_add(
    request: Request, term_id: int, body: AliasBody, p: Principal = Depends(require_write)
):
    _tax_call(docs.add_term_alias, get_archive(request), term_id, body.alias)
    return {"ok": True}


@router.delete("/taxonomy/{term_id}/aliases/{alias}", tags=["taxonomy"])
def api_alias_remove(
    request: Request, term_id: int, alias: str, p: Principal = Depends(require_write)
):
    docs.remove_term_alias(get_archive(request), term_id, alias)
    return {"ok": True}


@router.delete("/taxonomy/{term_id}", tags=["taxonomy"])
def api_term_delete(request: Request, term_id: int, p: Principal = Depends(require_write)):
    return {"documents_updated": _tax_call(docs.delete_term, get_archive(request), term_id)}


# --- maintenance -----------------------------------------------------------------------


class ExportBody(BaseModel):
    zip: bool = False


class ImportBody(BaseModel):
    path: str = Field(description="Path relative to <archive>/imports/")


@router.post("/export", tags=["maintenance"], status_code=202)
def api_export(request: Request, body: ExportBody, p: Principal = Depends(require_write)):
    jid = jobs.enqueue(get_archive(request).conn, "export", None, {"zip": body.zip}, max_attempts=1)
    return {"job_id": jid}


@router.post("/import", tags=["maintenance"], status_code=202)
def api_import(request: Request, body: ImportBody, p: Principal = Depends(require_write)):
    a = get_archive(request)
    base = (a.paths.root / "imports").resolve()
    target = (base / body.path).resolve()
    if base not in target.parents or not target.exists():
        raise ApiError(
            422, "invalid_path", _("The path must be inside <archive>/imports/ and exist.")
        )
    jid = jobs.enqueue(a.conn, "import", None, {"path": str(target)}, max_attempts=1)
    return {"job_id": jid}


@router.post("/index/rebuild", tags=["maintenance"], status_code=202)
def api_reindex(request: Request, p: Principal = Depends(require_write)):
    return {"job_id": jobs.enqueue(get_archive(request).conn, "reindex", None, {}, max_attempts=1)}


@router.get("/status", tags=["maintenance"])
def api_status(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    return {**maintenance.status(a), "providers": describe(a.settings)}


def readiness(request: Request) -> tuple[bool, dict[str, Any]]:
    a = get_archive(request)
    checks: dict[str, Any] = {}
    try:
        with write_tx(a.conn):
            a.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('ready_probe', '1')")
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {type(e).__name__}"
    probe = a.paths.tmp / ".ready-probe"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        checks["archive_writable"] = "ok"
    except OSError as e:
        checks["archive_writable"] = f"error: {e.strerror}"
    st = maintenance.status(a)
    checks["worker"] = "ok" if st["worker_alive"] else "not running"
    ok = checks["database"] == "ok" and checks["archive_writable"] == "ok"
    return ok, checks


health_router = APIRouter()


@health_router.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


@health_router.get("/ready", tags=["health"])
def ready(request: Request):
    ok, checks = readiness(request)
    return JSONResponse(
        {"status": "ok" if ok else "error", "checks": checks}, status_code=200 if ok else 503
    )
