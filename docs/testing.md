# Testing

## Running the tests

```sh
uv sync            # installs dev dependencies (pytest, ruff, reportlab)
make test          # = uv run pytest -q
make lint          # ruff check + ruff format --check on src, tests, contrib, scripts,
                   # plus the translation check (scripts/i18n.py check de)
```

The suite (about 470 tests) needs no network access, no accounts and no API keys. AI providers,
OCR (except one test) and the IMAP server are replaced by fakes. Test documents are generated
synthetically (`tests/helpers.py`, `tests/corpus.py`); all names, numbers and addresses in them
are invented.
The tests run with `language="de"` (`make_settings` in `tests/conftest.py`), because most
assertions check the German texts; English output is covered in `test_i18n.py`.

For the one test that runs real OCR (`test_tesseract_offline_ocr_reads_german`, marker
`tesseract`) install `tesseract` with German language data; without it the test is skipped. The
CI workflow installs `tesseract-ocr`, `tesseract-ocr-deu`, `tesseract-ocr-eng` and DejaVu fonts
(used to render the synthetic scans). A second job builds the container image and checks that it
starts and offers the setup page; on `main` and on release tags the image is pushed to
`ghcr.io/jo-tud/heftig` once both jobs passed.

Run a single file or test with `uv run pytest tests/test_search.py -q` or
`uv run pytest -k typo`.

## What is covered

