"""E-mails as documents: .eml files from the folder, uploads and the IMAP keyword."""

from email.message import EmailMessage

import pytest

from heftig import documents as docs
from heftig import mail
from heftig.archive import Archive
from heftig.consume import ConsumeWatcher
from heftig.imap_import import process_message
from heftig.media import MAIL, inspect_file, render_page
from heftig.wordboxes import page_words

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import image_bytes, text_image, text_pdf


def eml(
    subject="Mietvertrag Lindenstraße",
    body="Hallo Jo,\n\nanbei der unterschriebene Mietvertrag.\n\nViele Grüße\nAnna\n",
    attachments=(),
    html=None,
    date="Wed, 01 Oct 2025 14:03:00 +0200",
    sender="Anna Müller <anna@example.org>",
) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "Jo <jo@example.org>"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = f"<{abs(hash((subject, body)))}@example.org>"
    if body is not None:
        m.set_content(body)
    if html is not None:
        if body is None:
            m.set_content(html, subtype="html")
        else:
            m.add_alternative(html, subtype="html")
    for name, ctype, data in attachments:
        maintype, subtype = ctype.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


# --- recognising and reading -----------------------------------------------------------------


def test_recognised_by_content_not_by_name(tmp_path):
    p = tmp_path / "irgendwas.bin"
    p.write_bytes(eml())
    info = inspect_file(p, 100, 50)
    assert (info.mime_type, info.ext, info.page_count) == (MAIL, "eml", 1)
    assert mail.looks_like_mail_bytes(b"From jo@example.org Mon Sep 1 2025\n" + eml())
    for not_mail in (b"Hallo Welt\nZweite Zeile\n", b"Subject: x\n\nnur Betreff",
                     b"Note: From: me\n\n", text_pdf(["x"])):  # fmt: skip
        assert not mail.looks_like_mail_bytes(not_mail)


def test_html_only_mail_becomes_readable_text():
    html = (
        "<html><head><style>p{color:red}</style><title>T</title></head><body>"
        "<div style='display:none'>Vorschautext</div><p>Ihre <b>Bestellung</b> ist da.</p>"
        "<table><tr><td>Artikel</td><td>12,00&nbsp;€</td></tr><tr><td>Summe</td>"
        "<td>99,00 €</td></tr></table><ul><li>eins</li><li>zwei</li></ul>"
        "<script>alert(1)</script></body></html>"
    )
    text = mail.parse(eml(body=None, html=html)).body
    assert "Ihre Bestellung ist da." in text
    assert "Summe    99,00 €" in text and "• zwei" in text
    for hidden in ("color:red", "Vorschautext", "alert", "T\n"):
        assert hidden not in text


def test_plain_text_stub_next_to_html_is_ignored():
    html = "<p>" + "Ausführlicher Text der Rechnung. " * 20 + "</p>"
    text = mail.parse(eml(body="Bitte HTML ansehen.", html=html)).body
    assert text.startswith("Ausführlicher Text")


def test_forwarded_inline_gives_subject_and_date_of_the_original():
    body = (
        "Zur Ablage.\n\n-------- Weitergeleitete Nachricht --------\n"
        "Betreff: \tNebenkostenabrechnung 2024\nDatum: \tMon, 15 Sep 2025 09:12:00 +0200\n"
        "Von: \tHausverwaltung <hv@example.com>\nAn: \tAnna\n\nSehr geehrte Mieterin, ...\n"
    )
    m = mail.parse(eml(subject="WG: Nebenkostenabrechnung 2024", body=body))
    assert m.title_subject == "Nebenkostenabrechnung 2024"
    assert m.document_date.date().isoformat() == "2025-09-15"
    assert m.forwarded.sender == "Hausverwaltung <hv@example.com>"
    # Outlook-style German header block with a written-out date
    body = (
        "________________________________\nVon: Bank <info@bank.example>\n"
        "Gesendet: Montag, 3. März 2025 08:15\nAn: Jo\nBetreff: Kontoauszug\n\nText"
    )
    m = mail.parse(eml(subject="AW: WG: Kontoauszug", body=body))
    assert (m.title_subject, m.document_date.date().isoformat()) == ("Kontoauszug", "2025-03-03")


