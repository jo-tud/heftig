from email.message import EmailMessage

import pytest

from heftig import documents as docs
from heftig.archive import Archive
from heftig.imap_import import ImapPoller, process_message

from .conftest import make_settings
from .helpers import image_bytes, text_image, text_pdf


class FakeImap:
    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.seen: list[int] = []
        self.moved: list[tuple[int, str]] = []
        self.fail_fetch: set[int] = set()
        self.deleted: list[int] = []
        self.trash: str | None = None

    def select(self, mailbox):
        return self.uidvalidity

    def uids_after(self, last_uid):
        return sorted(u for u in self.messages if u > last_uid)

    def fetch(self, uid):
        if uid in self.fail_fetch:
            raise RuntimeError("Verbindung abgebrochen")
        return self.messages[uid]

    def mark_seen(self, uid):
        self.seen.append(uid)

    def move(self, uid, mailbox):
        self.moved.append((uid, mailbox))

    def delete(self, uid):
        self.deleted.append(uid)

    def find_trash(self):
        return self.trash

    def logout(self):
        pass


def mail(
    subject: str, attachments: list[tuple[str, str, bytes]], msgid: str | None = None, inline=()
):
    m = EmailMessage()
    m["From"] = "Absender Beispiel <absender@example.org>"
    m["To"] = "archiv@example.org"
    m["Subject"] = subject
    m["Date"] = "Mon, 01 Sep 2025 10:00:00 +0200"
    if msgid:
        m["Message-ID"] = msgid
    m.set_content("Hallo,\nanbei das Dokument.\n")
    for name, ctype, data in attachments:
        maintype, subtype = ctype.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    for name, data in inline:
        m.add_attachment(data, maintype="image", subtype="png", filename=name, disposition="inline",
                         cid="<logo@x>")  # fmt: skip
    return m.as_bytes()


@pytest.fixture
def imap_archive(tmp_path):
    a = Archive(make_settings(tmp_path, imap_host="imap.example.org", imap_user="archiv"))
    yield a
    a.close()


def test_attachments_become_documents_with_shared_import_ref(imap_archive):
    a = imap_archive
    pdf1, pdf2 = text_pdf(["Rechnung eins"]), text_pdf(["Rechnung zwei"])
    logo = image_bytes(text_image("L", size=(40, 20)))
    raw = mail("Fwd: Rechnungen", [("r1.pdf", "application/pdf", pdf1), ("r2.pdf", "application/pdf", pdf2),
                                   ("brief.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK..")],
               msgid="<m1@example.org>", inline=[("logo.png", logo)])  # fmt: skip
    fake = FakeImap({101: raw})
    summary = ImapPoller(a, client_factory=lambda: fake).poll()
    assert summary["error"] is None and fake.seen == [101]
    statuses = sorted(r["status"] for r in summary["results"])
    assert statuses == ["created", "created", "rejected", "skipped"]
    rows = a.conn.execute("SELECT id FROM documents").fetchall()
    metas = [docs.load_meta(a, r[0]) for r in rows]
    refs = {m.source_details["import_ref"] for m in metas}
    assert len(refs) == 1 and all(m.source == "email" for m in metas)
    m = metas[0]
    assert m.source_details["message_id"] == "<m1@example.org>"
    assert m.source_details["from"] == "absender@example.org"
    assert "password" not in str(m.source_details).lower()
    assert m.source_details["message_date"].startswith("Mon, 01 Sep 2025")
    assert m.received_at != m.source_details["message_date"]
    ev = dict(
        a.conn.execute(
            "SELECT result, message FROM ingest_events WHERE result IN ('skipped','rejected')"
        ).fetchall()
    )
    assert "logo" in ev["skipped"] and "not supported" in ev["rejected"]


