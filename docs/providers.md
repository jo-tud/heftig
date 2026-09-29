# OCR and classification providers

Heftig separates two AI-related tasks, each with its own provider, model, endpoint, API key and
cloud permission:

- **OCR / text extraction** (`HEFTIG_OCR_*`): only for pages without a usable embedded text layer
  (image-only PDF pages, photos, TIFF scans).
- **Classification** (`HEFTIG_CLASSIFY_*`): title, document date, correspondent, document type,
  tags, summary and custom fields, from the extracted text.

Uploading, storing and searching never depend on either. With no provider at all, documents are
archived and searchable by their embedded text and filename, and all metadata can be entered by
hand.

The easiest way to choose is the page *Settings → Change AI* (`/settings/ai`, also part of the
first setup): offline (Tesseract + rules), Anthropic, OpenAI or a local/own server. It sets the
classification and, if you tick it, AI text recognition with the same provider, asks for the
consent before anything goes to a cloud service, tests the connection and lists the models of
an endpoint. Everything below can also be set with environment variables, which then take
precedence (the page shows them as fixed).

## Available providers

**OCR** (`HEFTIG_OCR_PROVIDER`)

| Provider | Where it runs | Notes |
|---|---|---|
| `tesseract` (default) | local | Tesseract command-line tool. Needs the `tesseract` binary and language data (`HEFTIG_OCR_LANGUAGES`, default `deu+eng`). Included in the container image. |
| `none` | - | No OCR. Image pages get the note "OCR is turned off". |
| `openai_compatible` | local or cloud | Any server with an OpenAI-style `/chat/completions` endpoint and image input (Ollama, LM Studio, llama.cpp server, vLLM, hosted services). Local or cloud is decided by the base URL. |
| `openai` | cloud | OpenAI API (`https://api.openai.com/v1` unless `HEFTIG_OCR_BASE_URL` is set). |
| `anthropic` | cloud | Anthropic Messages API via the official SDK (`anthropic` package: extra `.[anthropic]`, included in the container image). |
| `mock` | local | Test double: returns "MOCK OCR Seite N", can fail chosen pages (`HEFTIG_MOCK_FAIL=ocr:2`). |

**Classification** (`HEFTIG_CLASSIFY_PROVIDER`)

