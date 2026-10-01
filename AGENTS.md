# Working on Heftig (for coding agents and people)

Heftig is a self-hosted document archive: FastAPI + Jinja2 (server-rendered HTML), SQLite with
FTS5, vanilla JavaScript, no build step. Read `docs/architecture.md` before larger changes.

## Map

| Where | What |
|---|---|
| `src/heftig/ingest.py` | every document enters here (hash, type check, original stored, job queued) |
| `src/heftig/mail.py`, `mailpdf.py` | e-mails (`.eml`) as documents: reading them, their pages (rendered, plus the attachments' pages; `media.compose_mail`) |
| `src/heftig/processing.py` | the job: text extraction (PDF text, OCR), classification, review reasons |
| `src/heftig/classify.py` | validates what a classifier returns before anything is applied |
| `src/heftig/providers/` | OCR and AI adapters (Tesseract, rules, Anthropic, OpenAI-compatible), prompts |
| `src/heftig/documents.py` | loading/saving a document (`persist`: sidecar + DB row + index), user edits |
| `src/heftig/search.py`, `expand.py`, `synonyms.py`, `datephrases.py` | search: query parsing, what a word stands for (forms, compounds, similar spellings, synonyms), date phrases, FTS5, ranking, facets, suggestions; quality measured by `searcheval.py` and `tests/search_bench.py` (docs/search.md) |
| `src/heftig/semantic.py`, `local_embed.py` | the optional search by meaning: pieces, vectors, fusion with the word search; the built-in ONNX model |
| `src/heftig/binders.py`, `titles.py`, `duplicates.py`, `trash.py`, `combine.py` | paper filing in named binders, consistent titles, duplicates, trash, combining documents |
| `src/heftig/web/ui.py`, `web/templates/` | the HTML pages; `web/api.py` the REST API; `web/setup.py` setup and connection pages, their "Test" buttons in `connections.py` |
| `src/heftig/worker.py` | background loop: scanner folder, IMAP, jobs, embedding for the search by meaning, hourly maintenance |
| `src/heftig/config.py`, `settings_store.py` | settings: environment variables win over values saved in the web interface |
| `src/heftig/i18n.py`, `locale/de/messages.po` | interface languages |
| `src/heftig/migrations/` | SQL migrations, applied in order on start |
| `tests/` | pytest; `conftest.py` has `archive`, `make_settings`, `ingest_bytes`, `process_all` |

## Rules that are easy to break

- **The files are the truth.** A document is `archive/documents/<id>/metadata.json` + text; the
  database is an index that `heftig rebuild-db` recreates. Change data only through
  `documents.persist` (inside `db.write_tx`), never with SQL alone.
- **Originals are never modified.** Anything derived (thumbnails, word boxes) goes to the cache.
- **Fields the user edited are locked** (`field_locks`); reprocessing must not overwrite them.
- **Interface texts are English in the code**, wrapped for translation: `_("…")` in Python and
  templates, `t("…")` in JavaScript, `ngettext` for counts, named placeholders `%(name)s`.
  Add the German translation to `src/heftig/locale/de/messages.po`
  (`uv run python scripts/i18n.py update de` adds new texts; `… check de` must report nothing
  missing). Never call `_()` at import time – use `N_()` and translate when shown.
- **Stored texts are English** (review reasons, errors); they are translated when displayed
  (`| reason` filter, `i18n.translate_text`).
- **No inline scripts or style attributes** – the Content Security Policy forbids them. Put code
  in `web/static/*.js`, styles in `app.css`.
- **Nothing leaves the machine unless the user switched it on** (cloud AI needs an explicit
  permission per task). Search never calls a network service (the model of the search by
  meaning runs locally; only the worker downloads it, once, after the user switched it on).
- **One worker per archive.** Jobs are leased; long work must renew its lease.

## Checks

```sh
make test      # offline, about a minute
make lint      # ruff check + format check + i18n check
uv run python scripts/i18n.py check de
uv run python scripts/demo_archive.py /tmp/demo && HEFTIG_ARCHIVE_DIR=/tmp/demo uv run heftig run
```

Tests run with `language="de"` by default (most assertions check German texts); English output
is covered in `tests/test_i18n.py`.
