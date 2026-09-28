# Search

The search is local and deterministic: SQLite FTS5 with BM25 ranking, per-field weights and a
small, bounded typo correction. It never calls a network service or a language model (the
optional AI search only turns a question into the filters of this search, see
[providers.md](providers.md#what-is-sent)). The code
is in `src/heftig/search.py` (queries) and `src/heftig/index.py` (index rows).

## Tokenisation and German folding

Both the indexed text and the query go through the same normalisation (`textnorm.fold`):

1. Unicode NFC, then case folding (`ß` becomes `ss`, `MÜLLER` becomes `müller`).
2. German umlauts are transliterated: `ä` -> `ae`, `ö` -> `oe`, `ü` -> `ue`.
3. All other diacritics are removed (`é` -> `e`).

So `Müllerstraße`, `Muellerstrasse` and `MUELLERSTRASSE` are the same word. A token is a run of
letters and digits; everything else separates tokens (`Beispielstadt-Süd` -> `beispielstadt`,
`sued`). The FTS5 table uses the `unicode61` tokenizer with `remove_diacritics 2` on the already
folded text.

A query word that contains separators (`Allianz-Versicherung`, `DE12-3456`) is searched as a
phrase of its parts. Words broken by hyphenation at a line end in OCR text (`Rech-` / `nung`)
are additionally indexed in joined form, so `Rechnung` finds them.

## Indexed fields and weights

Each document is one row in the FTS5 table with separate columns, so matches can be weighted by
where they occur. BM25 weights (`index.COLUMN_WEIGHTS`, higher = more important):

| Column | Weight | Content |
|---|---|---|
| `ident` | 12.0 | Document ID (with and without hyphens), SHA-256, and every number-like token from custom field values, title and file name, including separator-free variants |
| `title` | 10.0 | Title |
| `correspondent` | 8.0 | Correspondent name plus all its aliases |
| `custom` | 7.0 | Custom fields as `key value` |
| `doctype` | 6.0 | Document type plus aliases |
| `tags` | 6.0 | Tags |
| `dates` | 4.0 | Document date, received date and filing date as `YYYY`, `YYYY-MM`, `YYYY-MM-DD` (tokenised into year, month and day numbers) |
| `filename` | 3.0 | Original file name |
| `summary` | 2.5 | Summary |
| `body` | 1.0 | Full extracted text, plus separator-free variants of numbers in the text |

The effect: a match in the title, correspondent, type or tags beats the same word somewhere in the
body text, and an exact contract number stored as a custom field beats a letter that merely
mentions it.

**Tiers before BM25.** Weights alone let a long letter that mentions a word many times (or a
rare compound) outrank the document that *is* what was searched for. Results are therefore
ordered in tiers first, BM25 within a tier:

1. all words (in the forms below) in the document's own description - title, correspondent,
   type, tags, custom fields, IDs, dates, notes;
2. the same, counting compounds that end in a word (`Einkommensteuerbescheid` for `steuerbescheid`);
3. all words somewhere, including the text;
4. only via compounds in the text.

`steuerbescheide` thus lists the tax assessments themselves before letters that mention one.

**Year boost.** Years are very common tokens, so BM25 gives them little weight. If the query
contains a bare year (`19xx` or `20xx`, not inside a phrase), documents whose **document date**
lies in that year get their score multiplied by `search.YEAR_BOOST` = 1.5 (BM25 scores are
negative in SQLite, lower is better, so this improves their rank). This is why
`Allianz Versicherung 2025` puts the 2025 insurance document from Allianz above a 2023 one and
above a ticket that mentions the "Allianz Arena".

## Numbers versus words

| Token | Matching |
|---|---|
| contains a digit (`83729381`, `DE12`, `2025`) | exact token only; `8372` does not match `83729381` |
| shorter than 3 characters | exact token only |
| other words | prefix match: `versich` finds `Versicherung`, `rechnung` finds `Rechnungsbetrag` |
| words of 6+ letters | also their base form and compounds ending in them (see below) |

**German word forms and compounds.** A prefix search misses two things in German: a query word
that is longer than the stored form (`steuerbescheide` vs. `Steuerbescheid`, `verträge` vs.
`Vertrag`) and compounds that end in it (`Einkommensteuerbescheid`, `Stromrechnung`,
`Kaltmiete`). For letter-only words of at least 6 letters the search therefore also looks for
the base form (one ending `-en`, `-es`, `-e`, `-n`, `-s` removed; an umlaut in the last syllable
turned back: `kontoauszüge` → `kontoauszug`), and adds up to 40 indexed words that end in the
word or a base form (optionally inflected), found in the index vocabulary. Highlighting, "found
in" and the page hits use the same rule. Shorter words stay plain prefixes (`bad` does not
become `ba*`). The suggestion list offers a type/correspondent/tag filter for the base form too
(`rechnungen` → type `Rechnung`).

**Separator-free variants.** Numbers are often printed with spaces, dots, slashes or hyphens.
For every run of 2 to 6 number-like parts separated by a single space, `.`, `/` or `-`, the
index also contains the concatenated form: `8372 9381` and `8372-9381` add `83729381`,
`DE12 3456 78` adds `de12345678` (and shorter combinations). Only numbers from **structured
metadata** (custom field values, title, file name) go into the high-weight `ident` column.
Variants of numbers that occur only in the body text are appended to the `body` column, so such a
document is still found, but ranked below the document whose metadata contains the number.

A query that is exactly a document ID (UUID) or a SHA-256 (64 hex digits, any case) returns that
single document directly, marked as an exact match.

## Query processing

1. Exact ID / SHA-256 check (see above).
2. Date phrases ("März 2025", "letztes Jahr" ...) become a document-date filter (see below).
3. Parse the query: field syntax becomes filters, `"..."` becomes a phrase, everything else
   becomes search terms. Unknown prefixes such as `Vertragsnr:83729381` are treated as plain
   text. An unbalanced quote is reported and the quotes are ignored. At most 12 terms are used
   (reported if more were given).
4. Build the filters. Invalid values (e.g. `year:zwanzig`, an unknown source) return an
   explanatory message and no results instead of an error. Values are always passed as SQL
   parameters, and search terms are reduced to folded letter/digit tokens before they are put
   into the FTS5 expression, so neither SQL nor FTS5 syntax can be injected.
5. Typo correction for terms that match nothing (below).
6. Search with **all** terms (AND). If that finds nothing and there is more than one term, search
   again with **any** term (OR) and tell the user ("Nicht alle Suchbegriffe kommen gemeinsam
   vor - zeige Teiltreffer.").
7. Rank, sort and paginate; build snippets and match reasons.

Each result contains ID, title, document date (and its status), received date, ingest sequence,
correspondent, document type, tags, source, status, text status, MIME type, page count, paper and
filing information, the BM25 rank, a highlighted snippet (HTML-escaped, matches in `<mark>`) and
the **reasons**: the fields that matched (`Nummer/ID`, `Titel`, `Korrespondent`, `Dokumenttyp`,
`Tag`, `Zusatzfeld`, `Dateiname`, `Datum`, `Zusammenfassung`, `Text`).

## Typo correction

Correction is a fallback for words that occur nowhere, not a fuzzy search:

- Only single-word terms that contain no digit and have at least 4 characters are candidates.
- A term is corrected only if it matches **nothing** in the index vocabulary (for words of 3+
  characters: no indexed token starts with it). If any document contains the word, or a longer
  word starting with it, nothing is corrected.
- Candidates are the indexed tokens (from all columns, including OCR text) that start with the
  **same first letter**, contain no digit, and differ in length by at most the allowed distance.
  They are read with a range query on the FTS5 vocabulary table, not by scanning documents.
- The allowed edit distance (Levenshtein, bounded computation) is 1 for words up to 5
  characters and 2 for longer words. The best candidate is the one with the smallest distance,
  then the one occurring in the most documents, then alphabetical order.
- Corrections are reported (`corrections: [{"from": "telekomm", "to": "telekom"}]` in the API;
  the UI shows "Gesucht mit korrigierten Begriffen: telekomm -> telekom"). The API also reports `fuzzy.candidates_checked` and
  `fuzzy.ms`, so the cost is measurable; the acceptance test requires fewer than 200 checked
  candidates for the test corpus.

Limits: a typo in the first letter is not corrected; a swapped pair of letters counts as two
edits; phrases and numbers are never corrected; a misspelled word that happens to exist in some
document (e.g. as an OCR error) or is the prefix of an existing word is not corrected.

## Field syntax

| Syntax | German alias | Effect |
|---|---|---|
| `correspondent:Name`, `correspondent:"Name with spaces"` | `korrespondent:`, `von:` | Correspondent, matched against names and aliases (normalised). Several are combined with OR. |
| `type:Rechnung` | `typ:` | Document type (names and aliases). Several: OR. |
| `tag:Versicherung` | - | Tag. Several: all must be present (AND). |
| `year:2025` | `jahr:` | Document date in that year. Documents without a document date are excluded. |
| `received:2026`, `received:2026-09`, `received:2026-09-15`, `received:2026-01..2026-06` | `eingang:` | Received date (inclusive range; either side of `..` may be empty). |
| `source:email` | `quelle:` | `scanner`, `folder`, `web`, `api`, `email`, `import`. |

An unknown name in `correspondent:`, `type:` or `tag:` yields no results and the note that the
name is not known. Field syntax and free text can be mixed:
`correspondent:"Telekom Deutschland GmbH" year:2026 rechnung`. Nobody needs the syntax: the
filter panel offers the same and more.

Note the difference between `year:2025` (a hard filter on the document date) and a bare `2025`
(a search term that matches dates, numbers and text, with the year boost).

## Filters

The web UI's filter panel and the API (`GET /api/documents`) accept:

| Parameter | Meaning |
|---|---|
| `q` | query text as above |
| `correspondent`, `document_type` (repeatable) | any of the given terms |
| `tag` (repeatable) | all given tags (or any, see `tag_mode`) |
| `date_from`, `date_to` | document date range (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`) |
| `received_from`, `received_to` | received date range, independent of the document date |
| `source` (repeatable) | input source |
| `status` (repeatable) | `queued`, `processing`, `done`, `needs_review`, `failed` |
| `filed` | `yes` (paper filing confirmed) or `no` (paper, not yet filed) |
| `filing_section` | e.g. `2026-09` |
| `cf_key`, `cf_min`, `cf_max` | numeric range on a custom field, e.g. `Betrag` between 40 and 50 |
| `tag_mode` | `all` (default) or `any` |
| `literal` | `1`: do not interpret date phrases in `q` |
| `sort` | `relevance`, `received`, `document_date`, `title` |
| `page`, `per_page` | pagination (`per_page` max. 100) |

All filters and the query are combined with AND. The UI shows active filters as removable chips
with a reset link.

## Sorting and tie-breaks

| Sort | Order |
|---|---|
| `relevance` (default when the query has search terms) | BM25 rank (with year boost), then `received_at` DESC, then `ingest_sequence` DESC |
| `received` (default without search terms) | `received_at` DESC, `ingest_sequence` DESC |
| `document_date` | document date DESC (documents without date last), then `received_at` DESC, `ingest_sequence` DESC |
| `title` | title (case-insensitive) ASC, then `received_at` DESC, `ingest_sequence` DESC |

`ingest_sequence` is unique, so every order is total and pagination is stable: paging through the
results returns every document exactly once.

## Indexing and rebuild

The index is updated incrementally in the same transaction as every document change
(ingestion, processing, edit, filing, taxonomy rename/merge/alias change for all affected
documents). A full rebuild is available with `heftig reindex` or on the settings page, and
`heftig rebuild-db` rebuilds the index together with the document tables from the sidecars. The
test suite checks that both rebuilds return exactly the same results for the acceptance queries.

## Date phrases

German date phrases in the query become a document-date range and are removed from the search
text (`heftig/datephrases.py`). Recognised, case-insensitive, first phrase only:

| Example | Range |
|---|---|
| `März 2025`, `Maerz 2025`, `Jan. 2026` | that month |
| `ab/seit Mai 2024`, `bis/vor/nach Mai 2024` | open ranges around the month |
| `letztes Jahr`, `vorletztes Jahr`, `dieses Jahr` | calendar year |
| `letzten/diesen Monat`, `letztes Quartal`, `letzte Woche` | calendar month / quarter / week (Mon-Sun) |
| `letzte 3 Monate`, `in den letzten 30 Tagen`, `vergangenen 2 Jahre` | from today back |
| `seit 2023`, `ab 2023`, `bis 2023`, `vor 2020`, `nach 2020` | open year ranges |
| `von 2021 bis 2023`, `2021 bis 2023` | year span |
| `im Jahr 2019`, `Jahrgang 2019` | that year |

Phrases inside `"quotes"` are left alone, and a bare year (`2025`) stays a search term (with the
year boost). The result page names the interpretation and links to the same search with
`literal=1`, which searches the words as text. The timeline and date shortcuts replace a
recognised phrase instead of combining with it.

## Facets and timeline

`search(..., with_facets=True)` (UI always, API with `facets=true`) returns counts over the
current results: correspondents, document types, tags, sources and documents per month of the
document date (plus the number without a date). Groups that combine with OR - correspondent,
type, source, date and tags in "any" mode - are counted without their own filter, so the other
values of the group stay visible with the number of documents they would add. Tags in "all" mode
narrow the result and are counted within it. Every facet value is a plain link that toggles the
parameter, so the filter column works without JavaScript; on narrow screens it opens as a bottom
sheet (search.js). The timeline shows years, and months after choosing one year.

`tag_mode=any` switches the tag filter from "all tags" to "at least one".

## Suggestions while typing

`GET /api/suggest?q=` (`search.suggest`) returns, in this order:

- correspondents, document types and tags whose name or alias starts with the input (or has a
  word starting with it) and that are used by documents; looked up for the whole input, else its
  last two words, else the last word. `replace` names the part of the input a chosen value
  replaces - the UI then turns it into a filter chip and keeps the rest as text;
- numbers from custom fields containing the typed digits (separators ignored) - opens the document;
- a recognised date phrase (information only);
- up to four matching documents (the normal search, only if all words match).

## Saved and recent searches

Saved searches are a name plus the query string of the search page (known parameters only),
stored in `saved_searches.json` at the archive root - not in SQLite, so they survive
`rebuild-db` and are included in backups and exports (merged on import, by query). API:
`GET/POST /api/searches`, `DELETE /api/searches/{id}`. Recent searches are kept per device in
the browser's `localStorage` only.

## Hits inside a document and similar documents

Result links carry `?q=`. The document page counts hits per page in the extracted text
(`search.highlight_terms` applies the same date-phrase removal and typo correction as the
search) and the page viewer jumps to the first page with a hit.
`GET /documents/{id}/pages/{n}/hits?q=` returns the boxes of matching words, normalised to
0..1: from the PDF text layer when the page has one, otherwise from Tesseract's word boxes on
the rendered page (`heftig/wordboxes.py`, computed on first request, one run at a time, cached in
`documents/<id>/cache/words-p<n>.json`, regenerable). Without Tesseract there are no marks, the
jump still works.

"Similar documents" (`search.similar`, `GET /api/documents/{id}/similar`) takes the up to 12 most
distinctive words of a document (TF-IDF over the index vocabulary, numbers with 6+ digits
included), searches for any of them with BM25 and ranks the same correspondent (x1.6) and type
(x1.2) higher. Local and deterministic like the rest of the search.

## Semantic search

Not part of V1. There are no embeddings and no fake "semantic" results. The extension point is
the `Embedder` protocol (`embed(texts) -> vectors`) in `src/heftig/providers/base.py`; a later
version can add a separate, explicitly selected search mode with its own vector index. The
normal search described here will stay local, deterministic and model-free.

## AI search (✦ AI)

Next to *Search*, the button *✦ AI* (or Ctrl+Enter) sends the request to the classification
provider and turns it into the filters of the ordinary search: "phone bills 2024 over $50"
becomes the senders of your phone providers, type Invoice, document date 2024 and Amount >= 50.

- **What is sent:** the request, today's date, and the archive's correspondent/type/tag names
  (with aliases) and custom field keys - the same lists the classification sends. Never document
  contents. With Anthropic the list part is cached; the model is `HEFTIG_AI_SEARCH_MODEL`
  (default `claude-haiku-4-5-20251001`, about a second, a fraction of a cent per search).
- **Checked, not trusted:** names must exist in the archive (aliases are resolved, unknown names
  are reported as "not in the archive"), dates must be valid, the amount field must exist.
- **Transparent:** the result is an ordinary search URL; the recognised filters appear as chips
  that can be removed, a banner shows the model's one-sentence reading and offers to search
  without AI. Reloading or going back does not ask the model again.
- **Cost:** every AI search is recorded in the AI cost overview (task `search`).
- Offered only when the classification provider is Anthropic or OpenAI-compatible and cloud use
  is allowed; the local rule classifier has no AI search.
