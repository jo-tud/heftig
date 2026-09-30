-- Search by meaning (optional): vectors of an embedding model per document chunk. Derived
-- data like the word index; the worker fills it again when it is missing.
CREATE TABLE doc_embeddings (
    doc_id TEXT NOT NULL,
    chunk INTEGER NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,  -- normalised, as 8-bit integers: value = byte * scale
    scale REAL NOT NULL,
    PRIMARY KEY (doc_id, chunk)
);
CREATE TABLE doc_embed_state (
    doc_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL  -- the document's revision when it was embedded
);
