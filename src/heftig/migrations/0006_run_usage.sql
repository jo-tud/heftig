-- Token usage and estimated cost (list prices) of every AI call, for the cost overview.
ALTER TABLE processing_runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE processing_runs ADD COLUMN output_tokens INTEGER;
ALTER TABLE processing_runs ADD COLUMN cost_usd REAL;
CREATE INDEX idx_runs_finished ON processing_runs(finished_at);
