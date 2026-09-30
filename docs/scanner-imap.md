# Scanner, phone and e-mail setup

All input paths end in the same pipeline (validation, SHA-256 deduplication, archiving,
processing, indexing). This page explains how to connect a network scanner, a phone and an
e-mail mailbox.

## Scanner to network folder

The typical setup: the multifunction printer saves scans as PDF into a shared SMB folder, and
Heftig watches that folder (the *consume folder*).

```text
  HP printer --SMB--> share on NAS / router / home server --mounted as--> consume/ --> Heftig worker
```

### Where to host the share

Host the share on a device that is always on, so scans arrive even while the Heftig laptop is
switched off. Heftig imports everything that accumulated as soon as the worker runs again.

| Option | Notes |
|---|---|
| NAS | Create a share (e.g. `scans`) and a dedicated user for the printer. Best option. |
| Router with USB storage | Many routers can share a USB stick or disk via SMB. Check that it supports SMB2 or newer. |
| Samba on a home server | If Heftig runs on the server itself, the share can simply be the consume folder. |
| The Heftig laptop itself | Works, but scans fail while the laptop is off or asleep. |

Minimal Samba share on a Linux home server:

```ini
# /etc/samba/smb.conf
[scans]
   path = /srv/scans
   valid users = scanner
   writable = yes
   create mask = 0660
   directory mask = 0770
```

```sh
sudo useradd --system --no-create-home scanner
sudo smbpasswd -a scanner             # password for the printer
sudo install -d -o scanner -g scanner -m 0770 /srv/scans
```

Heftig needs read, write and delete permission in this folder (it deletes or moves a file after
importing it), e.g. by adding the Heftig user to the `scanner` group, or by mounting the share
with that user's credentials.

### HP multifunction printer: Scan to Network Folder

Menu names differ between HP models and firmware versions; the steps are usually:

1. Find the printer's IP address (printer display: network/wireless summary, or your router).
2. Open `http://<printer-ip>` in a browser. This is the printer's **Embedded Web Server** (EWS).
   Log in as administrator if asked (the default PIN is often printed on a label on the device).
3. Go to **Scan** -> **Scan to Network Folder** (sometimes *Network Folder Setup* or
   *Scan to Network Folder Setup*) and choose **Add** / **New**.
4. Enter:
   - display name, e.g. `Archiv`;
   - network path in UNC form, e.g. `\\192.168.1.20\scans` (IP addresses avoid name resolution
     problems);
   - user name and password of the dedicated share user; optionally the workgroup/domain;
   - optionally a PIN for the quick set.
5. Scan settings:
   - file type **PDF** (not JPEG, which produces one file per page);
   - **multi-page / "scan all pages to one file"** enabled, so that a letter from the document
     feeder becomes one PDF (the exact option name varies);
   - resolution 300 dpi, colour or greyscale (300 dpi is a good trade-off for OCR);
   - file name prefix, e.g. `scan`, with the timestamp suffix the printer offers. Heftig does not
     care about file names; they are only displayed.
6. Use **Save and Test**; a test file should appear in the share.

Each file becomes one document. If your printer writes one file per page, the pages appear as
separate documents; *Combine with another document …* on the document page joins them, but
fixing the scanner setting saves that work.
Some older HP devices only speak SMB1; prefer a firmware update over enabling SMB1 on your NAS.

### Settings for digitising old folders

- **File type: PDF (image), not "searchable PDF".** Pages with an embedded text layer skip OCR;
  the printer's own OCR is weaker than the AI or Tesseract.
- **300 dpi.** Heftig renders pages at 300 dpi anyway; 600 dpi only makes files bigger and the
  feeder slower, 200 dpi hurts the local Tesseract (fallback, blank pages, search marks).
- **Greyscale** as default, a second profile in **colour** for stamps, highlighter and
  certificates. Never black/white (1 bit): faint dot-matrix and thermal print gets lost.
- **Duplex is fine:** blank back sides are recognised and not sent to the paid AI.
- **One letter per scan job**, staples removed; receipts, thermal paper and fragile sheets on the
  flatbed or with the phone camera page.
