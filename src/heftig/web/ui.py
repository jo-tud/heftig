"""Server-rendered web UI (Jinja2 templates, a little vanilla JS); English with translations
(see :mod:`heftig.i18n`)."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import (
    auth,
    binders,
    combine,
    i18n,
    jobs,
    maintenance,
    saved_searches,
    senders,
    sessions,
    settings_store,
    split,
    synonyms,
    trash,
)
from .. import documents as docs
from .. import taxonomy as tax
from ..consume import list_quarantine
from ..db import iso, parse_iso, utcnow, write_tx
from ..duplicates import keep_both, open_pairs
from ..i18n import N_, _, ngettext
from ..ingest import ingest_stream
from ..media import THUMB_VERSION
from ..models import LOCKABLE_FIELDS
from ..processing import ocr_all_pages, reprocess
from ..providers.registry import describe
from ..search import (
    SearchParams,
    SearchSyntaxError,
    count_hits,
    highlight_terms,
    search,
    similar,
    word_matches,
)
from ..textnorm import parse_number
from . import api
from . import search_view as sv
from .deps import (
    SESSION_COOKIE,
    ApiError,
    Principal,
    get_archive,
    principal,
    require_user,
    require_write,
)
from .search_view import SOURCE_LABELS, STATUS_LABELS

# API tokens (scanner apps, the Claude connection) use the REST API; the HTML pages and their
# forms - token management, settings, the paid AI search - need a browser login. Only page
# images and files may be fetched with a token (the Claude connection shows pages).
_TOKEN_OK = re.compile(
    r"/documents/[^/]+/(pages/\d+\.webp|preview\.webp|original|(mail-)?attachments/[^/]+)"
)


def _browser_only(request: Request) -> None:
    p = principal(request)
    if p is None or p.via == "session":
        return
    if request.method == "GET" and _TOKEN_OK.fullmatch(request.url.path):
        return
    raise ApiError(403, "session_required", _("Only with a sign-in in the browser (API: /api/…)."))


# texts of the browser scripts (static/*.js call t("...")), collected once at startup
JS_MESSAGES = i18n.js_msgids(Path(__file__).parent / "static")

router = APIRouter(include_in_schema=False, dependencies=[Depends(_browser_only)])

FIELD_LABELS = i18n.Labels({
    "title": N_("Title"),
    "document_date": N_("Document date"),
    "correspondent": N_("Sender"),
    "document_type": N_("Document type"),
    "tags": N_("Tags"),
    "summary": N_("Summary"),
    "custom_fields": N_("Custom fields"),
})  # fmt: skip
TEXT_STATUS = i18n.Labels({
    "pending": N_("pending"), "ok": N_("complete"), "partial": N_("partial"),
    "failed": N_("failed"), "empty": N_("no text"),
})  # fmt: skip
DATE_STATUS = i18n.Labels({
    "unknown": N_("unknown"), "ai": N_("detected automatically"),
    "ai_uncertain": N_("detected automatically – uncertain"), "user": N_("manual"),
    "import": N_("imported"), "none_found": N_("no date found"),
    "as_of": N_("as-of date (no letter date)"), "mail": N_("sent date of the e-mail"),
})  # fmt: skip
STAGE_LABELS = i18n.Labels({
    "extract": N_("Text recognition"), "classify": N_("Classification"), "queued": N_("waiting"),
    "export": N_("Export"), "import": N_("Import"), "reindex": N_("Search index"),
    "rebuild": N_("Rebuild"), "titles": N_("Titles"),
})  # fmt: skip
RESULT_LABELS = i18n.Labels({
    "created": N_("newly archived"), "duplicate": N_("duplicate"), "rejected": N_("rejected"),
    "skipped": N_("skipped"), "imported": N_("imported"), "deleted": N_("deleted"),
    "already_imported": N_("already imported"), "error": N_("error"), "restored": N_("restored"),
    "replaced": N_("kept as source document"),
})  # fmt: skip


def _dt(value: str | None) -> str:
    """UTC ISO -> <time> element; JS turns it into local time, fallback shows UTC."""
    if not value:
        return ""
    d = parse_iso(value)
    if d is None:
        return value
    return f"{i18n.format_date(d)} {d:%H:%M} UTC"


def _date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return i18n.format_date(datetime.fromisoformat(value[:10]))
    except ValueError:
        return value


def _reason(text: str) -> str:
    """Stored review reasons and notes (English) in the interface language. Documents from
    before the English interface keep their German texts."""
    m = re.fullmatch(r"Mögliche Dublette bei (\w+)", text or "")
    if m:
        return f"{m.group(1)}: ähnlicher Name existiert schon"
    return i18n.translate_text(text)


def asset_version(static_dir: Path) -> str:
    """A short hash over the app's own static files: part of their URLs (?v=…), so a browser
    loads every changed script or stylesheet right after an update instead of a cached one."""
    import hashlib

    h = hashlib.sha256()
    for f in sorted(static_dir.glob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


_ENV: Any = None  # the template environment (set by make_templates), for page fragments


def make_templates(path: Path) -> Jinja2Templates:
    t = Jinja2Templates(directory=str(path))
    t.env.globals["asset_v"] = asset_version(path.parent / "static")
    t.env.autoescape = True
    global _ENV
    _ENV = t.env
    t.env.add_extension("jinja2.ext.i18n")
    t.env.install_gettext_callables(  # type: ignore[attr-defined]
        i18n.raw_gettext, i18n.raw_ngettext, newstyle=True, pgettext=i18n.raw_pgettext
    )
    t.env.globals["lang"] = i18n.current
    t.env.globals["LANGUAGES"] = i18n.LANGUAGES
    t.env.globals["js_messages"] = lambda: i18n.js_messages(JS_MESSAGES)
    t.env.filters["number"] = i18n.format_number
    t.env.filters["dt"] = _dt
    t.env.filters["date"] = _date
    t.env.filters["reason"] = _reason
    t.env.globals.update(
        FIELD_LABELS=FIELD_LABELS,
        SOURCE_LABELS=SOURCE_LABELS,
        STATUS_LABELS=STATUS_LABELS,
        TEXT_STATUS=TEXT_STATUS,
        DATE_STATUS=DATE_STATUS,
        RESULT_LABELS=RESULT_LABELS,
        STAGE_LABELS=STAGE_LABELS,
        THUMB_VERSION=THUMB_VERSION,
        LOCKABLE_FIELDS=LOCKABLE_FIELDS,
    )
    return t


def render(request: Request, name: str, *, http_status: int = 200, **ctx: Any) -> HTMLResponse:
    p = getattr(request.state, "principal", None) or principal(request)
    ctx.setdefault("user", p.user if p else None)
    ctx.setdefault("csrf", p.csrf if p else "")
    ctx.setdefault("nav", "")
    if ctx["user"] and "inbox_count" not in ctx:
        ctx["inbox_count"] = _open_tasks(request)
    undo = request.query_params.get("undo", "")
    if undo.startswith(("doc:", "batch:")) and "undo" not in ctx:
        ctx["undo"] = undo[:80]
        if undo.startswith("batch:" + combine.BATCH_PREFIX):
            ctx["undo_label"] = _(
                "Combined – the individual documents are kept under Source documents."
            )
        elif undo.startswith("batch:" + split.BATCH_PREFIX):
            ctx["undo_label"] = _("Saved as new documents – the original is in the trash.")
        elif undo.startswith("doc:"):
            row = request.app.state.archive.conn.execute(
                "SELECT title FROM trash WHERE id=?", (undo[4:],)
            ).fetchone()
            if row and row[0]:
                ctx["undo_label"] = _("“%(title)s” moved to the trash.", title=row[0][:80])
            ctx["undo_paper"] = _trashed_paper(request.app.state.archive, undo[4:])
            ctx["undo_back"] = request.url.path + (
                "?" + request.url.query if request.url.query else ""
            )

    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=http_status)


def _trashed_paper(a, doc_id: str) -> dict[str, Any] | None:
    """Where the paper of a deleted, filed document lies - to take it out, or to leave it in
    the binder (then its place keeps counting). None if there is nothing to do: not filed,
    already elsewhere, or its place was taken over by another copy."""
    from ..models import DocumentMetadata

    row = a.conn.execute("SELECT metadata_json FROM trash WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        return None
    meta = DocumentMetadata.model_validate_json(row[0])
    if meta.filing_sequence is None or meta.paper_location or meta.paper_discarded_at:
        return None
    if a.conn.execute(
        "SELECT 1 FROM documents WHERE filing_sequence=?", (meta.filing_sequence,)
    ).fetchone():
        return None
    pos = docs.filing_position(a, meta)
    if pos is None:
        return None
    return {"doc_id": doc_id, "binder": pos.binder or "–", "section": pos.section,
            "n": pos.position_from_top, "kept": binders.sheet_kept(a.paths, doc_id)}  # fmt: skip


def _open_tasks(request: Request) -> int:
    """For the badge on "Inbox": documents to check, possible duplicates, paper to file."""
    try:
        conn = request.app.state.archive.conn
        return conn.execute(
            "SELECT (SELECT COUNT(*) FROM documents WHERE status IN ('needs_review','failed'))"
            " + (SELECT COUNT(*) FROM duplicate_candidates WHERE status='open')"
            " + (SELECT COUNT(*) FROM documents WHERE paper=1 AND filing_sequence IS NULL"
            "    AND paper_location IS NULL AND paper_discarded_at IS NULL AND (scan_session_id"
            "    IS NULL OR scan_session_id NOT IN (SELECT id FROM scan_sessions"
            "    WHERE closed_at IS NULL)))"
        ).fetchone()[0]
    except Exception:  # noqa: BLE001 - a badge must never break a page
        return 0


def error_page(request: Request, status: int, message: str) -> HTMLResponse:
    return render(request, "error.html", http_status=status, status_code=status, message=message)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


# --- login ------------------------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if principal(request):
        return redirect(_safe_next(next))
    if auth.user_count(get_archive(request).conn) == 0:
        return redirect("/setup")  # first start: create the account there
    return render(request, "login.html", next=_safe_next(next))


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    nxt = _safe_next(str(form.get("next") or "/"))
    try:
        token, _csrf = await run_in_threadpool(
            api.do_login, request, str(form.get("username", "")), str(form.get("password", ""))
        )
    except ApiError as e:
        return render(
            request, "login.html", http_status=e.status_code, next=nxt, error=e.detail["message"],
            no_user=False,
        )  # fmt: skip
    resp = redirect(nxt)
    api.set_session_cookie(request, resp, token)
    return resp


@router.post("/logout")
async def logout(request: Request, p: Principal = Depends(require_write)):
    await run_in_threadpool(
        auth.logout, get_archive(request).conn, request.cookies.get(SESSION_COOKIE)
    )
    resp = redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# --- documents / search -----------------------------------------------------------------


def _page(value: str) -> int:
    try:
        return min(max(int(value), 1), 10_000)
    except ValueError:
        return 1


def _params_from_query(request: Request) -> SearchParams:
    return _params_from(request.query_params)


def _params_from(qp) -> SearchParams:

    def f(name: str) -> float | str | None:
        # an amount that cannot be read stays text: the search reports it instead of
        # silently showing everything
        v = (qp.get(name) or "").strip()
        return (parse_number(v) if parse_number(v) is not None else v) if v else None

    return SearchParams(
        q=qp.get("q", ""),
        correspondent=[v for v in qp.getlist("correspondent") if v],
        document_type=[v for v in qp.getlist("document_type") if v],
        tags=[v for v in qp.getlist("tag") if v],
        date_from=qp.get("date_from") or None,
        date_to=qp.get("date_to") or None,
        received_from=qp.get("received_from") or None,
        received_to=qp.get("received_to") or None,
        source=[v for v in qp.getlist("source") if v],
        email_from=[v for v in qp.getlist("email_from") if v],
        status=[v for v in qp.getlist("status") if v],
        filed=qp.get("filed") or None,
        filing_section=qp.get("filing_section") or None,
        filing_binder=qp.get("filing_binder") or None,
        cf_key=qp.get("cf_key") or None,
        cf_min=f("cf_min"),
        cf_max=f("cf_max"),
        sort=qp.get("sort") or None,
        page=_page(qp.get("page", "1")),
        per_page=25,
        tag_mode="any" if qp.get("tag_mode") == "any" else "all",
        session=qp.get("session") or None,
        literal=bool(qp.get("literal")),
        meaning=qp.get("meaning") != "0",
    )


def meaning_embedder(a, params: SearchParams):
    """The embedding model for this search, when the search by meaning is on, the model is
    there and documents are embedded (it runs on this computer). Also with ``meaning=0``:
    search() then leaves it out, and the search page offers to switch it back on."""
    from .. import semantic

    if not params.q:
        return None
    return semantic.for_search(a.conn, a.settings)


@router.get("/")
def documents_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    params = _params_from_query(request)
    today = date.today()
    result = search(
        a.conn, params, with_facets=True, today=today, embedder=meaning_embedder(a, params)
    )
    names = {}
    if params.session:
        s = sessions.get(a.conn, params.session)
        names = {params.session: s["name"]} if s else {}
    email_names = senders.load(a.paths)
    view = sv.build(params, result, request.query_params, today, names, email_names)
    pages = (result.total + result.per_page - 1) // result.per_page
    sections = [
        r[0]
        for r in a.conn.execute(
            "SELECT DISTINCT filing_section FROM documents WHERE filing_section IS NOT NULL "
            "ORDER BY filing_section DESC"
        )
    ]
    cf_keys = [
        r[0] for r in a.conn.execute("SELECT DISTINCT key FROM custom_field_values ORDER BY key")
    ]
    binder_names = [b["name"] for b in reversed(binders.load(a.paths))]
    saved = saved_searches.load(a.paths)
    for e in saved:
        e["href"] = "/?" + e["query"]
    from .. import aisearch

    return render(
        request,
        "documents.html",
        nav="documents",
        ai_available=aisearch.available(a),
        ai_query=request.query_params.get("ai", "")[:300],
        ai_note=request.query_params.get("ai_note", "")[:400],
        result=result,
        params=params,
        v=view,
        chips=view["chips"],
        page_count=pages,
        base_qs=view["query"],
        sections=sections,
        binder_names=binder_names,
        cf_keys=cf_keys,
        saved_searches=saved,
        saved_current=next(
            (e for e in saved if e["query"] == saved_searches.clean_query(view["query"])), None
        ),
        start_page=not params.q and not view["active"],
        message=request.query_params.get("msg"),
        email_names=email_names,
    )


def _saved_sync(a, form) -> RedirectResponse:
    action = _form_val(form, "action")
    back = saved_searches.clean_query(_form_val(form, "query"))
    try:
        if action == "save":
            saved_searches.add(a.paths, _form_val(form, "name"), back)
            msg = _("Search saved.")
        elif action == "delete":
            saved_searches.delete(a.paths, _form_val(form, "id"))
            msg = _("Saved search removed.")
        else:
            return redirect("/")
    except saved_searches.SavedSearchError as e:
        msg = str(e)
    return redirect("/?" + urlencode([*parse_qsl(back), ("msg", msg)]))


# --- trash and bulk deletion ----------------------------------------------------------

BULK_LIMIT = 2000


@router.get("/trash")
def trash_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    groups = trash.listing(a)
    for g in groups:
        for it in g["items"]:
            it["paper"] = _trashed_paper(a, it["id"])
    return render(
        request, "trash.html", nav="settings", groups=groups,
        total=sum(len(g["items"]) for g in groups), days=a.settings.trash_retention_days,
        message=request.query_params.get("msg"),
    )  # fmt: skip


def _trash_sync(a, form, query_action: str) -> RedirectResponse:
    action = _form_val(form, "action") or query_action
    target = _form_val(form, "target")
    back = _form_val(form, "back") or "/trash"
    try:
        if action == "restore" and target.startswith("batch:" + combine.BATCH_PREFIX):
            combined = target[6 + len(combine.BATCH_PREFIX) :]
            r = combine.undo(a, combined)
            if not r["ids"]:
                raise trash.TrashError("; ".join(sorted(set(r["failed"]))) or _("Nothing to do."))
            return redirect(f"/documents/{r['ids'][0]}?" + urlencode(
                {"msg": _("Combining undone – the individual documents are back.")}))  # fmt: skip
        if action == "restore" and target.startswith("batch:" + split.BATCH_PREFIX):
            r = split.undo(a, target[6 + len(split.BATCH_PREFIX) :])
            if not r["ids"]:
                raise trash.TrashError("; ".join(sorted(set(r["failed"]))) or _("Nothing to do."))
            return redirect(f"/documents/{r['ids'][0]}?" + urlencode(
                {"msg": _("Undone – the original document is back.")}))  # fmt: skip
        if action == "restore" and target.startswith("batch:"):
            r = trash.restore_batch(a, target[6:])
            msg = ngettext(
                "%(num)d document restored.", "%(num)d documents restored.", r["restored"]
            )
            if r["failed"]:
                msg += " " + _(
                    "Not possible: %(reasons)s", reasons="; ".join(sorted(set(r["failed"])))
                )
        elif action == "restore" and target.startswith("doc:"):
            meta = trash.restore(a, target[4:])
            return redirect(f"/documents/{meta.id}?" + urlencode({"msg": _("Restored.")}))
        elif action in ("paper_stays", "paper_out") and target.startswith("doc:"):
            row = a.conn.execute(
                "SELECT metadata_json FROM trash WHERE id=?", (target[4:],)
            ).fetchone()
            if row is None:
                raise trash.TrashError(_("No longer in the trash."))
            from ..models import DocumentMetadata

            if action == "paper_stays":
                binders.keep_sheet(a, DocumentMetadata.model_validate_json(row[0]))
                msg = _("The sheet stays in the binder – its place keeps counting.")
            else:
                binders.sheet_taken_out(a, target[4:])
                msg = _("Noted: the sheet is out of the binder.")
        elif action == "purge" and target.startswith("purge:"):
            trash.purge(a, target[6:])
            msg = _("Deleted permanently.")
        elif action == "empty":
            n = trash.empty(a)
            msg = ngettext(
                "Trash emptied (%(num)d document).", "Trash emptied (%(num)d documents).", n
            )
        else:
            msg = ""
    except trash.TrashError as e:
        msg = str(e)
    back = _safe_next(back)
    return redirect(back + ("&" if "?" in back else "?") + urlencode({"msg": msg}))


@router.post("/trash/action")
async def trash_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(
        _trash_sync, get_archive(request), form, request.query_params.get("action", "")
    )


# --- source documents: what was combined into another document, kept for good -------------


@router.get("/sources")
def sources_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    return render(
        request, "sources.html", nav="settings", groups=trash.sources_listing(a),
        days=a.settings.trash_retention_days, message=request.query_params.get("msg"),
    )  # fmt: skip


@router.get("/sources/{doc_id}/original")
def source_original(
    request: Request, doc_id: str, inline: int = 0, p: Principal = Depends(require_user)
):
    try:
        meta = trash.source_meta(get_archive(request), doc_id)
    except docs.DocumentNotFound:
        return Response(status_code=404)
    return api.original_response(request, doc_id, bool(inline), meta=meta)


def _sources_sync(a, form, action: str) -> RedirectResponse:
    target = _form_val(form, "target")
    try:
        if action == "undo" and target.startswith(combine.BATCH_PREFIX):
            r = combine.undo(a, target[len(combine.BATCH_PREFIX) :])
            if not r["ids"]:
                raise trash.TrashError("; ".join(sorted(set(r["failed"]))) or _("Nothing to do."))
            return redirect(f"/documents/{r['ids'][0]}?" + urlencode(
                {"msg": _("Combining undone – the individual documents are back.")}))  # fmt: skip
        if action == "restore" and target:
            meta = trash.restore(a, target)
            return redirect(f"/documents/{meta.id}?" + urlencode({"msg": _("Restored.")}))
        if action == "delete" and target:
            trash.discard_source(a, target)
            msg = _("Moved to the trash.")
        else:
            msg = ""
    except (trash.TrashError, docs.DocumentNotFound) as e:
        msg = str(e) if isinstance(e, trash.TrashError) else _("Document not found.")
    return redirect("/sources?" + urlencode({"msg": msg}))


@router.post("/sources/action")
async def sources_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(
        _sources_sync, get_archive(request), form, request.query_params.get("action", "")
    )


def _bulk_selection(a, qp) -> dict[str, Any]:
    import hashlib

    from ..search import matching_ids

    params = _params_from(qp)
    items = sv.query_items(qp)
    criteria = [k for k, _v in items if k == "q" or k in sv.CHIP_LABELS]
    if not criteria:
        return {
            "error": _("Only for a search or a filtered selection – not for the whole archive.")
        }
    try:
        ids = matching_ids(a.conn, params, BULK_LIMIT)
    except SearchSyntaxError as e:
        return {"error": str(e)}
    return {
        "params": params,
        "ids": ids[:BULK_LIMIT],
        "too_many": len(ids) > BULK_LIMIT,
        "fingerprint": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:32],
        "label": sv.describe(params, items, email_names=senders.load(a.paths)),
        "query": urlencode([(k, v) for k, v in items if k != "sort"]),
    }


@router.get("/bulk-delete")
def bulk_delete_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    sel = _bulk_selection(a, request.query_params)
    if "error" in sel:
        return error_page(request, 400, sel["error"])
    rows = {
        r["id"]: r
        for r in a.conn.execute(
            f"SELECT id, title, original_filename, document_date, metadata_json FROM documents "
            f"WHERE id IN ({','.join('?' for _i in sel['ids'])})",
            sel["ids"],
        )
    } if sel["ids"] else {}  # fmt: skip
    items = []
    for doc_id in sel["ids"]:
        r = rows.get(doc_id)
        if r:
            items.append({"id": doc_id, "title": r["title"] or r["original_filename"],
                          "document_date": r["document_date"],
                          "correspondent": json.loads(r["metadata_json"]).get("correspondent")})  # fmt: skip
    return render(
        request, "bulk_delete.html", nav="documents", items=items, n=len(items),
        too_many=sel["too_many"], limit=BULK_LIMIT, label=sel["label"], query=sel["query"],
        fingerprint=sel["fingerprint"], back="/?" + sel["query"],
        days=a.settings.trash_retention_days, message=request.query_params.get("msg"),
    )  # fmt: skip


def _bulk_sync(a, form) -> RedirectResponse:
    from starlette.datastructures import QueryParams

    query = _form_val(form, "query")
    sel = _bulk_selection(a, QueryParams(query))
    again = "/bulk-delete?" + query + "&"
    if "error" in sel or sel["too_many"]:
        return redirect("/?" + query)
    if _form_val(form, "fingerprint") != sel["fingerprint"]:
        return redirect(
            again + urlencode({"msg": _("The results have changed – please check again.")})
        )
    if _form_val(form, "count").strip() != str(len(sel["ids"])):
        return redirect(
            again
            + urlencode({"msg": _("The number you typed does not match – nothing was deleted.")})
        )
    batch = trash.new_batch()
    with i18n.language("en"):  # stored in English; shown translated
        label = sv.describe(
            sel["params"], sv.query_items(QueryParams(query)), email_names=senders.load(a.paths)
        )
        reason = _("Bulk deletion “%(label)s”", label=label)[:200]
    for doc_id in sel["ids"]:
        try:
            trash.trash_document(a, doc_id, reason=reason, batch=batch)
        except docs.DocumentNotFound:
            pass
    return redirect("/?" + urlencode({"undo": f"batch:{batch}"}))


@router.post("/bulk-delete")
async def bulk_delete(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_bulk_sync, get_archive(request), form)


@router.post("/searches")
async def saved_search_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_saved_sync, get_archive(request), form)


def _page_sizes(a, m: dict) -> list[tuple[float, float]]:
    """Width/height of every page as shown - turned pages swap them."""
    from ..media import page_sizes

    try:
        sizes = page_sizes(a.paths.resolve(m["original_relpath"]), m["mime_type"])
    except Exception:  # broken or missing original: the viewer falls back to A4 boxes
        sizes = []
    sizes = sizes or [(595.0, 842.0)] * (m.get("page_count") or 1)
    turned = {int(k): v for k, v in (m.get("page_rotation") or {}).items()}
    return [(h, w) if turned.get(n) in (90, 270) else (w, h) for n, (w, h) in enumerate(sizes, 1)]


def _split_parts(a, m: dict) -> list[dict[str, Any]]:
    """The other documents split from the same original (for "Split from")."""
    src = (m.get("source_details") or {}).get("split_from")
    if not isinstance(src, dict) or not src.get("id"):
        return []
    return [r for r in split.parts_of(a, str(src["id"])) if r["id"] != m["id"]]


# --- AI search: a request in plain words becomes filters -------------------------------------


class _RateLimit:
    """At most `n` calls per `seconds` (per process) - a paid cloud call is behind it."""

    def __init__(self, n: int, seconds: float):
        import threading

        self.n, self.seconds, self.calls, self.lock = n, seconds, [], threading.Lock()

    def allow(self) -> bool:
        import time

        now = time.monotonic()
        with self.lock:
            self.calls = [t for t in self.calls if now - t < self.seconds]
            if len(self.calls) >= self.n:
                return False
            self.calls.append(now)
            return True


_AI_SEARCH_LIMIT = _RateLimit(20, 60)


@router.get("/ai-search")
def ai_search(request: Request, q: str = "", p: Principal = Depends(require_user)):
    """Ask the model for filters, then show the ordinary search with them (a normal URL:
    reloading or going back does not ask again)."""
    from .. import aisearch

    q = q.strip()[:300]
    if not q:
        return redirect("/")
    if not _AI_SEARCH_LIMIT.allow():
        return redirect("/?" + urlencode(
            {"q": q, "msg": _("Too many AI searches in a short time – please wait a moment.")}))  # fmt: skip
    try:
        plan = aisearch.plan(get_archive(request), q)
    except aisearch.AISearchUnavailable as e:
        return redirect(
            "/?" + urlencode({"q": q, "msg": _("AI search not possible: %(error)s", error=str(e))})
        )
    note = plan.explanation
    if plan.dropped:
        note += " " + _("(not in the archive: %(names)s)", names=", ".join(plan.dropped[:5]))
    return redirect("/?" + urlencode([*plan.items, ("ai", q), ("ai_note", note)]))


# --- review flow: one document after the other ----------------------------------------------

REVIEW_WHERE = "status IN ('needs_review','failed')"


def _review_remaining(conn) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM documents WHERE {REVIEW_WHERE}").fetchone()[0]


@router.get("/review/next")
def review_next(request: Request, after: str = "", p: Principal = Depends(require_user)):
    """The next document to check (newest first); `after`: continue behind that document,
    even if it just left the list."""
    nxt = _next_to_review(get_archive(request).conn, after)
    if nxt is None:
        return redirect("/inbox?" + urlencode({"msg": _("All reviewed – nothing left open.")}))
    return redirect(f"/documents/{nxt}?review=1")


def _next_to_review(conn, after: str = "") -> str | None:
    order = "ORDER BY received_at DESC, ingest_sequence DESC LIMIT 1"
    row = None
    ref = conn.execute(
        "SELECT received_at, ingest_sequence FROM documents WHERE id=?", (after,)
    ).fetchone() if after else None  # fmt: skip
    if ref:
        row = conn.execute(
            f"SELECT id FROM documents WHERE {REVIEW_WHERE} AND id != ? "
            f"AND (received_at, ingest_sequence) < (?, ?) {order}",
            (after, ref[0], ref[1]),
        ).fetchone()
    if row is None:  # from the start (again)
        row = conn.execute(
            f"SELECT id FROM documents WHERE {REVIEW_WHERE} AND id != ? {order}", (after,)
        ).fetchone()
    return row[0] if row else None


def _search_back(request: Request) -> str:
    """The result list the user came from (same site), to return there after deleting."""
    from urllib.parse import urlsplit

    ref = urlsplit(request.headers.get("referer", ""))
    if ref.netloc and ref.netloc != request.url.netloc:
        return "/"
    if ref.path == "/" and ref.query:
        return "/?" + urlencode(
            [(k, v) for k, v in parse_qsl(ref.query) if k not in ("undo", "msg", "ai", "ai_note")]
        )
    return "/"


def _doc_url(doc_id: str, form=None, **params: str) -> str:
    """The document page, staying in the review flow when the form came from it."""
    q = {k: v for k, v in params.items() if v}
    if form is not None and str(form.get("review") or "") == "1":
        q["review"] = "1"
    return f"/documents/{doc_id}" + ("?" + urlencode(q) if q else "")


def _mail_parts(a, m: dict) -> list:
    """The attachments of an archived e-mail (for downloading them one by one)."""
    from .. import mail

    if m.get("mime_type") != mail.MIME:
        return []
    try:
        return mail.parse(a.paths.resolve(m["original_relpath"]).read_bytes()).attachments
    except (OSError, ValueError, LookupError) as e:
        logging.getLogger("heftig.web").warning(
            "e-mail %s: attachments not read: %s", m.get("id"), e
        )
        return []


@router.get("/documents/{doc_id}")
def document_page(request: Request, doc_id: str, p: Principal = Depends(require_user)):
    a = get_archive(request)
    detail = api.document_detail(request, doc_id)
    tp = docs.load_text_pages(a, doc_id)
    events = [
        dict(r)
        for r in a.conn.execute(
            "SELECT * FROM ingest_events WHERE doc_id=? ORDER BY id DESC LIMIT 20", (doc_id,)
        )
    ]
    for e in events:
        e["source_details"] = json.loads(e["source_details"] or "{}")
    budget_pages = _budget_pages(tp)
    # coming from a search: pages with hits (the viewer jumps there and marks the words)
    q = request.query_params.get("q", "")[:500]
    hit_terms = highlight_terms(a.conn, q) if q else []
    hit_pages = []
    if hit_terms and tp:
        for pg in tp.pages:
            n = count_hits(pg.text, hit_terms)
            if n:
                hit_pages.append({"page": pg.page, "count": n})
    # blank pages (empty backs of duplex scans) are hidden - except pages with search hits
    # and when all pages are asked for
    blank = docs.blank_pages(tp, detail["metadata"].get("page_blank"))
    # pages turned after their text was read from the image: offer to read them again
    turned = {int(k): v for k, v in (detail["metadata"].get("page_rotation") or {}).items()}
    read_turned = [
        pg.page for pg in (tp.pages if tp and not tp.user_confirmed else [])
        if pg.method in ("ocr", "none") and not pg.blank and turned.get(pg.page, 0) != pg.turn
    ]  # fmt: skip
    all_pages = request.query_params.get("pages") == "all"
    hits = {h["page"] for h in hit_pages}
    keep = {k: v for k, v in request.query_params.items() if k in ("q", "review")}
    return render(
        request,
        "document.html",
        nav="inbox" if request.query_params.get("review") == "1" else "documents",
        blank_pages=blank,
        read_turned=read_turned,
        hidden_pages=[] if all_pages else [n for n in blank if n not in hits],
        all_pages=all_pages,
        url_all_pages=f"/documents/{doc_id}?"
        + urlencode({**keep, "pages": "all"})
        + "#viewer-card",
        url_some_pages=f"/documents/{doc_id}"
        + ("?" + urlencode(keep) if keep else "")
        + "#viewer-card",
        d=detail,
        m=detail["metadata"],
        pos=detail["filing_position"],
        current_binder=binders.current(a) if detail["metadata"].get("paper") else "",
        binder_names=[b["name"] for b in reversed(binders.load(a.paths))],
        sender_names=senders.load(a.paths),
        text_pages=tp.pages if tp else [],
        events=events,
        terms={k: tax.list_terms(a.conn, k) for k in tax.KINDS},
        providers=detail["providers"],
        duplicates=open_pairs(a.conn, doc_id),
        page_sizes=_page_sizes(a, detail["metadata"]),
        mail_parts=_mail_parts(a, detail["metadata"]),
        split_parts=_split_parts(a, detail["metadata"]),
        saved=request.query_params.get("saved"),
        message=request.query_params.get("msg"),
        q=q if hit_terms else "",
        hit_pages=hit_pages,
        similar=similar(a.conn, doc_id),
        budget_pages=budget_pages,
        trash_days=a.settings.trash_retention_days,
        back=_search_back(request),
        review=(
            {"remaining": _review_remaining(a.conn)}
            if request.query_params.get("review") == "1"
            else None
        ),  # fmt: skip
    )


@router.get("/documents/{doc_id}/original")
def document_original(
    request: Request, doc_id: str, inline: int = 0, p: Principal = Depends(require_user)
):
    return api.original_response(request, doc_id, bool(inline))


@router.get("/documents/{doc_id}/mail-attachments/{index}")
def document_mail_attachment(
    request: Request, doc_id: str, index: int, p: Principal = Depends(require_user)
):
    return api.mail_attachment_response(request, doc_id, index)


PAGE_WIDTHS = (480, 960, 1440, 2048)


@router.get("/documents/{doc_id}/pages/{page}.webp")
def document_page_image(
    request: Request, doc_id: str, page: int, w: int = 960, p: Principal = Depends(require_user)
):
    """One page as WebP for the page viewer. Rendered once per width and cached (regenerable)."""
    import io

    from ..media import render_width
    from ..storage import atomic_write_bytes

    a = get_archive(request)
    meta = docs.load_meta(a, doc_id)
    if not 1 <= page <= (meta.page_count or 1):
        return Response(status_code=404)
    width = min(PAGE_WIDTHS, key=lambda x: (x < w, abs(x - w)))  # smallest step >= w
    turn = docs.rotation(meta, page)
    rot = f"-r{turn}" if turn else ""
    cache = docs.files(a, doc_id).dir / "cache" / f"p{page}-{width}{rot}.webp"
    headers = {
        "Cache-Control": "private, max-age=604800",
        "ETag": f'"{meta.sha256[:16]}-{page}-{width}{rot}"',
    }
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    if not cache.exists():
        img = render_width(
            a.paths.resolve(meta.original_relpath), meta.mime_type, page - 1, width,
            a.settings.max_image_megapixels, turn,
        )  # fmt: skip
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=82, method=4)
        if not docs.cache_writable(a, doc_id):  # deleted meanwhile: serve, don't cache
            return Response(buf.getvalue(), media_type="image/webp", headers=headers)
        atomic_write_bytes(cache, buf.getvalue())
    return FileResponse(cache, media_type="image/webp", headers=headers)


@router.get("/documents/{doc_id}/pages/{page}/hits")
def document_page_hits(
    request: Request, doc_id: str, page: int, q: str = "", p: Principal = Depends(require_user)
):
    """Boxes (0..1, from the top left) of the words on one page that match the search `q`."""
    from ..wordboxes import page_words

    a = get_archive(request)
    try:
        meta = docs.load_meta(a, doc_id)
    except docs.DocumentNotFound:
        return Response(status_code=404)
    if not 1 <= page <= (meta.page_count or 1):
        return Response(status_code=404)
    terms = highlight_terms(a.conn, q[:500])
    if not terms:
        return {"boxes": [], "source": "none"}
    words, source = page_words(a, meta, page)
    boxes = [w[1:] for w in words if word_matches(w[0], terms)]
    return JSONResponse(
        {"boxes": boxes, "source": source}, headers={"Cache-Control": "private, no-store"}
    )


@router.get("/documents/{doc_id}/preview.webp")
def document_preview(
    request: Request, doc_id: str, w: int = 144, p: Principal = Depends(require_user)
):
    """Thumbnail of the first page (the first one that is not blank) in exactly the requested
    width (one per screen density, so the browser does not scale again); made once per width
    and cached (regenerable)."""
    from ..media import THUMB_VERSION, THUMB_WIDTHS, make_thumbnail
    from ..storage import atomic_write_bytes

    a = get_archive(request)
    width = min(THUMB_WIDTHS, key=lambda x: (x < w, abs(x - w)))  # smallest step >= w
    try:
        meta = docs.load_meta(a, doc_id)
    except (docs.DocumentNotFound, ValueError):
        return Response(status_code=404)
    cover = docs.cover_page(a, meta)
    turn = docs.rotation(meta, cover + 1)
    page = (f"-p{cover + 1}" if cover else "") + (f"-r{turn}" if turn else "")
    headers = {
        "Cache-Control": "private, max-age=604800",
        "ETag": f'"{meta.sha256[:16]}-t{THUMB_VERSION}-{width}{page}"',
    }
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    cache = docs.files(a, doc_id).dir / "cache" / f"thumb-v{THUMB_VERSION}-{width}{page}.webp"
    if not cache.exists():
        try:
            data = make_thumbnail(
                a.paths.resolve(meta.original_relpath), meta.mime_type,
                a.settings.max_image_megapixels, width, cover, turn,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - a broken original: no thumbnail
            return Response(status_code=404)
        if not docs.cache_writable(a, doc_id):
            return Response(data, media_type="image/webp", headers=headers)
        atomic_write_bytes(cache, data)
    return FileResponse(cache, media_type="image/webp", headers=headers)


STALE_MSG = N_(
    "The document has been changed in the meantime (e.g. by the automatic processing). "
    "Please check the page and enter your change again."
)


def _form_val(form, name: str) -> str:
    # browsers send textarea line breaks as CRLF
    return str(form.get(name) or "").replace("\r\n", "\n").strip()


def _stale(form, meta) -> bool:
    rev = str(form.get("revision") or "")
    return rev != "" and rev != str(meta.revision)


def _form_changes(form, meta) -> tuple[dict[str, Any], str | None]:
    """The fields the form changes compared to ``meta`` (or an error message)."""
    changes: dict[str, Any] = {}

    def val(name: str) -> str:
        return _form_val(form, name)

    if "title" in form and val("title") != meta.title:
        changes["title"] = val("title")
    if "document_date" in form and (val("document_date") or None) != meta.document_date:
        changes["document_date"] = val("document_date") or None
    for fld in ("correspondent", "document_type"):
        if fld in form and (val(fld) or None) != getattr(meta, fld):
            changes[fld] = val(fld) or None
    # only parse the tag field when the user edited it (tag names may contain commas)
    if "tags" in form and val("tags") != ", ".join(meta.tags):
        changes["tags"] = [t.strip() for t in val("tags").split(",") if t.strip()]
    if "summary" in form and val("summary") != meta.summary:
        changes["summary"] = val("summary")
    # custom fields: rows cf_key_N / cf_type_N / cf_value_N / cf_currency_N
    if "cf_key_0" not in form:
        return changes, None
    cfs: dict[str, Any] = {}
    i = 0
    while f"cf_key_{i}" in form:
        key = val(f"cf_key_{i}")
        value = val(f"cf_value_{i}")
        if key and value:
            ftype = val(f"cf_type_{i}") or "string"
            entry: dict[str, Any] = {"type": ftype, "value": value}
            if ftype in ("number", "monetary"):
                num = parse_number(value)  # "1.234,56", "1.234" (= 1234), "12.50"
                if num is None:
                    return {}, _("Invalid number in the field “%(field)s”", field=key)
                entry["value"] = num
            if ftype == "monetary":
                entry["currency"] = (val(f"cf_currency_{i}") or "EUR").upper()
            cfs[key] = entry
        i += 1
    current_cfs = {
        k: v.model_dump(mode="json", exclude_none=True) for k, v in meta.custom_fields.items()
    }
    normalized = {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in cfs.items()}
    if normalized != current_cfs:
        changes["custom_fields"] = cfs
    return changes, None


def _edit_sync(a, doc_id: str, form) -> Response:
    meta = docs.load_meta(a, doc_id)
    if str(form.get("autosave") or "") == "1":
        return _autosave(a, doc_id, form, meta)
    if _stale(form, meta):
        return redirect(_doc_url(doc_id, form, msg=_(STALE_MSG)))
    changes, error = _form_changes(form, meta)
    if error:
        return redirect(f"/documents/{doc_id}?" + urlencode({"msg": error}))
    # fields changed in this edit are locked automatically; the checkboxes decide the rest
    # (tags are never locked automatically - their checkbox always counts)
    locks = {
        f: form.get(f"lock_{f}") == "on" for f in LOCKABLE_FIELDS if f not in changes or f == "tags"
    }
    try:
        if changes or any(bool(meta.field_locks.get(f)) != v for f, v in locks.items()):
            docs.update_fields(a, doc_id, changes, locks)
    except (docs.EditError, ValueError) as e:
        return redirect(_doc_url(doc_id, form, msg=str(e)))
    if _form_val(form, "then") == "review_next":
        # "Reviewed → next": saved, confirmed (open suggestions are dropped), next one
        docs.mark_reviewed(a, doc_id)
        return redirect("/review/next?" + urlencode({"after": doc_id}))
    return redirect(_doc_url(doc_id, form, saved="1"))


def _budget_pages(tp) -> int:
    """Pages read locally because of the AI page budget (offered to be read with AI)."""
    return sum(
        1 for pg in (tp.pages if tp else [])
        if "Seitenbudget" in pg.provider or "page budget" in pg.provider
    )  # fmt: skip


def _review_parts(a, doc_id: str, form) -> dict[str, str]:
    """The review box, the review bar's texts and the status badge as they are now - the
    page swaps them in after an autosave, so what it shows (and the suggestion buttons,
    which refer to positions in the list) always matches the saved document."""
    from markupsafe import Markup

    meta = docs.load_meta(a, doc_id)
    m = meta.model_dump(mode="json")
    in_review = str(form.get("review") or "") == "1"
    review = {"remaining": _review_remaining(a.conn)} if in_review else None
    rv = Markup('<input type="hidden" name="review" value="1">') if in_review else Markup("")
    macros = _ENV.get_template("_review.html").module
    badge = _ENV.get_template("_macros.html").module.status_badge
    return {
        "review_html": str(macros.review_card(m, _form_val(form, "csrf_token"), rv, review,
                                              _budget_pages(docs.load_text_pages(a, doc_id)))),
        "review_count_html": str(macros.review_count(review)),
        "review_note_html": str(macros.review_note(m)),
        "status_html": str(badge(meta.status)),
        "date_hint_html": str(macros.date_hint(m)),
    }  # fmt: skip


def _autosave(a, doc_id: str, form, meta) -> JSONResponse:
    """One field saved as soon as the user leaves it (or its lock toggled, or an undo).
    Answers with the new revision and what is needed to undo the change."""
    if _stale(form, meta):
        return JSONResponse({"ok": False, "stale": True, "error": _(STALE_MSG)}, status_code=409)
    try:
        if form.get("undo"):
            snap = json.loads(str(form.get("undo")))
            meta = docs.restore_field(a, doc_id, snap)
            return JSONResponse(
                {"ok": True, "revision": meta.revision, **_review_parts(a, doc_id, form)}
            )
        field = _form_val(form, "field")
        if field == "*":
            return _autosave_all(a, doc_id, form, meta)
        if field.startswith("lock:"):
            name = field[5:]
            if name not in LOCKABLE_FIELDS:
                raise docs.EditError(_("Field “%(field)s” cannot be locked.", field=name))
            meta = docs.update_fields(a, doc_id, {}, {name: form.get(f"lock_{name}") == "on"})
            return JSONResponse({"ok": True, "revision": meta.revision, "undo": None,
                                 **_review_parts(a, doc_id, form)})  # fmt: skip
        if field not in LOCKABLE_FIELDS:
            raise docs.EditError(_("Field “%(field)s” cannot be edited.", field=field))
        changes, error = _form_changes(form, meta)
        if error:
            raise docs.EditError(error)
        if field not in changes:
            return JSONResponse({"ok": True, "revision": meta.revision, "undo": None})
        snap = docs.field_snapshot(meta, field)
        meta = docs.update_fields(a, doc_id, {field: changes[field]})
    except (docs.EditError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "revision": meta.revision, "undo": snap,
                         "locked": bool(meta.field_locks.get(field)),
                         **_review_parts(a, doc_id, form)})  # fmt: skip


def _autosave_all(a, doc_id: str, form, meta) -> JSONResponse:
    """Several fields at once, when the page is left while a field is still being edited
    (back button, closing the tab): only the fields the page names as changed, in one write -
    there is no time left for one request per field. Without undo."""
    names = [f for f in _form_val(form, "fields").split(",") if f]
    changes, error = _form_changes(form, meta)
    if error:
        raise docs.EditError(error)
    changes = {f: v for f, v in changes.items() if f in names}
    locks = {
        f[5:]: form.get(f"lock_{f[5:]}") == "on"
        for f in names
        if f.startswith("lock:") and f[5:] in LOCKABLE_FIELDS
    }
    if changes or locks:
        meta = docs.update_fields(a, doc_id, changes, locks)
    return JSONResponse({"ok": True, "revision": meta.revision, "undo": None,
                         **_review_parts(a, doc_id, form)})  # fmt: skip


@router.post("/documents/{doc_id}/edit")
async def document_edit(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_edit_sync, get_archive(request), doc_id, form)


def _action_sync(a, doc_id: str, form) -> RedirectResponse:
    action = str(form.get("action") or "")
    meta = docs.load_meta(a, doc_id)
    msg = ""
    if action == "file":
        docs.mark_filed(a, doc_id)
        msg = _("Marked as filed.")
    elif action == "unfile":
        docs.unmark_filed(a, doc_id)
        msg = _("Filing undone.")
    elif action == "take_out":
        docs.take_out(a, doc_id, _form_val(form, "where"))
        msg = _("Noted as taken out – the sheet keeps its place.")
    elif action == "put_back":
        docs.put_back(a, doc_id)
        msg = _("Back in its place.")
    elif action == "refile":
        target = _form_val(form, "binder")
        if target not in {b["name"] for b in binders.load(a.paths)}:
            raise ApiError(400, "invalid", _("Unknown binder."))
        docs.mark_filed(a, doc_id, binder=target)
        msg = _("Moved to binder %(name)s, on top of the current section.", name=target)
    elif action == "paper_folder":
        docs.set_paper_state(a, doc_id, location=_form_val(form, "location"))
        msg = _("Location saved.")
    elif action == "paper_discarded":
        docs.set_paper_state(a, doc_id, discarded=True)
        msg = _("Noted: the paper is not kept.")
    elif action == "paper_reset":
        docs.set_paper_state(a, doc_id)
        msg = _("Undone.")
    elif action in ("reprocess_extract", "reprocess_classify", "reprocess_all"):
        stages = {"reprocess_extract": ["extract"], "reprocess_classify": ["classify"],
                  "reprocess_all": ["extract", "classify"]}[action]  # fmt: skip
        reprocess(a, [doc_id], stages)
        msg = _("Reprocessing scheduled.")
    elif action == "reviewed":
        # the page may show an older state (e.g. reclassified meanwhile): nothing unseen is
        # confirmed
        if _stale(form, meta):
            return redirect(_doc_url(doc_id, form, msg=_(STALE_MSG)))
        docs.mark_reviewed(a, doc_id)
        if str(form.get("review") or "") == "1":
            return redirect("/review/next?" + urlencode({"after": doc_id}))
        msg = _("Marked as reviewed.")
    elif action in ("rotate", "rotate_back"):
        which = str(form.get("page") or "")
        degrees = 90 if action == "rotate" else -90
        try:
            docs.rotate_pages(a, doc_id, None if which == "all" else [int(which)], degrees)
        except (ValueError, docs.EditError):
            raise ApiError(400, "invalid", _("Unknown page.")) from None
        keep = {"pages": "all"} if str(form.get("pages") or "") == "all" else {}
        anchor = "#viewer-card" if which == "all" else f"#page-{which}"
        return redirect(_doc_url(doc_id, form, **keep) + anchor)
    elif action == "page_blank":
        try:
            page = int(str(form.get("page") or ""))
        except ValueError:
            raise ApiError(400, "invalid", _("Unknown page.")) from None
        blank = str(form.get("blank") or "") == "1"
        try:
            docs.set_page_blank(a, doc_id, page, blank)
        except docs.EditError as e:
            return redirect(_doc_url(doc_id, form, msg=str(e)))
        msg = (_("Page %(num)s is hidden as blank.", num=page) if blank
               else _("Page %(num)s is always shown.", num=page))  # fmt: skip
        return redirect(_doc_url(doc_id, form, msg=msg, pages="all") + f"#page-{page}")
    elif action == "ocr_all_pages":
        ocr_all_pages(a, doc_id)
        msg = _("All pages are being recognized with AI and the document is classified again.")
    elif action.startswith(("accept_", "dismiss_")):
        # suggestion indexes are only valid for the revision the page showed
        if _stale(form, meta):
            return redirect(_doc_url(doc_id, form, msg=_(STALE_MSG)))
        kind, idx = action.split("_", 1)
        fn = docs.accept_suggestion if kind == "accept" else docs.dismiss_suggestion
        try:
            fn(a, doc_id, int(idx))
        except (docs.EditError, ValueError) as e:
            msg = str(e)
    elif action == "delete" and str(form.get("review") or "") == "1":
        # in the review: on to the next document (found before this one leaves the list)
        nxt = _next_to_review(a.conn, doc_id)
        trash.trash_document(a, doc_id)
        undo = urlencode({"undo": f"doc:{doc_id}"})
        if nxt is None:
            return redirect("/inbox?" + undo)
        return redirect(f"/documents/{nxt}?review=1&{undo}")
    elif action == "delete":
        trash.trash_document(a, doc_id)
        back = _safe_next(_form_val(form, "back") or "/")
        if not (back == "/" or back.startswith("/?")):
            back = "/"
        sep = "&" if "?" in back else "?"
        return redirect(back + sep + urlencode({"undo": f"doc:{doc_id}"}))
    return redirect(_doc_url(doc_id, form, msg=msg))


@router.post("/documents/{doc_id}/action")
async def document_action(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_action_sync, get_archive(request), doc_id, form)


# --- notes & attachments ----------------------------------------------------------------


@router.get("/documents/{doc_id}/attachments/{att_id}")
def document_attachment(
    request: Request,
    doc_id: str,
    att_id: str,
    inline: int = 0,
    p: Principal = Depends(require_user),
):
    return api.attachment_response(request, doc_id, att_id, bool(inline))


def _note_autosave(a, doc_id: str, form) -> JSONResponse:
    """A note saved as soon as the user leaves it (or pauses typing). A new note comes with
    the id the page chose for it, so a save sent twice changes it instead of adding it
    again; an emptied note is deleted. Answers with the note as the page shows it."""
    from markupsafe import Markup

    note_id = _form_val(form, "note_id")
    text = str(form.get("text") or "")
    try:
        if _form_val(form, "action") == "add":
            meta = docs.add_note(a, doc_id, text, note_id or None)
            note_id = note_id or meta.notes[-1].id
        else:
            meta = docs.edit_note(a, doc_id, note_id, text)
    except docs.EditError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    note = next((n for n in meta.notes if n.id == note_id), None)
    html = ""
    if note is not None:
        m = meta.model_dump(mode="json")
        rv = (
            Markup('<input type="hidden" name="review" value="1">')
            if str(form.get("review") or "") == "1"
            else Markup("")
        )
        macro = _ENV.get_template("_notes.html").module.note_article
        html = str(macro(m, note.model_dump(mode="json"), _form_val(form, "csrf_token"), rv))
    return JSONResponse({"ok": True, "revision": meta.revision, "note_id": note_id if note else None,
                         "deleted": note is None, "note_html": html})  # fmt: skip


def _notes_sync(a, doc_id: str, form) -> Response:
    action = str(form.get("action") or "")
    if str(form.get("autosave") or "") == "1" and action in ("add", "save"):
        return _note_autosave(a, doc_id, form)
    note_id = str(form.get("note_id") or "")
    text = str(form.get("text") or "")
    try:
        if action == "add":
            docs.add_note(a, doc_id, text)
            msg = _("Note saved.")
        elif action == "save":
            docs.edit_note(a, doc_id, note_id, text)
            msg = _("Note changed.")
        elif action == "delete":
            docs.delete_note(a, doc_id, note_id)
            msg = _("Note deleted.")
        elif action == "delete_attachment":
            docs.delete_attachment(a, doc_id, str(form.get("attachment_id") or ""))
            msg = _("Attachment deleted.")
        elif action == "attach":
            n = 0
            for f in form.getlist("files"):
                if hasattr(f, "file") and f.filename:
                    docs.add_attachment(
                        a, doc_id, f.file, f.filename, str(form.get("description") or "")
                    )
                    n += 1
            msg = (
                ngettext("%(num)d attachment added.", "%(num)d attachments added.", n)
                if n
                else _("No file selected.")
            )
        else:
            msg = ""
    except docs.EditError as e:
        msg = str(e)
    return redirect(f"/documents/{doc_id}?" + urlencode({"msg": msg}) + "#notes-h")


@router.post("/documents/{doc_id}/notes")
async def document_notes(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_notes_sync, get_archive(request), doc_id, form)


# --- duplicates ------------------------------------------------------------------------


def _facts(a, m: dict, pos) -> dict[str, str]:
    cf = "; ".join(
        f"{k}: {v.get('value')}{' ' + v['currency'] if v.get('currency') else ''}"
        for k, v in (m.get("custom_fields") or {}).items()
    )
    if pos:
        paper = _(
            "filed: %(section)s, position %(position)s",
            section=pos.section, position=pos.position_from_top,
        )  # fmt: skip
    elif m.get("paper_location"):
        paper = m["paper_location"]
    elif m.get("paper_discarded_at"):
        paper = _("not kept")
    else:
        paper = _("paper, open") if m.get("paper") else "–"
    received = (
        f"{_dt(m.get('received_at'))} · {SOURCE_LABELS.get(m.get('source'), m.get('source'))}"
    )
    return {
        _("Title"): m.get("title") or m.get("original_filename"),
        _("Document date"): _date(m.get("document_date")) if m.get("document_date") else "–",
        _("Sender"): m.get("correspondent") or "–",
        _("Type"): m.get("document_type") or "–",
        _("Pages"): str(m.get("page_count") or "?"),
        _("File"): f"{m.get('mime_type')}, {round((m.get('size_bytes') or 0) / 1024)} KB",
        i18n.pgettext("duplicate facts", "Received"): received,
        _("Text"): TEXT_STATUS.get(m.get("text_status"), m.get("text_status")),
        _("Paper"): paper,
        _("Tags"): ", ".join(m.get("tags") or []) or "–",
        _("Custom fields"): cf or "–",
        _("Notes/attachments"): f"{len(m.get('notes') or [])} / {len(m.get('attachments') or [])}",
    }


def _user_data(m: dict, pos) -> list[str]:
    """What would be lost (into the trash) with this side."""
    out = []
    if pos or m.get("paper_location"):
        out.append(_("paper filing"))
    if m.get("notes"):
        out.append(ngettext("%(num)d note", "%(num)d notes", len(m["notes"])))
    if m.get("attachments"):
        out.append(ngettext("%(num)d attachment", "%(num)d attachments", len(m["attachments"])))
    if any(m.get("field_locks", {}).values()):
        out.append(_("manual corrections"))
    return out


def _recommend(sides: list[dict], diff: dict) -> tuple[int | None, str]:
    """Which side to keep (0/1) and why - or None when nothing speaks for one side."""
    score = [0.0, 0.0]
    why: list[list[str]] = [[], []]
    marked = [
        p for p in diff["pages"] if p.get("more_ink") in ("a", "b") and p["status"] != "missing"
    ]
    for p in marked:
        i = 0 if p["more_ink"] == "a" else 1
        score[i] += 3
        why[i].append(_("additional marks on p. %(page)s (e.g. a signature)", page=p["page"]))
    for i, s in enumerate(sides):
        if s["user_data"]:
            score[i] += 2
            why[i].append(_("has %(items)s", items=", ".join(s["user_data"])))
    pa, pb = (sides[0]["m"].get("page_count") or 0), (sides[1]["m"].get("page_count") or 0)
    if pa != pb:
        i = 0 if pa > pb else 1
        score[i] += 2
        why[i].append(_("more pages"))
    if score[0] == score[1]:
        return None, ""
    i = 0 if score[0] > score[1] else 1
    return i, "; ".join(dict.fromkeys(why[i]))


def _warm_pagediff(a, id_a: str, id_b: str) -> None:
    """Prepare the next pair's page comparison in the background (it is cached)."""
    import threading

    from ..pagediff import compare_documents

    def run() -> None:
        try:
            compare_documents(a, docs.load_meta(a, id_a), docs.load_meta(a, id_b))
        except Exception:  # noqa: BLE001 - only a cache warm-up
            logging.getLogger("heftig.web").debug("page diff warm-up failed", exc_info=True)

    threading.Thread(target=run, daemon=True, name="pagediff-warm").start()