| File | Verifies |
|---|---|
| `test_ingest.py` | Every source (`scanner`, `folder`, `web`, `api`, `email`) stores a byte-identical original; PDF, JPEG, PNG, multi-page TIFF accepted; type detected by content, not extension; identical SHA-256 keeps one original and logs a duplicate event; different files with the same text are not merged; size and page limits; monotonic sequences and `received_at` kept on reprocessing; auto-filing for the scanner; manual filing independent of the document date; originals are read-only. |
| `test_consume.py` | Files are taken only when stable, then removed; growing files wait; temporary names ignored; subfolders scanned; unsupported files quarantined with a reason file; files present before start are imported (laptop was off); the source stays when the archive commit fails; repeated failures end in quarantine; `move` mode; missing consume folder reported, also a network share that is down (without stopping the worker); a PDF written page by page with pauses is waited for; the folder for digital files; quarantine *Retry* and *Hide*; a file swapped for a link or FIFO is never read; oversized files stay in the folder. |
| `test_imap.py` | With a `FakeImap` server: attachments become documents with a shared import reference; polling is idempotent and resumes by UID; a changed UIDVALIDITY rescans without duplicates; a failing message is not marked and the error is visible; connection errors recorded; move-after-import; direct MIME document and mail without attachment; optional `.eml` archiving; a failing message is skipped after 3 attempts; delete-after-import only when everything is archived, into the server's trash folder (`\Trash` special-use flag); allowed senders and the attachment limit. |
| `test_mail.py` | E-mails as documents: `.eml` recognised by content, not by name; HTML mails as readable text; an inline forward gives the original's subject and date; one document with the PDF and image attachments as pages (a scanned one read with OCR), the same mail again is a duplicate; the mail date is not replaced by the classifier; characters the page font cannot show stay in the text; the keyword archives an inline forward or a mail forwarded as attachment, without it only the attachments are imported, a mail without files points to it, and it can be switched off; upload and attachment download; renderings in `cache/mail/` are cached and pruned. |
| `test_processing.py` | Embedded PDF text needs no OCR; partial OCR failure is visible and keeps the other pages; without any OCR the document stays usable; transient errors retried with backoff delay; final attempt records the error instead of looping; resume after a crash skips finished stages; worker restart requeues interrupted jobs; provider failure keeps metadata and original; locked fields and manual tag decisions survive reprocessing; cloud providers need the explicit permission. |
| `test_classify.py` | Valid output applied with provenance; invented (unbacked) dates not applied; uncertain dates flagged; invalid values dropped; existing terms and aliases reused; near-duplicate new terms become suggestions; sensible new terms created; low-confidence names only suggested; prompt treats the document as data; values must match their evidence; rule-based classifier works offline and reads English letters; e-mailed documents cannot create categories. |
| `test_rotation.py` | Turning one page or all and back (the original stays byte for byte); a turned page is read turned; search marks turn with the page; the viewer loads a turned page under a new address, and offers to read pages again that were read before they were turned. |
| `test_search.py` | The search acceptance corpus (below), plus snippet escaping, field syntax, invalid syntax reported instead of raised (including SQL/FTS metacharacters), unknown field prefixes as plain text, exact ID/SHA-256, default sort and stable pagination, custom-field amount filter, alias search, OR fallback, identical results after `reindex` and `rebuild-db`. |
| `test_maintenance.py` | `check` on a clean archive and after corruption; repair after a crash between sidecar and DB write; repair adopts orphan sidecars and requeues them; requeues unfinished processing; reports and adopts orphan originals; `rebuild-db` after losing the database; backup and restore on a fresh directory; DB snapshot consistency; atomic writes keep the old file on failure; paths cannot escape the archive; restrictive permissions; raw-response retention. |
| `test_export_import.py` | Export -> import round trip (directory and ZIP) preserves document count, hashes, text, metadata, taxonomy, aliases, ingest and filing order and locks; the export is open and contains no secrets; conflicts reported, not overwritten; import into a non-empty archive renumbers; tampered exports refused; zip-slip and zip bombs refused. |
| `test_providers.py` | OpenAI-compatible OCR sends inline `data:` images only; the classifier uses JSON schema mode and keeps the API key out of error messages; HTTP 429 is transient; capability check for text-only endpoints; JSON parsing tolerates code fences; cloud detection and gating; the default configuration sends nothing to the cloud; real Tesseract reads German text (needs `tesseract`); Anthropic prompt caching of the archive-wide part; reasoning parts (`<think>`) and content-part answers from open models, one retry on invalid JSON; `max_completion_tokens` for OpenAI. |
| `test_web.py` | `/health` and `/ready` (incl. unwritable archive); everything requires login; cookie flags and login rate limit; CSRF required for session writes; upload, detail, original download and duplicate; unsupported upload rejected with a friendly message; upload size limits; API token create/use/revoke; PATCH locks and delete confirmation; filing via API; HTML escaping; all UI pages render; form edit with CSRF; autosave of one field with undo, of several fields at once when the page is left, and the review box sent back after a save; taxonomy merge via API; OpenAPI schema; edited fields are locked; the review loop (*Reviewed → next*, *Skip*, *Trash → next* with the undo bar on the next document); suggestions in bulk; camera page and manifest; the installable app (service worker and its offline page, public and without user data); privacy-mode markup; page viewer and thumbnails; categories page; *Not kept* leaves the filing list. |
| `test_duplicates.py` | PDF and phone photo of the same invoice are listed as possible duplicates (same number and amount); monthly invoices of one sender are not; identical text without metadata is found; "keep both" is stored in both sidecars and survives rescan, database rebuild and export/import; review page, inbox list, document notice and delete-with-confirmation; the next pair opens after a decision; `heftig duplicates`; template invoices with recurring numbers or another month in words are not duplicates; misread numbers do not hide the same invoice; "Sep" and "September" are the same period. Statements of different periods (other dates in the text) are not duplicates, a stamp date or one misread digit does not rule a pair out; open pairs are checked again when the rules change. |
| `test_regressions.py` | Defects found in the code review of 2026-09-28: form posts from browsers that send `Origin: null`; stale edit forms (revision check), tags containing commas, CRLF in text areas; renaming a term to an empty name; import refusing foreign `original_relpath` values; placeholder names and crashing jobs never leave a document in `processing`; no parallel jobs for one document; requested stages re-run when merged into a waiting job; retry re-runs all stages; worker survives a locked database; manual tag decisions follow rename/delete; import resumes unfinished processing and stays idempotent after renumbering; export skips documents with a missing original; unreadable and undeletable files in the scanner folder; move mode never overwrites; strict ISO dates; clean 4xx errors on odd input; deleted documents are not resurrected; numbers typed with a space; rate limit uses the proxy-added address; automatic database snapshot and the copy before an update. |
| `test_setup.py` | First start: the account needs the setup code (a file only its owner can read); the AI choices (cloud only with consent, nothing stored on refusal); the search step; mail with presets, folders and allowed senders; scanner step and finish; settings given by the environment are shown as fixed; stored keys and the mail password never go to another server; a rejected API key is named as such. |
| `test_settings_store.py` | Stored settings apply and survive a restart; the environment wins and is fixed; invalid values refused; secrets kept when a field is left empty; web server and worker reload on a new revision; exports contain no stored secrets. |
| `test_i18n.py` | Every interface text has a German translation with the same placeholders; stored English texts are shown translated; date and number formats; choosing the language from `Accept-Language`; pages are English by default; German pages carry the translated browser-script texts. |
| `test_binders.py` | Filing goes into the current binder and continues in the next when it is full; *Taken out …* keeps the place and *Put back in its place* returns it; moving to another binder and *Not kept*; renaming; filings from before binders are adopted; `binders.json` survives `rebuild-db` and travels with exports; the pages. |
| `test_trash.py` | Trash, restore and purge; the same file again while in the trash; rebuild and expiry; delete with undo; bulk delete only with a matching count and unchanged results; failed restore or delete leaves everything as it was; restore when the filing position is taken; numbers of trashed documents are not reused. |
| `test_combine.py` | Pages, text and user data are combined; undo restores the parts; refused while processing or alone; combine page, undo bar, from the duplicate view and via API. |
| `test_titles.py` | Title normalisation; the classifier gets the titles of similar documents; harmonisation proposals, accepting them and their safety rules; needs an AI provider; the page; proposals never drop the sender's name. |
| `test_fallback.py` | Unreachable AI: OCR falls back to Tesseract and classification to rules, the document is searchable at once; permanent errors do not fall back; catch-up when the AI is back; state shown in inbox and document. |
| `test_blank_pages.py` | Empty duplex backs are detected and hidden in the viewer (never a whole document); the user's *Not blank* / *Blank* decision is kept only where it differs and survives re-extraction; the thumbnail uses the first page that is not blank; search hits show hidden pages; older documents are checked once. |
| `test_bulk_ai.py` | Cloud OCR cost guards: image size limit, parallel pages, recorded cost and page cache, rate limits postpone without fallback or a used-up attempt, a rejected image falls back for that page only, catch-up never queues twice, first-page model, blank pages and the page budget stay local. |
| `test_dates.py` | Date evidence in every format real documents use must back the date; English dates; stored suggestions re-checked without AI (`heftig recheck-dates`); documents without a letter date get the date they are made up to ("per", end of "vom … bis …", "Stand") from their first two pages, never a contract end after arrival. |
| `test_notes.py` | Notes (never touched by AI, searchable) and attachments (byte-identical, content-addressed, shared, served safely); forms; a note saved when its field is left (added once even when sent twice, then changed, deleted when emptied); export/import/check include them; index rebuilt after a schema migration. |
| `test_pagediff.py` | Page comparison of two copies: same page scanned twice, a signature on one side, unrelated letters, text diff; identical copies resolved automatically, but never the copy with user data, tag decisions or a signature; the compare page. Pages with a text layer: the changed words are marked, rendering noise is not, a signature outside the text still is. |
| `test_sessions.py` | Scan batches: back into the binder, into Heftig's filing as the stack comes out of the scanner (first scanned on top), sort out with keep-original suggestions (applied only to what the user saw); forgotten batches end and do not grab new mail; paper states on the document; the inbox card. |
| `test_expand.py` | What a search word stands for: word forms with umlaut spellings, compounds, splitting a compound into its parts, similar spellings (Damerau, OCR confusions) ranked last, words that mean the same by stem and phrase (ranked below the word itself), function words (kept when quoted); closer matches and more words rank higher; a question ranks by its rare words; a new alias is searchable at once. |
| `test_synonyms.py` | The archive's own words that mean the same (`synonyms.json`): parsed and cleaned, searched, saved on the settings page, carried by export and import; `heftig search-eval` with its JSON output and `--meaning`. |
| `test_search_quality.py` | The search benchmark (below) stays above its floors (MRR@10, success@1/@5, queries without a right result), and every kind of query has cases. |
| `test_senders.py` | E-mailed documents: filter and counts by sender address (any case), names for addresses cleaned and merged, `senders.json` in export, backup and import; the document page, the list's hover text, the filter, the chip, the settings page and the API. |
| `test_semantic.py` | Search by meaning with a fake model: documents embedded once and again only when their pieces change, large and small pieces, meaning finds other words, word matches stay first, filters apply, a failing model keeps the word results, fusion weights for keywords and questions, the "near" rules, 8-bit vector store with and without numpy, the worker's thread, the setup step and settings (progress updated in place); the built-in model's download checks, pooling and unloading after idle time; with the real model downloaded (`~/.cache/heftig-models`, else skipped) that it finds by meaning. |
| `test_search_ux.py` | German and English date phrases (and words that are none); switching them off; tag mode any/all; facets over the current results; suggestions; similar documents; highlighting; ID fragments; German word forms and compounds. |
| `test_search_web.py` | Search page: facets, timeline and toggle links; date phrase notice and literal link; tag mode switch; saved searches; suggest API; hits on the document page and on scanned pages via OCR word boxes; every filter link works; odd filter values do not break pages. |
| `test_aisearch.py` | AI search: the model's plan is checked against the archive, malformed values dropped; the page flow. |
| `test_mcp.py` | Read-only tokens can read but not write; `/api/lines` and `/api/fields`; the MCP tools; errors explained; `heftig mcp` needs a token. |
| `test_security.py` | Findings of the security audit: catastrophic regular expressions stopped; large bodies refused before parsing; login limit counts parallel attempts and stores no names; canonical document IDs; every TIFF frame checked; API tokens cannot use the browser pages. |
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

