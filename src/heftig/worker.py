"""The background worker: folder polling, IMAP polling and the job queue.

Run exactly one worker per archive. On start every job still marked ``processing`` is put back
into the queue (it belonged to a previous, interrupted worker) and continues where it stopped.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from . import jobs, maintenance, sessions, trash
from .archive import Archive
from .consume import ConsumeWatcher
from .db import get_meta, now_iso, set_meta, write_tx
from .documents import DocumentNotFound
from .i18n import N_
from .processing import mark_job_outcome, prune_raw_responses, run_process_job

log = logging.getLogger("heftig.worker")


def run_job(archive: Archive, job) -> str:
    conn = archive.conn
    kind = job["kind"]
    try:
        if kind == "process":
            return run_process_job(archive, job)
        from . import maintenance

        handler = maintenance.JOB_HANDLERS.get(kind)
        if handler is None:
            jobs.finish(
                conn, job["id"], "failed", error=N_("Unknown job type %(kind)s") % {"kind": kind}
            )
            return "failed"
        return handler(archive, job)
    except DocumentNotFound:
        jobs.finish(conn, job["id"], "failed", error=N_("The document was deleted"))
        return "failed"
    except Exception as e:
        log.exception("job %s (%s) crashed", job["id"], kind)
        error = f"{type(e).__name__}: {e}"
        status = jobs.fail(conn, job["id"], error, archive.settings.job_backoff_seconds)
        if job["doc_id"]:
            # keep the document status truthful (never stuck in "processing")
            try:
                mark_job_outcome(archive, job["doc_id"], status, error)
            except Exception:
                log.exception("could not update status of %s", job["doc_id"])
        return status


def run_until_idle(archive: Archive, max_jobs: int = 10_000) -> int:
    """Process queued jobs synchronously until none are due (tests, CLI)."""
    n = 0
    while n < max_jobs:
        job = jobs.claim(archive.conn, archive.settings.job_lease_seconds)
        if job is None:
            break
        run_job(archive, job)
        n += 1
    return n


class Worker:
    def __init__(self, archive: Archive):
        self.archive = archive
        self.stop = threading.Event()
        self.consume = ConsumeWatcher(archive)
        # optional local folder for digital files
        folder = archive.settings.folder_path
        self.folder = ConsumeWatcher(archive, "folder", folder, paper=False) if folder else None
        self._imap = None
        self._last_consume = 0.0
        self._last_imap = 0.0
        self._last_maint = 0.0
        self._last_ai = time.monotonic()  # first catch-up check after ai_retry_minutes

    def _imap_poller(self):
        if self._imap is None and self.archive.settings.imap_host:
            from .imap_import import ImapPoller

            self._imap = ImapPoller(self.archive)
        return self._imap

    def heartbeat(self) -> None:
        with write_tx(self.archive.conn):
            set_meta(self.archive.conn, "worker_heartbeat", now_iso())
            set_meta(self.archive.conn, "consume_status", self.consume.last_error or "ok")
            if self.folder:
                set_meta(self.archive.conn, "folder_status", self.folder.last_error or "ok")

    def run_forever(self) -> None:
        s = self.archive.settings
        n = jobs.requeue_all_processing(self.archive.conn)
        if n:
            log.info("resumed %s interrupted job(s)", n)
        pool = ThreadPoolExecutor(max_workers=s.worker_concurrency, thread_name_prefix="job")
        running: dict[int, Future] = {}
        log.info(
            "worker started (concurrency %s, scanner folder %s, local folder %s)",
            s.worker_concurrency,
            s.consume_path,
            s.folder_path or "-",
        )
        try:
            while not self.stop.is_set():
                self.tick(pool, running)
                self.stop.wait(1.0)
        finally:
            pool.shutdown(wait=True, cancel_futures=False)
            self.archive.close()
            log.info("worker stopped")

    def tick(self, pool: ThreadPoolExecutor, running: dict[int, Future]) -> None:
        try:
            if self.archive.refresh_settings():  # changed in the web interface
                self._imap, self._last_imap = None, 0.0  # new mail settings: poll right away
        except Exception:
            log.exception("reading the settings failed")
        s = self.archive.settings
        now = time.monotonic()
        try:
            self.heartbeat()
            if now - self._last_consume >= s.consume_poll_seconds:
                self._last_consume = now
                for watcher in (self.consume, self.folder):
                    if watcher is None:
                        continue
                    for r in watcher.poll():
                        log.info("%s: %s -> %s", watcher.source, r.get("path"), r.get("status"))
            poller = self._imap_poller()
            if poller and now - self._last_imap >= s.imap_poll_seconds:
                self._last_imap = now
                poller.poll()
            if now - self._last_ai >= s.ai_retry_minutes * 60:
                self._last_ai = now
                from .processing import catch_up_ai

                res = catch_up_ai(self.archive)
                if res["queued"]:
                    log.info(
                        "AI reachable again: %s document(s) queued for AI processing", res["queued"]
                    )
            if now - self._last_maint >= 3600:
                self._last_maint = now
                jobs.requeue_expired(self.archive.conn)
                prune_raw_responses(self.archive)
                sessions.end_idle(self.archive)  # a forgotten scan session must not grab mail
                trash.purge_expired(self.archive)
                maintenance.snapshot_if_due(self.archive)
                if not get_meta(self.archive.conn, maintenance.BLANK_CHECK_KEY):
                    r = maintenance.detect_blank_pages(self.archive)  # once, for older archives
                    log.info("blank pages: %s found in %s document(s)", r["pages"], r["documents"])
        except Exception:
            log.exception("worker tick failed")
        for jid in [j for j, f in running.items() if f.done()]:
            running.pop(jid)
        try:
            while len(running) < s.worker_concurrency:
                job = jobs.claim(self.archive.conn, s.job_lease_seconds)
                if job is None:
                    break
                running[job["id"]] = pool.submit(run_job, self.archive, job)
        except Exception:  # e.g. "database is locked" during a long rebuild: try next tick
            log.exception("claiming jobs failed")
