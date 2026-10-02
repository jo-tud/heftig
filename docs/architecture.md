# Architecture

Heftig is a small Python application (FastAPI, Jinja2 templates, SQLite with FTS5, pypdfium2,
Pillow, httpx; for the search Snowball stemmers and, for the optional search by meaning,
onnxruntime, tokenizers and numpy). There is no message broker, no external database and no cloud dependency.

## Components

```text
            browser / API client            scanner share        IMAP mailbox
                    |                            |                     |
             +------v------+              +------v---------------------v------+
             |  web        |              |  worker                           |
             |  heftig     |              |  heftig worker                    |
             |  serve      |              |  - polls consume/ (every 10 s)    |
             |  (uvicorn)  |              |  - polls IMAP (every 300 s)       |
             +------+------+              |  - runs jobs (thread pool)        |
                    |                     +------+----------------------------+
                    |   same code, same archive  |
             +------v----------------------------v------+
             |  archive/                                |
             |    originals/  documents/<uuid>/  ...    |   files: authoritative
             |    index.sqlite (WAL)                    |   DB: index + operational state
             +------------------------------------------+
```

| Component | Command | Responsibilities |
|---|---|---|
| Web | `heftig serve` | Web UI, REST API (`/api/...`, OpenAPI at `/api/docs`), first setup and settings pages, login, uploads (ingested synchronously in the request), `/health`, `/ready` |
| Worker | `heftig worker` | Consume folder polling, IMAP polling, processing jobs (OCR, classification), export/import/reindex/rebuild/title jobs, embedding documents for the search by meaning, heartbeat, hourly maintenance |
| Both | `heftig run` | Web server plus the worker in a thread of the same process (simple local setup) |
| Database | `archive/index.sqlite` | Search index and a queryable copy of all document metadata; primary store for jobs, users, sessions, API tokens, IMAP cursors, the ingest event log and the settings saved in the web interface |
| Sidecars | `archive/documents/<uuid>/`, `archive/trash/<uuid>/`, `taxonomy.json`, `binders.json`, `saved_searches.json`, `synonyms.json`, `senders.json` | Authoritative, human-readable document data; the database can be rebuilt from them |

