-- Counting documents per correspondent / type (taxonomy lists, suggestions, classification
-- prompt) scanned the whole documents table per term; with 1000+ documents this got slow.
CREATE INDEX IF NOT EXISTS idx_documents_correspondent ON documents(correspondent_id);
CREATE INDEX IF NOT EXISTS idx_documents_doctype ON documents(document_type_id);