### Search benchmark

`tests/search_bench.py` is a synthetic German household archive (61 documents, with scans full
of OCR errors, documents without metadata, a newsletter and an ad that mention many search
words) with 94 judged queries and 50 held-out ones; `tests/test_search_quality.py` keeps the
figures above a floor, `uv run python scripts/search_bench.py [--meaning] [--heldout]` prints
every query. `scripts/real_bench.py` measures the whole search on questions real people asked
(downloads the data; not part of the suite). See [search.md](search.md#measuring-search-quality).

## Manual smoke test

`make smoke` (`scripts/smoke.py`) automates most of the following against real processes with
Tesseract. To do it by hand:

1. Start a fresh instance: `docker compose up -d` (or natively:
   `HEFTIG_ARCHIVE_DIR=/tmp/heftig-test/archive heftig run`) and create the account on
   <http://127.0.0.1:8765/setup> with the code from `archive/setup-token`. Without the code the
   account cannot be created.
2. Log out and in again. Check that a wrong password is rejected and that repeated wrong attempts
   are rate-limited.
3. **Digital PDF:** upload a born-digital PDF on *Add*. It should be `done` quickly, with
   text from the embedded layer (page method "embedded PDF text").
4. **Phone photo:** on a phone in the LAN (or with the browser's device emulation), upload a
   photo of a printed letter as *Scan/photo of a paper document*. It should be OCRed and appear
   under "Paper still to file" in the inbox.
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
    (top) of the month's section in the current binder, the first at position 2. *Taken out …*
    and *Put back in its place* on the first must keep its position.
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
  (stability polling, missing folder) and a failing file system call (a share that is down), not
  with real CIFS mounts.
- **The web UI** is tested at the HTTP level (rendering, forms, CSRF); there are no browser or
  accessibility tests. Mobile layout is checked manually.
- **Scale:** no load tests; the test corpus has ten documents.