Settings come from environment variables (`config.Settings`) plus the values saved on the setup
and settings pages (`settings_store`, stored in the database's `meta` table). An environment
variable wins; a saved value applies to web server and worker without a restart (both compare
`meta.settings_revision` and reload).

The main modules:

| Module | Purpose |
|---|---|
| `ingest.py`, `consume.py`, `imap_import.py` | Intake: one ingest function, the watched folders, the mailbox |
| `processing.py`, `classify.py`, `providers/` | Text extraction, classification, validation of AI output, provider adapters and prompts |
| `documents.py`, `storage.py`, `db.py`, `index.py`, `migrations/` | Sidecars, atomic writes, the SQLite database and its FTS index |
| `search.py`, `expand.py`, `synonyms.py`, `datephrases.py`, `aisearch.py`, `wordboxes.py` | Search and ranking, what a search word stands for (forms, compounds, similar spellings), words that mean the same, date phrases, the optional AI search, hit boxes on page images |
| `semantic.py`, `local_embed.py`, `searcheval.py` | The optional search by meaning (pieces, vectors, fusion with the word search) and its built-in ONNX model; measuring search quality |
| `duplicates.py`, `pagediff.py`, `combine.py`, `split.py`, `trash.py` | Possible duplicates and the page comparison, combining documents, splitting a document / arranging its pages, the trash |
| `binders.py`, `sessions.py` | Paper filing in named binders (`binders.json`), batches for scanning old binders |
| `titles.py`, `taxonomy.py`, `suggestions.py`, `saved_searches.py` | Title normalisation and harmonisation, categories, AI suggestions, saved searches |
| `settings_store.py`, `web/setup.py`, `connections.py` | Settings saved in the web interface, the setup/settings pages for AI, search, mail and scanner, and the connection tests behind their "Test" buttons (IMAP presets, model lists) |
| `i18n.py`, `locale/` | Interface languages: English source texts, gettext catalogues (`locale/de/messages.po`), translation of stored texts when shown |
| `auth.py`, `web/` | Users, sessions, API tokens; the FastAPI app, HTML pages and REST API |
| `maintenance.py`, `worker.py`, `jobs.py`, `cli.py`, `mcp_server.py` | Check/repair/rebuild/export/import/backup, the worker loop, the job queue, the command line, the MCP server |

Run **exactly one worker per archive**. On start the worker puts every job still marked
`processing` back into the queue, which is only correct if no other worker is running.

SQLite runs in WAL mode with `synchronous=FULL` and a 30 s busy timeout, so the web process and
the worker read concurrently. All writes go through `BEGIN IMMEDIATE` transactions
(`db.write_tx`) and are therefore serialised across processes. Every thread has its own
connection.

The worker's main loop runs once per second: reload the settings if they were changed in the web
interface, write a heartbeat (`meta.worker_heartbeat`, used by `/ready` and the status page),
poll the consume folder (and the optional folder for digital files) every
`HEFTIG_CONSUME_POLL_SECONDS`, poll IMAP every `HEFTIG_IMAP_POLL_SECONDS` (only if a mail server
is configured), every `HEFTIG_AI_RETRY_MINUTES` check whether an unreachable AI provider is back
([providers.md](providers.md#when-the-ai-provider-is-unreachable)), once an hour requeue jobs with
expired leases, prune old raw AI responses, end forgotten scan batches, purge expired trash and
write the automatic database snapshot, every 30 s (with the search by meaning switched on) start
a thread that embeds new and changed documents (after a failure every ten minutes), then claim
due jobs until `HEFTIG_WORKER_CONCURRENCY` jobs
are running.

## Ingestion pipeline

Every input path (web upload, API upload, consume folder, IMAP, `heftig ingest`,
`heftig ingest-eml`) calls the same function, `ingest.ingest_stream`. The steps are ordered so
that a crash at any point leaves either nothing, or something `heftig repair` can finish.

1. **Stream to a private temp file** in `archive/tmp/` (same filesystem as the archive), computing
   SHA-256 on the fly and aborting as soon as `HEFTIG_MAX_UPLOAD_MB` is exceeded. The file is
   fsynced.
2. **Validate by content.** Magic bytes decide the type (PDF, JPEG, PNG, TIFF), or a header
   section the type e-mail (`.eml`); the file is then actually opened with pdfium or Pillow (an
   e-mail: parsed and its pages composed, see `mail.py`). Encrypted, broken, empty, oversized
   (`HEFTIG_MAX_PAGES`, `HEFTIG_MAX_IMAGE_MEGAPIXELS`) or unknown files are rejected with a
   message (stored in English, shown in the interface language). A rejection is recorded as an
   ingest event; the source is never deleted by the ingest function itself.
3. **One exclusive transaction:**
   - If a document with the same SHA-256 exists: append a `duplicate` event to its
     `metadata.json` and to the event log. No second original, no new document.
   - Otherwise: move the temp file to `originals/<aa>/<sha256>.<ext>` (`chmod 0400`, atomic
     rename, fsync of the directory), allocate the next `ingest_sequence`, write
     `metadata.json` atomically, insert the database row and the search index row, record the
     `created` event and enqueue a `process` job. If the source is configured for auto-filing
     (`HEFTIG_AUTO_FILE_SOURCES`), `filed_at`/`filing_sequence`/`filing_section` are set here.
   - Commit.
4. **Only after the commit returns** may the caller remove the source: the consume watcher
   deletes or moves the file, the IMAP poller marks or moves the message.

What happens after a crash at each point:

| Crash after | State | Recovery |
|---|---|---|
| step 1 or 2 | Temp file in `archive/tmp/` | Source still exists and is picked up again. `heftig repair` deletes temp files older than one hour. |
| rename of the original, before commit | Original without a document (`orphan_original`) | If the source still exists (it does, step 4 did not run), the next ingest finds the file at its target path, verifies its hash and reuses it. Otherwise `heftig repair --adopt-orphans` ingests it. |
| sidecar write, before commit | `metadata.json` without a database row (`orphan_sidecar`) | `heftig repair` loads the sidecar into the database and queues processing if no text was extracted yet. |
| commit, before source removal | Document archived, source still present | The file is ingested again and recognised as a duplicate (one more event, no second original). |

All later changes to a document (processing results, edits, filing, taxonomy merges) go through
`documents.persist()` inside a write transaction: first the sidecar is written atomically (temp
file, fsync, rename, fsync of the directory) with an incremented `revision`, then the database
row and the index are updated, then the transaction commits. If the process dies in between,
`heftig check` reports a `revision_mismatch` and `heftig repair` takes the newer version
(normally the sidecar).

Deleting a document moves it to the trash (`trash.py`): its sidecar folder moves from
`documents/<id>/` to `trash/<id>/`, the original stays in `originals/`, and it can be restored
until it is purged - after `HEFTIG_TRASH_RETENTION_DAYS` (30) or explicitly. Purging is the only
operation that removes an original (and only one that no other live or trashed document
references); it must be confirmed explicitly (UI: in the trash; API:
`DELETE /api/trash/<id>?confirm=<id>`, while `DELETE /api/documents/<id>?confirm=<id>` only
moves to the trash).

## Jobs

Jobs live in the `jobs` table. Kinds: `process` (per document), `export`, `import`, `reindex`,
`rebuild`, `titles` (title harmonisation). Status: `queued`, `processing`, `done`,
`needs_review`, `failed`. Each job records its current stage, progress (0..1), attempts, last
error and a JSON result.

| Mechanism | Behaviour |
|---|---|
| Claiming | A worker takes the oldest due job (`status = queued`, `next_run_at <= now`; maintenance jobs before processing jobs), sets `processing`, increments `attempts` and sets a lease of `HEFTIG_JOB_LEASE_SECONDS` (900 s). |
| Lease | Every progress update extends the lease. Leases that expire (worker died) are requeued by the hourly maintenance and by `heftig repair`; on worker start all `processing` jobs are requeued immediately. |
| Retry and backoff | A transient failure (timeout, connection error, HTTP 408/409/429/5xx, rate limit) or an unexpected exception requeues the job with a delay of `HEFTIG_JOB_BACKOFF_SECONDS * 2^(attempts-1)` (30 s, 60 s, 120 s, ... capped at 6 h), up to `HEFTIG_JOB_MAX_ATTEMPTS` (5). |
| Final attempt | On the last attempt a transient provider error is no longer retried; it is recorded on the page / in the document history and the job finishes, so a job never loops forever. |
| Resume | A `process` job stores the completed stages in its payload (`done: ["extract"]`). A resumed job skips them. Each stage writes its results in one transaction, so re-running a half-finished stage is safe. |
| Coalescing | Queueing a `process` job for a document that already has a queued (not yet started) one merges the stages instead of creating a second job. |
| Manual retry | Failed or needs-review jobs can be retried from the inbox or via `POST /api/jobs/<id>/retry` (resets attempts). |

Export, import, reindex and rebuild jobs are started from the UI/API and run with
`max_attempts = 1` (`titles`: 2).

## Processing stages

A `process` job runs `extract` and/or `classify`. Reprocessing (per document, a selection or all
documents; UI, `POST /api/reprocess`, `heftig reprocess`) queues either or both stages.

**extract**

1. If the stored text is marked as user-confirmed (`text_pages.json: user_confirmed`), skip.
   (The format supports confirmed text; the V1 UI has no text editor yet.)
2. PDFs: read the embedded text layer per page (pdfium). A page with fewer than
   `HEFTIG_MIN_TEXT_CHARS_PER_PAGE` (40) characters counts as image-only.
3. Image-only PDF pages and all image pages/TIFF frames are rendered at `HEFTIG_OCR_DPI` (300,
   capped by the megapixel limit, EXIF rotation applied) to PNG and passed to the OCR provider
   one page at a time. A short embedded fragment on such a page is kept if the OCR result does
   not already contain it.
4. A failing page is recorded with its error; the other pages continue. Result:
   `text_status` = `ok`, `empty`, `partial` (some pages failed) or `failed` (all failed, no text).
5. Write `text_pages.json`, `text.md`, the preview thumbnail, a history entry and a
   `processing_runs` row in one transaction.

**classify**

1. Build the request: document text (truncated to `HEFTIG_CLASSIFY_MAX_CHARS`, keeping the first
   80 % and the last 20 %), original filename, page count, all existing correspondents, document
   types and tags with their aliases, and the existing custom field keys.
2. Call the classifier. No text means the stage is skipped and noted in the history.
3. Validate and apply the response (`classify.apply`, see [providers.md](providers.md#validation-rules)):
   locked fields are untouched, uncertain values become suggestions, nothing unbacked is
   applied.
4. Store the history entry and a `processing_runs` row with applied/suggested/dropped fields and
   the raw response (truncated to `HEFTIG_RAW_RESPONSE_MAX_KB`, deleted after
   `HEFTIG_RAW_RESPONSE_RETENTION_DAYS`).

A provider failure never modifies the original or existing metadata; it only adds a review
reason and a history entry. The document status is derived afterwards: `failed` if no text could
be extracted at all, `needs_review` if there are review reasons or open suggestions, otherwise
`done`.

## Provider protocols

Text extraction, classification and embeddings (the built-in model of the search by meaning)
are separate protocols in `src/heftig/providers/base.py`. One backend may implement several, but
each task is configured on its own (provider, model, base URL, key, cloud permission).

| Protocol | Method | Implementations |
|---|---|---|
| `TextExtractor` | `extract_page(image_png: bytes, page_number: int, languages: str) -> str` plus `capabilities` (`images`, `pdf`) | `tesseract`, `openai`, `openai_compatible`, `anthropic`, `mock` |
| `Classifier` | `classify(ClassifyRequest) -> ClassifyResponse` (parsed JSON + raw text) | `rules`, `openai`, `openai_compatible`, `anthropic`, `mock` |
| `Embedder` | `embed(texts: list[str]) -> list[list[float]]` | built-in ONNX model (`local_embed.py`, search by meaning, `semantic.py`) |

Every provider exposes `name`, `model`, `target` (where data goes, e.g. `local` or an API host),
`adapter_version` and, for classifiers, `prompt_version`; these are stored in the processing
history. The pipeline converts PDF pages to page images itself, so an extractor only has to
accept images; a text-only endpoint is detected via its capability flag and not sent images.
Providers are built by `providers/registry.py` from the settings; tests replace them with fakes
via `registry.override()`.

Errors are reported as `ProviderError(message, transient=bool)`; `ProviderUnavailable` means
"not usable with this configuration" (missing key, missing package, blocked cloud provider) and
is not retried.

## Security model

Heftig is designed for one person on a trusted machine or home network. It binds to `127.0.0.1`
by default; see [operations.md](operations.md#ports-and-network-access) for LAN and remote access.

**Authentication**

- Exactly one user, created on the first start at `/setup` (or with `heftig init`). There is no
  default password: as long as no user exists, the setup page only accepts the one-time code in
  `<archive>/setup-token` (also written to the log), so whoever reaches the port first cannot
  take over the archive. Passwords: minimum 10 characters, hashed with scrypt (N=2^15, r=8, p=1,
  random salt).
- Browser sessions: a random 256-bit token in the cookie `heftig_session` (`HttpOnly`,
  `SameSite=Strict`, `Secure` when the request came in via HTTPS, including `X-Forwarded-Proto`
  from a trusted proxy, or forced with `HEFTIG_COOKIE_SECURE`). Only the SHA-256 of the token is
  stored. Lifetime `HEFTIG_SESSION_HOURS` (14 days). Changing the password ends all sessions.
- API tokens (`Authorization: Bearer hft_...`): random, shown once, stored hashed, with name,
  prefix, last use and revocation. Tokens can be created in the settings page or with
  `heftig token create`, not with another token.
- Login rate limit per client address and per username (`HEFTIG_LOGIN_MAX_ATTEMPTS` = 5 per
  `HEFTIG_LOGIN_WINDOW_SECONDS` = 300 s), stored in the database so it survives restarts. Unknown
  users cost the same scrypt time as known ones.
- Every page and API endpoint except `/health`, `/ready`, `/login`, `/setup` (setup code, only
  while no user exists), static files and the OpenAPI schema requires authentication, including
  original downloads.

**CSRF.** Every state-changing request authenticated by a session cookie must carry the
session's CSRF token (`X-CSRF-Token` header or `csrf_token` form field), and a present
`Origin`/`Referer` must match the `Host` (or `X-Forwarded-Host` behind a trusted proxy).
`Origin: null`, which some browsers and privacy settings send, is treated like a missing header -
the token is still required. Bearer-token requests are not affected (browsers do not
send them automatically). `SameSite=Strict` is a second layer.

**Response headers.** HTML responses carry a strict Content-Security-Policy (`default-src
'self'`, no inline scripts or styles, `object-src 'none'`, `frame-ancestors 'self'`,
`form-action 'self'`), plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`X-Frame-Options: SAMEORIGIN`, a restrictive `Permissions-Policy` and `Cache-Control: no-store`.
Originals are served with their verified MIME type, as attachment by default; inline images get
`Content-Security-Policy: default-src 'none'; sandbox`. OCR text, AI output and filenames are
HTML-escaped by the template engine; search snippets are escaped before `<mark>` is added.

**Resource limits.**

| Limit | Setting (default) |
|---|---|
| File size, enforced while streaming | `HEFTIG_MAX_UPLOAD_MB` (100) |
| Request body (all files of one upload request) | 5 x max upload + 1 MB, answered with 413 |
| Pages per document | `HEFTIG_MAX_PAGES` (500) |
| Image size / decompression bombs; PDF render size | `HEFTIG_MAX_IMAGE_MEGAPIXELS` (150) |
| Tesseract time per page | `HEFTIG_OCR_PAGE_TIMEOUT_SECONDS` (180), one thread per call |
| Parallel jobs | `HEFTIG_WORKER_CONCURRENCY` (2, max 16); pdfium calls are serialised |
| Provider calls | `HEFTIG_PROVIDER_TIMEOUT_SECONDS` (120) |
| E-mail attachments | `HEFTIG_IMAP_MAX_ATTACHMENT_MB` (50) |
| ZIP import | no absolute paths or `..`, no encrypted entries, compression ratio <= 200, entries may not exceed their declared size |

**No shell, no trusted filenames.** External programs (only Tesseract) are started without a
shell, with an argument list, on a temp file whose name Heftig generated; the OCR language string
is checked against a whitelist. Incoming filenames are display data only: directories and control
characters are stripped, originals are stored under their hash. Every path taken from metadata
is resolved and must stay inside the archive root; document IDs must be UUIDs; API path
parameters never map directly to filesystem paths (the import API only accepts paths below
`<archive>/imports/`).

**SSRF.** Page images are sent to AI providers inline (base64 / `data:` URL), never as URLs, so a
provider never fetches anything on Heftig's behalf. The HTTP client does not follow redirects.
Base URLs come only from configuration, never from document content.

**Prompt injection.** Document text is treated as untrusted data: the system prompts say so
explicitly, the text is wrapped in `<document>` tags (a closing tag inside the text is
neutralised), the filename is passed as a JSON string marked as untrusted. The model's answer is
only data: it is parsed as JSON, validated field by field (see
[providers.md](providers.md#validation-rules)) and never executed or rendered as HTML. The worst a
malicious document can achieve is wrong suggestions for its own metadata.

**Secrets and logs.** API keys and the IMAP password can be given as environment variables, as
files (`*_FILE`) or on the settings pages (then stored in the database's `meta` table, mode
0600). They are never logged, exported or shown again in the UI (the UI only shows whether a key
is configured); a stored key is only sent to the server it was entered for. Logs contain
document IDs and error types, not document content, cookies or tokens.

**Files.** Directories are created with umask 077, sidecars and the database with mode 0600,
originals 0400.

Malware scanning is not built in. Documents are only parsed by pdfium and Pillow and never
executed, but if you receive files from untrusted senders, consider scanning them before they
reach Heftig (e.g. ClamAV in front of the consume folder, see
[operations.md](operations.md#malware-scanning)).

## Not in V1, and where it would go

| Feature | Status | Extension point |
|---|---|---|
| Searchable OCR-PDF / PDF/A derivative | Not generated | Derivatives belong next to the sidecars in `documents/<uuid>/` (like the regenerable `preview.webp`), never replacing the original; the original's hash and path stay the contract. `ExtractCapabilities.pdf` already reports whether an extractor could take PDFs. |
| Semantic search / embeddings | Search by meaning ([search.md](search.md#search-by-meaning)): a built-in model on the CPU, off unless the setup assistant or the settings switch it on; used by every search once documents are embedded, merged with the word search by rank fusion | Vectors in `doc_embeddings`; a vector index (sqlite-vec) would only be needed far beyond household sizes. The word search stays local and deterministic. |
| Editing OCR text | Not in the UI | `text_pages.json` has `user_confirmed` and the page method `user`; re-extraction already respects confirmed text. |
| Multiple users, roles, sharing, workflows | Not planned for V1 | - |
| Digital signatures, revision-proof storage | Not provided | - |
| Built-in malware scanning | Not provided | Scan in front of `consume/` / the mail server. |


## Browser-side document camera

`/scan` (templates/scan.html, static/scan.js) runs entirely in the browser. `static/docdetect.js`
finds the sheet with a line-based detector (after Dropbox's document scanner and Tropin et al.
2021): edge strength from several colour channels, Hough lines, every quadrilateral of two
near-horizontal and two near-vertical lines (image borders included, for sheets that leave the
frame) scored by edge support along its sides and colour contrast to its surroundings. This
works on light backgrounds and with low contrast where "largest contour" fails; outlines below a
confidence score are not shown (white paper on a white wall). In the live view a funnel after
WeScan suppresses jitter: an outline is shown only when 3 of the last 8 detections agree, drawn
smoothly, and captured after about a second without movement, then refined on the full-resolution
frame. OpenCV.js warps the full-resolution photo, and a
small PDF writer in `scan.js` puts one JPEG per page into a PDF that is uploaded through the normal
`POST /api/documents` path - so the PDF the phone sends is the archived original. Third-party files
live in `static/vendor/` with licenses and checksums (`README.txt`). Only this page gets
`camera=(self)`, `'wasm-unsafe-eval'`/`'unsafe-eval'` (OpenCV.js/Emscripten) and `connect-src data:`
(OpenCV.js loads its embedded WebAssembly from a data: URL); all other pages keep the strict policy.
