-- Named binders for the paper filing (binders.json); each filed document records its binder.
ALTER TABLE documents ADD COLUMN filing_binder TEXT;
CREATE INDEX idx_documents_binder ON documents(filing_binder, filing_section, filing_sequence);