@router.get("/duplicates/next")
def duplicate_next(request: Request, after: str = "", p: Principal = Depends(require_user)):
    """Skip: the pair after `after` ("a:b") in the list, else the first one."""
    pairs = open_pairs(get_archive(request).conn)
    if not pairs:
        return redirect("/inbox?" + urlencode({"msg": _("No possible duplicates open.")}))
    keys = [f"{x['a']['id']}:{x['b']['id']}" for x in pairs]
    i = (keys.index(after) + 1) % len(keys) if after in keys else 0
    return redirect(f"/duplicates/{pairs[i]['a']['id']}/{pairs[i]['b']['id']}")


@router.get("/duplicates/{a_id}/{b_id}")
def duplicate_page(request: Request, a_id: str, b_id: str, p: Principal = Depends(require_user)):
    from ..pagediff import compare_documents, text_diff

    a = get_archive(request)
    pairs = open_pairs(a.conn)
    keys = [f"{x['a']['id']}:{x['b']['id']}" for x in pairs]
    here = next((i for i, k in enumerate(keys) if set(k.split(":")) == {a_id, b_id}), None)
    metas = [docs.load_meta(a, a_id), docs.load_meta(a, b_id)]
    sides = []
    for m in metas:
        md = m.model_dump(mode="json")
        pos = docs.filing_position(a, m)
        sides.append({"m": md, "pos": pos, "facts": _facts(a, md, pos),
                      "user_data": _user_data(md, pos), "sizes": _page_sizes(a, md)})  # fmt: skip
    diff = compare_documents(a, metas[0], metas[1])
    tdiff = text_diff(docs.get_text(a, a_id), docs.get_text(a, b_id))
    rows = [(k, sides[0]["facts"][k], sides[1]["facts"][k]) for k in sides[0]["facts"]]
    keep, why = _recommend(sides, diff)
    if here is not None and len(pairs) > 1:
        nxt = pairs[(here + 1) % len(pairs)]
        _warm_pagediff(a, nxt["a"]["id"], nxt["b"]["id"])
    return render(
        request, "duplicate.html", nav="inbox", sides=sides, diff=diff, tdiff=tdiff,
        differing=[r for r in rows if r[1] != r[2]], same=[r[0] for r in rows if r[1] == r[2]],
        keep=keep, why=why, pair=pairs[here] if here is not None else None,
        position=(here + 1) if here is not None else None, total=len(pairs),
        skip_after=f"{a_id}:{b_id}", message=request.query_params.get("msg"),
    )  # fmt: skip


