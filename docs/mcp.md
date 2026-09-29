# Asking Claude about your archive (MCP)

`heftig mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. Connected to
Claude (Desktop app, Claude Code, or any other MCP client), it lets the model search and read
your archive and answer questions that need several documents, for example:

- "How much did I spend on streaming in 2026?" - booking lines from all bank statements
- "When can I cancel my phone contract at the earliest?" - contract, amendments, notes
- "Which insurance policies do I have, and what do they cost per year?" - policies and premiums
- "Show me all letters from the tax office about 2024."

## How it works

The MCP server runs on the machine where Claude runs (started by Claude over stdio) and talks to
the running Heftig through its REST API with an API token. It never opens the archive files
itself, so it works the same with Heftig on the laptop, in a container or on a home server.

All tools are **read-only**. Create the token as "read only": Heftig then rejects every
changing request (`403 read_only`), whatever the model tries.

| Tool | Purpose |
|---|---|
| `archive_overview` | Counts per correspondent, type, tag, source and year plus the custom fields - the vocabulary for filters |
| `search_documents` | Full-text search with filters (same search as the web UI, incl. date phrases such as "March 2025" or "letztes Jahr") |
| `find_text` | Matching **text lines** across all documents within the filters, with page numbers - for sums, lists and "every mention of ..." questions without reading whole documents; plain substring or regular expression, optional neighbouring lines |
| `field_values` | Extracted structured values per document (e.g. `Amount` with currency, `Contract number`; German installations: `Betrag`, `Vertragsnummer`) |
| `get_document` | Metadata, summary, notes, attachments, filing and text per page (page ranges for long documents) |
| `get_page_image` | A page as image, to check an amount or table the text got wrong |
| `similar_documents` | Related documents (e.g. the other statements of the same account) |

The server also gives Claude instructions: learn the names first, use `find_text` /
`field_values` for questions across many documents, mind signs and the number format of the
document's language (`1.234,56` in German documents), check
doubtful values on the page image, and always name the sources with a link to the document page.

The same data is available without MCP: `GET /api/lines` and `GET /api/fields` (see `/api/docs`).

## Privacy

Everything a tool returns - text lines, document text, page images - becomes part of the
conversation and is sent to the provider of the model you use with it (with Claude: Anthropic).
Heftig only returns what the model asks for, and only reading. If that is not acceptable for some
documents, do not connect the archive, or use a local model with an MCP-capable client.

## Prompt injection

Document texts are written by third parties - a letter, an invoice, or anything sent to the
archive's mail address can contain text addressed to an AI ("ignore your instructions and …").
The server tells Claude that all document content is untrusted data and must never be sent
elsewhere without an explicit request; Heftig itself only offers read-only tools. The remaining
risk lies in the client: if the same Claude session also has tools that act outside (sending
e-mail, web requests, running code), a manipulated document could try to make Claude use them
with archive content. Prefer sessions without such tools for archive questions, keep tool
permission prompts on, and restrict who can mail documents (`HEFTIG_IMAP_ALLOWED_SENDERS`).

## Setup

1. **Token:** Heftig -> *Settings* -> *API tokens*: name e.g. "Claude", keep "read only" ticked,
   *Create token*, copy the token. Or on the command line:
   `heftig token create Claude --read-only` (inside the container:
   `podman exec heftig heftig token create Claude --read-only` for the installer,
   `docker compose exec web heftig token create Claude --read-only` for Compose).
   Store it in a file only you can read:

   ```bash
   mkdir -p ~/.config/heftig && install -m 600 /dev/null ~/.config/heftig/mcp-token
   nano ~/.config/heftig/mcp-token   # paste the token, save
   ```

2. **Install the command** on the machine where Claude runs (not needed if you use the container
   variant below):

   ```bash
   uv tool install --from '/path/to/heftig[mcp]' heftig    # or: pipx install '/path/to/heftig[mcp]'
   ```

3. **Register it in Claude.**

   *Claude Code / Claude Desktop Code tab:*

   ```bash
   claude mcp add heftig --scope user \
     -e HEFTIG_MCP_URL=http://127.0.0.1:8765 \
     -e HEFTIG_MCP_TOKEN_FILE=$HOME/.config/heftig/mcp-token \
     -- heftig mcp
   ```

   *Claude Desktop (chat):* in `claude_desktop_config.json` (Settings -> Developer -> Edit Config):

   ```json
   {
     "mcpServers": {
       "heftig": {
         "command": "/home/you/.local/bin/heftig",
         "args": ["mcp"],
         "env": {
           "HEFTIG_MCP_URL": "http://127.0.0.1:8765",
           "HEFTIG_MCP_TOKEN_FILE": "/home/you/.config/heftig/mcp-token"
         }
       }
     }
   }
   ```

   *Without installing anything on the host*, run it inside the Heftig container (the image
   includes the MCP extra); pass the token as environment variable:

   ```json
   "heftig": {
     "command": "podman",
     "args": ["exec", "-i", "-e", "HEFTIG_MCP_TOKEN", "heftig", "heftig", "mcp"],
     "env": { "HEFTIG_MCP_TOKEN": "hft_..." }
   }
   ```

   (`heftig` is the installer's container; with Compose use the web container's name shown by
   `docker compose ps`, and `docker` instead of `podman` where needed.)

4. **Links in answers** point to `HEFTIG_MCP_URL`. If you open Heftig under another address
   (e.g. via Tailscale HTTPS), set `HEFTIG_MCP_PUBLIC_URL=https://your-host.ts.net`.

Revoke the token in the settings at any time; the connection then stops working immediately.

## Troubleshooting

- "Heftig is not reachable at ...": is Heftig running, and is the URL right?
- "Heftig rejected the API token (missing, wrong or revoked)": token missing, mistyped or
  revoked.
- `heftig mcp` needs the `mcp` extra: `pip install 'heftig[mcp]'`.
- Claude Code: `claude mcp list` shows whether the server starts; its stderr ends up in the MCP log.
