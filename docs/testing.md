# Testing

## Running the tests

```sh
uv sync            # installs dev dependencies (pytest, ruff, reportlab)
make test          # = uv run pytest -q
make lint          # ruff check + ruff format --check on src, tests, contrib
```

The suite needs no network access, no accounts and no API keys. AI providers, OCR (except one
test) and the IMAP server are replaced by fakes. Test documents are generated synthetically
(`tests/helpers.py`, `tests/corpus.py`); all names, numbers and addresses in them are invented.

For the one test that runs real OCR (`test_tesseract_offline_ocr_reads_german`, marker
`tesseract`) install `tesseract` with German language data; without it the test is skipped. The
CI workflow installs `tesseract-ocr`, `tesseract-ocr-deu` and DejaVu fonts (used to render the
synthetic scans).

Run a single file or test with `uv run pytest tests/test_search.py -q` or
`uv run pytest -k typo`.

## What is covered

| File | Verifies |
|---|---|
| `test_ingest.py` | Every source (`scanner`, `folder`, `web`, `api`, `email`) stores a byte-identical original; PDF, JPEG, PNG, multi-page TIFF accepted; type detected by content, not extension; identical SHA-256 keeps one original and logs a duplicate event; different files with the same text are not merged; size and page limits; monotonic sequences and `received_at` kept on reprocessing; auto-filing for the scanner; manual filing independent of the document date; originals are read-only. |
| `test_consume.py` | Files are taken only when stable, then removed; growing files wait; temporary names ignored; subfolders scanned; unsupported files quarantined with a reason file; files present before start are imported (laptop was off); the source stays when the archive commit fails; repeated failures end in quarantine; `move` mode; missing consume folder reported. |
| `test_imap.py` | With a `FakeImap` server: attachments become documents with a shared import reference; polling is idempotent and resumes by UID; a changed UIDVALIDITY rescans without duplicates; a failing message is not marked and the error is visible; connection errors recorded; move-after-import; direct MIME document and mail without attachment; optional `.eml` archiving. |
| `test_processing.py` | Embedded PDF text needs no OCR; partial OCR failure is visible and keeps the other pages; without any OCR the document stays usable; transient errors retried with backoff delay; final attempt records the error instead of looping; resume after a crash skips finished stages; worker restart requeues interrupted jobs; provider failure keeps metadata and original; locked fields and manual tag decisions survive reprocessing; cloud providers need the explicit permission. |
| `test_classify.py` | Valid output applied with provenance; invented (unbacked) dates not applied; uncertain dates flagged; invalid values dropped; existing terms and aliases reused; near-duplicate new terms become suggestions; sensible new terms created; low-confidence names only suggested; prompt treats the document as data; rule-based classifier works offline. |
| `test_search.py` | The search acceptance corpus (below), plus snippet escaping, field syntax, invalid syntax reported instead of raised (including SQL/FTS metacharacters), unknown field prefixes as plain text, exact ID/SHA-256, default sort and stable pagination, custom-field amount filter, alias search, OR fallback, identical results after `reindex` and `rebuild-db`. |
| `test_maintenance.py` | `check` on a clean archive and after corruption; repair after a crash between sidecar and DB write; repair adopts orphan sidecars and requeues them; requeues unfinished processing; reports and adopts orphan originals; `rebuild-db` after losing the database; backup and restore on a fresh directory; DB snapshot consistency; atomic writes keep the old file on failure; paths cannot escape the archive; restrictive permissions; raw-response retention. |
| `test_export_import.py` | Export -> import round trip (directory and ZIP) preserves document count, hashes, text, metadata, taxonomy, aliases, ingest and filing order and locks; the export is open and contains no secrets; conflicts reported, not overwritten; import into a non-empty archive renumbers; tampered exports refused; zip-slip and zip bombs refused. |
| `test_providers.py` | OpenAI-compatible OCR sends inline `data:` images only; the classifier uses JSON schema mode and keeps the API key out of error messages; HTTP 429 is transient; capability check for text-only endpoints; JSON parsing tolerates code fences; cloud detection and gating; the default configuration sends nothing to the cloud; real Tesseract reads German text (needs `tesseract`). |
| `test_web.py` | `/health` and `/ready` (incl. unwritable archive); everything requires login; cookie flags and login rate limit; CSRF required for session writes; upload, detail, original download and duplicate; unsupported upload rejected with a friendly message; upload size limits; API token create/use/revoke; PATCH locks and delete confirmation; filing via API; HTML escaping; all UI pages render; form edit with CSRF; taxonomy merge via API; OpenAPI schema. |
| `test_duplicates.py` | PDF and phone photo of the same invoice are listed as possible duplicates (same number and amount); monthly invoices of one sender are not; identical text without metadata is found; "keep both" is stored in both sidecars and survives rescan, database rebuild and export/import; review page, inbox list, document notice and delete-with-confirmation. |
| `test_regressions.py` | Defects found in the code review of 2026-09-28: form posts from browsers that send `Origin: null`; stale edit forms (revision check), tags containing commas, CRLF in text areas; renaming a term to an empty name; import refusing foreign `original_relpath` values; placeholder names and crashing jobs never leave a document in `processing`; no parallel jobs for one document; requested stages re-run when merged into a waiting job; retry re-runs all stages; worker survives a locked database; manual tag decisions follow rename/delete; import resumes unfinished processing and stays idempotent after renumbering; export skips documents with a missing original; unreadable and undeletable files in the scanner folder; move mode never overwrites; strict ISO dates; clean 4xx errors on odd input; deleted documents are not resurrected; numbers typed with a space; rate limit uses the proxy-added address. |
| `test_smoke.py` | Ingest -> process -> search in one go. |
| `test_paperless_script.py` | The Paperless migration script against a real Heftig export: field mapping, dry run without network, upload against an `httpx.MockTransport` (term lookup/creation, checksum skip, multipart payload, idempotent second run), corrupt originals reported, missing credentials. |