def _duplicate_sync(a, form) -> RedirectResponse:
    action = str(form.get("action") or "")
    ida, idb = str(form.get("a") or ""), str(form.get("b") or "")
    docs.load_meta(a, ida)
    docs.load_meta(a, idb)
    if action == "keep":
        keep_both(a, ida, idb)
        return _next_duplicate(a, _("Both documents are kept."))
    if action in ("delete_a", "delete_b"):
        victim = ida if action == "delete_a" else idb
        keeper = docs.load_meta(a, idb if victim == ida else ida)
        with i18n.language("en"):  # stored in English; shown translated
            reason = _("Duplicate of “%(title)s”", title=keeper.title or keeper.original_filename)
        gone = docs.load_meta(a, victim)
        placed = gone.filing_sequence is not None and not (
            keeper.filing_sequence is not None or keeper.paper_location or keeper.paper_discarded_at
        )
        trash.trash_document(a, victim, reason=reason)
        msg = _("Duplicate moved to the trash.")
        if placed:
            # the paper in the binder is the same letter: the kept copy takes over its place
            docs.take_filing(a, keeper.id, gone)
            msg = _("Duplicate moved to the trash; the kept copy takes over its place in the "
                    "binder.")  # fmt: skip
        return _next_duplicate(a, msg, undo=f"doc:{victim}")
    if action == "combine":
        # the recommended copy (signed, annotated ...) comes first
        order = [idb, ida] if str(form.get("first") or "") == idb else [ida, idb]
        try:
            new = combine.combine(a, order)
        except combine.CombineError as e:
            return redirect(f"/duplicates/{ida}/{idb}?" + urlencode({"msg": str(e)}))
        return _next_duplicate(a, _("Combined."), undo=f"batch:{combine.batch_for(new.id)}")
    return redirect("/inbox")


