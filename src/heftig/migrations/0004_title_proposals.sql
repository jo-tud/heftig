-- Proposed consistent titles from the "harmonise titles" job. Temporary review data: nothing is
-- changed until the user accepts a proposal (which then becomes a normal, locked title edit).
CREATE TABLE title_proposals (
    doc_id      TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    old_title   TEXT NOT NULL,
    new_title   TEXT NOT NULL,
    group_label TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
