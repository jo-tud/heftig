# Data format

The archive directory is the only persistent state. It is designed to stay readable without
Heftig: originals are unchanged files, document data is JSON and Markdown, and the SQLite
database can be rebuilt from those files (except for the operational state listed below).

## Archive layout

```text
archive/
  originals/ab/<sha256>.<ext>        byte-identical originals, mode 0400, never modified
  originals/attachments/ab/<sha256>.<ext>  files attached to documents (see "Notes and attachments")
  documents/<uuid>/metadata.json     authoritative per-document metadata (sidecar)
  documents/<uuid>/text.md           extracted text of the whole document
  documents/<uuid>/text_pages.json   text per page incl. method, provider and errors
  documents/<uuid>/preview.webp      thumbnail of page 1 (regenerable)
  documents/<uuid>/cache/            rendered pages, word boxes, OCR page cache (regenerable)
  trash/<uuid>/                      sidecar folders of deleted documents until they are purged
  taxonomy.json                      correspondents, document types, tags + aliases (sidecar)
  saved_searches.json                saved searches of the search page (optional)
  binders.json                       the binders of the paper filing: name, started, full since
  index.sqlite                       database: index, metadata copy, jobs, auth, import state
  index.sqlite-wal, -shm             SQLite write-ahead log (part of the database while running)
  setup-token                        one-time code for /setup, only while no user exists
  consume/                           watched input folder (default location; often mounted elsewhere)
  quarantine/                        rejected consume files + <name>.reason.json (hidden ones in ausgeblendet/)
  email/                             archived .eml sources (only with HEFTIG_IMAP_ARCHIVE_EML=true)
  backup/index-snapshot.sqlite       consistent database copy (`heftig db-snapshot`, automatic every 6 h)
  backup/vor-update-v<N>.sqlite      database copy taken before a migration (last three kept)
  exports/                           exports started from the web UI / API
  imports/                           place exports here to import them via the API
  tmp/                               staging area for uploads (same filesystem, cleaned by repair)
```

- `<ext>` is derived from the detected content type: `pdf`, `jpg`, `png` or `tif`. `ab` is the
  first two hex digits of the SHA-256. The path is deterministic: the same file always gets the
  same path, and a second copy is never stored.
- `<uuid>` is a random UUID assigned at first ingestion. It never changes, also not across
  export/import.
- All timestamps are UTC in ISO 8601 with `Z` (`2026-09-28T08:20:00Z`) and are converted to local
  time only in the UI. Dates without time (`document_date`) are `YYYY-MM-DD`.
- All JSON files are UTF-8, written atomically (temp file, fsync, rename, fsync of the directory).

## metadata.json

Validated by the Pydantic model `heftig.models.DocumentMetadata` (unknown fields are rejected).
The JSON Schema is in [metadata.schema.json](metadata.schema.json); regenerate it with
`heftig schema` (or `make schema`).

Example (shortened):

```json
{
  "schema_version": 1,
  "id": "c8195316-d391-4b60-958e-2499595ec523",
  "sha256": "5f67860a204fe885a15244026ddfc18c3041e527cef1a35950319689a9d02273",
  "original_filename": "telekom_2026_09.pdf",
  "original_relpath": "originals/5f/5f67860a204fe885a15244026ddfc18c3041e527cef1a35950319689a9d02273.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 1527,
  "page_count": 1,
  "source": "web",
  "source_details": {"client": "browser", "kind": "paper"},
  "received_at": "2026-09-28T08:51:54Z",
  "ingest_sequence": 1,
  "paper": true,
  "document_date": "2026-09-03",
  "document_date_status": "ai",
  "document_date_reason": "Found in the text: “03.09.2026”",
  "filed_at": "2026-09-28T08:51:55Z",
  "filing_sequence": 1,
  "filing_section": "2026-09",
  "filing_binder": "Heftig 1",
  "title": "Mobilfunkrechnung September 2026",
  "correspondent": "Telekom Deutschland GmbH",
  "document_type": "Rechnung",
  "tags": ["Telefon"],
  "summary": "Mobilfunkrechnung der Telekom für September 2026 über 39,95 EUR.",
  "custom_fields": {"Betrag": {"type": "monetary", "value": 39.95, "currency": "EUR"}},
  "field_locks": {"title": true},
  "field_sources": {"title": "user", "document_date": "ai", "correspondent": "ai", "tags": "ai"},
  "tag_overrides": {"added": [], "removed": []},
  "suggestions": [],
  "status": "done",
  "text_status": "ok",
  "review_reasons": [],
  "ingest_events": [
    {"at": "2026-09-28T08:51:54Z", "source": "web", "source_details": {"client": "browser", "kind": "paper"},
     "original_filename": "telekom_2026_09.pdf", "result": "created"}
  ],
  "processing_history": [
    {"task": "extract", "at": "2026-09-28T08:51:55Z", "status": "ok", "provider": "embedded",
     "model": "", "target": "", "adapter_version": "", "prompt_version": "", "fields": [],
     "error": null, "by": "system"}
  ],
  "revision": 7,
  "updated_at": "2026-09-28T08:51:55Z"
}
```