def test_subject_prefixes_and_keyword():
    assert mail.clean_subject("Re: AW: WG: Fwd: Rechnung") == "Rechnung"
    assert mail.has_keyword("WG: Rechnung #mail", "#mail")
    assert mail.has_keyword("#MAIL Rechnung", "#mail")
    assert not mail.has_keyword("WG: #mailing-liste", "#mail")
    assert not mail.has_keyword("Rechnung", "")
    assert mail.remove_keyword("WG: Rechnung #mail bitte", "#mail") == "WG: Rechnung bitte"


# --- one document: the mail and its attachments -------------------------------------------------


def test_eml_from_the_folder_is_one_document_with_its_attachments(archive):
    raw = eml(
        attachments=[
            ("vertrag.pdf", "application/pdf", text_pdf(["Mietvertrag Seite eins zwischen Vermieterin und Mieter über die Wohnung", "Seite zwei mit den Unterschriften beider Parteien und dem Datum"])),
            ("notiz.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml"
             ".document", b"PK\x03\x04 kein echtes docx"),
        ]
    )  # fmt: skip
    archive.settings.consume_path.mkdir(parents=True, exist_ok=True)
    (archive.settings.consume_path / "Mietvertrag.eml").write_bytes(raw)
    w = ConsumeWatcher(archive)
    w.poll()
    res = w.poll()
    assert [r["status"] for r in res] == ["created"]
    doc_id = res[0]["document_id"]
    meta = docs.load_meta(archive, doc_id)
    assert meta.mime_type == MAIL and meta.original_relpath.endswith(".eml")
    assert not meta.paper  # from the scanner folder, but not a sheet of paper
    assert meta.page_count == 3  # the mail, then the two PDF pages
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

    process_all(archive)
    meta = docs.load_meta(archive, doc_id)
    assert meta.title == "Mietvertrag Lindenstraße"
    assert (meta.document_date, meta.document_date_status) == ("2025-10-01", "mail")
    tp = docs.load_text_pages(archive, doc_id)
    first = tp.pages[0].text
    assert "Von: Anna Müller <anna@example.org>" in first
    assert "Datum: 01.10.2025, 14:03" in first
    assert "Anhänge: vertrag.pdf" in first and "notiz.docx" in first
    assert "anbei der unterschriebene Mietvertrag" in first
    assert "Mietvertrag Seite eins" in tp.pages[1].text and tp.pages[1].method == "embedded"
    assert docs.files(archive, doc_id).preview.exists()
    # the original is kept byte for byte
    assert archive.paths.resolve(meta.original_relpath).read_bytes() == raw
    # search marks on the mail page come from the rendered text layer
    words, source = page_words(archive, meta, 1)
    assert source == "pdf" and any(w[0] == "Mietvertrag" for w in words)


def test_scanned_attachment_is_read_with_ocr(archive):
    scan = image_bytes(text_image("Quittung"), "JPEG")
    res = ingest_bytes(archive, eml(attachments=[("quittung.jpg", "image/jpeg", scan)]), "q.eml")
    process_all(archive)
    tp = docs.load_text_pages(archive, res.doc_id)
    assert [p.method for p in tp.pages] == ["embedded", "ocr"]


def test_mail_date_is_not_replaced_by_the_classifier(archive):
    body = "Der Termin ist am 24.12.2025.\nRechnung vom 03.02.2024 anbei.\n"
    res = ingest_bytes(archive, eml(body=body), "termin.eml")
    process_all(archive)
    meta = docs.load_meta(archive, res.doc_id)
    assert meta.document_date == "2025-10-01" and meta.document_date_status == "mail"


