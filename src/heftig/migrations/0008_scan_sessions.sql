-- Scan sessions: a stack of paper from one existing folder, scanned in one go. The session
-- rows are operational state; what matters per document (session, where the paper is) lives
-- in the sidecar and is mirrored into the columns below for filtering.
CREATE TABLE scan_sessions (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    mode             TEXT NOT NULL,          -- folder | refile | sort
    started_at       TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    ended_at         TEXT,                   -- no new documents join after this
    closed_at        TEXT                    -- paper decisions done / dismissed
);
ALTER TABLE documents ADD COLUMN scan_session_id TEXT;
ALTER TABLE documents ADD COLUMN paper_location TEXT;
ALTER TABLE documents ADD COLUMN paper_discarded_at TEXT;
CREATE INDEX idx_documents_session ON documents(scan_session_id, ingest_sequence);
