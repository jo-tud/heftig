# Using Heftig

How the parts fit together in daily use. Setup and operation: [operations.md](operations.md).

## The daily routine

1. **Documents come in** – from the scanner folder, the mailbox, the phone camera (*Add → Scan*)
   or an upload (*Add*). When uploading, choose *digital document* for born-digital files or
   *paper* for a scan or photo of paper you still have to file.
2. **Heftig reads and sorts them** in the background: text from the PDF or by OCR, then title,
   date, sender, type, tags, amounts. The badge on *Inbox* counts what needs you.
3. **The inbox** lists the tasks: documents to review (an uncertain date, AI suggestions, a
   failed text recognition), possible duplicates, and paper still to file. *Reviewed → next*
   walks through them one by one; *Skip* leaves a document for later, *Trash → next* deletes it
   and goes on (with *Undo* on the next document).
4. **Correct what's wrong** on the document page. A field is saved as soon as you leave it (or
   pick a value); *Saved · Undo* appears next to it, and *Undo* restores the previous value,
   its lock and the AI's suggestions. Nothing is saved while you type, and links and buttons
   on the page wait for a save still running. Every field you edit is locked – reprocessing
   never overwrites it. Suggestions of the AI are accepted or dismissed there (*Dismiss* only
   removes the suggestion; the field keeps its value), or on *Suggestions in bulk* (linked
   from the inbox) for many documents at once.
5. **File the paper** and click *Filed just now* on its page (or *Filed now* in the inbox list) –
   or let Heftig do it for every scan (*Settings → Scanner and phone → I file every scanned letter
   right away*).

Nothing is ever lost by a click: deleted documents go to the trash for 30 days, combining and
bulk deletion can be undone.

## Paper without archive numbers

Paper goes into binders, one section per month, newest letter always **on top** of the current
month's section. Heftig knows the **current binder** (*Settings → Binders*, first called
"Binder 1"; rename it to what you write on its spine). When you mark a letter as filed, Heftig
records the binder, the section and the order; the document page then shows "binder Heftig 1,
section 2026-09, position 3 from the top" and the letters directly above and below it. The
section is the month you *filed* the letter, not its date.

The everyday cases:

- **Binder full:** *Settings → Binders → This binder is full – start the next one* (the name
  is suggested: "Heftig 1" → "Heftig 2"). From then on everything goes into the new binder;
  positions in the full one stay as they are.
- **Taking a letter out** (lending it, handing it to the tax adviser): *Taken out …* on its
  page, optionally with where it is. It keeps its place; *Put back in its place* returns it.
- **Moving a letter to another binder:** *Into another binder …* – it goes on top of that
  binder's current section.
