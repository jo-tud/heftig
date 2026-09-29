"""Document processing job: text extraction (embedded text first, OCR for image pages) and
classification. Stages are idempotent; a job resumed after a crash skips completed stages.

A provider failure never touches the stored original or the existing metadata: it is recorded
in the processing history and shown in the inbox.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

from . import classify as cls
from . import documents as docs
from . import jobs
from .archive import Archive
from .db import get_meta, iso, now_iso, set_meta, utcnow, write_tx
from .i18n import N_, _
from .media import (
    ink_ratio,
    make_preview,
    ocr_image_bytes,
    pdf_embedded_text,
    render_page,
    to_png_bytes,
)
from .models import DocumentMetadata, HistoryEntry, PageText, TextPages
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable
from .storage import atomic_write_json, read_json
from .textnorm import fold

log = logging.getLogger("heftig.processing")

Progress = Callable[[str, float], None]


class RetryLater(Exception):
    """Transient provider error: the job is retried with backoff.

    Rate limits postpone the job without using up one of its attempts.
    """

    def __init__(self, message: str, *, delay: float | None = None, rate_limited: bool = False):
        super().__init__(message)
        self.delay = delay
        self.rate_limited = rate_limited


LOCAL_PROVIDERS = {"tesseract", "mock", "rules", "none"}


def _mark_ai(archive: Archive, reachable: bool, task: str | None = None) -> None:
    """Remember since when a remote AI task (ocr/classify) is unreachable (shown in the inbox).
    Without `task` both are updated (e.g. after a successful probe)."""
    conn = archive.conn
    with write_tx(conn):
        for t in [task] if task else ["ocr", "classify"]:
            key = f"ai_unreachable_since_{t}"
            since = get_meta(conn, key)
            if reachable and since:
                set_meta(conn, key, "")
            elif not reachable and not since:
                set_meta(conn, key, now_iso())


def ai_unreachable_since(conn) -> str | None:
    values = [get_meta(conn, f"ai_unreachable_since_{t}") for t in ("ocr", "classify")]
    values = [v for v in values if v]
    return min(values) if values else None


def _fallback_extractor(archive: Archive, primary) -> object | None:
    if not archive.settings.ai_fallback or primary is None or primary.name in LOCAL_PROVIDERS:
        return None
    try:
        from .providers.tesseract import TesseractExtractor

        return TesseractExtractor(timeout=archive.settings.ocr_page_timeout_seconds)
    except ProviderUnavailable:
        return None


def _record_run(
    conn: sqlite3.Connection,
    doc_id: str,
    task: str,
    provider,
    status: str,
    started: str,
    error: str | None = None,
    suggestions: object = None,
    raw: str | None = None,
    max_kb: int = 64,
    usage: tuple[int, int, float | None] | None = None,
) -> None:
    if raw is not None and len(raw) > max_kb * 1024:
        raw = raw[: max_kb * 1024] + "\n[… truncated]"
    tin, tout, cost = usage if usage else (None, None, None)
    conn.execute(
        "INSERT INTO processing_runs(doc_id, task, provider, model, target, adapter_version, "
        "prompt_version, status, error, suggestions, raw_response, started_at, finished_at, "
        "input_tokens, output_tokens, cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            task,
            getattr(provider, "name", "") or "",
            getattr(provider, "model", "") or "",
            getattr(provider, "target", "") or "",
            getattr(provider, "adapter_version", "") or "",
            getattr(provider, "prompt_version", "") or "",
            status,
            error,
            json.dumps(suggestions, ensure_ascii=False) if suggestions is not None else None,
            raw,
            started,
            now_iso(),
            tin,
            tout,
            cost,
        ),
    )


def _usage_since(provider, before: tuple[int, int, float] | None):
    """Tokens/cost a provider used since `before` (None if it does not meter usage)."""
    meter = getattr(provider, "usage", None)
    if meter is None or before is None:
        return None
    tin, tout, cost = meter.snapshot()
    return (tin - before[0], tout - before[1], (cost - before[2]) if meter.priced else None)


def _usage_start(provider) -> tuple[int, int, float] | None:
    meter = getattr(provider, "usage", None)
    return meter.snapshot() if meter is not None else None


# --- OCR page cache: pages already paid for survive retries, restarts and rate limits -------


def _ocr_cache_path(archive: Archive, doc_id: str, page: int) -> Path:
    return docs.files(archive, doc_id).dir / "cache" / f"ocr-p{page}.json"


def _ocr_cache_key(meta, extractor, page: int) -> dict:
    model_for = getattr(extractor, "page_model", None)
    return {
        "sha256": meta.sha256,
        "provider": extractor.name,
        "model": model_for(page) if model_for else getattr(extractor, "model", ""),
        "adapter": getattr(extractor, "adapter_version", ""),
    }


def _ocr_cache_get(archive: Archive, meta, extractor, page: int) -> str | None:
    try:
        data = read_json(_ocr_cache_path(archive, meta.id, page))
    except (OSError, ValueError):
        return None
    if data.get("key") == _ocr_cache_key(meta, extractor, page):
        return data.get("text")
    return None


def _ocr_cache_put(archive: Archive, meta, extractor, page: int, text: str) -> None:
    if not docs.cache_writable(archive, meta.id):
        return
    try:
        atomic_write_json(
            _ocr_cache_path(archive, meta.id, page),
            {"key": _ocr_cache_key(meta, extractor, page), "text": text},
        )
    except OSError as e:  # the cache is an optimisation only
        log.warning("doc %s page %s: OCR cache not written: %s", meta.id, page, e)


def is_blank_page(path, mime: str, index: int, s) -> bool:
    """(Almost) no ink on the page - see media.ink_ratio. False if it cannot be measured."""
    try:
        return ink_ratio(path, mime, index, s.max_image_megapixels) <= s.ocr_blank_max_ink
    except Exception as e:  # noqa: BLE001 - measuring is optional
        log.warning("%s page %s: ink check failed: %s", path.name, index + 1, e)
        return False


def _local_extractor(archive: Archive):
    """Tesseract for pages that should not go to a paid AI (None if not installed)."""
    try:
        from .providers.tesseract import TesseractExtractor

        return TesseractExtractor(timeout=archive.settings.ocr_page_timeout_seconds)
    except ProviderUnavailable:
        return None


def clear_ocr_cache(archive: Archive, doc_id: str) -> None:
    for f in (docs.files(archive, doc_id).dir / "cache").glob("ocr-p*.json"):
        with contextlib.suppress(OSError):
            f.unlink()


# --- extraction --------------------------------------------------------------------------


def extract(archive: Archive, doc_id: str, progress: Progress, final_attempt: bool) -> None:
    s = archive.settings
    meta = docs.load_meta(archive, doc_id)
    previous = docs.load_text_pages(archive, doc_id)
    if previous and previous.user_confirmed:
        log.info("doc %s: text confirmed by user, extraction skipped", doc_id)
        return
    started = now_iso()
    path = archive.paths.resolve(meta.original_relpath)
    page_count = meta.page_count or 1
    embedded: list[str] = [""] * page_count
    if meta.mime_type == "application/pdf":
        embedded = pdf_embedded_text(path)
        page_count = len(embedded)

    pages: list[PageText] = []
    need_ocr: list[int] = []
    for i in range(page_count):
        t = embedded[i] if i < len(embedded) else ""
        if len(t.strip()) >= s.min_text_chars_per_page:
            pages.append(PageText(page=i + 1, method="embedded", text=t, chars=len(t.strip())))
        else:
            pages.append(PageText(page=i + 1, method="none", text=t, chars=len(t.strip())))
            need_ocr.append(i)

    extractor = None
    unavailable: str | None = None
    if need_ocr:
        try:
            extractor = registry.get_extractor(s)
            if extractor is None:
                unavailable = N_("OCR is turned off (HEFTIG_OCR_PROVIDER=none)")
            elif not extractor.capabilities.images:
                unavailable = N_("OCR provider %(provider)s does not accept images") % {
                    "provider": extractor.name
                }
                extractor = None
        except ProviderUnavailable as e:
            unavailable = str(e)

    errors: list[str] = []
    fallback = _fallback_extractor(archive, extractor)
    used_fallback = False
    primary_ok = False
    remote = extractor is not None and extractor.name not in LOCAL_PROVIDERS
    usage_before = _usage_start(extractor)
    notes: list[str] = []  # review reasons that are not page errors

    def image_for(i: int, provider) -> tuple[bytes, str]:
        img = render_page(path, meta.mime_type, i, s.ocr_dpi, s.max_image_megapixels)
        if provider is not None and provider.name not in LOCAL_PROVIDERS:
            return ocr_image_bytes(img, s.ocr_max_side)
        return to_png_bytes(img), "image/png"

    def run_primary(i: int) -> str:
        cached = _ocr_cache_get(archive, meta, extractor, i + 1) if remote else None
        if cached is not None:
            return cached
        data, media_type = image_for(i, extractor)
        text = extractor.extract_page(data, i + 1, s.ocr_languages, media_type=media_type)
        if remote:
            _ocr_cache_put(archive, meta, extractor, i + 1, text)
        return text

    # 0) blank pages (the empty backs of duplex scans): marked, so the viewer hides them.
    #    Cost guards for paid OCR: blank pages and pages beyond the per-document budget are
    #    read locally (free); without a local OCR they stay empty / are reported
    local_only: dict[int, str] = {}  # page index -> reason ("blank" | "budget")
    for i in need_ocr:
        if is_blank_page(path, meta.mime_type, i, s):
            pages[i].blank = True
            # (a page the user called "not blank" is read like any other)
            if (remote or extractor is None) and meta.page_blank.get(i + 1) is not False:
                local_only[i] = "blank"
    if remote:
        ai_pages = [i for i in need_ocr if i not in local_only]
        budget = s.ocr_ai_max_pages
        if budget and not meta.ocr_all_pages and len(ai_pages) > budget:
            for i in ai_pages[budget:]:
                local_only[i] = "budget"
            notes.append(
                N_(
                    "Text: Large document – %(pages)s pages read without AI (more than "
                    "%(budget)s pages) – if needed, use “Recognize all pages with AI”"
                )
                % {"pages": len(ai_pages) - budget, "budget": budget}
            )
    ai_pages = [i for i in need_ocr if i not in local_only]
    # (without any OCR configured, blank pages simply stay empty)
    local_ocr = _local_extractor(archive) if local_only and extractor is not None else None

    # 1) all pages with the configured provider - in parallel for remote providers
    results: dict[int, str | BaseException] = {}

    def keep_lease(done: int) -> None:
        # local OCR of many pages takes minutes: report progress, which renews the job lease
        # (otherwise the job would be handed to a second worker thread)
        progress("extract", min(0.95, done / max(1, len(need_ocr)) * 0.95))

    for n_local, (i, why) in enumerate(local_only.items(), 1):
        keep_lease(n_local)
        if local_ocr is None:
            results[i] = (
                ""
                if why == "blank"
                else ProviderError(
                    N_("not recognized: more pages than the AI page budget, no local OCR")
                )
            )
            continue
        try:
            data, media_type = image_for(i, local_ocr)
            results[i] = local_ocr.extract_page(data, i + 1, s.ocr_languages, media_type=media_type)
        except Exception as e:  # noqa: BLE001
            results[i] = e
    if extractor is None:
        for i in ai_pages:
            results[i] = ProviderUnavailable(unavailable or N_("OCR not available"))
    elif ai_pages:
        # pages in parallel for cloud services; a model server on this computer or in the
        # network would only queue them (and time out)
        cloud = remote and s.provider_is_cloud(s.ocr_provider, s.ocr_base_url)
        workers = min(s.ocr_page_concurrency if cloud else 1, len(ai_pages))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr") as pool:
            futures = {pool.submit(run_primary, i): i for i in ai_pages}
            for done, fut in enumerate(as_completed(futures), 1):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except BaseException as e:  # noqa: BLE001 - evaluated per page below
                    results[i] = e
                progress("extract", done / max(1, len(ai_pages)) * 0.95)

    # 2) evaluate per page: rate limits -> retry the job later (finished pages are cached);
    #    AI unreachable or image rejected -> local fallback for that page
    rate_limited = [r for r in results.values() if isinstance(r, ProviderError) and r.rate_limited]
    if rate_limited and not final_attempt:
        raise RetryLater(str(rate_limited[0]), delay=rate_limited[0].retry_after, rate_limited=True)
    for i in need_ocr:
        page = pages[i]
        r = results[i]
        provider = extractor
        if isinstance(r, ProviderError) and fallback is not None:
            keep_lease(len(local_only))  # the fallback reads this page locally
        text = None
        if i in local_only:
            if isinstance(r, str):
                page.text = r
                page.method = "ocr" if local_ocr is not None else "none"
                page.provider = _local_provider_label(
                    local_ocr.name if local_ocr else None, blank=local_only[i] == "blank"
                )
                page.chars = len(r.strip())
            else:
                page.error = str(r)
                errors.append(_page_error(i + 1, r))
            continue
        if isinstance(r, str):
            text = r
            primary_ok = True
        elif isinstance(r, ProviderError):
            if r.transient and not fallback and not final_attempt:
                raise RetryLater(str(r))
            if fallback is not None and (r.transient or remote):
                # transient: redo with AI later; permanent (e.g. image rejected): note it
                log.warning("doc %s page %s: %s – local OCR instead", doc_id, i + 1, r)
                try:
                    data, media_type = image_for(i, fallback)
                    text = fallback.extract_page(
                        data, i + 1, s.ocr_languages, media_type=media_type
                    )
                    provider = fallback
                    if r.transient:
                        used_fallback = True
                    else:
                        notes.append(
                            N_("Text: Page %(page)s read locally (AI refused: %(error)s)")
                            % {"page": i + 1, "error": r}
                        )
                except Exception as e2:  # noqa: BLE001
                    r = e2 if isinstance(e2, ProviderError) else r
            if text is None:
                page.error = str(r)
                page.provider = extractor.name if extractor else ""
                errors.append(_page_error(i + 1, r))
                continue
        else:  # rendering problems etc.: record, continue with the next page
            log.warning("doc %s page %s: extraction failed: %s", doc_id, i + 1, type(r).__name__)
            page.error = N_("Page could not be processed (%(error)s)") % {"error": type(r).__name__}
            errors.append(_page_error(i + 1, page.error))
            continue
        # text-poor page: keep the embedded fragment unless OCR already contains it
        embedded_part = page.text.strip()
        squash = " ".join(fold(text).split())
        if embedded_part and " ".join(fold(embedded_part).split()) not in squash:
            page.text = embedded_part + "\n\n" + text.strip()
        else:
            page.text = text
        page.method = "ocr"
        page.provider = provider.name if provider is extractor else fallback_name(provider.name)
        page.chars = len(page.text.strip())

    total_chars = sum(p.chars for p in pages)
    failed_pages = sum(1 for p in pages if p.error)
    if failed_pages == 0:
        text_status = "ok" if total_chars else "empty"
    elif failed_pages == len(pages) and total_chars == 0:
        text_status = "failed"
    else:
        text_status = "partial"

    preview = None
    try:
        hidden = set(docs.blank_pages(TextPages(page_count=len(pages), pages=pages,
                                                extracted_at=started), meta.page_blank))  # fmt: skip
        cover = next((i for i in range(len(pages)) if i + 1 not in hidden), 0)
        preview = make_preview(path, meta.mime_type, s.max_image_megapixels, page_index=cover)
    except Exception as e:  # preview is optional and regenerable
        log.warning("doc %s: preview failed: %s", doc_id, type(e).__name__)

    conn = archive.conn
    with write_tx(conn):
        meta = docs.load_meta(archive, doc_id)  # reload: user may have edited meanwhile
        tp = TextPages(page_count=len(pages), pages=pages, extracted_at=now_iso())
        docs.write_text(archive, doc_id, tp)
        if preview:
            docs.write_preview(archive, doc_id, preview)
        meta.page_count = len(pages)
        meta.text_status = text_status  # type: ignore[assignment]
        if used_fallback:
            if "extract" not in meta.ai_pending:
                meta.ai_pending.append("extract")
        elif need_ocr and extractor is not None and extractor.name not in LOCAL_PROVIDERS:
            meta.ai_pending = [st for st in meta.ai_pending if st != "extract"]
        meta.review_reasons = [r for r in meta.review_reasons if not r.startswith("Text:")]
        if errors:
            meta.review_reasons.append(
                N_("Text: %(failed)s of %(pages)s pages without recognized text")
                % {"failed": failed_pages, "pages": len(pages)}
            )
        for note in notes:
            _add_reason(meta, note)
        docs.add_history(
            meta,
            HistoryEntry(
                task="extract",
                at=now_iso(),
                status=text_status,
                provider=(extractor.name if extractor else ("embedded" if not need_ocr else "")),
                model=getattr(extractor, "model", "") or "",
                target=getattr(extractor, "target", "") or "",
                adapter_version=getattr(extractor, "adapter_version", "") or "",
                error="; ".join(errors)[:1000] or None,
            ),
        )
        docs.persist(archive, meta)
        _record_run(
            conn, doc_id, "extract", extractor, text_status, started,
            error="; ".join(errors)[:2000] or None, usage=_usage_since(extractor, usage_before),
        )  # fmt: skip
    if used_fallback:
        _mark_ai(archive, reachable=False, task="ocr")
    elif primary_ok:
        _mark_ai(archive, reachable=True, task="ocr")
    progress("extract", 1.0)


# --- classification ----------------------------------------------------------------------


def classify(archive: Archive, doc_id: str, progress: Progress, final_attempt: bool) -> None:
    s = archive.settings
    started = now_iso()
    conn = archive.conn
    try:
        classifier = registry.get_classifier(s)
    except ProviderUnavailable as e:
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            _add_reason(meta, N_("Classification: %(error)s") % {"error": e})
            docs.add_history(
                meta,
                HistoryEntry(task="classify", at=now_iso(), status="unavailable", error=str(e)),
            )
            docs.persist(archive, meta)
            _record_run(conn, doc_id, "classify", None, "unavailable", started, error=str(e))
        return
    if classifier is None:
        return
    progress("classify", 0.1)
    meta = docs.load_meta(archive, doc_id)
    text = docs.get_text(archive, doc_id)
    if not text.strip():
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            docs.add_history(
                meta,
                HistoryEntry(
                    task="classify",
                    at=now_iso(),
                    status="skipped",
                    provider=classifier.name,
                    error=N_("No text available"),
                ),  # fmt: skip
            )
            meta.ai_pending = [st for st in meta.ai_pending if st != "classify"]
            docs.persist(archive, meta)
        return
    request = cls.build_request(archive, meta, text)
    fell_back = False
    ai_classifier = classifier
    usage_before = _usage_start(classifier)
    try:
        response = classifier.classify(request)
    except ProviderError as e:
        if e.rate_limited and not final_attempt:
            # busy, not unreachable: wait and ask the AI again instead of falling back
            raise RetryLater(str(e), delay=e.retry_after, rate_limited=True) from e
        if e.transient and s.ai_fallback and classifier.name not in LOCAL_PROVIDERS:
            # remote AI unreachable: classify with the local rules now, redo with AI later
            from .providers.rules import RulesClassifier

            log.warning("doc %s: %s – falling back to local rules", doc_id, e)
            _mark_ai(archive, reachable=False, task="classify")
            classifier = RulesClassifier()
            classifier.name = fallback_name("rules")
            response = classifier.classify(request)
            fell_back = True
        else:
            response = None
            error = e
    else:
        if classifier.name not in LOCAL_PROVIDERS:
            _mark_ai(archive, reachable=True, task="classify")
    if response is None:
        e = error
        if e.transient and not final_attempt:
            raise RetryLater(str(e)) from e
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            _add_reason(meta, N_("Classification failed: %(error)s") % {"error": e})
            # a permanent failure is not "pending": the catch-up must not pay for it again
            meta.ai_pending = [st for st in meta.ai_pending if st != "classify"]
            docs.add_history(
                meta,
                HistoryEntry(
                    task="classify",
                    at=now_iso(),
                    status="failed",
                    provider=classifier.name,
                    model=classifier.model,
                    target=classifier.target,
                    adapter_version=classifier.adapter_version,
                    prompt_version=classifier.prompt_version,
                    error=str(e)[:500],
                ),  # fmt: skip
            )
            docs.persist(archive, meta)
            _record_run(
                conn, doc_id, "classify", classifier, "failed", started, error=str(e),
                usage=_usage_since(ai_classifier, usage_before),
            )  # fmt: skip
        return
    progress("classify", 0.8)
    with write_tx(conn):
        meta = docs.load_meta(archive, doc_id)
        # (German prefixes: reasons stored before the interface became English)
        meta.review_reasons = [
            r for r in meta.review_reasons
            if not r.startswith(("Classification", "Klassifikation", "Mögliche Dublette"))
            and r not in (cls.DATE_UNCERTAIN, "Dokumentdatum unsicher")
        ]  # fmt: skip
        result = cls.apply(archive, meta, response.data, text, classifier.name)
        if fell_back:
            if "classify" not in meta.ai_pending:
                meta.ai_pending.append("classify")
        else:
            meta.ai_pending = [st for st in meta.ai_pending if st != "classify"]
        for r in result.review_reasons:
            _add_reason(meta, r)
        docs.add_history(
            meta,
            HistoryEntry(
                task="classify",
                at=now_iso(),
                status="ok",
                provider=classifier.name,
                model=classifier.model,
                target=classifier.target,
                adapter_version=classifier.adapter_version,
                prompt_version=classifier.prompt_version,
                fields=result.applied,
                error=(N_("dropped: %(fields)s") % {"fields": ", ".join(result.dropped)})[:500]
                if result.dropped
                else None,
                by="ai",
            ),
        )
        docs.persist(archive, meta)
        _record_run(
            conn, doc_id, "classify", classifier, "ok", started,
            suggestions={"applied": result.applied, "suggested": result.suggested,
                         "dropped": result.dropped, "data": response.data},
            raw=response.raw, max_kb=s.raw_response_max_kb,
            usage=_usage_since(ai_classifier, usage_before),
        )  # fmt: skip
    progress("classify", 1.0)


# review reasons of a failed job (German: stored before the interface became English)
_PROCESSING_FAILED = ("Processing failed", "Verarbeitung fehlgeschlagen")


def fallback_name(provider: str) -> str:
    """The stored provider name of a page/classification done by the local fallback."""
    return N_("%(provider)s (fallback)") % {"provider": provider}


def _local_provider_label(provider: str | None, blank: bool) -> str:
    """The stored provider of a page read locally to save paid OCR (blank / over budget)."""
    if provider is None:
        return N_("blank page") if blank else N_("page budget")
    if blank:
        return N_("%(provider)s (blank page)") % {"provider": provider}
    return N_("%(provider)s (page budget)") % {"provider": provider}


def _page_error(page: int, error: object) -> str:
    return N_("Page %(page)s: %(error)s") % {"page": page, "error": error}


def _add_reason(meta: DocumentMetadata, reason: str) -> None:
    if reason not in meta.review_reasons:
        meta.review_reasons.append(reason)


# --- job runner --------------------------------------------------------------------------

STAGES = {"extract": extract, "classify": classify}


def run_process_job(archive: Archive, job) -> str:
    """Run a 'process' job. Returns the final job status."""
    conn = archive.conn
    s = archive.settings
    job_id = job["id"]
    doc_id = job["doc_id"]
    payload = json.loads(job["payload"] or "{}")
    stages = [st for st in payload.get("stages", ["extract", "classify"]) if st in STAGES]
    done = set(payload.get("done", []))
    final_attempt = job["attempts"] >= job["max_attempts"]

    try:
        docs.load_meta(archive, doc_id)
    except docs.DocumentNotFound:
        jobs.finish(conn, job_id, "failed", error=N_("Document no longer exists"))
        return "failed"

    _set_doc_status(archive, doc_id, "processing")
    todo = [st for st in stages if st not in done]
    for idx, stage in enumerate(todo):

        def progress(st: str, frac: float, _idx: int = idx) -> None:
            jobs.set_stage(conn, job_id, st, (_idx + frac) / len(todo), s.job_lease_seconds)

        progress(stage, 0.0)
        try:
            STAGES[stage](archive, doc_id, progress, final_attempt)
        except RetryLater as e:
            if e.rate_limited:
                status = jobs.postpone(conn, job_id, str(e), e.delay or s.rate_limit_pause_seconds)
            else:
                status = jobs.fail(conn, job_id, str(e), s.job_backoff_seconds)
            _set_doc_status(archive, doc_id, "queued" if status == "queued" else None)
            return status
        done.add(stage)
        payload["done"] = sorted(done)
        jobs.update_payload(conn, job_id, payload)

    with write_tx(conn):
        meta = docs.load_meta(archive, doc_id)
        if any(r.startswith(_PROCESSING_FAILED) for r in meta.review_reasons):
            meta.review_reasons = [
                r for r in meta.review_reasons if not r.startswith(_PROCESSING_FAILED)
            ]
            docs.persist(archive, meta)
    final = _final_doc_status(archive, doc_id)
    meta = docs.load_meta(archive, doc_id)
    jobs.finish(
        conn,
        job_id,
        "needs_review" if final == "needs_review" else ("failed" if final == "failed" else "done"),
        result={"text_status": meta.text_status, "review_reasons": meta.review_reasons},
        error="; ".join(meta.review_reasons)[:500] or None,
    )
    try:  # possible content duplicates are listed for review; truly identical ones resolved
        from .duplicates import check_document, resolve_identical

        if check_document(archive, doc_id):
            resolve_identical(archive, doc_id)
    except Exception:
        log.exception("duplicate check for %s failed", doc_id)
    return final


def _set_doc_status(archive: Archive, doc_id: str, status: str | None) -> None:
    with write_tx(archive.conn):
        meta = docs.load_meta(archive, doc_id)
        if status is None:
            status = _compute_status(meta)
        if meta.status != status:
            meta.status = status  # type: ignore[assignment]
            docs.persist(archive, meta, bump=True)


def _compute_status(meta: DocumentMetadata) -> str:
    if meta.text_status == "failed":
        return "failed"
    if meta.review_reasons or meta.suggestions:
        return "needs_review"
    return "done"


def _final_doc_status(archive: Archive, doc_id: str) -> str:
    with write_tx(archive.conn):
        meta = docs.load_meta(archive, doc_id)
        status = _compute_status(meta)
        meta.status = status  # type: ignore[assignment]
        docs.persist(archive, meta)
        return status


def mark_job_outcome(archive: Archive, doc_id: str, job_status: str, error: str) -> None:
    """After an unexpected job error: queued again -> 'queued'; given up -> visible problem."""
    with write_tx(archive.conn):
        meta = docs.load_meta(archive, doc_id)
        if job_status == "queued":
            meta.status = "queued"
        else:
            _add_reason(meta, N_("Processing failed: %(error)s") % {"error": error[:300]})
            meta.status = "failed" if meta.text_status in ("pending", "failed") else "needs_review"
        docs.persist(archive, meta)


def reprocess(archive: Archive, doc_ids: list[str], stages: list[str]) -> list[int]:
    """Queue re-extraction and/or re-classification. Locked fields stay untouched."""
    stages = [st for st in ("extract", "classify") if st in stages]
    if not stages:
        raise ValueError(_("No processing stage selected"))
    for doc_id in doc_ids:  # validate everything before queueing anything
        docs.load_meta(archive, doc_id)
    out = []
    for doc_id in doc_ids:
        if "extract" in stages:
            clear_ocr_cache(archive, doc_id)  # a requested re-extraction really reads again
        out.append(
            jobs.enqueue(
                archive.conn,
                "process",
                doc_id,
                {"stages": stages},
                max_attempts=archive.settings.job_max_attempts,
            )  # fmt: skip
        )
        _set_doc_status(archive, doc_id, "queued")
    return out


def ocr_all_pages(archive: Archive, doc_id: str) -> list[int]:
    """Read every page with the AI (beyond the per-document page budget) and reclassify."""
    with write_tx(archive.conn):
        meta = docs.load_meta(archive, doc_id)
        meta.ocr_all_pages = True
        docs.persist(archive, meta)
    return reprocess(archive, [doc_id], ["extract", "classify"])


def prune_raw_responses(archive: Archive) -> int:
    cutoff = iso(utcnow() - timedelta(days=archive.settings.raw_response_retention_days))
    with write_tx(archive.conn):
        cur = archive.conn.execute(
            "UPDATE processing_runs SET raw_response=NULL "
            "WHERE raw_response IS NOT NULL AND started_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def catch_up_ai(archive: Archive) -> dict:
    """Redo AI stages of documents processed with the local fallback, once the AI answers."""
    conn = archive.conn
    batch = archive.settings.ai_catch_up_batch
    # documents already queued or in progress are not queued again (no double payment)
    rows = conn.execute(
        'SELECT id FROM documents d WHERE metadata_json LIKE \'%"ai_pending": ["%\' '
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.doc_id = d.id "
        "AND j.status IN ('queued', 'processing')) ORDER BY ingest_sequence LIMIT ?",
        (batch,),
    ).fetchall()
    if not rows:
        return {"pending": 0, "queued": 0}
    backlog = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='process' AND status='queued'"
    ).fetchone()[0]
    if backlog >= batch:
        # a big import is running: catch up afterwards instead of adding to the AI load
        return {"pending": len(rows), "queued": 0, "deferred": backlog}
    if not registry.probe_ai(archive.settings):
        _mark_ai(archive, reachable=False)
        return {"pending": len(rows), "queued": 0}
    _mark_ai(archive, reachable=True)
    queued = 0
    for (doc_id,) in rows:
        meta = docs.load_meta(archive, doc_id)
        stages = ["extract", "classify"] if "extract" in meta.ai_pending else ["classify"]
        jobs.enqueue(conn, "process", doc_id, {"stages": stages},
                     max_attempts=archive.settings.job_max_attempts)  # fmt: skip
        queued += 1
    return {"pending": len(rows), "queued": queued}


def revalidate_dates(archive: Archive) -> dict[str, int]:
    """Re-check document dates that were only suggested ("not backed by the text") with the
    current evidence rules, from the stored classifier answer - no new AI call. Documents
    without a date get the date they are made up to, if the text names one.

    Applies the date where the quote now backs it (as the classification would have), removes
    the suggestion and settles the document status.
    """
    conn = archive.conn
    min_conf = archive.settings.classify_min_confidence
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM documents WHERE metadata_json LIKE ? "
            "OR metadata_json LIKE '%Datum nicht wörtlich im Text%'",
            (f"%{cls.DATE_NOT_BACKED}%",),
        )
    ]
    report = {"checked": len(ids), "applied": 0}
    for doc_id in ids:
        run = conn.execute(
            "SELECT suggestions FROM processing_runs WHERE doc_id=? AND task='classify' "
            "AND status='ok' ORDER BY id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        data = (json.loads(run[0]) or {}).get("data") if run and run[0] else None
        if not data:
            continue
        text = docs.get_text(archive, doc_id)
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            sug = [x for x in meta.suggestions if x.field == "document_date"]
            if not sug or meta.locked("document_date"):
                continue
            iso, evidence = sug[0].value, data.get("document_date_evidence")
            squashed = cls._squash(text)
            if not (
                isinstance(evidence, str)
                and isinstance(iso, str)
                and data.get("document_date") == iso
                and cls._in_text(evidence, squashed)
                and cls._evidence_matches_date(evidence, iso)
            ):
                continue
            conf = cls._conf(data, "document_date_confidence")
            meta.suggestions = [x for x in meta.suggestions if x.field != "document_date"]
            meta.document_date = iso
            meta.field_sources["document_date"] = "ai"
            meta.document_date_reason = cls.found_in_text(evidence)
            if conf >= min_conf:
                meta.document_date_status = "ai"
            else:
                meta.document_date_status = "ai_uncertain"
                _add_reason(meta, cls.DATE_UNCERTAIN)
            if meta.status in ("needs_review", "done"):
                meta.status = _compute_status(meta)  # type: ignore[assignment]
            docs.add_history(
                meta, HistoryEntry(task="classify", at=now_iso(), status="date-revalidated",
                                   fields=["document_date"], by="system")
            )  # fmt: skip
            docs.persist(archive, meta)
            report["applied"] += 1
    # no date found: the date the document is made up to, if the text names one
    report["as_of"] = 0
    for (doc_id,) in conn.execute(
        "SELECT id FROM documents WHERE document_date IS NULL"
    ).fetchall():
        text = docs.get_text(archive, doc_id)
        with write_tx(conn):
            meta = docs.load_meta(archive, doc_id)
            if (meta.document_date or meta.locked("document_date")
                    or meta.document_date_status != "none_found"
                    or not cls.apply_as_of_date(meta, text)):  # fmt: skip
                continue
            docs.add_history(
                meta, HistoryEntry(task="classify", at=now_iso(), status="date-as-of",
                                   fields=["document_date"], by="system")
            )  # fmt: skip
            docs.persist(archive, meta)
            report["as_of"] += 1
    return report
