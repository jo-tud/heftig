# Migrating to Paperless-ngx

Heftig's export is an open format ([data-format.md](data-format.md#export-format)), so you are not
locked in. This page maps Heftig's fields to [Paperless-ngx](https://docs.paperless-ngx.com/) and
describes a small migration script. A migration is **not lossless**: some Heftig data has no
Paperless equivalent, and Paperless recomputes some values itself.

## Field mapping

| Heftig | Paperless-ngx | Notes |
|---|---|---|
| original file (`originals/...`) | uploaded document (original) | Byte-identical upload. Paperless keeps the original and additionally creates its own archived PDF/A version. Paperless identifies originals by MD5 (`checksum`) and rejects duplicates. An archived e-mail is uploaded as its `.eml` file; Paperless reads e-mails only with its Tika and Gotenberg integration switched on. |
| `title` | `title` | Paperless allows at most 128 characters; the script truncates. An empty Heftig title becomes the file name without extension. |
| `correspondent` | `correspondent` | Looked up by name, created if missing. |
| `document_type` | `document_type` | Looked up by name, created if missing. |
| `tags` | `tags` | Looked up by name, created if missing. |
| `document_date` | `created` | Sent as `YYYY-MM-DD`. If Heftig has no document date, Paperless determines `created` itself (from the content or the file). |
| `received_at` | `added` (conceptually) | **Known loss.** Paperless sets `added` to the time of the upload; `post_document` has no parameter for it. It could only be corrected afterwards directly in Paperless (API or database, depending on your Paperless version); the script does not do this. The script prints `received_at` in its dry-run output. |
| text (`text.md`) | `content` | **Not transferred.** Paperless runs its own OCR on every upload, so its `content` will differ from Heftig's text (better or worse, depending on the engines). |
| `custom_fields` | custom fields | Not uploaded by the script. Create the fields in Paperless and fill them afterwards if needed. Type mapping: `string` -> String, `number` -> Float (or Integer), `monetary` -> Monetary (Paperless format: currency code + amount, e.g. `EUR123.45`), `date` -> Date, `boolean` -> Boolean. |
| taxonomy aliases | - | Paperless has no aliases. Its matching rules (`match`, `matching_algorithm`) are a different concept; the script creates new terms with matching disabled (`matching_algorithm: 0`), so Paperless does not start auto-assigning them. |
| `filed_at`, `filing_sequence`, `filing_section`, `filing_binder`, `paper_location` (paper position) | - | No equivalent. Suggestion: a custom field "Filing" (e.g. `Binder 1 / 2026-09 / 14`) or a note. Paperless's archive serial number (ASN) is an integer, but it is usually meant to be printed on the paper, which Heftig deliberately avoids. |
| `ingest_sequence`, `source`, `source_details`, `ingest_events` | - | No equivalent. Suggestion: a note, or a string custom field "Heftig-ID" with the document ID so you can trace back to the export. |
| `field_locks`, `field_sources`, `suggestions`, `document_date_status`, `processing_history` | - | No equivalent; Paperless has its own history of changes. |
| `status`, `review_reasons` | inbox tags (conceptually) | Paperless marks new documents with its configured inbox tag(s); open review items are not transferred. |
| `summary` | - | Could go into a note. |
| `notes`, `attachments` | - | Not transferred. Notes could go into Paperless notes; attached files would have to be uploaded as documents of their own. |

## Migration script

`contrib/paperless/heftig_to_paperless.py` is a small standalone script (Python 3.11+, only
`httpx` as a dependency). It reads an **unzipped** Heftig export directory and, for each
document in ingest order:

1. verifies the original's SHA-256 against `metadata.jsonl` (a mismatch is reported, the document
   is skipped);
2. computes the original's MD5 and asks Paperless `GET /api/documents/?checksum__iexact=<md5>`;
   documents Paperless already has are skipped, so the script can be re-run after an
   interruption or partial failure;
3. looks up correspondent, document type and tags by name
   (`GET /api/correspondents/?name__iexact=...` etc.) and creates missing ones
   (`POST`, `matching_algorithm: 0`);
4. uploads the original with `POST /api/documents/post_document/` including `title`, `created`,
   `correspondent`, `document_type` and `tags`.

Authentication uses a Paperless API token (`Authorization: Token ...`), which you can create in
the Paperless web UI (profile / API auth token). Configuration comes from the environment:
`PAPERLESS_URL` and `PAPERLESS_TOKEN`.

**Status: the script has only been tested against a mocked Paperless-ngx API**
(`tests/test_paperless_script.py`), not against a real Paperless instance. Try it on a test
instance first, with `--dry-run` and then with `--limit`.

### Example

```sh
# 1. Export from Heftig (native installation)
heftig export ./exp
#    in a container: docker compose exec worker heftig export /archive/exports
#    (the export then appears in ./archive/exports/ on the host)

# 2. See what would be uploaded (no network access; prints one JSON object per document)
PAPERLESS_URL=https://paperless.example.org PAPERLESS_TOKEN=<token> \
  python contrib/paperless/heftig_to_paperless.py ./exp/heftig-export-20260928-120000 --dry-run

# 3. Upload the first three documents to a test instance, check them in Paperless
PAPERLESS_URL=https://paperless.example.org PAPERLESS_TOKEN=<token> \
  python contrib/paperless/heftig_to_paperless.py ./exp/heftig-export-20260928-120000 --limit 3

# 4. Upload everything (already uploaded documents are skipped)
PAPERLESS_URL=https://paperless.example.org PAPERLESS_TOKEN=<token> \
  python contrib/paperless/heftig_to_paperless.py ./exp/heftig-export-20260928-120000
```

In the repository you can run the script with `uv run python contrib/paperless/...`, which
provides `httpx`. The script prints one line per document and a JSON summary (planned,
uploaded, skipped, errors, created terms) to stderr; the exit code is 1 if any document failed.
A ZIP export must be unzipped first.

Paperless consumes uploads asynchronously: a successful upload only means the file was accepted.
Check Paperless's task list for documents that failed to consume, fix the cause and run the
script again; documents that did not make it are not known by checksum yet and will be uploaded
again.

The dry-run output also shows, per document, the Heftig data that has no Paperless field
(`not_mapped`: Heftig ID, `received_at`, ingest sequence, source, paper filing, custom fields,
locks). If you want to keep it, extend the script to write it into a note
(`POST /api/documents/<id>/notes/`) or custom fields once Paperless has consumed the document.
