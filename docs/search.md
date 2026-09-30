# Search

The search is local and deterministic: SQLite FTS5 with BM25 ranking and per-field weights, and
German word handling on top - word stems, compounds, other words for the same thing, and
spellings one or two letters away for OCR errors. It never calls a network service or a
language model (the optional AI search only turns a question into the filters of this search,
see [providers.md](providers.md#what-is-sent)). The code is in `src/heftig/search.py` (queries
and ranking), `src/heftig/expand.py` (what a search word stands for), `src/heftig/synonyms.py`
and `src/heftig/index.py` (index rows). How well it works is measured, see
[Measuring search quality](#measuring-search-quality).

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
| `notes` | 5.0 | Your notes, attachment names and descriptions |
| `summary` | 2.5 | Summary |
| `body` | 1.0 | Full extracted text, plus separator-free variants of numbers in the text |

The effect: a match in the title, correspondent, type or tags beats the same word somewhere in the
body text, and an exact contract number stored as a custom field beats a letter that merely
mentions it.

**Levels and tiers before BM25.** A search word stands for more than itself (next section):
its word forms, other words for the same thing, similar spellings. Weights alone would let a
long letter full of such words, or a rare compound, outrank the document that *is* what was
searched for. Results are therefore ordered like Meilisearch's ranking rules - each step only
decides between documents the steps before left equal:

1. **Words found** - only when not all words occur anywhere (see query processing): documents
   with more of the search words first.
2. **Tier** - where and how the words were found:
   1. all words, as typed, in the document's own description - title, correspondent, type,
      tags, custom fields, IDs, dates, notes;
   2. the same with their word forms and compounds (`Einkommensteuerbescheid` for
      `steuerbescheid`, `Kind` for `Kindern`);
   3. the same with other words for them and compound parts (`Betriebskosten` for
      `Nebenkosten`, `Strom` + `Rechnung` for `Stromrechnung`);
   4. - 6. the same three, somewhere including the text;
   7. only through similar spellings (OCR errors).
3. **BM25** with the column weights, computed over the words of the document's tier only (a
   letter full of compounds does not beat the one with the word itself), multiplied by:
   - 2 when the **title** has every search word (in some form);
   - 1.5 when the words stand **close together** (within 10 words, FTS5 `NEAR`);
   - 1.5 when a year in the query is the **document's year** (see below).
4. Received date, then ingest order, newest first.

`steuerbescheide` thus lists the tax assessments themselves before letters that mention one,
and `Allianz Versicherung` puts the letter from Allianz above a flyer that has "Allianz Arena"
in one paragraph and "Versicherung" in another.

**Year boost.** Years are very common tokens, so BM25 gives them little weight. If the query
contains a bare year (`19xx` or `20xx`, not inside a phrase), documents whose **document date**
lies in that year, or whose **title** contains it, get the factor `search.YEAR_BOOST` = 1.5.
This is why `Allianz Versicherung 2025` puts the 2025 insurance document from Allianz above a
2023 one, and `Steuerbescheid 2023` finds the "Einkommensteuerbescheid 2023" issued in 2024.

## What a search word stands for

| Token | Matching |
|---|---|
| contains a digit (`83729381`, `DE12`, `2025`) | exact token only; `8372` does not match `83729381` |
| shorter than 3 characters | exact token only |
| other words | prefix match: `versich` finds `Versicherung`, `rechnung` finds `Rechnungsbetrag` |

On top of that, letter-only words are looked up in the **index vocabulary** (the FTS5 `fts5vocab`
table - the words that occur in the archive), at query time. Nothing is added to the index, and
a changed rule needs no reindexing. The results are cached until a document changes.

**Word forms** (tier level 2). German inflects: the query word can be longer than the stored
form (`steuerbescheide` vs. `Steuerbescheid`), or differ by an umlaut (`Ärzte` - `Arzt`,
`Häusern` - `Haus`, `Verträge` - `Vertrag`). Two rules cover this:

- a light suffix rule for words of 6+ letters (one ending `-en`, `-es`, `-e`, `-n`, `-s`
  removed; an umlaut in the last syllable turned back: `kontoauszüge` → `kontoauszug`), searched
  as a prefix like the word itself (level 1);
- every indexed word with the same **Snowball stem** (the German algorithm of the
  [snowballstemmer](https://snowballstem.org/algorithms/german/stemmer.html) package; it reads
  `ae`/`oe`/`ue` as umlauts, so the folded forms stem like the original ones): `Kindern` →
  `Kind`, `Kinder`, `Kindes`. Only words that share the stem exactly are added - `Kinder`
  does not become `Kindergarten` this way.

**Compounds** (level 2). German joins words: `Einkommensteuerbescheid`, `Stromrechnung`,
`Kaltmiete`. For words of 5+ letters the search adds up to 40 indexed words that **end** in the
word or one of its forms (optionally inflected), and, for words of 6+ letters, words that have
it **in the middle** with a word after the joint (`Versicherung` in
`Rentenversicherungsnummer`, `Schornsteinfeger` in `Bezirksschornsteinfegermeister` - but
`Miete` is not in `Vermieter`). Most frequent first.

**Compound parts** (level 3). The other direction: a compound that is rare or occurs nowhere
is split into two parts that are words of the archive, and a document with both parts matches
(`Stromrechnung` → `Strom` + `Rechnung` finds "Jahresabrechnung Strom"; `Müllgebühren` →
`Müll` + `Gebühren` finds "Abfallgebühren", because Müll/Abfall are synonyms). The split is the
one whose parts are most common in the archive, joints `-s-`, `-es-`, `-n-`, `-en-`, `-e-`,
`-er-` allowed, and only if the parts are more common than the whole word (Koehn & Knight, 2003).
A part counts as a word if it is indexed, has indexed words with its stem or has synonyms; the
first part also if it starts an indexed word whose rest is a word (`Müll` in `Müllabfuhr` needs
`abfuhr`), the last part if it starts an indexed word. Each part then stands for its forms,
compounds and synonyms. Hyphenated query words (`Kfz-Steuer`) are searched as a phrase and,
at this level, as their parts.

**Other words for the same thing** (level 3): see [below](#other-words-for-the-same-thing).

**Similar spellings** (level 4, last tier). Scans contain OCR errors - `Kündiqung`,
`Kaltrniete`, `Versicherunqsschein` - that no search word matches. Indexed words that start
with the same two letters and are at most one edit away (words of 5-8 letters) or two edits
(9+ letters) are added; swapped neighbouring letters and the OCR confusions `rn`/`m`, `cl`/`d`,
`vv`/`w` count as one edit (the same thresholds as Meilisearch). Numbers and words under five
letters are never matched this way.

**Function words.** Words like `die`, `vom`, `über`, `für`, `mit` (German and English, see
`expand.STOPWORDS`) are left out when the query has other words: `die Rechnung vom Zahnarzt`
searches `Rechnung Zahnarzt`. A quoted phrase keeps them.

**Quoted words** are searched as typed, without forms, compounds, synonyms or similar
spellings.

Highlighting, "found in" and the page hits use the same rules, so a match through a form, a
synonym or an OCR spelling is marked too. The suggestion list offers a type/correspondent/tag
filter for the base form (`rechnungen` → type `Rechnung`).

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
2. Date phrases ("March 2025", "last year", "März 2025" ...) become a document-date filter (see
   below).
3. Parse the query: field syntax becomes filters, `"..."` becomes a phrase, everything else
   becomes search terms; function words are left out. Unknown prefixes such as
   `Vertragsnr:83729381` are treated as plain text. An unbalanced quote is reported and the
   quotes are ignored. At most 12 terms are used (reported if more were given).
4. Build the filters. Invalid values (e.g. `year:zwanzig`, an unknown source) return an
   explanatory message and no results instead of an error. Values are always passed as SQL
   parameters, and search terms are reduced to folded letter/digit tokens before they are put
   into the FTS5 expression, so neither SQL nor FTS5 syntax can be injected.
5. Look up what each term stands for (forms, compounds, parts, synonyms, similar spellings);
   typo correction for terms that match nothing in any of these ways (below).
6. Search with **all** terms (AND), each in any of its forms. If that finds nothing and there
   is more than one term, search again with **any** term (OR) and tell the user ("Not all
   search terms occur together – showing partial matches."); documents with more of the words
   come first.
7. Rank, sort and paginate; build snippets and match reasons.

Each result contains ID, title, document date (and its status), received date, ingest sequence,
correspondent, document type, tags, source, status, text status, MIME type, page count, paper and
filing information, the BM25 rank, a highlighted snippet (HTML-escaped, matches in `<mark>`) and
the **reasons**: the fields that matched (`Number/ID`, `Title`, `Sender`, `Document type`, `Tag`,
`Custom field`, `File name`, `Date`, `Summary`, `Text`, `Note/attachment`; in the interface
language).

## Typo correction

Correction is a fallback for words that occur nowhere, not a fuzzy search (the similar
spellings above are that):

- Only single-word terms that contain no digit and have at least 4 characters are candidates.
- A term is corrected only if it matches **nothing**: no indexed word starts with it or one of
  its forms, no word has its stem, no compound contains it, it cannot be split into parts and
  has no synonym that occurs.
- Candidates are the indexed words (from all columns, including OCR text) that start with the
  **same first letter or one that sounds alike** (`v`/`f`/`w`, `c`/`k`/`z`, `d`/`t`, `b`/`p`,
  `g`/`k`, `i`/`j`/`y`, `s`/`z`), contain no digit and differ in length by at most the allowed
  distance. They are read with a range query on the FTS5 vocabulary table, not by scanning
  documents, and prefiltered by shared letter pairs.
- The allowed edit distance is 1 for words up to 5 characters and 2 for longer words; swapped
  neighbouring letters and `rn`/`m` count as one edit. The best candidate is the one with the
  smallest distance (a changed first letter counts one more), then the one occurring in the
  most documents, then alphabetical order.
- Corrections are reported (`corrections: [{"from": "telekomm", "to": "telekom"}]` in the API;
  the UI shows "Search term corrected: telekomm -> telekom"). The API also reports
  `fuzzy.candidates_checked` and `fuzzy.ms`, so the cost is measurable.

Examples: `Telekomm` → `telekom`, `Rechnugn` → `rechnung`, `Wodafone` → `vodafone`,
`Scornsteinfeger` → `schornsteinfeger`. Phrases and numbers are never corrected; a misspelled
word that happens to exist in some document is not corrected (but may still find the right one
through its similar spellings).

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
| `filing_binder` | name of a binder, e.g. `Binder 1` |
| `session` | ID of a scan batch |
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
| `relevance` (default when the query has search terms) | words found, tier, BM25 with title, proximity and year factors (see above), then `received_at` DESC, then `ingest_sequence` DESC |
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

## Other words for the same thing

People search with their word, letters use the sender's: `Nebenkosten` - "Betriebskosten",
`Handy` - "Mobilfunk", `Kfz-Steuer` - "Kraftfahrzeugsteuer", `Krankschreibung` -
"Arbeitsunfähigkeitsbescheinigung", `GEZ` - "Rundfunkbeitrag". A small built-in list of such
groups (`src/heftig/synonyms.py`, about 40 groups for household paperwork, plus a few English
words people type for German documents: `invoice` - `Rechnung`) is searched at tier level 3,
below the word itself. The groups are kept unambiguous on purpose: a synonym that is only
sometimes right would push wrong documents up. A search word belongs to a group when it has the
same stem as one of its entries (`Handys` → `Handy`); entries of fewer than five letters are
matched exactly (`Kfz`, not every word starting with "kfz"), longer ones as prefixes.

**Your own groups.** Settings → *Search: words that mean the same*: one group per line, words or
phrases separated by commas (`Kita, Kindergarten Sonnenschein`). They are stored in
`synonyms.json` at the archive root - not in SQLite, so they survive `rebuild-db` and are part
of backups and exports (merged on import). Changes apply to the next search. Names of senders,
types and tags have their own aliases (Categories); those are searched like the name itself.

## Measuring search quality

Search quality is measured, not guessed. `tests/search_bench.py` holds a synthetic German
household archive - 61 documents (bills, contracts, notices, pay slips, certificates; scans
with OCR errors and without metadata; a newsletter and an ad that mention many search words in
passing) - and 94 queries with judged results (grade 2: what was searched for, grade 1: also a
good answer, and documents that must not come first). A second set of 50 queries was written
after the rules, without looking at the results, to check that the rules are not fitted to the
first. `src/heftig/searcheval.py` computes:

| Measure | Meaning |
|---|---|
| MRR@10 | 1 / position of the first right document (0 if not in the first ten), averaged |
| Success@1, @5 | share of queries whose best document is first / on the first screen |
| nDCG@10 | all right documents by grade and position |
| Recall@10 | share of the right documents in the first ten |
| noise | documents marked wrong that come before the first right one |

`uv run python scripts/search_bench.py` prints every query (`--heldout` the second set, `-q
Kaltmiete` one query); `tests/test_search_quality.py` fails when the figures drop below a floor.

| | MRR@10 | Success@1 | Success@5 | no result |
|---|---|---|---|---|
| before (prefix, forms, compounds ending in the word, typo fallback) | 0.72 | 66 % | 78 % | 19 of 94 |
| now | 0.98 | 94 % | 99 % | 1 of 94 |
| held-out set, before → now | 0.90 → 0.94 | 90 % → 94 % | 90 % → 94 % | 5 → 2 of 50 |

The queries that still fail need knowledge of the world, not of words: `Wertpapiere` for an ETF
statement, `Elektriker` for a bill from "Elektro Schulz".

**Your own archive.** `heftig search-eval queries.json` runs your queries against your archive
and prints the same table. The file is a list of queries and the documents they should find,
named by title or by the start of their ID (8+ characters):

```json
[
  {"q": "Nebenkostenabrechnung 2023", "expect": ["Betriebskostenabrechnung 2023"]},
  {"q": "Kfz-Steuer", "expect": ["Kraftfahrzeugsteuerbescheid"], "also": ["3f2a91c0"]},
  {"q": "Hausrat", "expect": ["Beitragsrechnung Hausrat 2025"], "wrong": ["Werbung Hausrat"]}
]
```

`expect` are the documents searched for (grade 2), `also` other good answers (grade 1), `wrong`
documents that should not come before them. `--json` gives machine-readable output. Twenty to
fifty queries you really typed - especially the ones that disappointed you - are enough to see
whether a change helps.

**Compared with other systems.** Paperless-ngx (v3, Tantivy) indexes with a Snowball stemmer
and ranks with BM25 and a title boost; it does not split compounds (`Rentenversicherung` does
not find `Rentenversicherungsnummer`), has no proximity ranking beyond quoted phrases and no
synonyms, and its fuzzy search is off by default. Elasticsearch's German analysis (normalisation,
light stemmer, dictionary decompounder, synonyms, `fuzziness: AUTO`) and Meilisearch's ranking
rules (words, typo, proximity, attribute, exactness) are the models for the levels above; what
they add beyond that is semantic (vector) search.

## Date phrases

English and German date phrases in the query become a document-date range and are removed from
the search text (`heftig/datephrases.py`). Recognised, case-insensitive, first phrase only:

| Example | Range |
|---|---|
| `March 2025`, `Sep 2025`, `März 2025`, `Maerz 2025`, `Jan. 2026` | that month |
| `March to May 2025`, `between January and March 2025`, `von Nov. 2024 bis Feb. 2025` | month span |
| `since/after/before May 2024`, `ab/seit/bis/vor/nach Mai 2024` | open ranges around the month |
| `last year`, `this year`, `letztes Jahr`, `vorletztes Jahr`, `dieses Jahr` | calendar year |
| `last month`, `this quarter`, `last week`, `letzten Monat`, `letztes Quartal` | calendar month / quarter / week (Mon-Sun) |
| `last 3 months`, `past 30 days`, `letzte 3 Monate`, `in den letzten 30 Tagen` | from today back |
| `since 2023`, `until 2023`, `before 2020`, `after 2020`, `seit/ab/bis/vor/nach 2023` | open year ranges |
| `from 2021 to 2023`, `2021 to 2023`, `von 2021 bis 2023` | year span |
| `im Jahr 2019`, `Jahrgang 2019` | that year |

"2021 and 2023" (like "2021 und 2023") is not a span - both years stay search terms; "between
2021 and 2023" is one.

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

## Search by meaning (optional)

Words cannot find everything: `Wertpapiere` does not occur in an ETF statement, `Elektriker`
not in a bill from "Elektro Schulz". An embedding model can - it turns texts into vectors that
are close when the texts mean the same. Heftig offers this as a **separate, explicitly chosen
mode**; the normal search above stays local, deterministic and model-free.

**Setting it up.** Settings → *Search by meaning*: off (default), a model server on your network
(Ollama, LM Studio, llama.cpp - anything with an OpenAI-compatible `/v1/embeddings`; nothing
leaves your network) or OpenAI (`text-embedding-3-small`, 512 dimensions, about 2 US cents per
1,000 pages; needs the permission that document texts may be sent). Good local models for German
documents: `bge-m3`, `embeddinggemma`, `qwen3-embedding:0.6b` (`ollama pull bge-m3`). The same
with environment variables: `HEFTIG_EMBED_PROVIDER` (`none`, `openai`, `openai_compatible`),
`HEFTIG_EMBED_MODEL`, `HEFTIG_EMBED_BASE_URL`, `HEFTIG_EMBED_API_KEY(_FILE)`,
`HEFTIG_ALLOW_CLOUD_EMBED`.

**Preparing the documents.** The worker embeds every document in the background
(`src/heftig/semantic.py`): one piece with title, sender, type, tags, date and summary, then the
text in chunks of about 1,200 characters (at most 12), each starting with the title line. The
vectors are stored in SQLite (`doc_embeddings`), normalised, as 32-bit floats - derived data like
the word index, kept by `rebuild-db`, created again by the worker when they are missing or the
model changes. A document is embedded again only when the embedded pieces change (a hash of
them is kept), not when it is only filed or its status changes. Models that need task prefixes get them (`query:`/`passage:` for e5, `search_query:`
for nomic, the prompts of EmbeddingGemma and Qwen3). `heftig embed` does it right away; the
settings page shows how many documents are ready and the last error. Costs appear in the AI cost
overview (task `embed`, and `search` for each query).

**Searching.** On the results page, *≈ Search by meaning too* (URL parameter `meaning=1`, API
`meaning=true`) embeds the query - one call to the model - and compares it with every stored
chunk (cosine, in memory; no database extension). The 20 documents with the most similar chunk,
within the active filters, are merged with the word search's ranking by weighted Reciprocal Rank
Fusion (Cormack et al. 2009): score = 1 / (60 + word rank) + 0.5 / (60 + meaning rank). A
document found both ways comes first, one found only by meaning is added (reason "Meaning"),
and the word search's order counts double. When the model fails, the word search's results are
shown with the error.

**Measured** with `jinaai/jina-embeddings-v2-base-de` (German/English, 0.6 GB, on CPU; `uv run
python scripts/search_bench.py --meaning http://localhost:8765/v1 <model>` runs the benchmark
against any OpenAI-compatible server):

| | MRR@10 | Success@1 | Recall@10 | no result | time per query |
|---|---|---|---|---|---|
| main set, words only | 0.979 | 94 % | 96 % | 1 | ~4 ms |
| main set, words + meaning | 0.985 | 96 % | 100 % | 0 | ~120 ms |
| held-out set, words only | 0.940 | 94 % | 93 % | 2 | ~3 ms |
| held-out set, words + meaning | 0.990 | 98 % | 100 % | 0 | ~130 ms |

With equal weights (1 : 1) the main set dropped to 0.974: the model then pushed an ad for
insurance above the car insurance itself. The time is the embedding of the query on the
benchmark machine's CPU. Comparing it with 30,000 stored chunks (roughly 10,000 documents, 768
dimensions) takes about 20 ms with numpy (in the container image; `pip install
"heftig[semantic]"`) and about 1 s in pure Python without it.

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
- Offered only when the classification provider is Anthropic, OpenAI or OpenAI-compatible and
  may be used (a cloud service needs the cloud permission, a local server does not); the local
  rule classifier has no AI search. With OpenAI and OpenAI-compatible servers the classification
  model is used.
