-- API tokens can be limited to reading ("read"), e.g. for the MCP connection to Claude.
ALTER TABLE api_tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'full';