### Search acceptance corpus

`tests/corpus.py` builds ten synthetic German documents (Telekom invoices, a Vodafone contract
and a letter, Allianz insurance documents, football tickets mentioning the "Allianz Arena", a tax
assessment from "Finanzamt Beispielstadt-Süd", a municipal utility payment plan and a two-page
image-only health insurance scan) and processes them with a scripted classifier and fake OCR.
The acceptance cases:

| Case | Expectation |
|---|---|
| `Telekomm Rechnung` | Corrected to `telekom`; both Telekom invoices on top; fewer than 200 correction candidates checked. |
| `83729381` | The Vodafone contract with that contract number first; the letter printing it as `8372 9381` also found, ranked lower; a longer number with the same prefix not matched. |
| `Allianz Versicherung 2025` | The 2025 Allianz insurance document first, ahead of the 2023 one and the "Allianz Arena" tickets. |
| `"Allianz Arena"` | Phrase finds only the tickets; `"Arena Allianz"` finds nothing. |
| Tag + date filters | Tag `Telefon` from 2026-09-01; tag `Versicherung` in 2024-2025; two tags combined; document date and received date filtered separately. |
| Filter + free text | `rechnung` + correspondent + date range. |
| Umlauts | `Müllerstraße`, `muellerstrasse`, `MUELLERSTRASSE`, `Beispielstadt-Sued`, `Süd Einkommensteuer` all find the tax assessment. |
| OCR fragments | `Krankenversicherung` finds the scan where the word is hyphenated across a line break; page 2 of the scan is searchable. |
| Rebuild | The results of these queries are identical after `reindex` and after `rebuild-db`. |

## Manual smoke test

`make smoke` (`scripts/smoke.py`) automates most of the following against real processes with
Tesseract. To do it by hand:

1. Start a fresh instance: `docker compose up -d` and `docker compose run --rm web heftig init`
   (or natively: `HEFTIG_ARCHIVE_DIR=/tmp/heftig-test/archive heftig init` then `heftig run`).