- Start a **batch** in the inbox for each binder (see
  [guide.md](guide.md#digitising-existing-binders-batches)).

### Mounting the share on Linux

Install `cifs-utils` and store the credentials in a file only root can read:

```sh
sudo install -m 0600 /dev/null /etc/heftig-scans.cred
sudo tee /etc/heftig-scans.cred >/dev/null <<'EOF'
username=scanner
password=<share-password>
EOF
sudo mkdir -p /mnt/scans
```

`/etc/fstab` (one line):

```text
//192.168.1.20/scans  /mnt/scans  cifs  credentials=/etc/heftig-scans.cred,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,vers=3.0,_netdev,nofail,x-systemd.automount  0  0
```

- `uid`/`gid` must be the user Heftig runs as (natively and with rootless Podman: your user;
  rootful containers: `PUID`/`PGID`, see [operations.md](operations.md#permissions)), so Heftig
  may delete imported files.
- `_netdev` waits for the network; `nofail` lets the machine boot when the share is unreachable;
  `x-systemd.automount` mounts on first access. If the share is unreachable, Heftig shows the
  consume folder as not reachable in the inbox and retries at every poll.

Then point Heftig at the mount:

- native: `HEFTIG_CONSUME_DIR=/mnt/scans`;
- Compose: `HEFTIG_CONSUME_HOST_DIR=/mnt/scans` in `.env`;
- installer: mount the share at `~/heftig/scanner` (the folder the container watches). On
  SELinux systems the installer labels the folder with `:Z`, which CIFS mounts do not support;
  use Compose there (see below).

### Podman, SELinux and network mounts

`compose.yaml` mounts the consume folder with `:z`, which tells the container engine to relabel
the directory for SELinux. CIFS (and most NFS) mounts do not support relabelling, so the
container fails to start or cannot read the folder. Instead, give the whole mount the container
label via a mount option and drop `:z` for this volume:

```text
//192.168.1.20/scans  /mnt/scans  cifs  credentials=/etc/heftig-scans.cred,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,vers=3.0,context="system_u:object_r:container_file_t:s0",_netdev,nofail  0  0
```

```yaml
# compose.override.yaml - replaces the /consume mount of compose.yaml (merged by target path)
services:
  web:
    volumes:
      - /mnt/scans:/consume
  worker:
    volumes:
      - /mnt/scans:/consume
```

Check the merged result with `docker compose config` / `podman compose config`. The `uid`/`gid`
in the mount options must be the user Heftig runs as: `PUID`/`PGID` for rootful containers, your
own user for rootless Podman (see [operations.md](operations.md#permissions)).

### Auto-filing for a simple scanner routine

Everything from the consume folder is recorded with source `scanner` and marked as paper, so it
appears in the inbox under "Paper still to file" until you confirm *Filed now*. If your routine
is "scan, then immediately put the letter on top of the current month's section", let Heftig
record the filing at arrival: tick *I file every scanned letter right away* on
*Settings → Scanner and phone*, or set

```sh
HEFTIG_AUTO_FILE_SOURCES=scanner
```

The filing time is then the arrival time, and every scan goes on top as it arrives. Only use
this if you file letter by letter; for a whole stack scanned at once use a batch instead (see
[guide.md](guide.md#filing-a-scanned-stack)). For born-digital files use the folder for digital files (`HEFTIG_FOLDER_DIR`,
`./folder` with Compose; source `folder`, not marked as paper) or
`heftig ingest --source folder <files>`.

### How the consume folder is processed

- The worker polls the folder (including subfolders) every `HEFTIG_CONSUME_POLL_SECONDS` (10 s).
  Polling instead of filesystem events works on network shares and picks up files that arrived
  while Heftig was not running.
- A file is taken only when it is complete: size and modification time unchanged over
  `HEFTIG_CONSUME_STABLE_POLLS` (2) consecutive polls, and at least
  `HEFTIG_CONSUME_MIN_AGE_SECONDS` (5 s) old. Expect a delay of 10-30 seconds.
- Multi-page scans: some scanners (e.g. FRITZ!Box) write a PDF page by page and pause while the
  next page is scanned - longer than the stability wait. A PDF without its final `%%EOF` (or a
  JPEG without its end marker) is therefore not taken yet; Heftig waits up to
  `HEFTIG_CONSUME_INCOMPLETE_WAIT_SECONDS` (15 min) after the last change, then imports whatever
  is there (a broken file then goes to quarantine).
- Ignored: names starting with `.` or `~` or ending with `~`, temporary suffixes (`.tmp`,
  `.part`, `.partial`, `.crdownload`, `.download`, `.filepart`, `.swp`), `Thumbs.db`,
  `desktop.ini`, `.DS_Store`, symbolic links, hidden subfolders. Uploaders that write to a
  temporary name and rename at the end are picked up immediately after the rename (plus the
  stability wait).
- The source file is removed only **after** the original, metadata and processing job are
  committed. `HEFTIG_CONSUME_AFTER=delete` (default) deletes it; `move` moves it into
  `.heftig-verarbeitet/<timestamp>_<name>` inside the consume folder (clean that up yourself).
  Duplicates are removed the same way (the inbox shows "duplicate").
- Unsupported or broken files (wrong type, encrypted PDF, too large, too many pages) are moved
  to `archive/quarantine/<timestamp>_<name>` together with `<name>.reason.json`, and shown in the
  inbox. They are never silently deleted. The inbox offers *Retry* (import again, e.g. after an
  update) and *Hide from list* (moves the file to `quarantine/ausgeblendet/`).
- Unexpected errors (e.g. disk full) leave the file in place and are retried at the next polls;
  after `HEFTIG_CONSUME_MAX_FAILURES` (3) failed attempts the file goes to quarantine, so a bad
  file cannot cause an endless loop.

## Smartphone

- **Browser in the LAN:** enable LAN access ([operations.md](operations.md#ports-and-network-access)),
  open `http://<server-ip>:8765` on the phone, log in, and use *Add*. *Scan with the camera*
  finds the sheet, straightens it and collects several pages into one PDF (the live preview
  needs HTTPS, see [operations.md](operations.md#https)); the file picker offers the camera too.
  Choose *Scan/photo of a paper document* for paper you will file. Photos uploaded one by one
  become separate documents; *Combine with another document …* joins them.
- **Scan apps:** any app that crops, straightens and exports **one multi-page PDF** per letter
  gives much better OCR than raw photos (for example the document scanner built into many phone
  file or notes apps). Upload the PDF via the browser, or save it into a folder that is synced
  into the consume folder (e.g. with Syncthing or Nextcloud; their temporary files start with a
  dot and are ignored until complete).
- **E-mail:** share the PDF to your mail app and send it to the archive mailbox (below). Works
  from anywhere without exposing Heftig.
- **API / shortcuts:** automation apps can upload with an API token (create one in the settings
  page):

  ```sh
  curl -H "Authorization: Bearer hft_<token>" -F "files=@scan.pdf" -F "kind=paper" \
       http://192.168.1.10:8765/api/documents
  ```

For access from outside your home network use a VPN, not a port forwarding.

## E-mail (IMAP)

Heftig polls one IMAP mailbox and imports document attachments. It never sends mail and does not
need an SMTP server.

### Setup

1. Create a **dedicated mailbox** (e.g. `archiv@example.org`) at your mail provider. Do not use
   your personal inbox: Heftig marks or moves every message it has processed.
2. Create an **app password** for it if your provider supports them (usually requires two-factor
   authentication). Heftig logs in with user name and password over implicit TLS (IMAPS, port
   993, certificate verified). STARTTLS and OAuth2-only providers are not supported.
3. Enter it on *Settings → E-mail import* (`/settings/mail`, also a step of the first setup):
   e-mail address and password; for common providers (Gmail, Outlook/Hotmail, iCloud, Yahoo,
   Fastmail, Posteo, mailbox.org, GMX, WEB.DE, T-Online) the server is filled in and the page
   says whether an app password is needed. Choose the folder to collect from, the allowed senders
   (below) and what happens after the import (mark as read, move to a folder, or delete). *Save
   and test* logs in and lists the mailbox's folders. The settings apply without a restart; the
   password is stored in the archive's database and never shown again.

   Alternatively, configure it with environment variables (they then take precedence and are
   shown as fixed on the page):

   ```sh
   HEFTIG_IMAP_HOST=imap.example.org
   HEFTIG_IMAP_PORT=993
   HEFTIG_IMAP_USER=archiv@example.org
   HEFTIG_IMAP_PASSWORD_FILE=/run/secrets/imap_password   # or HEFTIG_IMAP_PASSWORD=...
   HEFTIG_IMAP_MAILBOX=INBOX
   HEFTIG_IMAP_MOVE_TO=Archiviert        # optional; folder must exist. Empty: only mark as seen
   HEFTIG_IMAP_POLL_SECONDS=300
   ```

   Prefer the password file: it keeps the password out of the process environment and
   `docker inspect`. With Compose:

   ```yaml
   # compose.override.yaml
   services:
     worker:
       environment:
         HEFTIG_IMAP_PASSWORD_FILE: /run/secrets/imap_password
       secrets: [imap_password]
   secrets:
     imap_password:
       file: ./secrets/imap_password.txt     # chmod 600, not in git
   ```

4. With environment variables, restart the worker. The settings page shows the last poll, the
   last processed UID and the last error. `heftig imap-poll` polls once from the command line
   (exit code 1 on error).

Then forward documents to the mailbox (as attachment), or let other services send there directly.

### Who may send documents

Anyone who knows the mailbox address can send it documents, and they are processed like your
own (paid AI calls, and the AI's reading of them). Restrict it on *Settings → E-mail import*
(*Who may send documents?*, one address or `@domain` per line) or with:

```ini
HEFTIG_IMAP_ALLOWED_SENDERS=ich@example.org,partnerin@example.org,@meine-bank.example
```

- Mail from other senders is not imported; the inbox lists it as "Sender not allowed".
- The From address can be forged. If the receiving mail server recorded a failed DMARC check
  (`Authentication-Results: … dmarc=fail`), the message is refused even from an allowed address.
- At most `HEFTIG_IMAP_MAX_ATTACHMENTS` (20) attachments per message are imported.
- New correspondents, document types and tags proposed by the AI for e-mailed documents are
  never created automatically - they wait as suggestions (see *Suggestions in bulk*), so a
  crafted document cannot plant text in the category lists that every later AI request contains.

### What gets imported

- Only messages newer than the last processed UID are fetched (`BODY.PEEK`, so fetching itself
  does not mark them as read).
- Each attachment that is a PDF, JPEG, PNG or TIFF (by MIME type or file extension) becomes its
  own document with source `email`. All documents from one message share an `import_ref`.
  Attachments inside forwarded messages (`message/rfc822`) are found as well, and a message whose
  whole body is a PDF works too.
- The mail text itself is **not** turned into a document. Provenance is stored in
  `source_details`: sender address, subject, the message's `Date` header, Message-ID, mailbox,
  UID. `received_at` is the time of import, not the mail date. With
  `HEFTIG_IMAP_ARCHIVE_EML=true` the raw message is additionally kept as
  `archive/email/<import_ref>.eml`.
- **Who sent it** shows on the document page ("E-mail from anna@… · “subject”", linked to all
  documents from that address), when hovering over *E-mail* in the document list, and as the
  filter *E-mail from* with counts per address. *Settings → E-mail senders* lists the addresses;
  a name given there ("Anna") is shown instead of the address for all documents from it, also
  the ones imported before (`senders.json`).
- **Skip rules** (only for images, only these): inline images smaller than
  `HEFTIG_IMAP_SKIP_INLINE_IMAGES_BELOW_KB` (30 KB), and image attachments whose file name
  matches `HEFTIG_IMAP_SKIP_FILENAME_PATTERNS` (`logo*,image0*,signature*`, shell-style,
  case-insensitive). Set them to `0` / empty to disable. Skipped parts are logged as "skipped"
  in the inbox.
- **Unsupported attachments** (Word, ZIP, calendar invites, ...) are not archived; each one is
  recorded as a rejected ingest event with its type, visible in the inbox. A message without
  any supported file is reported as well. Nothing is dropped silently.
- Attachments larger than `HEFTIG_IMAP_MAX_ATTACHMENT_MB` (50 MB) are rejected with a message;
  the general `HEFTIG_MAX_UPLOAD_MB` limit also applies.

### Marking, idempotency and errors

- A message is marked as seen (or moved to `HEFTIG_IMAP_MOVE_TO`) only **after** all its parts
  are durably handled. Unless you switch on the deletion below, Heftig never deletes mail (moving
  uses IMAP `MOVE`, or copy + delete + expunge on servers without `MOVE`).
- The last processed UID is saved after every message, together with `UIDVALIDITY`. If the
  server resets `UIDVALIDITY`, Heftig looks at the whole mailbox again.
- Every attachment is recorded under (account, Message-ID or `uidvalidity:uid`, SHA-256 of the
  attachment), and identical files are deduplicated by SHA-256 anyway. Re-polling, a reset
  cursor or a restored backup therefore never creates duplicate documents.
- Optional, for a **dedicated** mailbox only: `HEFTIG_IMAP_DELETE_AFTER_IMPORT=true` removes a mail
  once all its documents are archived. It is moved to the server's trash folder (detected via the
  IMAP `\Trash` special-use flag, or set `HEFTIG_IMAP_TRASH_MAILBOX`); only servers without a
  trash folder get a permanent delete. Mails with a refused attachment or without any document
  are never deleted, just marked as read. Combine it with `HEFTIG_IMAP_ARCHIVE_EML=true` to keep
  the original message as provenance inside the archive.
- If processing a message fails (e.g. disk full), the message is not marked, the cursor stays
  before it, the error is shown on the settings page, and the message is retried at the next
  poll. After 3 failed attempts for the same message it is skipped: it stays unread in the
  mailbox, a "rejected" entry explains this in the inbox, and later messages are processed.
  Connection or login errors are recorded the same way. No mail is lost.

### Testing without a mail server

Save a message from your mail client as `.eml` and run:

```sh
heftig ingest-eml message.eml
```

This uses exactly the same attachment handling as the IMAP importer (without touching the IMAP
cursor) and prints the result per attachment. Running it twice shows `already_imported`.