- **Deleting a filed document:** the undo bar says where its paper lies ("take the paper out of
  binder Heftig 1: section 2026-09, position 3 from the top"). *Leave it in the binder* keeps
  the sheet's place counting instead, so the positions of the other letters stay right – also
  after the trash is emptied; such sheets are listed under *Settings → Binders*. When you keep
  one of two duplicates and the deleted one was filed, the kept copy takes over its place.
- **Paper that is never filed** (a referral left at the doctor's): *Not kept*, in the inbox
  list or on the document page.
- **Paper somewhere else entirely** (an old binder you don't file into): *Somewhere else …*
  with a note.

Three dates are kept apart:

| Field | Meaning | Set by |
|---|---|---|
| Document date | The date printed on the letter. May be empty, with a reason ("not found", "uncertain"). | The classifier (only if it appears in the text) or you |
| Received | When it first arrived in Heftig. Never changes. | Heftig |
| Filed | When the paper went into the binder. Empty until you confirm it. | You, or automatically for scans |

## Digitising existing binders (batches)

Before scanning a stack from an old binder, start a **batch** in the inbox: name the binder
("Insurance") and choose once what happens with the paper afterwards:

- **back into the binder** – every scan remembers "binder Insurance" as the paper's place;
- **into the Heftig filing** – at the end one click files all of them into the current binder,
  in scan order;
- **away, except what matters** – the AI suggests per document whether to keep the original
  (contracts, certificates, assessments, policies …); the inbox lists the few to keep with their
  position in the stack, and one click files those and marks the rest as discarded.

Every paper document that arrives while the batch runs belongs to it; the binder's name also helps
the classifier. A batch ends by itself after 8 hours without a new scan. Scanner settings for
this: [scanner-imap.md](scanner-imap.md#settings-for-digitising-old-folders).

## Blank pages

Scan everything duplex without sorting out the one-sided letters: the empty back sides are
recognised (almost no ink on the page, measured locally) and hidden in the page viewer – the
document header says e.g. *4 pages (2 blank)*, and a line above the pages says which ones are
hidden, with *show all pages*. There every page has a button at its bottom right: *Not blank –
show* for a page with something on it after all (a faint pencil note), *Blank – hide* for one
the detection missed. Your decision is kept in the sidecar and survives reprocessing. Hidden
pages are only hidden in the viewer: the original file stays as it was, their text stays
searchable, and a page with a search hit is always shown. Blank pages never go to a paid OCR,
and the thumbnail shows the first page that is not blank. Documents from before this existed
are checked once in the background (`heftig blank-pages` does it again).

## Duplicates and combining

The identical file never becomes a second document. The same letter in two forms – an e-mailed
PDF and a phone photo – is recognised after processing (similar text, same date and sender, same
invoice or contract number, same amount; a different date, amount, number or month named in the
text rules a pair out, so monthly bills are not flagged – "Sep" and "September" count as the same
month). The comparison shows both documents side by side, page by page,
with the differing areas marked – a signature, a stamp, a note that only one copy has – and
recommends which to keep. Keys: ← keep left, → keep right, B keep both, N skip. Truly identical
copies (the same text, every page visually the same) are resolved automatically; the copy with
your notes, attachments or corrections stays.

**Combining.** Pages of one letter that arrived as separate files, or two copies that belong
together (unsigned and signed): *Combine with another document …* on the document page (neighbours
in scan order and similar documents are suggested), or Z in the comparison. Heftig makes one PDF
with all pages in the chosen order, without re-encoding, keeps the recognised text (no second
paid text recognition) and puts the parts in the trash as one batch – *Undo* restores them.

## Finding things

![Search with filters, counts and highlighted hits](screenshots/search.png)

- Just type: `telekom invoice`. All words must match; if nothing does, Heftig falls back to any
  word and says so. Words match as prefixes (`insur` finds *insurance*).
- Case, umlauts and spacing in numbers don't matter: `Müllerstraße` = `muellerstrasse`,
  `83729381` also finds `8372 9381`. Numbers match exactly and rank higher in fields such as a
  contract number.
- Typos are corrected when a word occurs nowhere in the archive; the result says which
  correction was used.
- Date phrases become a filter: `invoice March 2025`, `phone last year`, `since 2023`,
  `last 3 months`, `between January and March 2025` (German works too: `Rechnung März 2025`,
  `letztes Jahr`). A bare `2025` stays a search word.
- `"exact phrase"` in quotes, and fields: `correspondent:"Harbor Bank"`, `type:Invoice`,
  `tag:Insurance`, `year:2025`, `received:2026-09`, `source:scanner`.
- The filter column shows counts per sender, type, tag and source, a timeline, date shortcuts,
  filing state and ranges for amounts. On a phone the filters open as a sheet.
- *✦ AI* (when an AI is set up) turns a question into filters – “phone bills over $50 last
  year” – and shows them as chips you can remove. Only the question and the names of your
  categories are sent, never document contents.
- Opening a result jumps to the page with the hit and marks the words. Each document lists
  similar ones.

How ranking works: [search.md](search.md).

## Privacy mode

On by default (per device; toggle in the top bar or `Shift+P`). It blurs amounts, IBANs, card
numbers, contract and customer numbers, custom field values and values after words like
"password" or "PIN", plus all page images. Tap a blurred item to reveal just that item. It
protects against someone looking over your shoulder – the data is still sent to the browser.

## Phone camera

*Add → Scan* is a document camera for phones: it finds the edges of the sheet, straightens it,
collects several pages and uploads them as one PDF. Over HTTPS the preview runs in the page and
takes the photo by itself once the sheet lies still; over plain HTTP the phone's camera app opens
instead and the corners can be adjusted by hand. Add the page to the home screen to open Heftig
directly on the camera. HTTPS without effort: [operations.md](operations.md#https).