| Provider | Where it runs | Notes |
|---|---|---|
| `rules` (default) | local | Deterministic patterns for German and English letters: first plausible date (`15.09.2026`, `15. September 2026`, `September 15, 2026`, ISO), correspondents and tags that already exist in your taxonomy (incl. aliases), document type from a keyword list (Rechnung/Invoice, Mahnung/Reminder, Vertrag/Contract, Bescheid/Tax assessment, Payslip, ...; named in the installation's language), contract/customer/invoice numbers and amounts (`39,95 EUR`, `Total due $39.95`). Never invents new correspondents. A useful baseline without any AI. |
| `none` | - | No classification; title stays the file name. |
| `openai_compatible` | local or cloud | As above; see [JSON modes](#json-modes). |
| `openai` | cloud | OpenAI API. |
| `anthropic` | cloud | Anthropic Messages API with structured output (JSON schema). |
| `mock` | local | `rules` plus failure injection (`HEFTIG_MOCK_FAIL=classify`) for tests. |

For `openai`, `openai_compatible` and `anthropic` set the model name with `HEFTIG_OCR_MODEL` /
`HEFTIG_CLASSIFY_MODEL`. `openai` and `openai_compatible` have no default model; `anthropic`
defaults to `claude-opus-5-5`. Model names are passed through unchanged, so any model your endpoint
offers can be used. For OCR the model must accept images.

## Cloud gating

Sending documents to a cloud service requires an explicit switch per task, both off by default:

```sh
HEFTIG_ALLOW_CLOUD_OCR=true        # page images may be sent to the configured OCR provider
HEFTIG_ALLOW_CLOUD_CLASSIFY=true   # document text may be sent to the configured classifier
```

A provider counts as cloud if it is `openai` or `anthropic`, or `openai_compatible` with a base
URL that is **not** local. A URL is local if its host is `localhost`, ends in `.localhost` or
`.local`, or resolves only to loopback, private (RFC 1918, IPv6 ULA) or link-local addresses. A
host name that cannot be resolved counts as cloud.

If a cloud provider is configured without its switch, it is not called at all: OCR pages get the
error "Cloud OCR is not allowed (set HEFTIG_ALLOW_CLOUD_OCR=true ...)", classification adds a
review reason, the job is not
retried, and the settings page shows the provider as blocked. The settings page, the inbox and
every document page show provider, model and target host for both tasks, and the document page
warns before a reprocess sends data to a cloud provider. The processing history of each document
records provider, model, target host and adapter/prompt version of every run.

Counted as local: `localhost`, `*.localhost`, `*.local`, `*.internal` (e.g.
`host.docker.internal`, `host.containers.internal`), loopback, private (RFC 1918), link-local
and carrier-grade NAT addresses (`100.64.0.0/10`, used by Tailscale). Everything else - including
host names that do not resolve - counts as cloud and needs the allow switch.

## Capabilities

The pipeline renders PDF pages to PNG images itself, so an OCR provider only needs to accept
images; PDFs are never uploaded to a provider. If an `openai`/`openai_compatible` endpoint
cannot take images (a text-only model), set

```sh
HEFTIG_OCR_SUPPORTS_IMAGES=false
```

and Heftig will not try OCR with it; affected pages are marked with a clear error instead of
failing with an obscure HTTP error. Use such an endpoint for classification only.

## JSON modes

Classifier responses must be one JSON object following a fixed schema (see
`src/heftig/providers/prompt.py`). For `openai` and `openai_compatible`,
`HEFTIG_CLASSIFY_JSON_MODE` selects how that is requested:

| Mode | Request | Use for |
|---|---|---|
| `schema` (default) | `response_format: {type: json_schema, strict: true, schema: ...}` | OpenAI and servers with structured-output support |
| `object` | `response_format: {type: json_object}`, schema in the system prompt | servers that support JSON mode but not schemas |
| `none` | no `response_format`, schema in the system prompt, "answer with a single JSON object" | servers that reject `response_format` |

The parser tolerates Markdown code fences and text around the JSON object. The Anthropic adapter
always uses structured output (`output_config.format` with the JSON schema). Whatever the mode,
the result is validated field by field (see [below](#validation-rules)).

## Example: local models with Ollama

Run Ollama on the same machine, pull a vision-capable model for OCR and a chat model for
classification (model names are placeholders; pick what fits your hardware):

```sh
ollama pull <local-vision-model>
ollama pull <local-chat-model>
```

Native installation:

```sh
HEFTIG_OCR_PROVIDER=openai_compatible
HEFTIG_OCR_BASE_URL=http://localhost:11434/v1
HEFTIG_OCR_MODEL=<local-vision-model>
HEFTIG_CLASSIFY_PROVIDER=openai_compatible
HEFTIG_CLASSIFY_BASE_URL=http://localhost:11434/v1
HEFTIG_CLASSIFY_MODEL=<local-chat-model>
HEFTIG_CLASSIFY_JSON_MODE=schema      # try object or none if the server rejects it
HEFTIG_PROVIDER_TIMEOUT_SECONDS=600   # local models on a CPU can be slow
```

No API key and no cloud switch are needed: `localhost` is local.

From inside a container, `localhost` is the container itself. Use the host's address instead:

- Docker: `http://host.docker.internal:11434/v1` (mapped by `extra_hosts` in `compose.yaml`);
- Podman: `http://host.containers.internal:11434/v1`.

Ollama listens only on `127.0.0.1` by default, which a container cannot reach. Start it with
`OLLAMA_HOST=0.0.0.0` (or the bridge/LAN address) and make sure the host firewall does not
expose port 11434 beyond your network. Both host names resolve to private addresses inside the
container, so the endpoint counts as local.

## Open and local models: what to know

Heftig talks to any server with an OpenAI-style `/chat/completions` endpoint and needs nothing
provider-specific. What makes a difference in practice:

- **Model size.** Classification needs a model that follows a JSON schema reliably and reads
  German or English letters well; in our experience that starts around 7–14 billion parameters.
  For OCR the model must accept images (a vision model).
- **Context length.** A classification request is about 8,000–10,000 tokens (text, your category
  names, examples). Ollama's default context is much smaller and it silently cuts the beginning
  of the prompt – start Ollama with `OLLAMA_CONTEXT_LENGTH=16384` (or more). With small models
  lower `HEFTIG_CLASSIFY_MAX_CHARS` (e.g. 12000) and `HEFTIG_OCR_MAX_SIDE` (e.g. 1600).
- **JSON mode.** `schema` (default) works with Ollama, llama.cpp, vLLM and LM Studio. If a server
  rejects it, use `object`, and `none` as the last resort (LM Studio does not accept `object`).
  An answer that is not valid JSON is asked for once more.
- **Reasoning models** (Qwen, DeepSeek and others that "think" first): the thinking part
  (`<think>…</think>`) is removed before the answer is read. They need more output tokens and
  time; raise `HEFTIG_PROVIDER_TIMEOUT_SECONDS` (e.g. 600) on slow hardware.
- **Speed.** Pages go to a local server one at a time (a cloud service gets several in parallel);
  the AI search waits for a local model as long as the timeout allows.
- **Local or cloud** is decided by the address: `localhost`, private networks, `.local` names and
  Tailscale addresses count as local and need no permission. A hosted service such as OpenRouter
  counts as cloud: switch on the permission, and see what is sent below.
- **Costs** are estimated only for models with a known list price (currently Claude). For other
  models the inbox shows the number of AI calls without a price instead of a misleading $0.
- Anthropic-only settings (`HEFTIG_ANTHROPIC_OCR_EFFORT`, `HEFTIG_OCR_FIRST_PAGE_MODEL`,
  `HEFTIG_AI_SEARCH_MODEL`) are ignored for other providers; the AI search uses the
  classification model there.

## Example: cloud providers

Anthropic for both tasks, key read from a file:

```sh
HEFTIG_OCR_PROVIDER=anthropic
HEFTIG_OCR_MODEL=claude-opus-5-5
HEFTIG_OCR_API_KEY_FILE=/run/secrets/anthropic_key
HEFTIG_ALLOW_CLOUD_OCR=true

HEFTIG_CLASSIFY_PROVIDER=anthropic
HEFTIG_CLASSIFY_MODEL=claude-opus-5-5
HEFTIG_CLASSIFY_API_KEY_FILE=/run/secrets/anthropic_key
HEFTIG_ALLOW_CLOUD_CLASSIFY=true
```

`HEFTIG_ANTHROPIC_OCR_EFFORT` (default `low`) is passed as `output_config.effort` for OCR calls;
set it to an empty string to use the model's default. Native installations need the extra:
`uv tool install '.[anthropic]'`.

OpenAI (or any OpenAI-compatible cloud service via `openai_compatible` + `*_BASE_URL`):

```sh
HEFTIG_CLASSIFY_PROVIDER=openai
HEFTIG_CLASSIFY_MODEL=<model-name>
HEFTIG_CLASSIFY_API_KEY_FILE=/run/secrets/openai_key
HEFTIG_ALLOW_CLOUD_CLASSIFY=true

HEFTIG_OCR_PROVIDER=openai
HEFTIG_OCR_MODEL=<vision-capable-model>
HEFTIG_OCR_API_KEY_FILE=/run/secrets/openai_key
HEFTIG_ALLOW_CLOUD_OCR=true
```

A common middle ground is local OCR (Tesseract) with cloud classification: then only extracted
text leaves the machine, never images.

After changing providers (on the settings page no restart is needed; environment variables need
one), use *Reprocess* (per document, selection
or all, separately for OCR and classification) to apply them to existing documents. Locked
fields and your tag decisions are kept.

## What is sent

| Task | Sent to the provider | Not sent |
|---|---|---|
| OCR | One PNG image per page **without** a usable embedded text layer (rendered at `HEFTIG_OCR_DPI`, default 300), a fixed transcription prompt with page number and expected languages. | Pages that already have embedded text, the original file, file name, metadata, other documents. |
| Classification | The extracted text (at most `HEFTIG_CLASSIFY_MAX_CHARS` = 24000 characters: first 80 % and last 20 %), the original file name, page count, the names and aliases of all your correspondents, document types and tags, the existing custom field keys, and title, correspondent, type and date of up to 8 similar documents (as naming pattern). | The original file, images, document IDs, dates of arrival, other documents' text. |
| Title harmonisation (only when started on the page "Consistent titles") | Per group of correspondent + document type: the correspondent and type names, and per document its current title, document date and whether the title is locked. | Any document text, file names, IDs, custom fields, notes. |
| AI search (only when you press "✦ AI") | Your question, today's date, and the names and aliases of your correspondents, document types and tags plus the custom field keys. | Any document content. The search itself then runs locally. |

The taxonomy is sent so the model can reuse existing names instead of inventing variants. Keep
in mind that it reveals which organisations you deal with. The normal search never calls any
provider; only the optional AI search does, as described above.

## API keys

- `HEFTIG_OCR_API_KEY` / `HEFTIG_OCR_API_KEY_FILE` and `HEFTIG_CLASSIFY_API_KEY` /
  `HEFTIG_CLASSIFY_API_KEY_FILE`: separate settings per task (both may point to the same file).
  The direct value wins over the file.
- Prefer the `*_FILE` variants (container secrets, a file with mode 0600). The file is read when
  the provider is created.
- Keys are only sent as the authentication header to the configured endpoint. They never appear
  in logs, error messages, the processing history, exports or the UI; the settings page only
  shows whether a key is configured.
- Never commit keys. `.env` is in `.gitignore`.

## Raw responses

The raw classifier response is stored in the database table `processing_runs` for diagnosis,
truncated to `HEFTIG_RAW_RESPONSE_MAX_KB` (64 KB), and deleted by the worker after
`HEFTIG_RAW_RESPONSE_RETENTION_DAYS` (30 days). The parsed result (applied, suggested and
dropped fields) is kept. Raw responses are not part of sidecars or exports. OCR output is not
stored separately; it becomes the document text.

## Validation rules

The model's answer is treated as a proposal and checked field by field
(`src/heftig/classify.py`). The rule is: **never invent, never override the user.**

| Field | Rule |
|---|---|
| any locked field | Never changed. Locked by editing it (or ticking the lock) and by accepting a suggestion. |
| `title` | String, cleaned of control characters, max. 200 characters, then normalised: quarters become `Q3 2020` ("3. Quartal 2020", "III. Quartal", "Q3/2020", "2020 Q3"), a leading year moves to the end ("2023 Steuerbescheid" -> "Steuerbescheid 2023"). |
| `document_date` | Must be a valid ISO date between 1900 and next year **and** backed by `document_date_evidence` that occurs verbatim in the document text (case, whitespace and umlaut spelling are ignored) and denotes the same date (`15.09.2026`, `2026-09-15`, `15.09.26`, `15. September 2026`). Unbacked -> only a suggestion ("Date not found word for word in the text"), the date stays empty. Backed with confidence >= `HEFTIG_CLASSIFY_MIN_CONFIDENCE` (0.6) -> applied (status `ai`); lower -> applied as `ai_uncertain` and flagged for review. `null` -> status `none_found`. |
| `correspondent`, `document_type` | A name matching an existing term or alias (normalised) is mapped to the canonical name and applied if confident, else suggested. An unknown name that looks like a near-duplicate of an existing term (same after dropping legal forms such as GmbH/AG/e.V., one name a word-prefix of the other, or edit distance 1-2) is **not** created: both the existing and the new name become suggestions and the document is flagged "Sender: a similar name already exists" (or document type). A genuinely new name with sufficient confidence is created automatically; otherwise suggested. |
| `tags` | Up to 8. Existing tags/aliases are reused; near-duplicates become suggestions; new tags are created. Tags you removed manually stay removed, tags you added stay added. |
| `summary` | String, max. 1200 characters. |
| `custom_fields` | Each needs a key, a type (`string`, `number`, `monetary`, `date`), a non-empty value and `evidence` that occurs verbatim in the text. Numbers are parsed in German and English notation; `monetary` needs an ISO currency code or `€`/`EUR` in the evidence; `string` values must themselves appear in the text (separators ignored). Anything else is dropped. Keys are matched to existing keys case- and umlaut-insensitively. |

Previous suggestions are replaced by each classification run. Dropped values are listed in the
processing history ("dropped: ..."). Suggestions are shown on the document page and can be
accepted (sets and locks the field) or dismissed.

The prompt tells the model that the document text is untrusted data and must not be followed as
instructions; see [architecture.md](architecture.md#security-model).

## Consistent titles

The prompt prescribes one naming scheme (`<kind> [<subject>] <sender> [<period>]`, e.g.
"Beitragsbescheid TK 2025" or "Payslip Acme March 2025"): the sender in the short form people
use ("TK", "ADAC", "Telekom", no legal forms such as GmbH), left out only for private
individuals; the period last as `Q3 2020`, `March 2025`, `2025` or `2024/2025`; no full dates,
IDs or file-name fragments. It passes the titles of the most similar documents already in the
archive (`search.similar`, up to 8, only documents with an AI, user or imported title), so
documents of the same kind get the same wording. Examples and month names follow the
installation's language.

For titles that already exist, **Settings -> Consistent titles** (`/titles`) starts a
background job (`titles`): per group of correspondent + type, only titles, dates and the two names
go to the classification provider in batches of up to 80 titles (method `complete_json`, JSON
schema `HARMONIZE_SCHEMA`); the page shows the number of requests and an estimated cost first.
The same scheme applies, and the sender's name stays in every title of the group. The answers
are normalised and stored as proposals (`title_proposals`, temporary); a proposal that would drop
a sender name the current title contains (the correspondent's name or one of its aliases) is
discarded. Nothing changes until you accept them; accepted titles are ordinary user edits
(locked). Locked titles are sent as fixed examples and never changed, and a title edited in the
meantime is never overwritten. Needs an AI classifier (`anthropic`, `openai`,
`openai_compatible`).

## Quality

Tesseract is a solid offline baseline for clean, printed, single-column letters. It is clearly
weaker than current vision models on handwriting, poor photos, low-contrast scans, stamps,
tables and multi-column layouts, and OCR errors directly reduce search quality. Typical choices:

- privacy first: Tesseract + `rules`, correct metadata by hand;
- good quality without cloud: a local vision model via `openai_compatible` (needs a capable GPU
  or patience);
- best quality: a cloud vision model, with the explicit cloud switches.

Pages that already contain a text layer (most born-digital PDFs) are never OCRed, so their text
quality does not depend on the provider.


## Costs, speed and rate limits

- **Costs:** every AI call records its token usage and an estimated cost at list prices
  (`processing_runs.input_tokens/output_tokens/cost_usd`, prices in
  `src/heftig/providers/pricing.py`). The inbox shows today / 7 days / total. The provider's
  invoice is authoritative.
- **Model per page:** with Anthropic, `HEFTIG_OCR_FIRST_PAGE_MODEL` reads page 1 (letterhead,
  sender, date, subject - what the classification relies on) with a stronger model and the
  other pages with `HEFTIG_OCR_MODEL`; e.g. Opus for page 1 and Sonnet for the rest costs a
  little more than Sonnet alone and about half of Opus for everything.
- **Images:** pages are rendered at `HEFTIG_OCR_DPI` and sent at most `HEFTIG_OCR_MAX_SIDE`
  pixels long (default 2000; providers downscale larger images anyway) as PNG, or JPEG when the
  PNG would exceed ~3.5 MB (noisy scans) - providers reject images above ~5 MB.
- **Parallel pages:** `HEFTIG_OCR_PAGE_CONCURRENCY` (default 3) pages of a document are read at
  the same time, on top of `HEFTIG_WORKER_CONCURRENCY` documents.
- **Page cache:** recognised pages are kept in `documents/<id>/cache/ocr-p<n>.json` (keyed by
  file hash, provider, model and adapter). A retry, restart or rate limit never pays for a page
  twice; *Reprocess* with text recognition clears it.
- **Blank pages and large documents:** before a page goes to a paid OCR provider its ink is
  measured (40 dpi, margins trimmed). Pages below `HEFTIG_OCR_BLANK_MAX_INK` (default 0.001 =
  0.1 %: blank duplex back sides, filler pages, a lone footer line) are read by the local
  Tesseract instead (free; nothing is lost if there is a line on it). At most
  `HEFTIG_OCR_AI_MAX_PAGES` (default 30) pages per document go to the AI; further pages are read
  locally and the document shows a note with a button "Recognize all pages with AI". Files above
  `HEFTIG_MAX_PAGES` (500) or `HEFTIG_MAX_UPLOAD_MB` (100) are rejected at intake.
- **Rate limits** (HTTP 429) are not treated as "unreachable": the job waits
  (`retry-after`, else `HEFTIG_RATE_LIMIT_PAUSE_SECONDS`, default 90) without using up an
  attempt, and nothing falls back to local processing.
- **Rejected page image** (a permanent error such as HTTP 400): with the local fallback enabled,
  that page is read by Tesseract and the document gets a review note; it is not queued for a
  paid AI retry.

**Prompt caching (Anthropic).** The classification prompt starts with a part that is the same
for every document of the archive - the instructions, all correspondents/types/tags with their
aliases and the custom field keys (several thousand tokens in a grown archive). This part is sent
as a separate block marked for caching; the document's own part (similar titles, file name,
text) follows. Within a few minutes the API reads the cached part at a tenth of the input
price (writing it once costs 1.25x), so bulk imports and scan sessions pay for it about once.
The block changes only when a category is added or renamed. Cost estimates price cache reads
and writes accordingly.

## When the AI provider is unreachable

With `HEFTIG_AI_FALLBACK=true` (default) a transient provider error (no internet, timeout, server
error - not a rate limit, see above) does not leave the document waiting: OCR continues with the local Tesseract
and classification with the local rules, so the document is searchable at once. The document is
marked (`ai_pending` in its sidecar, hint on the document page and in the inbox, where the time
since the provider became unreachable is shown). Every `HEFTIG_AI_RETRY_MINUTES` (default 60) the
worker checks reachability with a request that contains no document data (listing the provider's
models) and, once it answers, queues up to `HEFTIG_AI_CATCH_UP_BATCH` documents for AI processing.
Locked fields and manual tag decisions are respected as always. Permanent errors (e.g. a rejected
request) do not trigger the fallback and are shown for review. Documents that already have a queued or running
job are not queued again, the catch-up waits while a large import is queued, and a permanent
error during the catch-up removes the pending mark - so nothing is paid for twice or in a loop.
