"""E-mail ingestion: a dedicated IMAP mailbox polled over TLS.

- resume by UID (``UIDVALIDITY`` + last processed UID); if the server resets UIDVALIDITY,
  every message is looked at again and idempotency keys prevent duplicates
- idempotency key per attachment: (account, Message-ID or uidvalidity:uid, attachment SHA-256)
- every supported attachment becomes its own document; all share one ``import_ref``
- inline logos/signature images are skipped only by explicit, configurable rules; everything
  skipped or unsupported is recorded as an ingest event (visible in the inbox), never dropped
  silently
- a message is marked as seen / moved only after all its parts were durably handled
- the mail body itself is not turned into a document - unless its subject contains the
  keyword (``imap_mail_keyword``): then the e-mail itself is archived (``mail.py``), for a
  message forwarded as attachment the attached e-mail; optionally the raw .eml is kept as
  provenance under ``archive/email/``
"""

from __future__ import annotations

import email
import fnmatch
import hashlib
import imaplib
import io
import logging
import re
import ssl
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Protocol

from . import mail
from .archive import Archive
from .db import get_meta, now_iso, set_meta, write_tx
from .i18n import N_
from .ingest import IngestResult, ingest_stream, record_rejection
from .storage import atomic_write_bytes, display_filename

log = logging.getLogger("heftig.imap")

CANDIDATE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/tiff", "image/jpg"}
MAX_MESSAGE_FAILURES = 3
CANDIDATE_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff")


class ImapClient(Protocol):
    def select(self, mailbox: str) -> int: ...
    def uids_after(self, last_uid: int) -> list[int]: ...
    def fetch(self, uid: int) -> bytes: ...
    def mark_seen(self, uid: int) -> None: ...
    def move(self, uid: int, mailbox: str) -> None: ...
    def delete(self, uid: int) -> None: ...
    def find_trash(self) -> str | None: ...
    def logout(self) -> None: ...


class RealImapClient:
    """Thin wrapper around imaplib with TLS certificate verification."""

    def __init__(self, host: str, port: int, user: str, password: str, timeout: int = 60):
        ctx = ssl.create_default_context()
        self._c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
        self._c.login(user, password)

    def select(self, mailbox: str) -> int:
        typ, _ = self._c.select(_quote(mailbox))
        if typ != "OK":
            raise RuntimeError(N_("Mailbox %(mailbox)s cannot be selected") % {"mailbox": mailbox})
        typ, data = self._c.response("UIDVALIDITY")
        if not data or data[0] is None:
            typ, data = self._c.status(_quote(mailbox), "(UIDVALIDITY)")
            m = re.search(rb"UIDVALIDITY (\d+)", data[0] or b"")
            return int(m.group(1)) if m else 0
        return int(data[0])

    def uids_after(self, last_uid: int) -> list[int]:
        typ, data = self._c.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            raise RuntimeError(N_("UID SEARCH failed"))
        return sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last_uid)

    def fetch(self, uid: int) -> bytes:
        typ, data = self._c.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(N_("FETCH %(uid)s failed") % {"uid": uid})
        for part in data:
            if isinstance(part, tuple):
                return part[1]
        raise RuntimeError(N_("Message %(uid)s is empty") % {"uid": uid})

    def mark_seen(self, uid: int) -> None:
        self._c.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")

    def move(self, uid: int, mailbox: str) -> None:
        typ, _ = self._c.uid("MOVE", str(uid), _quote(mailbox))
        if typ != "OK":
            typ, _ = self._c.uid("COPY", str(uid), _quote(mailbox))
            if typ != "OK":
                raise RuntimeError(N_("Moving to %(mailbox)s failed") % {"mailbox": mailbox})
            self._c.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
            self._c.expunge()

    def delete(self, uid: int) -> None:
        """Permanently remove one message (only this UID when the server supports UIDPLUS)."""
        self._c.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        if "UIDPLUS" in getattr(self._c, "capabilities", ()):
            self._c.uid("EXPUNGE", str(uid))
        else:
            self._c.expunge()

    def find_trash(self) -> str | None:
        typ, data = self._c.list()
        if typ != "OK":
            return None
        for line in data or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', text)
            if m and "\\trash" in m.group("flags").lower():
                return m.group("name").strip().strip('"')
        return None

    def logout(self) -> None:
        try:
            self._c.logout()
        except Exception:  # connection may already be gone
            log.debug("imap logout failed", exc_info=True)