def _next_duplicate(a, msg: str, undo: str | None = None) -> RedirectResponse:
    """Work through the list: straight on to the next open pair, else back to the inbox."""
    pairs = open_pairs(a.conn)
    extra = [("undo", undo)] if undo else []
    if pairs:
        # with an undo bar the "moved to the trash" part is already on screen
        note = ("" if undo else f"{msg} ") + _("Next pair (%(num)s open).", num=len(pairs))
        return redirect(f"/duplicates/{pairs[0]['a']['id']}/{pairs[0]['b']['id']}?"
                        + urlencode([("msg", note), *extra]))  # fmt: skip
    return redirect(
        "/inbox?" + urlencode([("msg", f"{msg} " + _("No more possible duplicates.")), *extra])
    )


@router.post("/duplicates/action")
async def duplicate_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_duplicate_sync, get_archive(request), form)


# --- suggestions in bulk ---------------------------------------------------------------------


@router.get("/suggestions")
def suggestions_page(request: Request, p: Principal = Depends(require_user)):
    from .. import suggestions

    return render(
        request, "suggestions.html", nav="inbox", groups=suggestions.groups(get_archive(request)),
        message=request.query_params.get("msg"),
    )  # fmt: skip


def _suggestions_sync(a, form) -> RedirectResponse:
    from .. import suggestions

    action = _form_val(form, "action")
    ids = [str(v) for v in form.getlist("doc")]
    n = suggestions.apply(
        a, _form_val(form, "field"), str(form.get("value") or ""), ids, action == "accept"
    )
    if action == "accept":
        msg = ngettext("Accepted for %(num)d document.", "Accepted for %(num)d documents.", n)
    else:
        msg = ngettext("Dismissed for %(num)d document.", "Dismissed for %(num)d documents.", n)
    return redirect("/suggestions?" + urlencode({"msg": msg}))


