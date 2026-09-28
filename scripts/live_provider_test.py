"""Live test against the configured AI provider (reads .env). Costs real money - small corpus.

    uv run python scripts/live_provider_test.py [--max-usd 8]

Uses a throw-away archive under .smoke/, synthetic documents only (no personal data).
Prints token usage and an estimated cost (list prices, see PRICES below).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import ingest_bytes  # noqa: E402
from tests.corpus import DOCS  # noqa: E402
from tests.helpers import image_bytes, scan_pdf, text_image, text_pdf  # noqa: E402

from heftig import documents as docs  # noqa: E402
from heftig import jobs  # noqa: E402
from heftig.archive import Archive  # noqa: E402
from heftig.config import Settings  # noqa: E402
from heftig.processing import reprocess  # noqa: E402
from heftig.providers import anthropic_provider  # noqa: E402
from heftig.search import SearchParams, search  # noqa: E402
from heftig.worker import run_job  # noqa: E402

# USD per million tokens (input, output)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost(model: str) -> float:
    pin, pout = PRICES.get(model, (5.0, 25.0))
    u = anthropic_provider.USAGE
    return u["input_tokens"] / 1e6 * pin + u["output_tokens"] / 1e6 * pout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-usd", type=float, default=8.0)
    ap.add_argument("--model", help="override HEFTIG_OCR_MODEL and HEFTIG_CLASSIFY_MODEL")
    args = ap.parse_args()
    base = ROOT / ".smoke" / f"live-{time.strftime('%Y%m%d-%H%M%S')}"
    over = {"ocr_model": args.model, "classify_model": args.model} if args.model else {}
    s = Settings(archive_dir=base / "archive", job_backoff_seconds=5, job_max_attempts=3, **over)
    model = s.classify_model or anthropic_provider.DEFAULT_MODEL
    print(f"OCR: {s.ocr_provider}/{s.ocr_model}  Classification: {s.classify_provider}/{model}")
    a = Archive(s)
    # an existing correspondent with alias: the AI should reuse it
    from heftig import taxonomy as tax
    from heftig.db import write_tx

    with write_tx(a.conn):
        tid = tax.get_or_create(a.conn, "correspondent", "Telekom Deutschland GmbH")
        tax.add_alias(a.conn, tid, "Telekom")
        tax.get_or_create(a.conn, "tag", "Versicherung")
        tax.get_or_create(a.conn, "document_type", "Rechnung")

    picks = {"telekom_2026_09.pdf", "vodafone_vertrag.pdf", "allianz_hausrat_2025.pdf", "tickets.pdf",
             "finanzamt_bescheid.pdf"}  # fmt: skip
    ids = {}
    for name, text, _ in DOCS:
        if name in picks:
            ids[name] = ingest_bytes(a, text_pdf([text]), name).doc_id
    ids["scan_krankenkasse.pdf"] = ingest_bytes(a, scan_pdf([
        "Gesundheitskasse Beispiel\nMitgliedsbescheinigung\nfür die Krankenversicherung\n"
        "Datum: 05.06.2024\nVersichertennummer: A123456789",
        "Seite 2\nHinweise zum Datenschutz"]), "scan_krankenkasse.pdf", source="scanner").doc_id  # fmt: skip
    ids["IMG_4711.jpg"] = ingest_bytes(a, image_bytes(text_image(
        "Autowerkstatt Beispiel\nRechnung Nr. 2026-0815\nInspektion\nGesamt: 389,00 EUR\n"
        "Datum: 22.09.2026"), "JPEG"), "IMG_4711.jpg", paper=True).doc_id  # fmt: skip

    def drain() -> bool:
        while True:
            if cost(model) > args.max_usd:
                print(f"Budget limit of {args.max_usd} $ reached – stopping")
                return False
            job = jobs.claim(a.conn, 900)
            if job is None:
                queued = a.conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='queued'"
                ).fetchone()[0]
                if not queued:
                    return True
                time.sleep(2)
                continue
            run_job(a, job)

    t0 = time.time()
    ok = drain()
    print(
        f"\nProcessing: {time.time() - t0:.0f} s, {anthropic_provider.USAGE}, ~{cost(model):.3f} $\n"
    )
    for name, doc_id in ids.items():
        m = docs.load_meta(a, doc_id)
        tp = docs.load_text_pages(a, doc_id)
        methods = [p.method for p in tp.pages] if tp else []
        cfs = {
            k: (v.value, v.currency) if v.currency else v.value for k, v in m.custom_fields.items()
        }
        print(f"- {name}: [{m.status}] “{m.title}” | date {m.document_date} ({m.document_date_status}) | "
              f"{m.correspondent} | {m.document_type} | tags {m.tags} | fields {cfs} | text {methods}")  # fmt: skip
        if m.suggestions:
            print(f"    Suggestions: {[(x.field, x.value, x.reason) for x in m.suggestions]}")
        if m.review_reasons:
            print(f"    Review: {m.review_reasons}")
        if m.summary:
            print(f"    Summary: {m.summary}")
    print()
    for q in ("Telekomm Rechnung", "83729381", "Allianz Versicherung 2025", "Krankenversicherung",
              "Inspektion Werkstatt"):  # fmt: skip
        res = search(a.conn, SearchParams(q=q))
        names = [next(n for n, i in ids.items() if i == it["id"]) for it in res.items[:3]]
        print(f"Search “{q}” -> {names}")
    terms = tax.list_terms(a.conn, "correspondent")
    print(f"\nSenders: {[(t.name, t.doc_count) for t in terms]}")
    if ok:
        # locked field survives an AI re-run
        tid_doc = ids["telekom_2026_09.pdf"]
        docs.update_fields(a, tid_doc, {"title": "My own title"})
        reprocess(a, [tid_doc], ["classify"])
        drain()
        print(f"After reclassification: title = “{docs.load_meta(a, tid_doc).title}” (locked)")
    print(f"\nTotal: {anthropic_provider.USAGE}, estimated ~{cost(model):.3f} $ ({model})")
    a.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