def test_characters_beyond_the_pdf_font_stay_in_the_text(archive):
    res = ingest_bytes(archive, eml(body="Grüße aus Łódź ✓ – 東京\n"), "u.eml")
    process_all(archive)
    assert "Grüße aus Łódź ✓ – 東京" in docs.get_text(archive, res.doc_id)


def test_long_mail_has_several_pages(archive):
    body = "\n\n".join(f"Absatz {i}: " + "viel Text " * 60 for i in range(40))
    res = ingest_bytes(archive, eml(body=body), "lang.eml")
    process_all(archive)
    tp = docs.load_text_pages(archive, res.doc_id)
    assert len(tp.pages) > 2
    assert "Absatz 39" in tp.pages[-1].text and "Von:" not in tp.pages[-1].text


def test_same_mail_again_is_a_duplicate(archive):
    raw = eml()
    first = ingest_bytes(archive, raw, "a.eml")
    again = ingest_bytes(archive, raw, "b.eml", source="folder")
    assert (again.status, again.doc_id) == ("duplicate", first.doc_id)


# --- IMAP: the keyword in the subject -----------------------------------------------------------


@pytest.fixture
def imap_archive(tmp_path):
    a = Archive(make_settings(tmp_path, imap_host="imap.example.org", imap_user="archiv"))
    yield a
    a.close()


def test_keyword_archives_an_inline_forward_with_its_attachments(imap_archive):
    a = imap_archive
    body = (
        "-------- Weitergeleitete Nachricht --------\nBetreff: Kündigungsbestätigung\n"
        "Datum: Tue, 2 Sep 2025 11:00:00 +0200\nVon: Fitnessstudio <info@fit.example>\n\n"
        "Ihre Kündigung ist eingegangen.\n"
    )
    raw = eml(subject="WG: Kündigungsbestätigung #mail", body=body, sender="jo@example.org",
              attachments=[("bestaetigung.pdf", "application/pdf", text_pdf(["Bestätigt"]))])  # fmt: skip
    res = process_message(a, raw, account="acc", fallback_key="7:1")
    assert [r["status"] for r in res] == ["created"]
    process_all(a)
    meta = docs.load_meta(a, res[0]["document_id"])
    assert meta.mime_type == MAIL and meta.source == "email"
    assert meta.title == "Kündigungsbestätigung"
    assert meta.document_date == "2025-09-02"
    assert meta.page_count == 2  # the mail and the attached PDF in one document
    assert a.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    # polled again: nothing new
    again = process_message(a, raw, account="acc", fallback_key="7:1")
    assert [r["status"] for r in again] == ["already_imported"]


def test_keyword_archives_the_mail_forwarded_as_attachment(imap_archive):
    a = imap_archive
    inner = eml(subject="Zählerstand Strom", body="Ihr Zählerstand: 12345 kWh\n",
                sender="Stadtwerke <service@stadtwerke.example>")  # fmt: skip
    outer = EmailMessage()
    outer["From"] = "jo@example.org"
    outer["To"] = "archiv@example.org"
    outer["Subject"] = "Fwd: Zählerstand Strom #mail"
    outer["Message-ID"] = "<outer@example.org>"
    outer.set_content("siehe Anhang")
    outer.add_attachment(inner, maintype="message", subtype="rfc822", filename="Zählerstand.eml")
    res = process_message(a, outer.as_bytes(), account="acc", fallback_key="7:2")
    assert [r["status"] for r in res] == ["created"]
    process_all(a)
    meta = docs.load_meta(a, res[0]["document_id"])
    assert meta.title == "Zählerstand Strom"
    assert "Stadtwerke <service@stadtwerke.example>" in docs.get_text(a, meta.id)
    assert "siehe Anhang" not in docs.get_text(a, meta.id)


