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

1. **Words found** - for questions (three words or more) and when no document has all the words
   (see query processing): documents with the **important** words first. Each word counts by
   how rare it is in the archive (IDF: "Amtsblatt" counts much more than "bekomme"), a word
   found only through another word or a similar spelling counts half, in steps of a tenth;
   within a step BM25 of the words themselves decides. Counting words instead of weighing
   them let everyday words outweigh the one word that matters.
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

**Function words.** Articles, pronouns, prepositions, conjunctions, question words and auxiliary
and modal verbs (`die`, `vom`, `über`, `wann`, `ich`, `wurde`, `kann` ... - about 290 German and
English words, see `expand.STOPWORDS`) are left out when the query has other words: `die
Rechnung vom Zahnarzt` searches `Rechnung Zahnarzt`, "Wann bekomme ich Bescheid, ob meine Kur
bewilligt wurde?" searches `bekomme Bescheid Kur bewilligt`. A quoted phrase or word keeps them.

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
6. One or two words (keywords): documents with **all** of them (AND), each as typed or in one of
   its forms; other words for them, compound parts and similar spellings add documents but do
   not count as "all words" - otherwise "Amt" + "Blatt" in one letter would hide the one with
   "Amtsblatt". If no document has all the words, search again with **any** of them (OR), rank
   by the important words found (see ranking) and tell the user ("Not all search terms occur
   together – showing partial matches."). Three words or more (a written-out question): always
   ranked this way, documents with all words first - a question rarely has all its words in
   the answer.
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

**Real questions.** Queries written for a benchmark by the person who wrote the rules flatter
them. So the whole search (not only a part of it) was also measured on questions real people
asked, each over its documents ingested as PDFs into an ordinary archive:

- **German:** 1,491 citizen questions to the City of Munich ("Wie beantrage ich einen
  Anwohnerparkausweis?") with the answering service article among 810 articles
  ([it-at-m/LHM-Dienstleistungen-QA](https://huggingface.co/datasets/it-at-m/LHM-Dienstleistungen-QA),
  MIT);
- **English:** 648 questions from a finance forum with the answering posts among 2,000 posts
  (FiQA-2018 as published in [BEIR](https://github.com/beir-cellar/beir)).

| | Munich: MRR@10 | first right | Recall@10 | FiQA: MRR@10 | first right | Recall@10 |
|---|---|---|---|---|---|---|
| before (main branch) | 0.511 | 45 % | 64 % | 0.433 | 33 % | 45 % |
| words (this page) | 0.723 | 64 % | 88 % | 0.477 | 37 % | 50 % |
| words + meaning (built-in model) | **0.876** | **81 %** | **97 %** | **0.737** | **65 %** | **70 %** |

Every setting was chosen on a part of the questions (the first two fifths) and checked on the
rest; the figures above are over all of them. The words gained most from ranking a question by
its important words (see ranking) and from leaving out function words; the meaning gained from
counting more for questions (see "Search by meaning"). Forum questions are harder for words:
answers written by other people rarely use the asker's words.

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
rules (words, typo, proximity, attribute, exactness) are the models for the levels above; the
search by meaning below is the hybrid (words + vectors) search they offer on top.

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

## Search by meaning

Words cannot find everything: `Wertpapiere` does not occur in an ETF statement, `Elektriker`
not in a bill from "Elektro Schulz". An embedding model can - it turns texts into vectors that
are close when the texts mean the same. Heftig has one built in; it runs on the computer Heftig
runs on and sends nothing anywhere.

**Switching it on.** The setup assistant asks ("Use the search by meaning?", suggested: yes);
Settings → Search by meaning switches it on and off; the environment variable is
`HEFTIG_SEMANTIC_SEARCH=true`. **Without the assistant it is off.** `HEFTIG_SEMANTIC_THREADS`
limits the CPU threads the model uses (default: half the cores).

**The model** is [Snowflake Arctic Embed M v2.0](https://huggingface.co/Snowflake/snowflake-arctic-embed-m-v2.0)
(multilingual, Apache-2.0), int8-quantised, run with onnxruntime and the Hugging Face tokenizer -
no torch, no model server. It is downloaded **once, when documents are prepared for the first
time** (about 330 MB from huggingface.co, a fixed revision, every file checked against its
SHA-256) into `<archive>/models/`. That folder is not part of backups or exports; it is
downloaded again when missing. While it works it needs up to about 850 MB of memory - in the
worker while documents are prepared, in the web process while searches use it; after ten minutes
without use it is unloaded and the memory given back (loading it again takes a second or two).
`heftig embed` downloads it and prepares all pending documents right away (for example before
going offline).

Chosen by measurement on this page's benchmark, on a notebook CPU (4 cores, 2 threads used):

| Model (ONNX) | Download | Meaning alone: MRR@10 | Success@1 | Words + meaning, held-out MRR | Chunks per second |
|---|---|---|---|---|---|
| **Arctic Embed M v2.0, int8** | 330 MB | **0.976** | **93 %** | **0.990** | 17 |
| jina-embeddings-v2-base-de, fp32 | 660 MB | 0.893 | 78 % | 0.990 | 9 |
| bge-m3, fp32 (reference, too large for a notebook) | 2,300 MB | 0.892 | 81 % | 0.990 | 4 |
| granite-embedding-107m-multilingual, fp32 | 440 MB | 0.878 | 79 % | 0.970 | 24 |
| multilingual-e5-small, int8 | 135 MB | 0.848 | 77 % | 0.970 | 34 |
| multilingual-e5-base, int8 | 295 MB | 0.823 | 72 % | 0.967 | 21 |
| granite-embedding-97m-multilingual-r2, fp32 | 415 MB | 0.781 | 71 % | 0.970 | 18 |
| granite-embedding-97m-multilingual-r2, int8 | 125 MB | 0.736 | 63 % | 0.970 | 30 |
| jina-embeddings-v2-base-de, int8 | 165 MB | 0.726 | 56 % | 0.967 | 32 |
| paraphrase-multilingual-MiniLM-L12-v2, int8 | 135 MB | 0.666 | 51 % | 0.970 | 40 |

"Meaning alone" ranks the 144 benchmark queries by the model only, without the word search -
the figure that separates the models; combined with the word search the differences shrink,
because the words already find most documents. A household archive of 2,000 documents
(about 6,000 chunks) is prepared in about six minutes.

**Preparing the documents.** The worker embeds archived documents in the background, in a
thread of its own, a few seconds after they are processed (`src/heftig/semantic.py`): one piece
with title, sender, type, tags, date and summary, then the text in chunks of about 1,200
characters (at most 12), each starting with the title line. The vectors are stored in SQLite
(`doc_embeddings`), normalised, as 32-bit floats - derived data like the word index, kept by
`rebuild-db`. A document is embedded again only when the embedded pieces change (a hash of them
is kept), not when it is only filed or its status changes. Settings → Search by meaning shows
how many documents are prepared and the last error (for example a failed download, retried
after ten minutes).

**Searching.** There is nothing to choose: once documents are embedded, **every search with
words** also embeds the query (about 10-20 ms) and compares it with every stored chunk (cosine,
numpy; about 20 ms per 30,000 chunks). Documents not embedded yet are found by their words as
before. Every query is similar to *something*, so a document counts as near only if its most
similar chunk
- is **similar enough**: a cosine of at least 0.22 - calibrated for the built-in model on the
  benchmark: 85 % of the right documents lie above it, 92 % of the others below; this finds
  several documents of one kind (all the electricity bills for "power bill"), or
- **stands out** from the rest of the archive: at least max(2, √(2 ln n) − 0.8) standard
  deviations above the average over all n documents (the largest of n random values lies about
  √(2 ln n) above it) and a cosine of at least 0.15 - a single clear match with a low absolute
  value ("Wertpapiere" → the ETF statement, 0.17),

and is not more than 0.15 below the best one. (With fewer than ten documents only the first rule
applies.) Up to 20 of them are, within the active filters,
merged with the word search's ranking by weighted Reciprocal Rank Fusion (Cormack et al.
2009): score = 1 / (k + word rank) + w / (k + meaning rank).

- **Keywords** ("Rechnung Telekom 2023"): k = 60, w = 0.5 - the word search's order counts
  double; a document found only by meaning is added below the good word matches.
- **Written-out questions** - a question mark, or function words among three words or more
  ("Wie beantrage ich einen Parkausweis?", "wann kommt der Bescheid für die Kur"): k = 5, w = 2
  - the meaning leads, the words move documents up a few places. A question's words are rarely
  the answer's words; with the keyword settings the flat k = 60 let every document found by
  words pass the best one found by meaning.

A document found only by meaning has the reason "Meaning". `meaning=0` in the URL (API:
`meaning=false`) searches by words only. Suggestions while typing use the words only.

**Measured** (benchmark with the built-in model; `uv run python scripts/search_bench.py
--meaning`):

| | MRR@10 | Success@1 | Recall@10 | no result |
|---|---|---|---|---|
| main set, words only | 0.979 | 94 % | 96 % | 1 |
| main set, words + meaning | 0.995 | 96 % | 98 % | 0 |
| held-out set, words only | 0.940 | 94 % | 93 % | 2 |
| held-out set, words + meaning | 0.990 | 98 % | 100 % | 0 |

With equal weights (1 : 1) a model pushed an ad for insurance above the car insurance itself.
Without the two rules every query - also "Rezept Apfelkuchen" in an archive without recipes -
brought 20 documents; with them, five of six such queries bring none and one brings one. The
absolute rule alone lost "Wertpapiere" (0.17), the "stands out" rule alone lost groups of
similar documents (eight electricity bills, none standing out from the others).

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
