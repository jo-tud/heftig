"""Combining documents into one (separately scanned pages, signed + unsigned copy)."""

import io
import logging

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

from heftig import auth, combine, trash
from heftig import documents as docs
from heftig.search import SearchParams, search

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import image_bytes, multipage_tiff, text_image, text_pdf

PASSWORD = "richtig-langes-passwort"


def ids(archive, q):
    return [i["id"] for i in search(archive.conn, SearchParams(q=q, per_page=100)).items]


def test_pages_text_and_user_data_are_combined(archive):
    a = ingest_bytes(archive, text_pdf(["Mietvertrag Seite eins", "Mietvertrag Seite zwei"]),
                     "a.pdf", source="scanner").doc_id  # fmt: skip
    photo = image_bytes(text_image("Anlage Hausordnung"), "JPEG")
    b = ingest_bytes(archive, photo, "b.jpg").doc_id
    tiff = ingest_bytes(archive, multipage_tiff(["Nachtrag eins", "Nachtrag zwei"]), "c.tif").doc_id
    process_all(archive)
    docs.update_fields(archive, a, {"title": "Mietvertrag Wohnung"}, {})
    docs.add_note(archive, b, "Original im Ordner Wohnen")
    docs.add_attachment(archive, b, io.BytesIO(b"Quittung"), "q.txt")
    docs.mark_filed(archive, a)
    runs_before = archive.conn.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0]

    new = combine.combine(archive, [a, b, tiff])
    assert new.page_count == 5 and new.mime_type == "application/pdf"
    pdf = pdfium.PdfDocument(str(archive.paths.resolve(new.original_relpath)))
    assert len(pdf) == 5
    assert "Seite zwei" in pdf[1].get_textpage().get_text_bounded()  # text layer kept
    pdf.close()
    tp = docs.load_text_pages(archive, new.id)
    # the photo's text is carried over (the fake OCR numbers the page it read: its own page 1)
    assert [p.page for p in tp.pages] == [1, 2, 3, 4, 5] and tp.pages[2].text == "MOCK OCR Seite 1"
    assert new.title == "Mietvertrag Wohnung" and new.field_locks.get("title")
    assert [n.text for n in new.notes] == ["Original im Ordner Wohnen"]
    assert len(new.attachments) == 1 and new.filed_at and new.source == "scanner"
    assert [p["id"] for p in new.source_details["combined_from"]] == [a, b, tiff]
    # only the classification runs again - no second text recognition
    job = archive.conn.execute("SELECT payload FROM jobs WHERE doc_id=?", (new.id,)).fetchone()
    assert '"stages": ["classify"]' in job[0]
    process_all(archive)
    kinds = [r[0] for r in archive.conn.execute(
        "SELECT task FROM processing_runs ORDER BY id")][runs_before:]  # fmt: skip
    assert "extract" not in kinds
    meta = docs.load_meta(archive, new.id)
    assert meta.title == "Mietvertrag Wohnung" and meta.text_status == "ok"
    # the parts are kept as source documents (not in the Papierkorb), in page order; search
    # finds only the new document
    assert trash.listing(archive) == []
    group = trash.sources_listing(archive)[0]
    assert group["batch"] == combine.batch_for(new.id) and group["targets"][0]["id"] == new.id
    assert [it["id"] for it in group["items"]] == [a, b, tiff]
    assert ids(archive, "Mietvertrag") == [new.id]


def test_undo_restores_the_parts(archive):
    a = ingest_bytes(archive, text_pdf(["Vertrag ohne Unterschrift"]), "a.pdf").doc_id
    b = ingest_bytes(archive, text_pdf(["Vertrag mit Unterschrift"]), "b.pdf").doc_id
    process_all(archive)
    filed = docs.mark_filed(archive, a).filing_sequence
    new = combine.combine(archive, [b, a])
    assert new.filing_sequence == filed  # the paper stays where it is
    r = combine.undo(archive, new.id)
    assert r["restored"] == 2 and set(ids(archive, "Vertrag")) == {a, b}
    assert docs.load_meta(archive, a).filing_sequence == filed
    assert [g["items"][0]["id"] for g in trash.listing(archive)] == [new.id]


def test_refused_while_processing_or_alone(archive):
    a = ingest_bytes(archive, text_pdf(["Brief Teil eins"]), "a.pdf").doc_id
    b = ingest_bytes(archive, text_pdf(["Brief Teil zwei"]), "b.pdf").doc_id
    with pytest.raises(combine.CombineError, match="processed"):
        combine.combine(archive, [a, b])
    process_all(archive)
    with pytest.raises(combine.CombineError, match="two"):
        combine.combine(archive, [a, a])
    assert set(ids(archive, "Brief")) == {a, b}


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path, auto_resolve_identical=False))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    yield arch, c, csrf
    arch.close()


def test_combine_page_and_undo_bar(web):
    arch, c, csrf = web
    a = ingest_bytes(arch, text_pdf(["Kündigung Seite eins"]), "a.pdf").doc_id
    b = ingest_bytes(arch, text_pdf(["Kündigung Seite zwei"]), "b.pdf").doc_id
    process_all(arch)
    assert f"/combine?ids={a}" in c.get(f"/documents/{a}").text
    page = c.get(f"/combine?ids={a}").text  # the neighbour in scan order is offered
    assert f"ids={a}&amp;ids={b}" in page
    page = c.get(f"/combine?ids={a}&ids={b}").text
    assert "2 Seiten" in page and "Zusammenfügen" in page
    r = c.post("/combine", data={"csrf_token": csrf, "ids": [a, b]}, follow_redirects=False)
    loc = r.headers["location"]
    assert "undo=batch%3Acombine-" in loc
    page = c.get(loc).text
    assert "Zusammengefügt" in page and "S. 1: „" in page
    r = c.post("/trash/action", data={"csrf_token": csrf, "action": "restore",
               "target": loc.split("undo=")[1].replace("%3A", ":")}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/documents/{a}")
    assert set(ids(arch, "Kündigung")) == {a, b}


def test_combine_from_duplicate_view(web):
    arch, c, csrf = web
    text = "\n".join(f"Paragraph {n}: Vertragsbedingung und Kündigungsfrist" for n in range(12))
    a = ingest_bytes(arch, text_pdf([text]), "a.pdf").doc_id
    b = ingest_bytes(arch, text_pdf([text]) + b"\n%x", "b.pdf").doc_id
    process_all(arch)
    html = c.get(f"/duplicates/{a}/{b}").text
    assert 'value="combine"' in html
    r = c.post("/duplicates/action", data={"csrf_token": csrf, "a": a, "b": b,
               "action": "combine"}, follow_redirects=False)  # fmt: skip
    assert "undo=batch%3Acombine-" in r.headers["location"]
    new = ids(arch, "Kündigungsfrist")
    assert len(new) == 1 and docs.load_meta(arch, new[0]).page_count == 2


def test_api(web):
    arch, c, csrf = web
    a = ingest_bytes(arch, text_pdf(["Zeugnis Seite eins"]), "a.pdf").doc_id
    b = ingest_bytes(arch, text_pdf(["Zeugnis Seite zwei"]), "b.pdf").doc_id
    h = {"X-CSRF-Token": csrf}
    assert c.post("/api/documents/combine", json={"ids": [a, b]}, headers=h).status_code == 409
    process_all(arch)
    r = c.post("/api/documents/combine", json={"ids": [a, b]}, headers=h)
    new = r.json()["document_id"]
    assert r.status_code == 201 and ids(arch, "Zeugnis") == [new]
    assert set(c.post(f"/api/documents/{new}/uncombine", headers=h).json()["restored"]) == {a, b}