@router.post("/suggestions/action")
async def suggestions_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_suggestions_sync, get_archive(request), form)


# --- quarantine ----------------------------------------------------------------------------


def _quarantine_sync(a, form) -> RedirectResponse:
    from ..consume import QuarantineError, hide_quarantined, retry_quarantined

    name, action = _form_val(form, "file"), _form_val(form, "action")
    try:
        if action == "retry":
            r = retry_quarantined(a, name)
            if r["status"] == "rejected":
                msg = _("Still not accepted: %(reason)s", reason=i18n.translate_text(r["message"]))
            else:
                return redirect(f"/documents/{r['document_id']}?" + urlencode(
                    {"msg": _("Accepted from quarantine.")}))  # fmt: skip
        elif action == "hide":
            hide_quarantined(a, name)
            msg = _("Hidden (stays in quarantine/ausgeblendet/).")
        else:
            msg = ""
    except QuarantineError as e:
        msg = str(e)
    return redirect("/inbox?" + urlencode({"msg": msg}))


@router.post("/quarantine/action")
async def quarantine_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_quarantine_sync, get_archive(request), form)


# --- combining documents -----------------------------------------------------------------


@router.get("/combine")
def combine_page(request: Request, p: Principal = Depends(require_user)):
    """One page: the chosen documents in order (move up, remove) and what might belong to
    them (neighbours in scan order, similar documents, or a search)."""
    a = get_archive(request)
    ids = list(dict.fromkeys(request.query_params.getlist("ids")))[:20]
    chosen = combine.rows(a, ids)
    ids = [r["id"] for r in chosen]
    if not ids:
        return redirect("/")
    q = request.query_params.get("q", "")[:200]

    def link(new_ids: list[str]) -> str:
        return "/combine?" + urlencode([("ids", i) for i in new_ids] + ([("q", q)] if q else []))

    for n, r in enumerate(chosen):
        r["up"] = link(ids[: n - 1] + [ids[n], ids[n - 1]] + ids[n + 1 :]) if n else None
        r["remove"] = link([i for i in ids if i != r["id"]]) if len(ids) > 1 else None
        r["first_page"] = 1 + sum(c["page_count"] or 1 for c in chosen[:n])
    cands = combine.candidates(a, ids, q)
    for r in cands:
        r["add"] = link(ids + [r["id"]])
    problem = None
    if len(ids) > 1:
        try:
            combine.check(a, ids)
        except combine.CombineError as e:
            problem = str(e)
    return render(
        request, "combine.html", nav="documents", chosen=chosen, candidates=cands, q=q,
        pages=sum(r["page_count"] or 1 for r in chosen), problem=problem,
        message=request.query_params.get("msg"),
    )  # fmt: skip