def test_without_keyword_only_attachments_are_imported(imap_archive):
    a = imap_archive
    raw = eml(subject="WG: Rechnung", attachments=[("r.pdf", "application/pdf", text_pdf(["R"]))])
    res = process_message(a, raw, account="acc", fallback_key="7:3")
    meta = docs.load_meta(a, res[0]["document_id"])
    assert meta.mime_type == "application/pdf"


def test_mail_without_files_points_to_the_keyword(imap_archive):
    a = imap_archive
    process_message(a, eml(subject="Nur Text"), account="acc", fallback_key="7:4")
    msg = a.conn.execute("SELECT message FROM ingest_events ORDER BY id DESC").fetchone()[0]
    assert "#mail" in msg


def test_keyword_can_be_switched_off(tmp_path):
    a = Archive(make_settings(tmp_path, imap_host="h", imap_user="u", imap_mail_keyword=""))
    try:
        res = process_message(a, eml(subject="Text #mail"), account="acc", fallback_key="1:1")
        assert res[0]["status"] == "rejected"
    finally:
        a.close()


# --- web ----------------------------------------------------------------------------------------


def test_upload_page_and_attachment_download(tmp_path):
    from fastapi.testclient import TestClient

    from heftig import auth
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    auth.create_user(app.state.archive.conn, "jo", "richtig-langes-passwort")
    try:
        client = TestClient(app)
        csrf = client.post(
            "/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"}
        ).json()["csrf_token"]
        docx = b"PK\x03\x04 Word-Datei"
        raw = eml(attachments=[("notiz.docx", "application/octet-stream", docx)])
        r = client.post(
            "/api/documents",
            files=[("files", ("Mietvertrag.eml", raw, "message/rfc822"))],
            data={"kind": "digital"},
            headers={"X-CSRF-Token": csrf},
        )
        doc_id = r.json()["results"][0]["document_id"]
        process_all(app.state.archive)
        page = client.get(f"/documents/{doc_id}")
        assert page.status_code == 200
        assert "Anhänge der E-Mail" in page.text and "notiz.docx" in page.text
        r = client.get(f"/documents/{doc_id}/mail-attachments/0")
        assert r.status_code == 200 and r.content == docx
        assert r.headers["content-type"] == "application/octet-stream"
        assert "attachment" in r.headers["content-disposition"]
        assert client.get(f"/documents/{doc_id}/mail-attachments/1").status_code == 404
        img = client.get(f"/documents/{doc_id}/pages/1.webp")
        assert img.status_code == 200 and img.content[:4] == b"RIFF"
        assert client.get(f"/documents/{doc_id}/original").content == raw
    finally:
        app.state.archive.close()


def test_renderings_are_cached_and_pruned(archive):
    from heftig.maintenance import prune_mail_renderings

    res = ingest_bytes(archive, eml(), "a.eml")
    process_all(archive)
    cached = list((archive.paths.cache / "mail").glob("*.pdf"))
    assert [f.name.split("-")[0] for f in cached] == [docs.load_meta(archive, res.doc_id).sha256]
    stale = archive.paths.cache / "mail" / f"{'0' * 64}-de-v1.pdf"
    stale.write_bytes(b"%PDF")
    assert prune_mail_renderings(archive) == 1
    assert not stale.exists() and cached[0].exists()
    cached[0].unlink()  # recreated when needed
    meta = docs.load_meta(archive, res.doc_id)
    render_page(archive.paths.resolve(meta.original_relpath), MAIL, 0, 40, 50)
    assert cached[0].exists()


def test_keyword_setting_is_one_word():
    from heftig.web.setup import mail_changes

    form = {"imap_user": "a@example.org", "imap_host": "imap.example.org", "after": "seen"}
    changes, error = mail_changes({**form, "imap_mail_keyword": " #archiv "})
    assert changes["imap_mail_keyword"] == "#archiv" and error is None
    assert mail_changes({**form, "imap_mail_keyword": ""})[0]["imap_mail_keyword"] == ""
    assert mail_changes({**form, "imap_mail_keyword": "zwei Wörter"})[1]