def _quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _part_filename(part: EmailMessage) -> str:
    name = part.get_filename() or ""
    return display_filename(name) if name else ""


def _skip_reason(archive: Archive, part: EmailMessage, name: str, size: int) -> str | None:
    s = archive.settings
    ctype = part.get_content_type()
    inline = part.get_content_disposition() == "inline" or bool(part.get("Content-ID"))
    if ctype.startswith("image/") and inline and size < s.imap_skip_inline_images_below_kb * 1024:
        return N_("Inline image smaller than %(kb)s KB (logo/signature)") % {
            "kb": s.imap_skip_inline_images_below_kb
        }
    for pat in (p.strip() for p in s.imap_skip_filename_patterns.split(",") if p.strip()):
        if name and fnmatch.fnmatch(name.lower(), pat.lower()) and ctype.startswith("image/"):
            return N_("File name matches the exclusion rule “%(pattern)s”") % {"pattern": pat}
    return None


def _sender_refusal(s, msg: EmailMessage, sender: str) -> str | None:
    """Why a message is not imported (sender not on the allow list, or the receiving server
    found the sender address forged) - None when it may be imported."""
    allowed = s.imap_allowed_sender_set
    if not allowed:
        return None
    sender = sender.lower()
    domain = "@" + sender.rsplit("@", 1)[-1] if "@" in sender else ""
    if sender not in allowed and domain not in allowed:
        return N_("Sender not allowed (%(sender)s) – see HEFTIG_IMAP_ALLOWED_SENDERS") % {
            "sender": sender or N_("unknown")
        }
    # the From header is easy to forge; the receiving mail server records its checks
    results = " ".join(str(h) for h in msg.get_all("Authentication-Results") or []).lower()
    if "dmarc=fail" in results:
        return N_("Sender %(sender)s not verified (DMARC failed) – probably forged") % {
            "sender": sender
        }
    return None