### Fields

Fields marked * are additions to the minimal field list of the original specification.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Version of this format, currently `1`. |
| `id` | string (UUID) | Stable document ID. |
| `sha256` | string (64 hex) | SHA-256 of the original; verified on ingest, `heftig check` and import. |
| `original_filename` | string | Filename as received, for display only (directories and control characters removed). Never used as a path. |
| `original_relpath` | string | Path of the original relative to the archive root, `originals/<aa>/<sha256>.<ext>`. |
| `mime_type` | string | Detected type: `application/pdf`, `image/jpeg`, `image/png`, `image/tiff`. |
| `size_bytes`* | int | Size of the original. |
| `page_count`* | int or null | Pages (PDF) or frames (TIFF); set on ingest, confirmed by extraction. |
| `source` | enum | First arrival: `scanner` (consume folder), `folder` (`heftig ingest`), `web`, `api`, `email`, `import` (e.g. adopted by `heftig repair`). |
| `source_details` | object | Sparse provenance of the first arrival. Consume: `path` (relative to the consume folder). Web/API: `client`, `kind` (`digital`/`paper`). E-mail: `import_ref` (shared by all attachments of one message), `message_id`, `from` (address only), `subject`, `message_date` (the mail's Date header, provenance only), `mailbox`, `uid`, `uidvalidity`, `attachment`, optionally `eml`. Combined documents: `action: "combine"` and `combined_from` (`[{id, title, pages}]` of the parts in page order; the parts are in the trash as batch `combine-<id>`). Never credentials. |
| `received_at` | timestamp | First successful arrival. Set by the server, never changed (not by reprocessing, duplicates or import). |
| `ingest_sequence` | int | Unique, strictly increasing arrival counter; numbers of deleted documents are not reused. |
| `paper`* | bool | The document exists on paper and can be filed. True for `scanner`, for uploads marked as paper, and once filed. |
| `document_date` | date or null | Date printed on the document. |
| `document_date_status`* | enum | `unknown`, `ai` (found in the text, confident), `ai_uncertain` (found, low confidence; flagged for review), `user`, `none_found` (classifier found no date), `as_of` (no letter date: the date the document is made up to, e.g. "per 30.09.2021", found by a local rule), `import` (reserved). |
| `document_date_reason`* | string or null | Human-readable reason, e.g. the quoted evidence or why a proposal was not applied. |
| `filed_at` | timestamp or null | When the paper was filed. Null means: physical location not confirmed. |
| `filing_sequence` | int or null | Unique, increasing filing counter. Within a section, higher = further up in the stack. |
| `filing_section` | string or null | `YYYY-MM` (or `YYYY` with `HEFTIG_FILING_GRANULARITY=year`), derived from `filed_at` in local time. |
| `filing_binder`* | string or null | Name of the binder the paper is in (see [binders.json](#bindersjson)). A filed sheet with a `paper_location` has been taken out and keeps its place. |
| `paper_location`* | string or null | Where the paper is if not in its place in Heftig's filing: taken out (with an optional note), in an old binder (scan batch "back into the binder"), or "somewhere else". |
| `paper_discarded_at`* | timestamp or null | The paper was not kept ("Not kept", or discarded after a scan batch). |
| `scan_session`* | object or null | The scan batch the paper came in with: `{id, name, mode}`, `mode` `folder` (back into the binder), `refile` (into Heftig's filing) or `sort` (away, except what matters). |
| `keep_original`*, `keep_original_reason`*, `keep_original_source`* | bool / string / enum, or null | Whether to keep the paper original, suggested by the classifier (`ai`) or a rule (`rule`) in a scan batch, decided by the `user`. |
| `title` | string | Initially the filename without extension (source `rule`). |
| `correspondent` | string or null | Canonical name of a taxonomy term. |
| `document_type` | string or null | Canonical name of a taxonomy term. |
| `tags` | list of strings | Canonical tag names. |
| `summary` | string | Short summary. |
| `custom_fields` | object | Key -> `{type, value, currency}`; `type` is `string`, `number`, `monetary`, `date` or `boolean`; `value` is a string, number, boolean or null; `currency` is an ISO 4217 code (`EUR`) for `monetary`. Dates as `YYYY-MM-DD` strings. |
| `field_locks` | object | Field -> `true` for locked fields. Lockable: `title`, `document_date`, `correspondent`, `document_type`, `tags`, `summary`, `custom_fields`. Reprocessing never changes a locked field. |
| `field_sources`* | object | Field -> `ai`, `user`, `rule` or `import`: who set the current value. |
| `tag_overrides`* | object | `added` / `removed`: tags the user added or removed manually. Re-classification applies them on top of new AI tags, so a removed tag does not come back. |
| `suggestions`* | list | AI proposals that were not applied: `{field, value, reason, confidence}`. Replaced on every classification run; accepting one sets and locks the field. |
| `status`* | enum | `queued`, `processing`, `done`, `needs_review`, `failed`. |
| `text_status`* | enum | `pending`, `ok`, `partial` (some pages failed), `failed`, `empty` (no text on any page). |
| `review_reasons`* | list of strings | Why the document needs review, e.g. OCR errors, uncertain date, possible duplicate category. Stored in English, translated when shown. |
| `ingest_events`* | list | Every arrival of this exact file: `{at, source, source_details, original_filename, result}` with `result` `created` or `duplicate` (`imported` is reserved). |
| `processing_history` | list | Extraction, classification, edits, filing and merges: `{task, at, status, provider, model, target, adapter_version, prompt_version, fields, error, by}`. `task`: `extract`, `classify`, `edit`, `filing`, `import`, `merge`, `note`, `attachment`; `by`: `ai`, `user`, `rule`, `import`, `system`. The last 200 entries are kept. |
| `ai_pending`* | list | AI stages (`extract`, `classify`) that ran with the local fallback and are redone once the provider answers again ([providers.md](providers.md#when-the-ai-provider-is-unreachable)). |
| `ocr_all_pages`* | bool | The user asked for AI text recognition of every page, beyond `HEFTIG_OCR_AI_MAX_PAGES`. |
| `page_rotation`* | object | Pages the user turned, degrees clockwise (`{"2": 180}`: 90, 180 or 270). Applied when showing and reading the page; the original file is never changed. |
| `page_blank`* | object | The user's decision per page (`{"2": false, "5": true}`): `true` hides the page as blank, `false` always shows it – overrides `blank` in `text_pages.json`. |
| `trashed_at`*, `trash_reason`*, `trash_batch`* | string or null | Set while the document is in the trash (`trash/<uuid>/`); documents deleted together (a bulk deletion, the parts of a combined document) share a batch and can be restored together. |
| `revision`* | int | Incremented on every write; used by `heftig repair` to decide whether the sidecar or the database is newer. |
| `updated_at`* | timestamp | Last write. |

Large raw AI responses are **not** in the sidecar; they are kept for a limited time in the
database (see below).

## text.md and text_pages.json

`text.md` is the whole document's text, one block per page, with an HTML comment as page marker:

```markdown
<!-- page 1 -->
Telekom Deutschland GmbH
Ihre Rechnung für September 2026
...

<!-- page 2 -->
...
```

`text_pages.json` keeps the page mapping and how each page's text was obtained:

```json
{
  "schema_version": 1,
  "page_count": 1,
  "pages": [
    {"page": 1, "method": "embedded", "provider": "", "text": "Telekom Deutschland GmbH\n...",
     "chars": 157, "error": null, "blank": false, "turn": 0}
  ],
  "extracted_at": "2026-09-28T08:51:55Z",
  "user_confirmed": false
}
```

`method` is `embedded` (PDF text layer), `ocr`, `none` (no text; `error` explains why) or `user`
(reserved for user-edited text). `blank` marks a page without ink (e.g. the empty back of a duplex
scan): not sent to a paid OCR and hidden in the page viewer, unless `page_blank` in
`metadata.json` says otherwise. `turn` is how the page was turned (`page_rotation`) when its
image was read. `user_confirmed: true` makes re-extraction keep the text; V1 has
no UI for editing text yet.

## saved_searches.json

Saved searches: `{"version": 1, "searches": [{"id", "name", "query", "created_at"}]}`. `query` is
the query string of the search page, restricted to the known search parameters. Missing file =
no saved searches.

## binders.json

The binders of the paper filing, oldest first:
`{"version": 1, "binders": [{"name", "started_at", "full_at"}]}`. `name` is what is written on
the binder's spine (at most 60 characters); `full_at` is null for the binder still being filled.
New filings go into the newest binder that is not full (created on first use as "Binder 1", in
German "Ordner 1"). Each filed document also carries the binder's name in `filing_binder`, so
the sidecars alone say where the paper is. Renaming a binder rewrites the affected sidecars.
Missing file = no binders yet; filings from before binders existed are assigned to the first
binder. On import, unknown binders are added and count as full.

## taxonomy.json

All correspondents, document types and tags with aliases, rewritten on every taxonomy change:

```json
{
  "schema_version": 1,
  "terms": [
    {"kind": "correspondent", "name": "Telekom Deutschland GmbH", "aliases": ["DTAG"],
     "origin": "ai", "created_at": "2026-09-28T08:51:55Z"}
  ]
}
```

`kind` is `correspondent`, `document_type` or `tag`; `origin` records who created the term
(`user`, `ai`, `rule`, ...). Names are compared in normalised form (case, umlauts and
punctuation folded), so `TELEKOM DEUTSCHLAND GMBH` and `Telekom Deutschland GmbH` are the same
term. Documents store the canonical name; merging two terms rewrites the affected sidecars and
keeps the old name as an alias.

## What lives only in the database

The database holds a full copy of every sidecar (column `documents.metadata_json`) plus derived
tables (text, tags, custom field values, the FTS index); those are rebuilt by
`heftig rebuild-db`. The following state has **no sidecar** and cannot be reconstructed from the
originals or sidecars:

| Table | Content | In the export | Restored by import |
|---|---|---|---|
| `meta` | sequence counters, last export/backup/snapshot time, worker heartbeat, the settings saved in the web interface (`setting.<name>`, including API keys and the mail password) | `state/sequences.json` (counters); settings are not exported | counters are raised to at least the exported values |
| `ingest_events` | global arrival log incl. rejections, skipped attachments, deletions | `state/ingest_events.jsonl` | only into an empty archive |
| `imap_state` | UIDVALIDITY and last processed UID per account/mailbox | `state/imap_state.json` | yes (existing entries win) |
| `imap_items` | idempotency keys (account, Message-ID, attachment SHA-256) | `state/imap_items.jsonl` | yes (existing entries win) |
| `jobs` | job queue and history | open jobs in `state/open_jobs.jsonl`, for reference | no |
| `users` | user name and password hash | user names only (`state/users.json`) | no, create the account on `/setup` (or with `heftig init`) |
| `api_tokens` | token hashes, names, prefixes, usage | names, prefixes, dates only | no, create new tokens |
| `sessions`, `login_attempts` | browser sessions, rate limit | no | no |
| `processing_runs` | one row per extraction/classification run incl. applied/suggested/dropped fields and the raw AI response (truncated, pruned after the retention period) | no (the per-document `processing_history` is in the sidecar) | no |
| `consume_failures` | failure counter per consume file | no | no |
| `scan_sessions` | scan batches (name, mode, start and end); the documents keep their batch in `scan_session` | no | no |
| `title_proposals` | open proposals of the title harmonisation (temporary) | no | no |

Secrets (password hashes, session and API tokens, API keys, IMAP password) are never exported,
neither are the settings saved in the web interface.
To keep them, back up the whole archive directory ([operations.md](operations.md#backup)).

## Export format

`heftig export <dir> [--zip]` (or *Export* in the settings page, written to `<archive>/exports/`)
creates `heftig-export-YYYYMMDD-HHMMSS/` (or a `.zip` containing that directory; originals are
stored uncompressed in the ZIP):

```text
heftig-export-20260928-120000/
  manifest.json            format, versions, export time, document count, every file with SHA-256
  README.txt               short human-readable description
  metadata.schema.json     JSON Schema of metadata.json
  metadata.jsonl           one metadata object per line, in ingest order
  originals/ab/<sha>.<ext> unchanged originals
  documents/<uuid>/        metadata.json, text.md, text_pages.json
  taxonomy.json            correspondents, document types, tags with aliases
  saved_searches.json      saved searches (only if there are any; merged on import)
  binders.json             binders of the paper filing (merged on import; imported ones count as full)
  state/sequences.json     last_ingest_sequence, last_filing_sequence
  state/ingest_events.jsonl
  state/imap_state.json
  state/imap_items.jsonl
  state/open_jobs.jsonl
  state/users.json         user names and API token names/prefixes (no secrets)
```

`manifest.json`:

```json
{
  "format": "heftig-export",
  "format_version": 1,
  "metadata_schema_version": 1,
  "app_version": "0.1.0",
  "created_at": "2026-09-28T12:00:00Z",
  "document_count": 10,
  "files": [{"path": "metadata.jsonl", "sha256": "5059b9...", "size": 19497}]
}
```

`files` lists every file of the export except `manifest.json` itself. Document metadata, taxonomy
and state are read in one database read transaction, so they are mutually consistent. Preview
thumbnails are not exported (they are regenerated on import), nor are documents in the trash. In
`state/ingest_events.jsonl`, `source_details` is the raw JSON string as stored in the database.

The export is also the input for migrations to other systems, see [paperless.md](paperless.md).

## Import semantics

`heftig import <dir-or-zip>` (or `POST /api/import` with a path below `<archive>/imports/`):

1. A ZIP is extracted into `archive/tmp/` with zip-slip, encryption, size and compression-ratio
   checks.
2. `format` must be `heftig-export`; a newer `format_version` is refused.
3. **Every file listed in the manifest is verified by SHA-256 before anything is changed.** A
   single mismatch aborts the import.
4. The taxonomy is merged: missing terms are created, aliases added; an alias that already
   belongs to another term is counted as a conflict and skipped.
5. Documents are imported in `ingest_sequence` order. For each document:

   | Situation in the target | Result |
   |---|---|
   | Same ID, same SHA-256, same metadata (ignoring `revision`, `updated_at`, `status`) | `unchanged` |
   | Same ID, different metadata | conflict, **not overwritten** |
   | Same ID, different SHA-256 | conflict |
   | Different ID, same SHA-256 | conflict ("original already exists as document X") |
   | New | imported: original copied and re-verified, sidecar and text written as exported (`received_at`, locks, provenance, history unchanged), preview regenerated |

6. Sequence numbers: IDs, `ingest_sequence` and `filing_sequence` are kept. Only if a number is
   already used in the target (which can only happen when importing into a non-empty archive) is
   it replaced by the next free number (higher than every number in use); every renumbering is
   listed in the report. Into an empty archive, the exported order is reproduced exactly.
7. State: sequence counters are raised to the exported values; the global ingest event log is
   imported only if the target archive was empty; IMAP cursors and idempotency keys are merged
   (existing entries win). Users and API tokens are not imported (warning in the report).

Importing the same export twice is a no-op (`unchanged` for every document). The report
(printed as JSON by the CLI, stored as the job result in the UI) contains `imported`,
`unchanged`, `conflicts`, `renumbered`, `warnings` and taxonomy counters. The CLI exits with
code 3 if there were conflicts.

Documents are imported with the status they had at export time. A document exported while it
was still being processed is reported by `heftig check` as `unfinished_processing`;
`heftig repair` queues it again.

## Schema versions and migrations

| Item | Version field | Current |
|---|---|---|
| Database schema | `PRAGMA user_version` | 11 (number of the last migration) |
| `metadata.json` | `schema_version` | 1 |
| `text_pages.json` | `schema_version` | 1 |
| `taxonomy.json` | `schema_version` | 1 |
| Export | `manifest.json: format_version` (+ `metadata_schema_version`) | 1 |

Database migrations are numbered SQL files in `src/heftig/migrations/` (`0001_initial.sql`, ...).
Every process that opens the archive applies pending migrations in order at startup, each in its
own transaction together with the `user_version` update, so a failed migration leaves the
previous version intact. Downgrades are not supported: back up before upgrading
([operations.md](operations.md#upgrades-and-migrations)).

The sidecar formats are the long-term contract. Future versions must be able to read older
`schema_version`s (or migrate them explicitly); an import refuses exports from a newer format
version instead of guessing.


### `not_duplicate_of` (added 2026-09-28)

List of document IDs the user confirmed as *not* duplicates of this document ("keep both" on the
duplicate review page). Stored on both documents. Possible duplicates themselves are not stored in
sidecars: they are recomputed (`heftig duplicates --scan`) and kept in the database table
`duplicate_candidates` (migration `0002_duplicates.sql`).


### Notes and attachments (added 2026-09-28)

- `notes`: list of `{id, at, text, updated_at}` - the user's own comments. Never changed by
  processing or AI; indexed for search (column `notes`).
- `attachments`: list of `{id, filename, mime_type, size_bytes, sha256, relpath, added_at,
  description}`. Files are stored byte-identical and content-addressed next to the originals:
  `originals/attachments/<sha256[:2]>/<sha256>.<ext>`. The same file attached to several
  documents is stored once and removed only when the last reference is gone. Exports contain
  them under the same relative path; `heftig check` verifies their hashes.
