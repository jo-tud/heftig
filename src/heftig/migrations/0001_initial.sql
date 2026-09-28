-- Heftig database schema v1.
-- The database is an index/cache for document data (authoritative copies live in the
-- sidecar files) and the primary store for operational state (jobs, auth, import cursors).

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE documents (
    id                TEXT PRIMARY KEY,
    sha256            TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    original_relpath  TEXT NOT NULL,
    mime_type         TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    page_count        INTEGER,
    source            TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    ingest_sequence   INTEGER NOT NULL UNIQUE,
    document_date     TEXT,
    filed_at          TEXT,
    filing_sequence   INTEGER UNIQUE,
    filing_section    TEXT,
    paper             INTEGER NOT NULL DEFAULT 0,
    title             TEXT NOT NULL DEFAULT '',
    correspondent_id  INTEGER REFERENCES taxonomy(id),
    document_type_id  INTEGER REFERENCES taxonomy(id),
    summary           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'queued',
    text_status       TEXT NOT NULL DEFAULT 'pending',
    review_reasons    TEXT NOT NULL DEFAULT '[]',
    revision          INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT NOT NULL,
    metadata_json     TEXT NOT NULL
);
CREATE INDEX idx_documents_received ON documents(received_at DESC, ingest_sequence DESC);
CREATE INDEX idx_documents_docdate ON documents(document_date);
CREATE INDEX idx_documents_filing ON documents(filing_section, filing_sequence);
CREATE INDEX idx_documents_status ON documents(status);

CREATE TABLE document_text (
    doc_id  TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL
);

-- kind: correspondent | document_type | tag
CREATE TABLE taxonomy (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    norm       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin     TEXT NOT NULL DEFAULT 'user',
    UNIQUE (kind, norm)
);

CREATE TABLE taxonomy_alias (
    kind       TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    alias      TEXT NOT NULL,
    term_id    INTEGER NOT NULL REFERENCES taxonomy(id) ON DELETE CASCADE,
    PRIMARY KEY (kind, alias_norm)
);

CREATE TABLE document_tags (
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES taxonomy(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, tag_id)
);
CREATE INDEX idx_document_tags_tag ON document_tags(tag_id);

CREATE TABLE custom_field_values (
    doc_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    type       TEXT NOT NULL,
    value_text TEXT,
    value_num  REAL,
    PRIMARY KEY (doc_id, key)
);
CREATE INDEX idx_cf_key_num ON custom_field_values(key, value_num);

-- Every arrival of a file, including duplicates and rejections.
CREATE TABLE ingest_events (
    id             INTEGER PRIMARY KEY,
    doc_id         TEXT,
    sha256         TEXT,
    source         TEXT NOT NULL,
    source_details TEXT NOT NULL DEFAULT '{}',
    filename       TEXT NOT NULL DEFAULT '',
    result         TEXT NOT NULL,   -- created | duplicate | rejected | error
    message        TEXT NOT NULL DEFAULT '',
    import_ref     TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX idx_ingest_events_created ON ingest_events(created_at DESC);
CREATE INDEX idx_ingest_events_doc ON ingest_events(doc_id);

CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,          -- process | export | import | reindex | rebuild
    doc_id       TEXT,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL,          -- queued | processing | done | needs_review | failed
    stage        TEXT NOT NULL DEFAULT '',
    progress     REAL NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_run_at  TEXT NOT NULL,
    lease_until  TEXT,
    error        TEXT,
    result       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_jobs_queue ON jobs(status, next_run_at);
CREATE INDEX idx_jobs_doc ON jobs(doc_id);

CREATE TABLE processing_runs (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT NOT NULL,
    task            TEXT NOT NULL,       -- extract | classify
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT '',
    target          TEXT NOT NULL DEFAULT '',
    adapter_version TEXT NOT NULL DEFAULT '',
    prompt_version  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    error           TEXT,
    suggestions     TEXT,
    raw_response    TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX idx_runs_doc ON processing_runs(doc_id, id);

CREATE TABLE imap_state (
    account     TEXT NOT NULL,
    mailbox     TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL,
    last_poll_at TEXT,
    last_error  TEXT,
    PRIMARY KEY (account, mailbox)
);

CREATE TABLE imap_items (
    account     TEXT NOT NULL,
    message_key TEXT NOT NULL,   -- Message-ID, or uidvalidity:uid if missing
    part_sha256 TEXT NOT NULL,
    doc_id      TEXT,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (account, message_key, part_sha256)
);

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE TABLE login_attempts (
    id         INTEGER PRIMARY KEY,
    key        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_login_attempts ON login_attempts(key, created_at);

-- Folder watcher bookkeeping so a file that keeps failing is quarantined, not retried forever.
CREATE TABLE consume_failures (
    path       TEXT PRIMARY KEY,
    failures   INTEGER NOT NULL,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- Weighted full-text index. Columns are indexed separately so ranking can prefer
-- title/correspondent/type/tags over body text. Content is stored in folded form
-- (lowercase, umlauts transliterated) - display text comes from the documents table.
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
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE VIRTUAL TABLE doc_vocab USING fts5vocab(doc_fts, row);