def _combine_sync(a, ids: list[str]) -> RedirectResponse:
    try:
        new = combine.combine(a, ids)
    except combine.CombineError as e:
        return redirect("/combine?" + urlencode([("ids", i) for i in ids] + [("msg", str(e))]))
    return redirect(
        f"/documents/{new.id}?" + urlencode({"undo": f"batch:{combine.batch_for(new.id)}"})
    )


@router.post("/combine")
async def combine_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    ids = [str(v) for v in form.getlist("ids")][:20]
    return await run_in_threadpool(_combine_sync, get_archive(request), ids)


# --- splitting a document / rearranging its pages ----------------------------------------


@router.get("/documents/{doc_id}/split")
def split_page(request: Request, doc_id: str, p: Principal = Depends(require_user)):
    """The pages as tiles: cut between them, move them (drag, or keys), turn, remove."""
    a = get_archive(request)
    m = docs.load_meta(a, doc_id)
    count = m.page_count or 1
    busy = a.conn.execute(
        "SELECT 1 FROM jobs WHERE status IN ('queued','processing') AND doc_id=?", (doc_id,)
    ).fetchone()
    blank = set(docs.blank_pages(docs.load_text_pages(a, doc_id), m.page_blank))
    pages = [
        {"n": n, "turn": docs.rotation(m, n), "blank": n in blank} for n in range(1, count + 1)
    ]
    return render(
        request, "split.html", nav="documents", m=m, pages=pages,
        problem=_("The document is still being processed – please wait a moment.")
        if busy else None,
        message=request.query_params.get("msg"),
    )  # fmt: skip


def parse_layout(layout: str) -> tuple[list[list[int]], dict[int, int]]:
    """``1,2r90|4`` (parts separated by ``|``, pages by ``,``, ``r`` + degrees for a turned
    page) -> parts and the turn of every listed page. ValueError if unreadable."""
    parts: list[list[int]] = []
    turns: dict[int, int] = {}
    for chunk in layout.strip().split("|"):
        part = []
        for item in filter(None, chunk.split(",")):
            num, _r, turn = item.strip().partition("r")
            n = int(num)
            part.append(n)
            turns[n] = int(turn or 0)
        parts.append(part)
    return parts, turns


