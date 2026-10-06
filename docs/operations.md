# Operations

This page covers running Heftig on a laptop or home server: installation, network access,
storage, upgrades, backup and restore, integrity checks and troubleshooting. Commands are shown
for a native installation; in containers prefix them with `podman exec heftig` (installer),
`docker compose exec worker` (Compose, services running) or `docker compose run --rm web`
(Compose, services stopped), e.g. `podman exec heftig heftig status`.

## Installation

### With the installer (recommended)

```sh
curl -fsSL https://raw.githubusercontent.com/jo-tud/heftig/main/install.sh | sh
```

The script (read it first if you like: [install.sh](../install.sh)) needs Linux or macOS, `curl`
and Podman or Docker; on Linux it offers to install Podman with the system's package manager if
neither is there. On macOS start Docker Desktop, OrbStack, Colima or the Podman machine
(`podman machine start`) first. It then

1. creates `~/heftig/archive` (everything Heftig keeps) and `~/heftig/scanner` (the scanner
   folder: files put there are imported and removed);
2. downloads the image `ghcr.io/jo-tud/heftig` (x86-64 and ARM, e.g. a Raspberry Pi 4/5);
3. starts one container named `heftig` running web server and worker (`heftig run`), published
   on `127.0.0.1:8765`, with the restart policy `always` – so it comes back after a reboot
   (rootless Podman: the installer enables the standard `podman-restart.service` for your user;
   macOS: only if Docker Desktop, OrbStack, Colima or the Podman machine starts at login, which
   Colima and the Podman machine don't by default);
4. prints the address of the setup page, including a one-time setup code.

Options are environment variables for the script, e.g.
`curl -fsSL …/install.sh | HEFTIG_LAN=1 HEFTIG_PORT=8080 sh`:

| Variable | Meaning |
|---|---|
| `HEFTIG_HOME` | folder for archive and scanner folder (default `~/heftig`) |
| `HEFTIG_PORT` | port of the web interface (default `8765`) |
| `HEFTIG_LAN=1` | reachable from other devices in the network, not only this computer |
| `HEFTIG_BUILD=1` | build the image from the source instead of downloading it |
| `HEFTIG_IMAGE` | another image, e.g. a fixed version `ghcr.io/jo-tud/heftig:0.2` |
| `HEFTIG_NAME` | container name (default `heftig`) |

**Update:** run the installer again; it replaces the container and keeps the archive. Database
migrations run on start, after a copy of the database is saved to `archive/backup/`.

**Stop, start, logs:** `podman stop heftig`, `podman start heftig`, `podman logs heftig` (or
`docker …`).

**Uninstall:** `podman rm -f heftig` (or `docker rm -f heftig`), then delete `~/heftig` if you
don't want to keep the archive.

### Setup page and settings

On the first start Heftig has no account. The setup page (`/setup`, protected by the one-time
code in `archive/setup-token`, also written to the log) asks for the interface language and
creates the account, then walks through the AI (`/settings/ai`), the search by meaning
(`/settings/search`), the mailbox (`/settings/mail`) and the scanner (`/settings/scanner`). The
same pages are under *Settings* later (*Change AI*, *Search by meaning*, *E-mail import*,
*Scanner and phone*); the binders of the paper filing are on *Settings → Binders*, the archive's
own words that mean the same on *Settings → Search: words that mean the same*, names for the
addresses documents are e-mailed from on *Settings → E-mail senders*. (`heftig init` still
creates the account on the command line, e.g. for scripted installations;
`heftig set-password <name>` sets a new password, e.g. a forgotten one.)

What is saved on these pages is stored in the archive's database (`meta` table) and applies to
web server and worker without a restart. API keys and the mail password are never shown again;
they are stored in the archive's database (readable only by your user), so a copy or backup of
the archive folder contains them – protect backups accordingly. Exports (`heftig export`) do not
contain them.

Environment variables (and a `.env` file) still work and **win** over the settings page: a setting
given there is shown on the page but cannot be changed. An empty variable (`HEFTIG_IMAP_HOST=`)
counts as not set. That keeps existing installations and configuration management working. The
full list is under [Configuration reference](#configuration-reference).

### With Docker Compose

`compose.yaml` in the repository runs web server and worker as two services from one image
(restart policy `always`) and reads `.env` if it exists (see [.env.example](../.env.example);
optional, settings can also be made in the browser):

```sh
git clone https://github.com/jo-tud/heftig.git && cd heftig
mkdir -p archive consume folder   # create them yourself so they belong to your user
docker compose up -d              # or: podman compose up -d
```

Then open <http://127.0.0.1:8765/setup> (the setup code is in `archive/setup-token`).

### Without containers

Requirements: Linux, Python 3.11+, Tesseract with the language data of your documents.

```sh
# Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu
# Fedora:        sudo dnf install tesseract tesseract-langpack-eng tesseract-langpack-deu
uv tool install 'heftig[anthropic] @ git+https://github.com/jo-tud/heftig'
export HEFTIG_ARCHIVE_DIR=~/heftig-archive
heftig run                    # web server + worker in one process; then open /setup
```

For a permanent setup run `heftig serve` and `heftig worker` as two processes, e.g. with the
systemd user units in [contrib/systemd/](../contrib/systemd/).

## Ports and network access

| Setup | Listens on | Reachable from |
|---|---|---|
| Installer | container: `0.0.0.0:8765`; published on `127.0.0.1:<HEFTIG_PORT>` (all interfaces with `HEFTIG_LAN=1`) | this machine only |
| Native (`heftig serve` / `heftig run`) | `HEFTIG_HOST:HEFTIG_PORT`, default `127.0.0.1:8765` | this machine only |
| Compose | container: `0.0.0.0:8765`; published as `HEFTIG_PUBLISH`, default `127.0.0.1:8765` | this machine only |

**LAN access** (e.g. a phone in the same Wi-Fi):

- Installer: run it again with `HEFTIG_LAN=1`.
- Compose: set `HEFTIG_PUBLISH=0.0.0.0:8765` (all interfaces) or `HEFTIG_PUBLISH=192.168.1.10:8765`
  (one interface) in `.env` and run `docker compose up -d`.
- Native: `HEFTIG_HOST=0.0.0.0`.
- Open the port in the host firewall only for the local network, e.g. with firewalld:
  `sudo firewall-cmd --zone=home --add-port=8765/tcp --permanent && sudo firewall-cmd --reload`.
- **Rootful Docker bypasses the host firewall** for published ports (it inserts its own
  iptables rules), so `0.0.0.0` means: reachable in every network the machine joins - on a
  laptop also in a café's Wi-Fi. Bind to the one interface instead
  (`HEFTIG_PUBLISH=192.168.1.10:8765`, or the Tailscale address), or use rootless Podman, which
  respects the firewall.

Without TLS, the password and session cookie cross the network in clear text. That is acceptable
only in a trusted home network. The session cookie is marked `Secure` automatically when Heftig is
reached via HTTPS.

**Access from outside the home network:** do not forward the port on your router. Use either

- a **VPN** such as WireGuard or Tailscale, and keep Heftig bound to the LAN or VPN interface; or
- a **reverse proxy with TLS** (Caddy, nginx, Traefik) in front of Heftig bound to `127.0.0.1`,
  ideally combined with a VPN or an additional authentication layer.

Reverse proxy example (Caddy, obtains a certificate automatically):

```text
archiv.example.org {
    reverse_proxy 127.0.0.1:8765
}
```

With a reverse proxy:

- set `HEFTIG_TRUST_PROXY_HEADERS=true` so that the client address (`X-Forwarded-For`, used for
  the login rate limit) and the scheme (`X-Forwarded-Proto`, used for the `Secure` cookie flag)
  are taken from the proxy. Only do this if Heftig itself is reachable **only** through the
  proxy, otherwise clients can spoof these headers;
- the proxy must pass the original `Host` header (Caddy does; nginx:
  `proxy_set_header Host $host;`), because the CSRF check compares `Origin` with `Host`;
- allow large request bodies (nginx: `client_max_body_size 110m;` for the default 100 MB limit);
- `HEFTIG_COOKIE_SECURE=true` forces the `Secure` flag regardless of headers.

## Volumes and storage

| Container path | Host (Compose default) | Content |
|---|---|---|
| `/archive` | `./archive` (`HEFTIG_ARCHIVE_HOST_DIR`) | everything persistent: originals, sidecars, database, quarantine, exports |
| `/consume` | `./consume` (`HEFTIG_CONSUME_HOST_DIR`) | scanner input folder; may be a mounted network share |
| `/folder` | `./folder` (`HEFTIG_FOLDER_HOST_DIR`) | folder for digital files (source `folder`, not marked as paper) |

The installer mounts `~/heftig/archive` as `/archive` and `~/heftig/scanner` as `/consume`.

- `archive/` is the only directory you need to back up. Keep it on a **local disk**. SQLite over
  SMB/NFS is not reliable (file locking), so never put the archive itself on a network share;
  only the consume folder may live there.
- Natively, the consume folder defaults to `<archive>/consume`; set `HEFTIG_CONSUME_DIR` to use
  another path (e.g. a mounted scanner share, see [scanner-imap.md](scanner-imap.md)).
- Disk usage is roughly the size of all originals plus a few percent for text, thumbnails and the
  index.

## Permissions

- Heftig creates directories with umask 077, sidecars and the database with mode 0600 and
  originals with 0400. Only the owning user (and root) can read the archive.
- Containers: the entrypoint (`docker-entrypoint.sh`) adapts to how the container runs.
  - **Rootful Docker/Podman:** the container starts as root, gives the top-level `/archive` and
    `/consume` directories (not recursively) to `PUID:PGID` (default `1000:1000`) and then drops
    all privileges to that user. Set `PUID`/`PGID` in `.env` to your own IDs (`id -u`, `id -g`)
    so the files on the host belong to you.
  - **Rootless Podman/Docker:** the container's root already is your unprivileged host user;
    it keeps running as that user, so files in `./archive` belong to you. `PUID`/`PGID` are not
    used.
- Create `archive/`, `consume/` and `folder/` yourself before the first `docker compose up`, so
  they exist with your ownership.
- SELinux: `compose.yaml` mounts both volumes with `:z` so the container may access them. That
  does not work for CIFS/NFS mounts; see
  [scanner-imap.md](scanner-imap.md#podman-selinux-and-network-mounts).
- Backup programs must run as the same user (or root) to read the archive.

## Configuration reference

All settings are environment variables with the prefix `HEFTIG_` (or lines in a `.env` file in
the working directory; Compose passes `.env` to the containers). Defaults in brackets. The
authoritative list is `src/heftig/config.py`; [.env.example](../.env.example) has comments.
Settings marked † can also be made on the setup and settings pages (see
[Setup page and settings](#setup-page-and-settings)); a variable that is set wins. Secrets can be
given as files instead (`OCR_API_KEY_FILE`, `CLASSIFY_API_KEY_FILE`, `OCR_HEADERS_FILE`, `CLASSIFY_HEADERS_FILE`, `IMAP_PASSWORD_FILE`, e.g.
container secrets; `compose.yaml` has a commented example); the direct value wins over the file.

| Area | Variables |
|---|---|
| Storage | `ARCHIVE_DIR` (`./archive`), `CONSUME_DIR` (`<archive>/consume`), `FOLDER_DIR` (none), `HOST_SCANNER_DIR`, `HOST_FOLDER_DIR` (in a container: where these folders are on the host, only for display on the settings pages; set by `compose.yaml`, the installer sets the first) |
| Language | `LANGUAGE` (`en`/`de`)†: interface, and the language of AI-written titles and summaries; chosen on the setup page, later changeable only with the variable |
| Web | `HOST` (`127.0.0.1`), `PORT` (`8765`), `COOKIE_SECURE` (`auto`/`true`/`false`), `TRUST_PROXY_HEADERS` (`false`), `SESSION_HOURS` (`336`), `LOGIN_MAX_ATTEMPTS` (`5`), `LOGIN_WINDOW_SECONDS` (`300`) |
| Limits | `MAX_UPLOAD_MB` (`100`), `MAX_PAGES` (`500`), `MAX_IMAGE_MEGAPIXELS` (`150`), `OCR_DPI` (`300`), `OCR_PAGE_TIMEOUT_SECONDS` (`180`), `MIN_TEXT_CHARS_PER_PAGE` (`40`) |
| Worker | `WORKER_CONCURRENCY` (`2`, 1-16), `JOB_MAX_ATTEMPTS` (`5`), `JOB_BACKOFF_SECONDS` (`30`), `JOB_LEASE_SECONDS` (`900`), `RAW_RESPONSE_RETENTION_DAYS` (`30`), `RAW_RESPONSE_MAX_KB` (`64`), `DB_SNAPSHOT_HOURS` (`6`, 0 = off), `TRASH_RETENTION_DAYS` (`30`), `AUTO_RESOLVE_IDENTICAL` (`true`: the second of two truly identical documents goes to the trash) |
| Consume folder | `CONSUME_POLL_SECONDS` (`10`), `CONSUME_STABLE_POLLS` (`2`), `CONSUME_MIN_AGE_SECONDS` (`5`), `CONSUME_INCOMPLETE_WAIT_SECONDS` (`900`), `CONSUME_AFTER` (`delete`/`move`)†, `CONSUME_MAX_FAILURES` (`3`) |
| Paper filing | `AUTO_FILE_SOURCES` (empty; e.g. `scanner`)†, `FILING_GRANULARITY` (`month`/`year`) |
| OCR | `OCR_PROVIDER` (`tesseract`)†, `OCR_LANGUAGES` (`deu+eng`), `OCR_MODEL`†, `OCR_BASE_URL`†, `OCR_API_KEY`†, `OCR_API_KEY_FILE`, `OCR_HEADERS`†, `OCR_HEADERS_FILE` (additional HTTP headers, see [providers.md](providers.md#additional-http-headers)), `OCR_SUPPORTS_IMAGES` (`true`), `ANTHROPIC_OCR_EFFORT` (`low`), `ALLOW_CLOUD_OCR` (`false`)†, `OCR_FIRST_PAGE_MODEL` (empty), `OCR_MAX_SIDE` (`2000`), `OCR_PAGE_CONCURRENCY` (`3`), `OCR_BLANK_MAX_INK` (`0.001`), `OCR_AI_MAX_PAGES` (`30`, 0 = no limit) |
| Classification | `CLASSIFY_PROVIDER` (`rules`)†, `CLASSIFY_MODEL`†, `CLASSIFY_BASE_URL`†, `CLASSIFY_API_KEY`†, `CLASSIFY_API_KEY_FILE`, `CLASSIFY_HEADERS`†, `CLASSIFY_HEADERS_FILE`, `CLASSIFY_JSON_MODE` (`schema`), `CLASSIFY_MAX_CHARS` (`24000`), `CLASSIFY_MIN_CONFIDENCE` (`0.6`), `ALLOW_CLOUD_CLASSIFY` (`false`)†, `AI_SEARCH_MODEL` (`claude-haiku-4-5-20251001`), `PROVIDER_TIMEOUT_SECONDS` (`120`) |
| Search by meaning | `SEMANTIC_SEARCH` (`false`)†, `SEMANTIC_THREADS` (`0` = half the CPU cores); see [search.md](search.md#search-by-meaning) |
| AI availability | `AI_FALLBACK` (`true`), `AI_RETRY_MINUTES` (`60`), `AI_CATCH_UP_BATCH` (`25`), `RATE_LIMIT_PAUSE_SECONDS` (`90`); see [providers.md](providers.md#when-the-ai-provider-is-unreachable) |
| IMAP | `IMAP_HOST`†, `IMAP_PORT` (`993`)†, `IMAP_USER`†, `IMAP_PASSWORD`†, `IMAP_PASSWORD_FILE`, `IMAP_MAILBOX` (`INBOX`)†, `IMAP_MOVE_TO`†, `IMAP_DELETE_AFTER_IMPORT` (`false`)†, `IMAP_TRASH_MAILBOX` (auto-detected), `IMAP_ALLOWED_SENDERS` (empty = everyone)†, `IMAP_POLL_SECONDS` (`300`), `IMAP_MAX_ATTACHMENT_MB` (`50`), `IMAP_MAX_ATTACHMENTS` (`20`), `IMAP_SKIP_INLINE_IMAGES_BELOW_KB` (`30`), `IMAP_SKIP_FILENAME_PATTERNS` (`logo*,image0*,signature*`), `IMAP_ARCHIVE_EML` (`false`), `IMAP_MAIL_KEYWORD` (`#mail`)†, `IMAP_PAPER_KEYWORD` (`#paper`)† |
| Misc | `LOG_LEVEL` (`INFO`), `MOCK_FAIL` (tests only) |

Compose additionally reads `HEFTIG_PUBLISH`, `HEFTIG_ARCHIVE_HOST_DIR`,
`HEFTIG_CONSUME_HOST_DIR` and `HEFTIG_FOLDER_HOST_DIR`; the container entrypoint reads
`PUID`/`PGID` (see [Permissions](#permissions)). Environment variables are read at process start; restart both
services after changing one. Settings made on the settings page apply right away.

## Upgrades and migrations

1. Make a backup (`heftig backup`, see below).
2. Installer: run it again (it downloads the new image and replaces the container). Otherwise
   update the code: `git pull`.
3. Compose: `docker compose up -d --build`. Native: `uv tool install --force .` (or
   `pipx install --force .`), then restart `heftig-web` and `heftig-worker`.
4. Database migrations run automatically when a process opens the archive. Check with
   `heftig status` and `heftig check --quick`.

Downgrading to an older version after a migration is not supported; restore the backup instead.

## Search by meaning: model and memory

With the search by meaning switched on ([search.md](search.md#search-by-meaning)), the worker
downloads the embedding model once (about 330 MB from huggingface.co) into
`<archive>/models/` when it prepares documents for the first time, and then prepares new
documents in the background. While the model works it needs up to about 750 MB of memory (the
worker while it prepares documents, the web process while searches use it); after ten minutes
without use it is unloaded and the memory is given back to the system, so an idle Heftig stays
small. Preparing uses half the CPU cores (`HEFTIG_SEMANTIC_THREADS`): on a notebook about a
second per document of three pages, so 2,000 documents take about half an hour the first time.
Without internet access at that moment the download is retried every ten minutes (the error is
shown under Settings → Search by meaning); `heftig embed` downloads and prepares everything at
once. `models/` is not part of backups and exports - it is
downloaded again when missing. Switching it off keeps the prepared vectors (they are used again
when it is switched back on).

## Backup

Heftig's data is the `archive/` directory. Everything in it except `index.sqlite` is written
atomically and originals never change, so a file-level copy of those files is safe while
Heftig runs. **The database is different:** in WAL mode, committed data lives partly in
`index.sqlite` and partly in `index.sqlite-wal`, and SQLite moves data between them at any time
(checkpoints). A backup program copies the two files at different moments and can capture a
state that never existed, which may be corrupt or silently miss transactions. Copying a running
SQLite database is therefore not guaranteed to be consistent. Use one of these two methods:

**A. `heftig backup` (self-contained, simple)**

```sh
heftig backup /mnt/backup/heftig
# -> /mnt/backup/heftig/heftig-backup-20260928-030000/
```

The backup contains a consistent copy of the database made with SQLite's online backup API, plus
`originals/`, `documents/`, `trash/`, `sources/`, `taxonomy.json`, `saved_searches.json`, `binders.json`,
`synonyms.json`, `senders.json`, `email/`, `quarantine/` and a `backup.json` marker (not `models/`: the search
model is downloaded again when missing).
It is a full copy every time (no deduplication), so it suits small archives or an external disk.
In containers, mount the target:
`docker compose run --rm -v /mnt/backup/heftig:/backup web heftig backup /backup`. The
installer's container has no mount for a backup target; there, use method B on
`~/heftig/archive` (the snapshot is written automatically, see below).

**B. `heftig db-snapshot` + a backup tool (incremental, encrypted)**

```sh
heftig db-snapshot            # writes archive/backup/index-snapshot.sqlite (backup API)
restic -r /mnt/backup/restic backup /srv/heftig/archive \
  --exclude /srv/heftig/archive/index.sqlite \
  --exclude /srv/heftig/archive/index.sqlite-wal \
  --exclude /srv/heftig/archive/index.sqlite-shm \
  --exclude /srv/heftig/archive/tmp
```

Kopia and Borg work the same way: take the snapshot immediately before the backup run and back
up the archive directory without the live database files. Example crontab line for a Compose
setup:

```text
15 3 * * * cd /srv/heftig && docker compose exec -T worker heftig db-snapshot && restic -r /mnt/backup/restic backup /srv/heftig/archive --exclude-file /srv/heftig/backup-excludes.txt
```

Automatic copies inside the archive (no setup needed):

- The worker writes `archive/backup/index-snapshot.sqlite` every `HEFTIG_DB_SNAPSHOT_HOURS`
  (6 h; 0 = off), so method B always finds a recent snapshot even if the backup run cannot call
  `heftig db-snapshot` first. The inbox warns when the last one is more than twice as old.
- Before an update migrates the database, Heftig copies it to
  `archive/backup/vor-update-v<old version>.sqlite` (the last three are kept).
- These copies protect against a damaged database or a failed update, not against losing the
  disk: keep an off-site backup (method A or B) as well.

Notes:

- Backups contain all your documents plus the password hash, token hashes and the API keys and
  mail password saved on the settings pages. Encrypt them
  (restic, Borg and Kopia do) and store at least one copy off-site.
- The last backup and snapshot times are shown on the settings page and in `heftig status`.
- An export (`heftig export`) is not a full backup: it omits password hashes, token secrets and
  AI raw responses. It does contain the user name, token names and prefixes, open jobs with
  their error texts and the IMAP state (account as `user@host`) - treat it like the archive
  itself. It is the portable format for moving to another installation or system.

## Restore and verification

Restore into a **fresh, empty** directory and verify before switching over.

From method A:

```sh
heftig restore /mnt/backup/heftig/heftig-backup-20260928-030000 /srv/heftig/archive-restored
export HEFTIG_ARCHIVE_DIR=/srv/heftig/archive-restored
heftig check                 # verifies every original's SHA-256, sidecars, index
heftig status                # document count, job counts
heftig search telekom        # a few searches you know the answer to
```

From method B: restore the files with your backup tool into an empty directory, then

```sh
cd /srv/heftig/archive-restored
mv backup/index-snapshot.sqlite index.sqlite
rm -f index.sqlite-wal index.sqlite-shm
HEFTIG_ARCHIVE_DIR=/srv/heftig/archive-restored heftig repair   # adopts sidecars newer than the snapshot
HEFTIG_ARCHIVE_DIR=/srv/heftig/archive-restored heftig check
```

Verification checklist:

1. `heftig check` reports `"ok": true` (exit code 0).
2. The document count in `heftig status` matches the original installation (or the count shown
   on its settings page at backup time).
3. Searches for a few known documents return the same results as before; open one document and
   download its original.
4. Log in with your existing password (users are part of the database backup).

Then point the installation at the restored directory (`HEFTIG_ARCHIVE_DIR`, or move it to
`./archive` for Compose) and start the services. The consume folder is not part of the backup;
files in it are simply picked up again.

Practise a restore once after setting up backups, and again from time to time.

If only the database is lost or damaged and no backup exists, `heftig rebuild-db` recreates all
document data from the sidecars (see below). Users, API tokens, the job queue, IMAP cursors and
the global event log are lost in that case, as are the settings made on the settings pages: set
Heftig up again on `/setup` (or with `heftig init`); the IMAP importer then looks at the whole
mailbox again, and attachments that are already archived are only recorded as duplicates.

## Integrity check

```sh
heftig check          # full check incl. SHA-256 of every original (reads all files)
heftig check --quick  # without hashing
```

Output is JSON with the document count and a list of issues; exit code 0 means no issues, 3
means issues were found.

| Issue | Meaning | Fix |
|---|---|---|
| `missing_original` | original file is gone | restore it from a backup |
| `hash_mismatch` | original changed on disk (bit rot, manual edit) | restore it from a backup |
| `missing_sidecar` | `metadata.json` is missing | `heftig repair` rewrites it from the database |
| `invalid_sidecar` | `metadata.json` does not validate | restore it, or fix it by hand |
| `revision_mismatch` | sidecar and database differ (crash during a write) | `heftig repair` |
| `sidecar_hash_mismatch` | sidecar and database disagree about the SHA-256 | investigate manually |
| `orphan_sidecar` | sidecar without database row (crash during ingestion) | `heftig repair` |
| `orphan_original` | original without document | `heftig repair --adopt-orphans` ingests it |
| `unfinished_processing` | document marked queued/processing without a job | `heftig repair` requeues it |
| `index_mismatch` | search index row count differs | `heftig repair` or `heftig reindex` |

Run the full check regularly (e.g. monthly) and after moving or restoring the archive.

## Repair after a crash

After a power loss or a killed process, run:

```sh
heftig repair                    # safe to run repeatedly
heftig repair --adopt-orphans    # additionally ingest originals that have no document
```

It finishes interrupted operations: loads sidecars that were written but not committed, rewrites
missing sidecars from the database, takes the newer version on revision mismatches, requeues
unfinished processing and expired jobs, rebuilds the search index if needed and deletes stale
temp files (older than one hour). It prints the actions taken and the issues that remain.
Stopping the worker while repairing avoids races with running jobs. The worker itself resumes
interrupted jobs on start, so a plain restart is often enough.

## Rebuilding the index or the database

| Command | What it does | When |
|---|---|---|
| `heftig reindex` (also on the settings page) | Rebuilds the FTS search index from the database | index problems, after changing search code |
| `heftig embed` | Downloads the search model if needed and prepares all pending documents for the search by meaning now (the worker does it in the background anyway) | before going offline, to be done at once |
| `heftig rebuild-db` | Deletes all document, text, tag and taxonomy rows and reloads them from `documents/*/metadata.json`, `text_pages.json` and `taxonomy.json`, then rebuilds the index | database damaged or lost; stop the worker first |

`rebuild-db` keeps users, tokens, jobs, IMAP state and the event log if the database file still
exists. Search results are identical after both rebuilds (covered by the test suite).

## Export and import

```sh
heftig export /mnt/export            # directory; add --zip for a single file
heftig import /mnt/export/heftig-export-20260928-120000
```

The web UI starts exports as a job that writes to `<archive>/exports/`; the import API accepts
paths below `<archive>/imports/`. Format, conflict handling and what is (not) included:
[data-format.md](data-format.md#export-format).

## Logs

- Installer: `podman logs -f heftig` (or `docker logs -f heftig`).
- Compose: `docker compose logs -f worker` / `web`.
- systemd user units: `journalctl --user -u heftig-worker -f`.
- Level: `HEFTIG_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`).
- Logs contain document IDs, sequence numbers, sources, error types and the names of files
  taken from the consume folders; no document text, cookies, tokens or API keys. The web
  server's access log shows the path of each request without its query string, so search words
  (`?q=…`) and the Claude connection's questions are not logged.
- The *Inbox* is the user-facing log: import results, duplicates, rejected files and
  attachments, failed jobs, quarantine.

## Health and status

| Endpoint | Auth | Meaning |
|---|---|---|
| `GET /health` | none | Liveness: the web process answers (`{"status": "ok"}`). Used by the container healthcheck. |
| `GET /ready` | none | Readiness: writes to the database and to `archive/tmp/`. Returns 200 or 503 with `checks.database`, `checks.archive_writable` and `checks.worker` (`ok` or `not running`, from the heartbeat; informational, does not make the endpoint fail). |
| `GET /api/status` | login/token | Document and job counts, worker heartbeat, consume folder status, IMAP state, last export/backup/snapshot, provider configuration. Same as `heftig status`. |

The worker counts as alive if its heartbeat is younger than 90 seconds.

## Malware scanning

Heftig does not scan files. Documents are only parsed by pdfium and Pillow (in the worker, with
size limits) and never executed, and originals are served as downloads or sandboxed inline
views. If you receive files from untrusted senders, scan them before they reach Heftig, for
example:

- scanner/share: let the scanner write to a staging folder, scan it with ClamAV
  (`clamdscan --move=/srv/quarantine-av /srv/staging`) and move clean files into the consume
  folder with `mv` on the same filesystem (atomic rename);
- e-mail: rely on the mail provider's virus scanning, or scan the mailbox with your mail server.

A built-in integration (e.g. a ClamAV check before ingestion) is a possible later extension.

## Resource limits

- Memory is dominated by page rendering for OCR: an A4 page at 300 dpi is about 26 MB as an RGB
  image, per concurrently processed page; `HEFTIG_MAX_IMAGE_MEGAPIXELS` caps the worst case.
  With the default concurrency of 2, 1 GB for the worker is comfortable; vision models or large
  TIFFs may need more. With the search by meaning switched on, the worker and the web process
  each need about 750 MB more while the model is loaded (see
  [Search by meaning: model and memory](#search-by-meaning-model-and-memory)).
- CPU: Tesseract runs single-threaded per page; `HEFTIG_WORKER_CONCURRENCY` controls how many
  documents are processed in parallel.
- To enforce limits in Compose, add e.g. `mem_limit: 2g` and `cpus: 2` to the `worker` service
  (in a `compose.override.yaml`), or `MemoryMax=` / `CPUQuota=` in the systemd unit.
- On a laptop, lower `HEFTIG_OCR_DPI` (e.g. 200) or the concurrency to reduce load, at some cost
  in OCR quality.


## HTTPS

Heftig itself speaks plain HTTP and is meant to sit behind a TLS-terminating reverse proxy or a
VPN when it is used from other devices. HTTPS is also required for the live camera on `/scan`
(browsers only grant camera access to secure contexts) and for installing Heftig as an app
([guide.md](guide.md#install-as-an-app)). Common options:

- **Reverse proxy with a local certificate authority**, e.g. Caddy with `tls internal`: no
  third-party service, but the proxy's root certificate has to be installed once on every phone.
- **VPN with automatic certificates**, e.g. Tailscale (`tailscale serve`): valid certificates
  and access from outside the home network, requires the VPN app on the phone.
- **Own domain** with a certificate from Let's Encrypt (DNS challenge) and a DNS name that
  points to the LAN address.

Set `HEFTIG_TRUST_PROXY_HEADERS=true` behind a proxy so session cookies get the `Secure` flag.
