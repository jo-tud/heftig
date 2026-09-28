-- Possible content duplicates (different files, same document). Regenerable from the documents;
-- the user's "keep both" decision is stored in the sidecars (not_duplicate_of).
CREATE TABLE duplicate_candidates (
    doc_a      TEXT NOT NULL,
    doc_b      TEXT NOT NULL,
    score      REAL NOT NULL,
    reasons    TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'open',   -- open | kept_both
    created_at TEXT NOT NULL,
    decided_at TEXT,
    PRIMARY KEY (doc_a, doc_b)
);
CREATE INDEX idx_duplicates_status ON duplicate_candidates(status);