def process_message(
    archive: Archive,
    raw: bytes,
    *,
    account: str,
    fallback_key: str,
    extra_details: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Import all document attachments of one message. Idempotent."""
    s = archive.settings
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]
    message_id = (msg.get("Message-ID") or "").strip()
    message_key = message_id or fallback_key
    import_ref = "mail-" + hashlib.sha256(f"{account}|{message_key}".encode()).hexdigest()[:16]
    base = {
        "import_ref": import_ref,
        "message_id": message_id or None,
        "from": parseaddr(str(msg.get("From") or ""))[1][:200] or None,
        "subject": str(msg.get("Subject") or "")[:200] or None,
        "message_date": str(msg.get("Date") or "")[:100] or None,
        **(extra_details or {}),
    }
    conn = archive.conn
    refusal = _sender_refusal(s, msg, base["from"] or "")
    if refusal:
        if conn.execute(
            "SELECT 1 FROM imap_items WHERE account=? AND message_key=? AND part_sha256='refused'",
            (account, message_key),
        ).fetchone():
            return []
        record_rejection(
            archive, source="email", filename=base.get("subject") or N_("(no subject)"),
            message=refusal, details=base, import_ref=import_ref,
        )  # fmt: skip
        with write_tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO imap_items(account, message_key, part_sha256, doc_id, "
                "result, created_at) VALUES(?,?,'refused',NULL,'rejected',?)",
                (account, message_key, now_iso()),
            )
        return [{"status": "rejected", "message": refusal}]
    if s.imap_archive_eml:
        atomic_write_bytes(archive.paths.email / f"{import_ref}.eml", raw)
        base["eml"] = f"email/{import_ref}.eml"
    # a photographed letter: there is paper to file for what this e-mail brings
    paper = mail.has_keyword(base["subject"] or "", s.imap_paper_keyword)
    if paper:
        base["paper_keyword"] = s.imap_paper_keyword.strip()
    if mail.has_keyword(base["subject"] or "", s.imap_mail_keyword):
        return _archive_mails(archive, msg, raw, account, message_key, import_ref, base, paper)

    results: list[dict[str, Any]] = []
    candidates = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = _part_filename(part)
        disp = part.get_content_disposition()
        is_body_text = ctype in ("text/plain", "text/html") and disp != "attachment"
        if is_body_text or ctype in ("message/delivery-status", "application/pgp-signature"):
            continue
        payload = part.get_payload(decode=True) or b""
        size = len(payload)
        sha = hashlib.sha256(payload).hexdigest()
        done = conn.execute(
            "SELECT result, doc_id FROM imap_items WHERE account=? AND message_key=? "
            "AND part_sha256=?",
            (account, message_key, sha),
        ).fetchone()
        if done:
            results.append({"filename": name, "status": "already_imported", "result": done[0]})
            candidates += 1
            continue
        details = {**base, "attachment": name or None}
        is_candidate = ctype in CANDIDATE_TYPES or name.lower().endswith(CANDIDATE_EXTS)
        if not is_candidate:
            res = record_rejection(
                archive, source="email", filename=name or ctype,
                message=N_("Email attachment not supported (%(type)s)") % {"type": ctype},
                details=details, sha=sha,
                import_ref=import_ref,
            )  # fmt: skip
        elif (reason := _skip_reason(archive, part, name, size)) is not None:
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO ingest_events(sha256, source, source_details, filename, result, "
                    "message, import_ref, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (sha, "email", _json(details), name or ctype, "skipped", reason, import_ref,
                     now_iso()),
                )  # fmt: skip
            res = IngestResult("skipped", name or ctype, sha256=sha, message=reason)
        elif size > s.imap_max_attachment_mb * 1024 * 1024:
            res = record_rejection(
                archive, source="email", filename=name,
                message=N_("Attachment larger than %(mb)s MB") % {"mb": s.imap_max_attachment_mb},
                details=details,
                sha=sha, import_ref=import_ref,
            )  # fmt: skip
        elif candidates >= s.imap_max_attachments:
            res = record_rejection(
                archive, source="email", filename=name,
                message=N_("More than %(num)s attachments in one email – not imported")
                % {"num": s.imap_max_attachments},
                details=details, sha=sha, import_ref=import_ref,
            )  # fmt: skip
        else:
            candidates += 1
            res = ingest_stream(
                archive, io.BytesIO(payload), name or f"attachment.{ctype.split('/')[-1]}", "email",
                details, paper=paper, import_ref=import_ref,
            )  # fmt: skip
        with write_tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO imap_items(account, message_key, part_sha256, doc_id, "
                "result, created_at) VALUES(?,?,?,?,?,?)",
                (account, message_key, sha, res.doc_id, res.status, now_iso()),
            )
        results.append(res.as_dict())
    if candidates == 0 and not results:
        msg_text = N_("Email contains no supported file (PDF, JPEG, PNG, TIFF)")
        if s.imap_mail_keyword.strip():
            msg_text = N_(
                "Email contains no supported file (PDF, JPEG, PNG, TIFF) – to archive the e-mail "
                "itself, put %(keyword)s in the subject"
            ) % {"keyword": s.imap_mail_keyword.strip()}
        record_rejection(
            archive, source="email", filename=base.get("subject") or N_("(no subject)"),
            message=msg_text, details=base, import_ref=import_ref,
        )  # fmt: skip
        results.append({"status": "rejected", "message": N_("no supported file")})
    return results


def _archive_mails(
    archive: Archive,
    msg: EmailMessage,
    raw: bytes,
    account: str,
    message_key: str,
    import_ref: str,
    base: dict[str, Any],
    paper: bool = False,
) -> list[dict[str, Any]]:
    """The keyword is in the subject: archive the e-mail itself as a document - the e-mails
    attached to it (forwarded as attachment), or else this message (forwarded inline: the
    forwarded header block gives title, date and sender, the attachments are part of it). Its
    provenance is that of the archived message; who forwarded it is kept as ``forwarded_by``."""
    keyword = archive.settings.imap_mail_keyword.strip()
    attached = [p for p in mail.attachment_parts(msg) if p.get_content_type() == "message/rfc822"]
    # who sent it, its subject and date: those of the archived message, not of the forward
    forwarder = {"forwarded_by": base.get("from"), "mail_keyword": keyword}
    if attached:
        # left empty: ingesting the attached message fills them in from its own headers
        own = dict.fromkeys(("from", "subject", "message_date", "message_id"))
        details = {**base, **own, **forwarder}
        items = [(mail.payload(p), mail.part_filename(p, n + 1), details)
                 for n, p in enumerate(attached)]  # fmt: skip
    else:
        subject = mail.clean_subject(mail.remove_keyword(base.get("subject") or "", keyword))
        details = {**base, **forwarder}
        fwd = mail.parse(raw).forwarded
        if fwd is not None:  # forwarded inline: the header block of the forwarded message
            sender = parseaddr(fwd.sender)[1]
            if "@" in sender:
                details["from"] = sender[:200]
            else:
                details.pop("forwarded_by")  # no address in the block: the forward stays the sender
            if fwd.subject:
                details["subject"] = fwd.subject[:200]
            if fwd.date_text:
                details["message_date"] = fwd.date_text[:100]
        else:
            details.pop("forwarded_by")  # the keyword on an e-mail of one's own: it is the sender
        items = [(raw, f"{display_filename(subject)[:120] or 'e-mail'}.eml", details)]
    conn = archive.conn
    results: list[dict[str, Any]] = []
    for data, name, details in items:
        sha = hashlib.sha256(data).hexdigest()
        done = conn.execute(
            "SELECT result FROM imap_items WHERE account=? AND message_key=? AND part_sha256=?",
            (account, message_key, sha),
        ).fetchone()
        if done:
            results.append({"filename": name, "status": "already_imported", "result": done[0]})
            continue
        res = ingest_stream(
            archive, io.BytesIO(data), name, "email", details, paper=paper, import_ref=import_ref,
        )  # fmt: skip
        with write_tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO imap_items(account, message_key, part_sha256, doc_id, "
                "result, created_at) VALUES(?,?,?,?,?,?)",
                (account, message_key, sha, res.doc_id, res.status, now_iso()),
            )
        results.append(res.as_dict())
    return results


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


class ImapPoller:
    def __init__(self, archive: Archive, client_factory=None):
        self.archive = archive
        s = archive.settings
        self.account = f"{s.imap_user}@{s.imap_host}"
        self._factory = client_factory or self._connect

    def _connect(self) -> ImapClient:
        s = self.archive.settings
        password = s.secret("imap_password")
        if not password:
            raise RuntimeError(N_("IMAP password missing (HEFTIG_IMAP_PASSWORD[_FILE])"))
        return RealImapClient(s.imap_host, s.imap_port, s.imap_user, password)

    def _state(self, mailbox: str) -> tuple[int, int] | None:
        row = self.archive.conn.execute(
            "SELECT uidvalidity, last_uid FROM imap_state WHERE account=? AND mailbox=?",
            (self.account, mailbox),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def _save(self, mailbox: str, uidvalidity: int, last_uid: int, error: str | None) -> None:
        with write_tx(self.archive.conn):
            self.archive.conn.execute(
                "INSERT INTO imap_state(account, mailbox, uidvalidity, last_uid, last_poll_at, "
                "last_error) VALUES(?,?,?,?,?,?) ON CONFLICT(account, mailbox) DO UPDATE SET "
                "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, "
                "last_poll_at=excluded.last_poll_at, last_error=excluded.last_error",
                (self.account, mailbox, uidvalidity, last_uid, now_iso(), error),
            )

    def _dispose(self, client: ImapClient, uid: int, results: list[dict[str, Any]]) -> None:
        s = self.archive.settings
        archived = {"created", "duplicate", "already_imported"}
        statuses = {r.get("status") for r in results}
        # delete only when at least one document is safely archived and nothing was refused;
        # mails with rejected attachments or without documents stay (read) for the user
        if (
            s.imap_delete_after_import
            and statuses & archived
            and statuses <= archived | {"skipped"}
        ):
            trash = self._trash(client)
            if trash:
                client.move(uid, trash)
            else:
                client.delete(uid)
            return
        if s.imap_move_to:
            client.move(uid, s.imap_move_to)
        else:
            client.mark_seen(uid)

    def _trash(self, client: ImapClient) -> str | None:
        if self.archive.settings.imap_trash_mailbox:
            return self.archive.settings.imap_trash_mailbox
        if not hasattr(self, "_trash_cache"):
            try:
                self._trash_cache = client.find_trash()
            except Exception:
                log.warning("imap: could not detect trash folder", exc_info=True)
                self._trash_cache = None
        return self._trash_cache

    def _fail_key(self, uidvalidity: int, uid: int) -> str:
        return f"imap_fail:{self.account}:{uidvalidity}:{uid}"

    def _give_up(self, mailbox: str, uidvalidity: int, uid: int, error: Exception) -> bool:
        """Count failures of one message; after MAX_MESSAGE_FAILURES skip it (visibly)."""
        conn = self.archive.conn
        key = self._fail_key(uidvalidity, uid)
        with write_tx(conn):
            n = int(get_meta(conn, key, "0") or 0) + 1
            set_meta(conn, key, str(n))
        if n < MAX_MESSAGE_FAILURES:
            return False
        record_rejection(
            self.archive, source="email", filename=N_("Email UID %(uid)s") % {"uid": uid},
            message=N_(
                "Skipped after %(num)s attempts – stays unread in the mailbox “%(mailbox)s” "
                "(%(error)s)"
            ) % {"num": n, "mailbox": mailbox, "error": f"{type(error).__name__}: {str(error)[:200]}"},
            details={"mailbox": mailbox, "uid": uid, "uidvalidity": uidvalidity},
        )  # fmt: skip
        self._clear_failures(uidvalidity, uid)
        return True

    def _clear_failures(self, uidvalidity: int, uid: int) -> None:
        with write_tx(self.archive.conn):
            self.archive.conn.execute(
                "DELETE FROM meta WHERE key=?", (self._fail_key(uidvalidity, uid),)
            )

    def poll(self) -> dict[str, Any]:
        s = self.archive.settings
        mailbox = s.imap_mailbox
        summary: dict[str, Any] = {"messages": 0, "results": [], "error": None}
        state = self._state(mailbox)
        try:
            client = self._factory()
        except Exception as e:
            err = N_("Connection failed: %(error)s") % {"error": type(e).__name__}
            log.warning("imap: %s", err)
            self._save(mailbox, state[0] if state else 0, state[1] if state else 0, err)
            summary["error"] = err
            return summary
        try:
            uidvalidity = client.select(mailbox)
            last_uid = state[1] if state and state[0] == uidvalidity else 0
            if state and state[0] != uidvalidity:
                log.warning("imap: UIDVALIDITY changed, rescanning mailbox (dedup protects)")
            for uid in client.uids_after(last_uid):
                raw = client.fetch(uid)
                try:
                    res = process_message(
                        self.archive, raw, account=self.account,
                        fallback_key=f"{uidvalidity}:{uid}",
                        extra_details={"mailbox": mailbox, "uid": uid, "uidvalidity": uidvalidity},
                    )  # fmt: skip
                except Exception as e:
                    if not self._give_up(mailbox, uidvalidity, uid, e):
                        raise
                    # poison message: leave it unread in the mailbox, continue with the next one
                    last_uid = uid
                    self._save(mailbox, uidvalidity, last_uid, None)
                    continue
                self._clear_failures(uidvalidity, uid)
                # durable now -> mark, move or delete the mail
                self._dispose(client, uid, res)
                last_uid = uid
                self._save(mailbox, uidvalidity, last_uid, None)
                summary["messages"] += 1
                summary["results"] += res
            self._save(mailbox, uidvalidity, last_uid, None)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("imap poll failed: %s", err)
            st = self._state(mailbox)
            self._save(mailbox, st[0] if st else 0, st[1] if st else 0, err)
            summary["error"] = err
        finally:
            client.logout()
        return summary