def _split_sync(a, doc_id: str, layout: str) -> RedirectResponse:
    back = f"/documents/{doc_id}/split?"
    try:
        parts, turns = parse_layout(layout[:20000])
    except ValueError:
        return redirect(back + urlencode({"msg": _("The page layout could not be read.")}))
    try:
        new = split.split(a, doc_id, parts, turns)
    except split.SplitError as e:
        return redirect(back + urlencode({"msg": str(e)}))
    return redirect(
        f"/documents/{new[0].id}?" + urlencode({"undo": f"batch:{split.batch_for(doc_id)}"})
    )


@router.post("/documents/{doc_id}/split")
async def split_action(request: Request, doc_id: str, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(
        _split_sync, get_archive(request), doc_id, _form_val(form, "layout")
    )


# --- upload ------------------------------------------------------------------------------


@router.get("/titles")
def titles_page(request: Request, p: Principal = Depends(require_user)):
    from .. import titles

    a = get_archive(request)
    ai = describe(a.settings)["classify"]
    usable = ai["provider"] not in ("none", "rules", "mock") and not ai.get("blocked")
    model = ai.get("model") or ""
    if not model and ai["provider"] == "anthropic":
        from ..providers.anthropic_provider import DEFAULT_MODEL

        model = DEFAULT_MODEL
    last = a.conn.execute(
        "SELECT * FROM jobs WHERE kind='titles' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    job = dict(last) if last and last["status"] in ("queued", "processing") else None
    message = request.query_params.get("msg")
    groups = titles.proposals(a.conn)
    if last and last["status"] == "failed" and not groups and not message:
        message = _("Last attempt failed: %(error)s", error=i18n.translate_text(last["error"]))
    return render(
        request,
        "titles.html",
        nav="settings",
        job=job,
        groups=groups,
        total=sum(len(g["items"]) for g in groups),
        ai={"target": ai["target"], "model": model} if usable else None,
        est=titles.estimate(a.conn, model),
        message=message,
    )


def _titles_sync(a, form) -> RedirectResponse:
    from .. import titles

    action = _form_val(form, "action")
    if action == "generate":
        busy = a.conn.execute(
            "SELECT 1 FROM jobs WHERE kind='titles' AND status IN ('queued','processing')"
        ).fetchone()
        if not busy:
            titles.enqueue_job(a)
        return redirect("/titles")
    if action == "accept":
        r = titles.accept(a, [v for v in form.getlist("doc") if isinstance(v, str)])
        msg = ngettext("%(num)d title accepted.", "%(num)d titles accepted.", r["accepted"])
        if r["stale"]:
            msg += " " + _("%(num)s skipped (changed in the meantime).", num=r["stale"])
        rest = titles.pending_count(a.conn)
        if rest:
            titles.dismiss(a)  # unticked proposals were deliberately not taken
        return redirect("/titles?" + urlencode({"msg": msg}))
    if action == "dismiss":
        titles.dismiss(a)
        return redirect("/titles?" + urlencode({"msg": _("Suggestions dismissed.")}))
    return redirect("/titles")


@router.post("/titles/action")
async def titles_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_titles_sync, get_archive(request), form)


@router.get("/scan")
def scan_page(request: Request, p: Principal = Depends(require_user)):
    return render(
        request, "scan.html", nav="scan", session=sessions.active(get_archive(request).conn)
    )


def _session_sync(a, form) -> RedirectResponse:
    action = _form_val(form, "action")
    sid = _form_val(form, "session")
    # the documents the card showed (older pages without the field: everything)
    shown = {str(v) for v in form.getlist("shown")} if "shown" in form else None
    msg = ""
    try:
        if action == "start":
            s = sessions.start(a, _form_val(form, "name"), _form_val(form, "mode"))
            msg = _("Batch “%(name)s” is running – just scan now.", name=s["name"])
        elif action == "end":
            sessions.end(a, sid)
            msg = _("Batch ended.")
        elif action == "close":
            sessions.close(a, sid)
            msg = _("Hidden – open paper is listed under “Paper still to file”.")
        elif action == "file_all":
            n = sessions.file_all(a, sid, shown)
            msg = ngettext(
                "%(num)d document filed.", "%(num)d documents filed – the first scanned on top.", n
            )
        elif action == "apply_sort":
            kept, gone = sessions.apply_sort(a, sid, shown)
            msg = _("%(kept)s originals filed, %(gone)s marked as discarded.", kept=kept, gone=gone)
        elif action.startswith(("keep:", "discard:")):
            what, doc_id = action.split(":", 1)
            docs.set_keep_original(a, doc_id, what == "keep")
    except sessions.SessionError as e:
        msg = str(e)
    return redirect("/inbox?" + urlencode({"msg": msg} if msg else {}))


@router.post("/sessions/action")
async def session_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_session_sync, get_archive(request), form)


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    data = {
        # the app's identity: what Chromium derived from start_url before "id" was set, so
        # existing installations stay the same app
        "id": "/scan",
        "name": _("Heftig – document archive"),
        "short_name": "Heftig",
        "description": _("Your paper and digital documents, scanned, searchable and filed."),
        "lang": i18n.current(),
        "start_url": "/scan",
        "scope": "/",
        "display": "standalone",
        "background_color": "#111512",
        "theme_color": "#2f5d50",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
            # with room around the artwork, for launchers that cut icons into circles etc.
            {
                "src": "/static/icon-maskable-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": "/static/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
        "shortcuts": [
            {"name": i18n.pgettext("app shortcut", "Scan"), "url": "/scan"},
            {"name": i18n.pgettext("app shortcut", "Search"), "url": "/"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json")


def _offline_html(request: Request) -> str:
    return request.app.state.templates.get_template("offline.html").render()


@router.get("/offline", include_in_schema=False)
def offline_page(request: Request):
    """What the service worker shows when Heftig cannot be reached (no user data: it is kept
    in the browser)."""
    return HTMLResponse(_offline_html(request))


@router.get("/sw.js", include_in_schema=False)
def service_worker(request: Request):
    """The service worker, at the root so that it covers the whole site. Its version follows
    the offline page (language, text, stylesheet), so browsers fetch a new copy when it changes."""
    import hashlib

    v = request.app.state.templates.env.globals["asset_v"]
    config = {
        "version": hashlib.sha256(_offline_html(request).encode()).hexdigest()[:12],
        "assets": [f"/static/{f}?v={v}" for f in ("app.css", "pwa.js", "icon.svg")],
    }
    src = (Path(__file__).parent / "static" / "sw.js").read_text(encoding="utf-8")
    src = re.sub(r"^const CONFIG = .*$", f"const CONFIG = {json.dumps(config)};", src, count=1,
                 flags=re.M)  # fmt: skip
    return Response(src, media_type="text/javascript")


@router.get("/upload")
def upload_page(request: Request, p: Principal = Depends(require_user)):
    return render(request, "upload.html", nav="upload", results=None)


def _upload_sync(a, form) -> list[dict]:
    kind = "paper" if form.get("kind") == "paper" else "digital"
    results = []
    for f in form.getlist("files"):
        if not hasattr(f, "file"):
            continue
        res = ingest_stream(a, f.file, f.filename, "web", {"client": "browser", "kind": kind},
                            paper=kind == "paper")  # fmt: skip
        results.append(res.as_dict())
    return results


@router.post("/upload")
async def upload_submit(request: Request, p: Principal = Depends(require_write)):
    """Fallback without JavaScript."""
    form = await request.form()
    results = await run_in_threadpool(_upload_sync, get_archive(request), form)
    return render(request, "upload.html", nav="upload", results=results)


# --- inbox -------------------------------------------------------------------------------


@router.get("/inbox")
def inbox_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    conn = a.conn
    active = [
        jobs.as_dict(r)
        for r in conn.execute(
            "SELECT j.*, d.title, d.original_filename FROM jobs j LEFT JOIN documents d "
            "ON d.id=j.doc_id WHERE j.status IN ('queued','processing') ORDER BY j.id LIMIT 200"
        )
    ]
    problems = [
        jobs.as_dict(r)
        for r in conn.execute(
            "SELECT j.*, d.title, d.original_filename FROM jobs j LEFT JOIN documents d "
            "ON d.id=j.doc_id WHERE j.status IN ('failed') ORDER BY j.id DESC LIMIT 200"
        )
    ]
    review = [
        dict(r)
        for r in conn.execute(
            "SELECT id, title, original_filename, status, review_reasons, received_at FROM documents "
            "WHERE status IN ('needs_review','failed') ORDER BY received_at DESC LIMIT 200"
        )
    ]
    for r in review:
        r["review_reasons"] = json.loads(r["review_reasons"] or "[]")
    review_total = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status IN ('needs_review','failed')"
    ).fetchone()[0]
    events = []
    for r in conn.execute("SELECT * FROM ingest_events ORDER BY id DESC LIMIT 25"):
        e = dict(r)
        e["source_details"] = json.loads(e["source_details"] or "{}")
        events.append(e)
    unfiled = [
        dict(r)
        for r in conn.execute(
            "SELECT id, title, original_filename, received_at FROM documents "
            "WHERE paper=1 AND filing_sequence IS NULL AND paper_location IS NULL "
            "AND paper_discarded_at IS NULL AND (scan_session_id IS NULL OR scan_session_id "
            "NOT IN (SELECT id FROM scan_sessions WHERE closed_at IS NULL)) "
            "ORDER BY ingest_sequence DESC LIMIT 100"
        )
    ]
    return render(
        request,
        "inbox.html",
        nav="inbox",
        active=active,
        problems=problems,
        review=review,
        review_total=review_total,
        events=events,
        unfiled=unfiled,
        current_binder=binders.current(a),
        quarantine=list_quarantine(a)[:50],
        snapshot_warning=maintenance.snapshot_overdue(a),
        suggestion_groups=_suggestion_groups(a),
        duplicates=open_pairs(conn)[:50],
        title_proposals=conn.execute("SELECT COUNT(*) FROM title_proposals").fetchone()[0],
        session=sessions.card(a),
        auto_resolved=conn.execute(
            "SELECT COUNT(*) FROM trash WHERE batch LIKE 'auto-%' AND trashed_at > ?",
            (iso(utcnow() - timedelta(days=7)),),
        ).fetchone()[0],
        session_modes=sessions.MODES,
        message=request.query_params.get("msg"),
        counts=jobs.counts(conn),
        status=maintenance.status(a),
        providers=describe(a.settings),
    )


def _suggestion_groups(a) -> int:
    """Groups with the same suggestion on several documents (worth deciding in bulk)."""
    from .. import suggestions

    return suggestions.open_count(a)[1]


def _inbox_sync(a, form) -> RedirectResponse:
    action = str(form.get("action") or "")
    if action.startswith("retry_") and action[6:].isdecimal():
        jobs.retry(a.conn, int(action[6:]))
    elif action in ("reprocess_all_extract", "reprocess_all_classify"):
        ids = [r[0] for r in a.conn.execute("SELECT id FROM documents ORDER BY ingest_sequence")]
        reprocess(a, ids, ["extract"] if action.endswith("extract") else ["classify"])
    elif action == "reprocess_selected":
        ids = [str(v) for v in form.getlist("doc")]
        stages = [s for s in form.getlist("stage") if s in ("extract", "classify")] or ["classify"]
        if ids:
            reprocess(a, ids, stages)
    elif action.startswith("file_"):
        docs.mark_filed(a, action[5:])
        return redirect("/inbox#unf-h")  # stay at the list: the next sheet is right there
    elif action.startswith("notkept_"):
        # handed over (a referral at the doctor's), sent away or thrown out: nothing to file
        docs.set_paper_state(a, action[8:], discarded=True)
        return redirect("/inbox#unf-h")
    if action.startswith("reprocess_all"):
        return redirect("/settings?" + urlencode({"msg": _("Reprocessing scheduled.")}))
    return redirect("/inbox")


@router.post("/inbox/action")
async def inbox_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_inbox_sync, get_archive(request), form)


@router.get("/inbox/progress")
def inbox_progress(request: Request, p: Principal = Depends(require_user)):
    conn = get_archive(request).conn
    rows = conn.execute(
        "SELECT id, doc_id, status, stage, progress, error FROM jobs "
        "WHERE status IN ('queued','processing') ORDER BY id LIMIT 200"
    ).fetchall()
    return {"counts": jobs.counts(conn), "jobs": [dict(r) for r in rows]}


# --- settings ----------------------------------------------------------------------------


@router.get("/settings")
def settings_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    s = a.settings
    return render(
        request,
        "settings.html",
        nav="settings",
        providers=describe(s),
        terms={k: tax.list_terms(a.conn, k) for k in tax.KINDS},
        tokens=auth.list_tokens(a.conn),
        language_fixed="language" in settings_store.fixed(a.base_settings),
        status=maintenance.status(a),
        s=s,
        new_token=None,
        message=request.query_params.get("msg"),
        synonyms_text=synonyms.as_text(synonyms.load(a.paths)),
        builtin_synonyms=synonyms.builtin(),
        meaning=_meaning_view(a),
    )


def _meaning_view(a) -> dict[str, Any]:
    from .. import semantic
    from ..db import get_meta

    return {
        "active": semantic.available(a.settings),
        "status": semantic.status(a.conn, a.settings),
        "error": get_meta(a.conn, semantic.ERROR_KEY) or "",
    }


@router.post("/settings/language")
async def settings_language(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    a = get_archive(request)
    lang = _form_val(form, "language")
    if lang in i18n.LANGUAGES and "language" not in settings_store.fixed(a.base_settings):
        await run_in_threadpool(settings_store.save, a.conn, a.base_settings, {"language": lang})
        a.refresh_settings()
    return redirect("/settings")


@router.post("/settings/synonyms")
async def settings_synonyms(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    a = get_archive(request)
    groups = synonyms.parse_text(_form_val(form, "synonyms")[:50000])
    saved = await run_in_threadpool(synonyms.save, a.paths, groups)
    msg = ngettext("%(num)d group saved.", "%(num)d groups saved.", len(saved))
    return redirect("/settings?" + urlencode({"msg": msg}) + "#syn-h")


def _sender_rows(a) -> list[dict[str, Any]]:
    """Every address documents were e-mailed from, and every named one, with its name."""
    from ..search import EMAIL_FROM

    names = senders.load(a.paths)
    counts = {
        r[0]: r[1]
        for r in a.conn.execute(
            f"SELECT {EMAIL_FROM} AS a, COUNT(*) FROM documents d WHERE d.source = 'email' "
            "GROUP BY a HAVING a IS NOT NULL"
        )
    }
    rows = [{"address": x, "name": names.get(x, ""), "count": n} for x, n in counts.items()]
    rows += [{"address": x, "name": n, "count": 0} for x, n in names.items() if x not in counts]
    return sorted(rows, key=lambda r: (-r["count"], r["address"]))


@router.get("/settings/senders")
def settings_senders(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    msg = _("Saved.") if request.query_params.get("saved") else None
    return render(request, "senders.html", nav="settings", rows=_sender_rows(a), message=msg)


@router.post("/settings/senders")
async def settings_senders_save(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    a = get_archive(request)
    addresses = [str(v) for v in form.getlist("address")][: senders.MAX_NAMES]
    names = [str(v) for v in form.getlist("name")]
    await run_in_threadpool(senders.save, a.paths, dict(zip(addresses, names, strict=False)))
    return redirect("/settings/senders?saved=1")


@router.post("/settings/action")
async def settings_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_settings_sync, request, form, p)


CATEGORY_KINDS = i18n.Labels({
    "correspondent": N_("Senders"),
    "document_type": N_("Document types"),
    "tag": N_("Tags"),
})  # fmt: skip


@router.get("/binders")
def binders_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    binders.current(a)  # there always is one
    msg = {"next": _("New filings go into the new binder from now on."),
           "renamed": _("Renamed.")}.get(request.query_params.get("done", ""))  # fmt: skip
    return render(request, "binders.html", nav="settings", binders=binders.overview(a),
                  next_name=binders.next_name(binders.load(a.paths), a.settings.language),
                  message=msg)  # fmt: skip


@router.post("/binders")
async def binders_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_binders_sync, request, form)


def _binders_sync(request: Request, form) -> Any:
    a = get_archive(request)
    action = _form_val(form, "action")
    try:
        if action == "next":
            binders.start_next(a, _form_val(form, "name"))
            return redirect("/binders?done=next")
        if action == "rename":
            binders.rename(a, _form_val(form, "old"), _form_val(form, "name"))
            return redirect("/binders?done=renamed")
        if action == "sheet_out":
            binders.sheet_taken_out(a, _form_val(form, "sheet_out"))
            return redirect("/binders")
    except binders.BinderError as e:
        return render(request, "binders.html", http_status=400, nav="settings",
                      binders=binders.overview(a), error=str(e),
                      next_name=binders.next_name(binders.load(a.paths), a.settings.language))  # fmt: skip
    return redirect("/binders")


@router.get("/categories")
def categories_page(request: Request, p: Principal = Depends(require_user)):
    a = get_archive(request)
    qp = request.query_params
    kind = qp.get("kind") if qp.get("kind") in tax.KINDS else "correspondent"
    q = qp.get("q", "")[:100]
    sort = qp.get("sort") if qp.get("sort") in ("name", "count", "rare") else "name"
    all_terms = tax.list_terms(a.conn, kind)
    terms = all_terms
    if q:
        needle = q.casefold()
        terms = [t for t in terms if needle in (t.name + " " + " ".join(t.aliases)).casefold()]
    if sort == "count":
        terms = sorted(terms, key=lambda t: (-t.doc_count, t.norm))
    elif sort == "rare":
        terms = sorted(terms, key=lambda t: (t.doc_count, t.norm))
    open_id = qp.get("open")
    return render(
        request, "categories.html", nav="settings", kind=kind, kinds=CATEGORY_KINDS.items(),
        counts={k: len(tax.list_terms(a.conn, k)) for k in tax.KINDS}, terms=terms,
        all_terms=all_terms, q=q, sort=sort, open=int(open_id) if (open_id or "").isdigit() else None,
        param={"correspondent": "correspondent", "document_type": "document_type", "tag": "tag"}[kind],
        message=qp.get("msg"),
    )  # fmt: skip


def _categories_sync(a, form) -> RedirectResponse:
    action = _form_val(form, "action")
    kind = _form_val(form, "kind") if _form_val(form, "kind") in tax.KINDS else "correspondent"
    back = {"kind": kind, "q": _form_val(form, "back_q"), "sort": _form_val(form, "back_sort")}
    msg, open_id = "", None
    try:
        term_id = int(form.get("term_id")) if form.get("term_id") else None
        if action == "term_create":
            with write_tx(a.conn):
                tax.get_or_create(a.conn, kind, _form_val(form, "name"))
                tax.write_sidecar(a.conn, a.paths)
            msg = _("Entry created.")
        elif action == "term_rename" and term_id:
            n = docs.rename_term(a, term_id, _form_val(form, "name"))
            msg = ngettext(
                "Renamed (%(num)d document updated).", "Renamed (%(num)d documents updated).", n
            )
            open_id = term_id
        elif action == "term_merge" and term_id:
            into = tax.find_term(a.conn, kind, _form_val(form, "into"))
            if into is None:
                raise ValueError(_("“%(name)s” does not exist here.", name=_form_val(form, "into")))
            n = docs.merge_terms(a, term_id, into)
            msg = ngettext(
                "Merged (%(num)d document updated).", "Merged (%(num)d documents updated).", n
            )
            open_id = into
        elif action == "alias_add" and term_id:
            docs.add_term_alias(a, term_id, _form_val(form, "alias"))
            msg, open_id = _("Alias added."), term_id
        elif action == "alias_remove" and term_id:
            docs.remove_term_alias(a, term_id, _form_val(form, "alias"))
            msg, open_id = _("Alias removed."), term_id
        elif action == "term_delete" and term_id:
            n = docs.delete_term(a, term_id)
            msg = ngettext(
                "Deleted (%(num)d document updated).", "Deleted (%(num)d documents updated).", n
            )
    except (tax.TaxonomyError, ValueError, TypeError) as e:
        msg = _("Error: %(error)s", error=e)
    query = {k: v for k, v in back.items() if v}
    if open_id:
        query["open"] = str(open_id)
    query["msg"] = msg
    return redirect("/categories?" + urlencode(query) + (f"#t{open_id}" if open_id else ""))


@router.post("/categories/action")
async def categories_action(request: Request, p: Principal = Depends(require_write)):
    form = await request.form()
    return await run_in_threadpool(_categories_sync, get_archive(request), form)


def _settings_sync(request: Request, form, p: Principal):
    a = get_archive(request)
    action = str(form.get("action") or "")
    msg = ""
    try:
        if action == "token_create":
            scope = "read" if form.get("read_only") else "full"
            _tid, token = auth.create_api_token(
                a.conn, p.user.id, str(form.get("name") or "Token"), scope
            )
            return render(
                request, "settings.html", nav="settings", providers=describe(a.settings),
                terms={k: tax.list_terms(a.conn, k) for k in tax.KINDS},
                tokens=auth.list_tokens(a.conn), status=maintenance.status(a), s=a.settings,
                new_token=token, message=_("Token created – copy it now, it will not be shown again."),
                synonyms_text=synonyms.as_text(synonyms.load(a.paths)),
                builtin_synonyms=synonyms.builtin(), meaning=_meaning_view(a),
            )  # fmt: skip
        elif action == "token_revoke":
            auth.revoke_token(a.conn, int(form.get("token_id")))
            msg = _("Token revoked.")
        elif action == "export":
            jobs.enqueue(a.conn, "export", None, {"zip": form.get("zip") == "on"}, max_attempts=1)
            msg = _("Export scheduled – progress in the inbox.")
        elif action == "reindex":
            jobs.enqueue(a.conn, "reindex", None, {}, max_attempts=1)
            msg = _("Search index rebuild scheduled.")
        elif action == "snapshot":
            path = maintenance.db_snapshot(a)
            msg = _("Database snapshot written: %(path)s", path=path.relative_to(a.paths.root))
    except (tax.TaxonomyError, ValueError, TypeError) as e:
        msg = _("Error: %(error)s", error=e)
    return redirect("/settings?" + urlencode({"msg": msg}))
