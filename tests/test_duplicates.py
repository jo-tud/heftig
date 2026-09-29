"""Detection of content duplicates (different files, same document)."""

import logging

from fastapi.testclient import TestClient

from heftig import auth, maintenance
from heftig import documents as docs
from heftig.duplicates import open_pairs, scan_all
from heftig.providers import registry
from heftig.web.app import create_app

from .conftest import FakeExtractor, ScriptedClassifier, ingest_bytes, make_settings, process_all
from .corpus import load_corpus
from .helpers import image_bytes, text_image, text_pdf

INVOICE = (
    "Autowerkstatt Beispiel GmbH\nRechnung Nr. 2026-0815\nDatum: 22.09.2026\n"
    "Inspektion nach Herstellervorgabe, Ölwechsel, Bremsflüssigkeit\nGesamt: 389,00 EUR\n"
    "Zahlbar innerhalb von 14 Tagen ohne Abzug."
)
# the same invoice read by OCR from a phone photo: a few recognition errors
INVOICE_OCR = (
    "Autowerkstatt Beispiel GmbH\nRechnung Nr. 2026-0815\nDatum: 22.09.2026\n"
    "Inspektlon nach Herstellervorgabe, Olwechsel, Bremsflussigkeit\nGesamt: 389,00 EUR\n"
    "Zahlbar innerhalb von 14 Tagen ohne Abzug"
)
META = {
    "title": "Rechnung Inspektion", "document_date": "2026-09-22",
    "document_date_evidence": "22.09.2026", "document_date_confidence": 0.9,
    "correspondent": "Autowerkstatt Beispiel GmbH", "correspondent_confidence": 0.9,
    "document_type": "Rechnung", "document_type_confidence": 0.9,
    "custom_fields": [
        {"key": "Rechnungsnummer", "type": "string", "value": "2026-0815", "evidence": "Rechnung Nr. 2026-0815"},
        {"key": "Betrag", "type": "monetary", "value": "389,00", "currency": "EUR", "evidence": "Gesamt: 389,00 EUR"},
    ],
}  # fmt: skip


def _pdf_and_photo(archive):
    registry.override(extractor=FakeExtractor(pages={1: INVOICE_OCR}),
                      classifier=ScriptedClassifier(default=META))  # fmt: skip
    pdf = ingest_bytes(
        archive, text_pdf([INVOICE.replace("\n", " \n")]), "rechnung.pdf", source="email"
    )
    photo = ingest_bytes(archive, image_bytes(text_image("Foto"), "JPEG"), "IMG_1.jpg", paper=True)
    process_all(archive)
    return pdf.doc_id, photo.doc_id


def test_pdf_and_photo_of_same_invoice_are_listed(archive):
    a, b = _pdf_and_photo(archive)
    pairs = open_pairs(archive.conn)
    assert len(pairs) == 1
    assert {pairs[0]["a"]["id"], pairs[0]["b"]["id"]} == {a, b}
    reasons = " ".join(pairs[0]["reasons"])
    assert "same number" in reasons and "same amount" in reasons
    # nothing merged or deleted automatically
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_monthly_invoices_of_one_sender_are_not_duplicates(archive):
    load_corpus(archive)
    assert scan_all(archive) == 0


def test_same_text_without_metadata_is_found(archive):
    text = (
        "Mietvertrag für die Wohnung im zweiten Obergeschoss links, Kaltmiete und Nebenkosten " * 3
    )
    registry.override(
        extractor=FakeExtractor(pages={1: text}), classifier=ScriptedClassifier(default={})
    )
    ingest_bytes(archive, text_pdf([text[:90], text[90:180]]), "a.pdf")
    ingest_bytes(archive, image_bytes(text_image("x"), "PNG"), "b.png")
    process_all(archive)
    pairs = open_pairs(archive.conn)
    assert pairs and "Text" in pairs[0]["reasons"][0]


