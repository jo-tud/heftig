-- Document IDs and hashes leave the word index (fragments matched ordinary words):
-- rebuild the index on the next start.
INSERT INTO meta(key, value) VALUES('index_rebuild_required', '1')
    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
