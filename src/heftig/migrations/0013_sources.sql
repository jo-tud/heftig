-- Source documents: the documents that were combined into another one stay in the archive for
-- good. Like the Papierkorb, their sidecar folder moves out of documents/ (to sources/<id>/)
-- and they get a row in the trash table, marked kind = 'source'; they are never purged.
ALTER TABLE trash ADD COLUMN kind TEXT NOT NULL DEFAULT 'trash';
CREATE INDEX idx_trash_kind ON trash(kind, trashed_at);