2. Open <http://127.0.0.1:8765>, log in. Check that a wrong password is rejected and that
   repeated wrong attempts are rate-limited.
3. **Digital PDF:** upload a born-digital PDF on *Add*. It should be `done` quickly, with
   text from the embedded layer (page method "eingebetteter PDF-Text").
4. **Phone photo:** on a phone in the LAN (or with the browser's device emulation), upload a
   photo of a printed letter as *Scan/Foto eines Papierdokuments*. It should be OCRed and appear
   under "not yet filed" in the inbox.
5. **Scanner:** copy a scanned multi-page PDF into the consume folder. Within about 30 seconds it
   disappears from the folder and appears as a document with source `scanner`. Drop a `.txt`
   file there too: it must end up in the quarantine with a reason.
6. **E-mail:** `heftig ingest-eml message.eml` with a message that has a PDF attachment and a
   small logo; the PDF becomes a document, the logo is logged as skipped. Run it again: nothing
   new is created.
7. **Search:** search for a word from each document, a number from one of them, a word with a
   typo, and `source:scanner`. All three inputs must appear in the same search.
8. **Duplicate:** upload the digital PDF again: the result says "duplicate", no new document.
9. **Edit and reprocess:** change the title and the date of a document (lock them), then
   *Reprocess* (OCR and classification). The edited fields must stay unchanged.
10. **Filing:** mark two paper documents as filed; the second one must be shown at position 1
    (top) of the month's section, the first at position 2.
11. **Export -> import:** `heftig export /tmp/heftig-test/exp`, then import into a fresh archive
    (`HEFTIG_ARCHIVE_DIR=/tmp/heftig-test/archive2 heftig import /tmp/heftig-test/exp/heftig-export-*`).
    Compare `heftig status` (document count), a few searches, the filing positions and the locks.
    Importing a second time must report every document as unchanged.
12. **Backup -> restore:** `heftig backup /tmp/heftig-test/bak`, `heftig restore <backup> /tmp/heftig-test/restored`,
    then `HEFTIG_ARCHIVE_DIR=/tmp/heftig-test/restored heftig check` must report no issues and the
    same document count.
13. **Crash:** while documents are processing, kill the worker (`docker compose kill worker` or
    `kill -9`), start it again: the jobs continue and finish.

## Known gaps

- **IMAP** is tested only against `FakeImap` (an in-memory implementation of the small client
  interface), not against a real IMAP server. TLS login, `UID SEARCH`, `MOVE` fallbacks and
  provider quirks of `RealImapClient` are untested; `heftig ingest-eml` covers the message
  handling itself.
- **OpenAI and OpenAI-compatible adapters** are tested only with mocked HTTP responses. Real
  models may answer differently (e.g. reject a JSON mode); the strict validation limits the
  damage, but check a few documents after configuring a provider.
- **Anthropic** was additionally tested live with `scripts/live_provider_test.py`
  (`make live-test`, costs money): 7 synthetic documents (5 text PDFs, a 2-page scan, a phone
  photo), OCR plus classification, 11 requests per run (about 26k input / 2.4-3.4k output tokens).
  Measured on 2026-09-28 at list prices: `claude-opus-5-5` about 0.15 USD, `claude-sonnet-5`
  about 0.08 USD, `claude-opus-5` about 0.22 USD per run. All results were schema-valid; the
  existing correspondent was reused via its alias and a locked title survived re-classification.
  This is not part of CI.
- **The Paperless migration script** is tested only against a mocked Paperless-ngx API.
- **Tesseract quality** is checked with one synthetic German page, not with real-world scans.
- **Network shares** (SMB mounts disappearing, slow writes) are simulated with local folders
  (stability polling, missing folder), not with real CIFS mounts.
- **The web UI** is tested at the HTTP level (rendering, forms, CSRF); there are no browser or
  accessibility tests. Mobile layout is checked manually.
- **Scale:** no load tests; the test corpus has ten documents.