def test_polling_is_idempotent_and_resumes_by_uid(imap_archive):
    a = imap_archive
    fake = FakeImap({1: mail("A", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<a@x>")})
    poller = ImapPoller(a, client_factory=lambda: fake)
    poller.poll()
    poller.poll()
    assert a.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    fake.messages[2] = mail("B", [("b.pdf", "application/pdf", text_pdf(["B"]))], msgid="<b@x>")
    s = poller.poll()
    assert s["messages"] == 1 and fake.seen == [1, 2]
    assert a.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    st = a.conn.execute("SELECT uidvalidity, last_uid FROM imap_state").fetchone()
    assert tuple(st) == (7, 2)


def test_uidvalidity_change_rescans_without_duplicates(imap_archive):
    a = imap_archive
    msgs = {1: mail("A", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<a@x>")}
    fake = FakeImap(msgs)
    ImapPoller(a, client_factory=lambda: fake).poll()
    renumbered = FakeImap({50: msgs[1]}, uidvalidity=8)
    s = ImapPoller(a, client_factory=lambda: renumbered).poll()
    assert [r["status"] for r in s["results"]] == ["already_imported"]
    assert a.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_failure_does_not_mark_mail_and_is_visible(imap_archive):
    a = imap_archive
    fake = FakeImap({
        1: mail("A", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<a@x>"),
        2: mail("B", [("b.pdf", "application/pdf", text_pdf(["B"]))], msgid="<b@x>"),
    })  # fmt: skip
    fake.fail_fetch = {2}
    s = ImapPoller(a, client_factory=lambda: fake).poll()
    assert s["error"] and fake.seen == [1]
    st = a.conn.execute("SELECT last_uid, last_error FROM imap_state").fetchone()
    assert st["last_uid"] == 1 and "Verbindung" in st["last_error"]
    fake.fail_fetch = set()
    ImapPoller(a, client_factory=lambda: fake).poll()
    assert fake.seen == [1, 2]
    assert a.conn.execute("SELECT last_error FROM imap_state").fetchone()[0] is None


def test_connection_error_is_recorded(imap_archive):
    def broken():
        raise OSError("DNS")

    s = ImapPoller(imap_archive, client_factory=broken).poll()
    assert "Connection failed" in s["error"]


def test_move_after_import(tmp_path):
    a = Archive(make_settings(tmp_path, imap_host="h", imap_user="u", imap_move_to="Archiviert"))
    fake = FakeImap({3: mail("A", [("a.pdf", "application/pdf", text_pdf(["A"]))])})
    ImapPoller(a, client_factory=lambda: fake).poll()
    assert fake.moved == [(3, "Archiviert")] and fake.seen == []
    a.close()


def test_direct_mime_document_and_mail_without_attachment(imap_archive):
    a = imap_archive
    m = EmailMessage()
    m["Subject"] = "Scan"
    m["Message-ID"] = "<direct@x>"
    m.set_content(text_pdf(["Direkt"]), maintype="application", subtype="pdf")
    res = process_message(a, m.as_bytes(), account="t", fallback_key="k1")
    assert [r["status"] for r in res] == ["created"]
    res = process_message(a, mail("Nur Text", []), account="t", fallback_key="k2")
    assert res[0]["status"] == "rejected"
    ev = a.conn.execute("SELECT message FROM ingest_events WHERE result='rejected'").fetchone()[0]
    assert "no supported file" in ev


def test_eml_is_optionally_archived(tmp_path):
    a = Archive(make_settings(tmp_path, imap_archive_eml=True))
    raw = mail("A", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<a@x>")
    res = process_message(a, raw, account="t", fallback_key="k")
    meta = docs.load_meta(a, res[0]["document_id"])
    assert (a.paths.root / meta.source_details["eml"]).read_bytes() == raw
    # the mail body is not a document of its own
    assert a.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    a.close()


def test_poison_message_is_skipped_after_retries(imap_archive, monkeypatch):
    import heftig.imap_import as mod

    a = imap_archive
    fake = FakeImap({
        1: mail("kaputt", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<bad@x>"),
        2: mail("gut", [("b.pdf", "application/pdf", text_pdf(["B"]))], msgid="<good@x>"),
    })  # fmt: skip
    real = mod.process_message

    def flaky(archive, raw, **kw):
        if b"<bad@x>" in raw:
            raise ValueError("unlesbar")
        return real(archive, raw, **kw)

    monkeypatch.setattr(mod, "process_message", flaky)
    poller = ImapPoller(a, client_factory=lambda: fake)
    for _ in range(mod.MAX_MESSAGE_FAILURES - 1):
        assert poller.poll()["error"]
    s = poller.poll()
    assert s["error"] is None and fake.seen == [2]  # bad one stays unread in the mailbox
    ev = a.conn.execute("SELECT message FROM ingest_events WHERE result='rejected'").fetchone()[0]
    assert "Skipped" in ev and "unread" in ev


def test_delete_after_import_only_when_everything_is_archived(tmp_path):
    a = Archive(
        make_settings(tmp_path, imap_host="h", imap_user="u", imap_delete_after_import=True)
    )
    fake = FakeImap({
        1: mail("gut", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<1@x>"),
        2: mail("docx", [("b.pdf", "application/pdf", text_pdf(["B"])),
                         ("c.docx", "application/octet-stream", b"PK..")], msgid="<2@x>"),
        3: mail("nur Text", [], msgid="<3@x>"),
    })  # fmt: skip
    ImapPoller(a, client_factory=lambda: fake).poll()
    assert fake.deleted == [1]  # no trash folder -> expunged
    assert fake.seen == [2, 3]  # refused attachment / no document: kept for the user
    a.close()


def test_delete_goes_to_trash_when_available(tmp_path):
    a = Archive(
        make_settings(tmp_path, imap_host="h", imap_user="u", imap_delete_after_import=True)
    )
    fake = FakeImap(
        {1: mail("gut", [("a.pdf", "application/pdf", text_pdf(["A"]))], msgid="<1@x>")}
    )
    fake.trash = "Papierkorb"
    ImapPoller(a, client_factory=lambda: fake).poll()
    assert fake.moved == [(1, "Papierkorb")] and fake.deleted == []
    a.close()


def test_find_trash_parses_special_use_flags():
    from heftig.imap_import import RealImapClient

    c = RealImapClient.__new__(RealImapClient)

    class L:
        def list(self):
            return "OK", [
                b'(\\HasNoChildren) "/" INBOX',
                b'(\\HasNoChildren \\Trash) "/" "Gel&APY-scht"',
            ]

    c._c = L()
    assert c.find_trash() == "Gel&APY-scht"


def test_only_allowed_senders_and_a_limited_number_of_attachments(tmp_path):
    a = Archive(make_settings(tmp_path, imap_host="imap.example.org", imap_user="archiv",
                              imap_allowed_senders="ich@example.net, @example.org",
                              imap_max_attachments=2))  # fmt: skip
    # from the allowed domain, but with three attachments: two are taken
    three = [(f"r{n}.pdf", "application/pdf", text_pdf([f"Rechnung {n}"])) for n in range(3)]
    res = process_message(a, mail("Drei", three), account="t", fallback_key="k1")
    assert [r["status"] for r in res] == ["created", "created", "rejected"]
    # a stranger: nothing is imported, the refusal is recorded once
    stranger = mail("Hallo", [("x.pdf", "application/pdf", text_pdf(["Fremd"]))]).replace(
        b"absender@example.org", b"fremd@attacker.example"
    )
    res = process_message(a, stranger, account="t", fallback_key="k2")
    assert res[0]["status"] == "rejected" and "not allowed" in res[0]["message"]
    assert process_message(a, stranger, account="t", fallback_key="k2") == []
    # allowed address, but the receiving server says the From was forged
    forged = mail("Echt?", [("y.pdf", "application/pdf", text_pdf(["Gefälscht"]))]).replace(
        b"From:",
        b"Authentication-Results: mx.example.net; dmarc=fail header.from=example.org\r\nFrom:",
        1,
    )
    res = process_message(a, forged, account="t", fallback_key="k3")
    assert res[0]["status"] == "rejected" and "DMARC" in res[0]["message"]
    assert a.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    a.close()
