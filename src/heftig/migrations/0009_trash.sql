-- Papierkorb: deleted documents stay restorable for a while. Their sidecar folder moves to
-- archive/trash/<id>/ (authoritative, this table is rebuilt from it); the original stays in
-- originals/ until the document is purged.
CREATE TABLE trash (
    id               TEXT PRIMARY KEY,
    sha256           TEXT NOT NULL,
    original_relpath TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    trashed_at       TEXT NOT NULL,
    reason           TEXT NOT NULL DEFAULT '',
    batch            TEXT,                 -- one bulk deletion = one batch (restore together)
    metadata_json    TEXT NOT NULL
);
CREATE INDEX idx_trash_batch ON trash(batch);
CREATE INDEX idx_trash_trashed ON trash(trashed_at);
