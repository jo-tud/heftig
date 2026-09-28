-- Search index gains a "notes" column (user notes + attachment names/descriptions).
-- FTS5 tables cannot be altered: recreate them; the app rebuilds the index on start.
DROP TABLE doc_vocab;
DROP TABLE doc_fts;
CREATE VIRTUAL TABLE doc_fts USING fts5(
    doc_id UNINDEXED,
    ident,
    title,
    correspondent,
    doctype,
    tags,
    custom,
    filename,
    dates,
    summary,
    body,
    notes,
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE VIRTUAL TABLE doc_vocab USING fts5vocab(doc_fts, row);
INSERT INTO meta(key, value) VALUES('index_rebuild_required', '1')
    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