def test_keep_both_is_remembered_in_sidecars(archive, tmp_path):
    a, b = _pdf_and_photo(archive)
    from heftig.duplicates import keep_both

    keep_both(archive, a, b)
    assert open_pairs(archive.conn) == []
    assert docs.load_meta(archive, a).not_duplicate_of == [b]
    scan_all(archive)
    assert open_pairs(archive.conn) == []
    # survives losing the database and an export/import
    maintenance.rebuild_db(archive)
    scan_all(archive)
    assert open_pairs(archive.conn) == []
    from heftig.archive import Archive

    exp = maintenance.export_archive(archive, tmp_path / "exp")
    target = Archive(make_settings(tmp_path / "t"))
    maintenance.import_archive(target, exp)
    assert open_pairs(target.conn) == []
    target.close()


def test_review_pages_and_delete_action(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(make_settings(tmp_path))
    auth.create_user(app.state.archive.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    csrf = c.post(
        "/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"}
    ).json()["csrf_token"]
    a, b = _pdf_and_photo(app.state.archive)
    assert (
        "mögliche Dublette" in c.get("/inbox").text and "/duplicates/next" in c.get("/inbox").text
    )
    assert "Mögliche Dublette von" in c.get(f"/documents/{a}").text
    page = c.get(f"/duplicates/{a}/{b}")
    assert page.status_code == 200 and 'Beide<span class="long"> behalten</span>' in page.text
    assert c.get("/api/duplicates").json()[0]["score"] > 0.5
    # into the Papierkorb (undo offered on the next page) - no extra confirmation step
    r = c.post("/duplicates/action", data={"csrf_token": csrf, "a": a, "b": b, "action": "delete_b"},
               follow_redirects=False)  # fmt: skip
    assert "undo=doc" in r.headers["location"]
    # no pair left: back to the inbox with a note
    assert r.headers["location"].startswith("/inbox?") and "Keine+weiteren" in r.headers["location"]
    assert c.get(f"/api/documents/{b}").status_code == 404
    assert c.get("/api/duplicates").json() == []
    app.state.archive.close()


def test_after_a_decision_the_next_pair_opens(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(make_settings(tmp_path))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    csrf = c.post(
        "/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"}
    ).json()["csrf_token"]
    a, b = _pdf_and_photo(arch)
    # a second photo of the same invoice -> three pairs
    ingest_bytes(arch, image_bytes(text_image("Foto 2"), "JPEG"), "IMG_2.jpg", paper=True)
    process_all(arch)
    assert len(c.get("/api/duplicates").json()) == 3
    r = c.post("/duplicates/action", data={"csrf_token": csrf, "a": a, "b": b, "action": "delete_b",
               "confirm": b}, follow_redirects=False)  # fmt: skip
    loc = r.headers["location"]
    assert loc.startswith("/duplicates/") and "N%C3%A4chstes+Paar+%281+offen%29" in loc
    assert c.get(loc).status_code == 200
    arch.close()


def test_cli_duplicates(archive, capsys, monkeypatch):
    from heftig import cli

    a, b = _pdf_and_photo(archive)
    monkeypatch.setattr(cli, "_archive", lambda: archive)
    assert cli.main(["duplicates", "--scan"]) == 0
    out = capsys.readouterr().out
    assert "1 possible duplicate" in out and "/duplicates/" in out


def _template_invoice(month: int, amount: str, number: str) -> list[str]:
    """Pages of a template invoice: only date, period, amount and invoice number differ; the
    rest (terms, notes) is identical every month - > 98 % of the words, as template invoices are."""
    head = (
        f"Nordwind Hosting GmbH\nRechnung Nr. {number}\nDatum: 01.{month:02d}.2022\n"
        f"Leistungszeitraum {month:02d}/2022\nRechnungsbetrag: {amount} EUR\n"
        "Bankverbindung DE12 3456 7890 1234 5678 90\nKundennummer 77881234\n"
        "Mandatsreferenz ATP-4711"
    )
    terms = [
        "\n".join(f"Bedingung{p}{n} gilt für Klausel{p}{n} Vertragswerk" for n in range(40))
        for p in range(5)
    ]
    return [head, *terms]


def test_template_invoices_with_recurring_numbers_are_not_duplicates(archive):
    """Monthly template invoices: near-identical text and the same IBAN, customer number and
    mandate reference, but other dates/amounts/numbers - not duplicates."""
    by = {}
    for m in range(1, 7):
        amount = "149,00" if m % 2 else "151,20"  # some months with the same amount
        by[f"nordwind_{m}.pdf"] = {
            "title": f"Rechnung Nordwind {m}/2022", "document_date": f"2022-{m:02d}-01",
            "document_date_evidence": f"01.{m:02d}.2022", "document_date_confidence": 0.9,
            "correspondent": "Nordwind Hosting GmbH", "correspondent_confidence": 0.9,
            "document_type": "Rechnung", "document_type_confidence": 0.9,
            "custom_fields": [
                {"key": "IBAN", "type": "string", "value": "DE12 3456 7890 1234 5678 90",
                 "evidence": "DE12 3456 7890 1234 5678 90"},
                {"key": "Kundennummer", "type": "string", "value": "77881234",
                 "evidence": "Kundennummer 77881234"},
                {"key": "Betrag", "type": "monetary", "value": amount, "currency": "EUR",
                 "evidence": f"Rechnungsbetrag: {amount} EUR"},
            ],
        }  # fmt: skip
    registry.override(classifier=ScriptedClassifier(by_filename=by))
    for m in range(1, 7):
        amount = by[f"nordwind_{m}.pdf"]["custom_fields"][2]["value"]
        pdf = text_pdf(_template_invoice(m, amount, f"2022-{m:03d}"))
        ingest_bytes(archive, pdf, f"nordwind_{m}.pdf")
    process_all(archive)
    assert scan_all(archive) == 0
    # the same invoice twice (e.g. downloaded twice, other file) is still found
    again = _template_invoice(3, "149,00", "2022-003")
    again[-1] += "\nHinweis: erneut heruntergeladen"
    ingest_bytes(archive, text_pdf(again), "nordwind_3.pdf", source="email")
    process_all(archive)
    pairs = open_pairs(archive.conn)
    assert len(pairs) == 1 and "same document date" in pairs[0]["reasons"]


def test_template_invoices_naming_the_month_in_words_are_not_duplicates(archive):
    """No date recognised, month only as a word, all numbers equal."""

    terms = "\n".join(f"Bedingung{n} gilt für Klausel{n} Vertragswerk" for n in range(40))

    def invoice(month: str) -> list[str]:
        head = f"Nordwind Hosting GmbH\nIhre Rechnung für {month} 2022\nBetrag 149,00 EUR\nKundennummer 77881234"
        return [head, terms]

    meta = {"title": "Rechnung Nordwind", "correspondent": "Nordwind Hosting GmbH",
            "correspondent_confidence": 0.9}  # fmt: skip
    registry.override(classifier=ScriptedClassifier(default=meta))
    for month in ("Januar", "Februar", "März"):
        ingest_bytes(archive, text_pdf(invoice(month)), f"{month}.pdf")
    process_all(archive)
    assert scan_all(archive) == 0
    # English invoices with abbreviated months, as foreign providers write them ("April",
    # "Jun 27"); other months than above - "Januar" and "January" are the same month
    for month in ("April", "June"):
        en = [
            f"Nordwind Hosting Ltd\nInvoice {month} 2022\nDue {month[:3]} 27\nAmount 149.00 EUR",
            terms,
        ]
        ingest_bytes(archive, text_pdf(en), f"en_{month}.pdf")
    process_all(archive)
    assert scan_all(archive) == 0


def test_misread_numbers_do_not_hide_the_same_invoice():
    from heftig.duplicates import Profile, _tokens, compare

    words = ["Stadtwerke", "Beispielstadt", "Rechnung", "Strom", "Abschlag", "Kunde", "Zählernummer", "Verbrauch", "Arbeitspreis", "Grundpreis", "Netto", "Brutto", "Umsatzsteuer", "Zahlbar", "Überweisung"]  # fmt: skip
    nums = ["12345678", "2025", "39,95", "12,50", "1234", "567", "19", "7,59", "47,54", "4711",
            "0815", "180", "0,32", "57,60", "3456", "7890", "4455", "2211", "990", "88"]  # fmt: skip
    misread = list(nums)
    misread[3], misread[8 + 5] = "12,58", "57,68"  # the photo's OCR got two numbers wrong

    def prof(pid, ns, number="12345678", day="2025-03-15"):
        return Profile(pid, "t", 1, day, _tokens(" ".join(words + ns)),
                       idents={"rechnungsnummer": number}, amounts={"betrag": 47.54})  # fmt: skip

    assert compare(prof("a", nums), prof("b", misread))[0]
    # ... but another invoice number or date still rules it out
    assert not compare(prof("a", nums), prof("c", misread, number="12345679"))[0]
    assert not compare(prof("a", nums), prof("d", misread, day="2025-04-15"))[0]


def test_a_stamp_naming_the_month_short_is_the_same_period():
    """ "PAID Sep 14" stamped on the paper copy of a letter from September."""
    from heftig.duplicates import Profile, _tokens, compare

    text = "Summit Electronics Receipt September 12, 2026 TV 1,199.00 Warranty 99.00 Total 1,298.00"
    a = Profile("a", "", 1, "2026-09-12", _tokens(text))
    b = Profile("b", "", 1, "2026-09-12", _tokens(text + " PAID Sep 14"))
    assert "different period in the text" not in compare(a, b)[2]


def test_statements_of_different_periods_are_not_duplicates():
    """Quarterly statements: the same words and balance, only the period dates differ."""
    from heftig.duplicates import Profile, _dates, _tokens, compare

    body = " ".join(f"Kontoabschluss Tagesgeldkonto Zeile {n} Abschlussbetrag" for n in range(30))

    def prof(pid, text):
        return Profile(pid, "t", 1, None, _tokens(body + text), amounts={"kontostand": 0.12},
                       dates=_dates(text))  # fmt: skip

    q3 = prof("a", " Abschluss vom 30.06.2021 bis 30.09.2021 per 30.09.2021")
    q4 = prof("b", " Abschluss vom 30.09.2021 bis 31.12.2021 per 31.12.2021")
    ok, _, why = compare(q3, q4)
    assert not ok and "different dates in the text" in why
    # a date only on one copy (a received stamp) or one misread digit: still the same letter
    stamped = prof(
        "c", " Abschluss vom 30.06.2021 bis 30.09.2021 per 30.09.2021 Eingang 04.10.2021"
    )
    assert compare(q3, stamped)[0]
    misread = prof("d", " Abschluss vom 30.06.2021 bis 30.09.2021 per 30.08.2021")
    assert compare(q3, misread)[0]


def test_open_pairs_are_checked_again_when_the_rules_change(archive, monkeypatch):
    from heftig import duplicates

    a = ingest_bytes(archive, text_pdf([BODY_Q3]), "q3.pdf").doc_id
    b = ingest_bytes(archive, text_pdf([BODY_Q4]), "q4.pdf").doc_id
    process_all(archive)
    with duplicates.write_tx(archive.conn):  # as found by the rules before
        x, y = sorted((a, b))
        archive.conn.execute(
            "INSERT OR IGNORE INTO duplicate_candidates(doc_a, doc_b, score, reasons, status, "
            "created_at) VALUES(?,?,0.9,'[]','open','2026-09-28T00:00:00Z')", (x, y))  # fmt: skip
    assert duplicates.recheck_open(archive) == 1
    assert not duplicates.open_pairs(archive.conn)


BODY_Q3 = "\n".join(f"Kontoabschluss Zeile {n} Tagesgeldkonto" for n in range(20)) + (
    "\nAbschluss vom 30.06.2021 bis 30.09.2021"
)
BODY_Q4 = BODY_Q3.replace("30.06.2021 bis 30.09.2021", "30.09.2021 bis 31.12.2021")
