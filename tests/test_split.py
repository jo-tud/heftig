"""Splitting a document: cut into parts, pages moved, turned or left out."""

import io
import logging

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

from heftig import auth, split, trash
from heftig import documents as docs
from heftig.search import SearchParams, search

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import multipage_tiff, text_pdf

PASSWORD = "richtig-langes-passwort"


def ids(archive, q):
    return [i["id"] for i in search(archive.conn, SearchParams(q=q, per_page=100)).items]


def pdf_texts(archive, meta):
    pdf = pdfium.PdfDocument(str(archive.paths.resolve(meta.original_relpath)))
    try:
        return [pdf[i].get_textpage().get_text_bounded() for i in range(len(pdf))]
    finally:
        pdf.close()


def four_pages(archive, **kw):
    pages = ["Mietvertrag Seite eins", "Mietvertrag Seite zwei", "leer", "Stromrechnung Juni"]
    return ingest_bytes(archive, text_pdf(pages), "scan.pdf", **kw).doc_id


def test_parts_pages_text_and_user_data(archive):
    orig = four_pages(archive, source="scanner")
    process_all(archive)
    docs.update_fields(archive, orig, {"title": "Mietvertrag Wohnung"}, {})
    docs.add_note(archive, orig, "Original im Ordner Wohnen")
    docs.add_attachment(archive, orig, io.BytesIO(b"Quittung"), "q.txt")
    filed = docs.mark_filed(archive, orig)
    runs_before = archive.conn.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0]

    # page 3 left out, page 4 a document of its own and turned
    first, second = split.split(archive, orig, [[2, 1], [4]], {4: 90})
    assert (first.page_count, second.page_count) == (2, 1)
    assert pdf_texts(archive, first) == ["Mietvertrag Seite zwei", "Mietvertrag Seite eins"]
    assert pdf_texts(archive, second) == ["Stromrechnung Juni"]
    tp = docs.load_text_pages(archive, second.id)
    assert [p.page for p in tp.pages] == [1] and "Stromrechnung" in tp.pages[0].text
    assert second.page_rotation == {1: 90} and first.page_rotation == {}
    # the first part keeps the original's data and its place in the binder
    assert first.title == "Mietvertrag Wohnung" and first.field_locks.get("title")
    assert [n.text for n in first.notes] == ["Original im Ordner Wohnen"]
    assert len(first.attachments) == 1
    assert first.filing_sequence == filed.filing_sequence and first.paper_location is None
    # the others start fresh, but know where they came from and where their paper is
    assert not second.notes and not second.attachments and not second.field_locks.get("title")
    assert second.source == "scanner" and second.paper and second.filing_sequence is None
    assert "Mietvertrag Wohnung" in second.paper_location
    assert second.source_details["split_from"] == {
        "id": orig, "title": "Mietvertrag Wohnung", "pages": [4], "part": 2, "parts": 2,
    }  # fmt: skip
    # only the classification runs again - no second text recognition
    for part in (first, second):
        job = archive.conn.execute("SELECT payload FROM jobs WHERE doc_id=?", (part.id,)).fetchone()
        assert '"stages": ["classify"]' in job[0]
    process_all(archive)
    kinds = [r[0] for r in archive.conn.execute(
        "SELECT task FROM processing_runs ORDER BY id")][runs_before:]  # fmt: skip
    assert "extract" not in kinds
    assert docs.load_meta(archive, first.id).title == "Mietvertrag Wohnung"
    # the original is in the Papierkorb; search finds the parts
    group = trash.listing(archive)[0]
    assert group["batch"] == split.batch_for(orig) and [i["id"] for i in group["items"]] == [orig]
    assert orig not in ids(archive, "Mietvertrag")
    assert ids(archive, "Stromrechnung") == [second.id]
    assert [r["id"] for r in split.parts_of(archive, orig)] == [first.id, second.id]


def test_image_frames(archive):
    orig = ingest_bytes(archive, multipage_tiff(["Brief A", "Brief B", "Brief C"]), "s.tif").doc_id
    process_all(archive)
    a, b = split.split(archive, orig, [[1], [3, 2]])
    assert a.mime_type == b.mime_type == "application/pdf"
    assert (a.page_count, b.page_count) == (1, 2)
    # the fake OCR numbers the page it read: the text moves with its page
    assert [p.text for p in docs.load_text_pages(archive, b.id).pages] == [
        "MOCK OCR Seite 3", "MOCK OCR Seite 2",
    ]  # fmt: skip


def test_rearranged_only_is_one_new_document(archive):
    orig = four_pages(archive)
    process_all(archive)
    (new,) = split.split(archive, orig, [[1, 2, 4]])
    assert new.page_count == 3 and "(bearbeitet)" in new.original_filename
    assert new.source_details["split_from"]["parts"] == 1


def test_undo_restores_the_original(archive):
    orig = four_pages(archive)
    process_all(archive)
    filed = docs.mark_filed(archive, orig).filing_sequence
    parts = split.split(archive, orig, [[1, 2], [3, 4]])
    r = split.undo(archive, orig)
    assert r["ids"] == [orig] and ids(archive, "Mietvertrag") == [orig]
    assert docs.load_meta(archive, orig).filing_sequence == filed
    assert {i["id"] for g in trash.listing(archive) for i in g["items"]} == {p.id for p in parts}
    assert split.parts_of(archive, orig) == []


@pytest.mark.parametrize(
    ("parts", "turns", "match"),
    [
        ([[1, 2, 3, 4]], {}, "Nothing"),
        ([[]], {}, "at least one"),
        ([[1, 2], [2, 3]], {}, "only be in one"),
        ([[1, 5]], {}, "no page 5"),
        ([[1], [2]], {1: 45}, "90"),
    ],
)
def test_refused(archive, parts, turns, match):
    orig = four_pages(archive)
    process_all(archive)
    with pytest.raises(split.SplitError, match=match):
        split.split(archive, orig, parts, turns)
    assert ids(archive, "Mietvertrag") == [orig]


def test_refused_while_processing(archive):
    orig = four_pages(archive)
    with pytest.raises(split.SplitError, match="processed"):
        split.split(archive, orig, [[1], [2]])


def test_turning_alone_is_a_change(archive):
    orig = four_pages(archive)
    process_all(archive)
    (new,) = split.split(archive, orig, [[1, 2, 3, 4]], {2: 180})
    assert new.page_rotation == {2: 180}


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


def test_split_page_and_undo(web):
    arch, c, csrf = web
    orig = four_pages(arch)
    process_all(arch)
    assert f"/documents/{orig}/split" in c.get(f"/documents/{orig}").text
    page = c.get(f"/documents/{orig}/split").text
    assert 'data-pages="4"' in page and "split.js" in page
    r = c.post(f"/documents/{orig}/split", data={"csrf_token": csrf, "layout": "1,2|4r90"},
               follow_redirects=False)  # fmt: skip
    loc = r.headers["location"]
    assert "undo=batch%3Asplit-" in loc
    first, second = split.parts_of(arch, orig)
    assert loc.startswith(f"/documents/{first['id']}")
    page = c.get(loc).text
    assert "Als neue Dokumente gespeichert" in page and "Außerdem angelegt" in page
    assert f"/documents/{second['id']}" in page
    r = c.post("/trash/action", data={"csrf_token": csrf, "action": "restore",
               "target": loc.split("undo=")[1].replace("%3A", ":")}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/documents/{orig}")
    assert ids(arch, "Mietvertrag") == [orig]


def test_split_page_shows_problems(web):
    arch, c, csrf = web
    orig = four_pages(arch)
    process_all(arch)
    r = c.post(f"/documents/{orig}/split", data={"csrf_token": csrf, "layout": "1,2,3,4"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/documents/{orig}/split?")
    assert "Nichts zu" in c.get(r.headers["location"]).text
    r = c.post(f"/documents/{orig}/split", data={"csrf_token": csrf, "layout": "1,x"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/documents/{orig}/split?")


def test_api(web):
    arch, c, csrf = web
    orig = four_pages(arch)
    h = {"X-CSRF-Token": csrf}
    body = {"parts": [[1, 2], [4]], "rotation": {"4": 270}}
    assert c.post(f"/api/documents/{orig}/split", json=body, headers=h).status_code == 409
    process_all(arch)
    r = c.post(f"/api/documents/{orig}/split", json=body, headers=h)
    assert r.status_code == 201 and r.json()["trash_batch"] == f"split-{orig}"
    first, second = r.json()["document_ids"]
    assert docs.load_meta(arch, second).page_rotation == {1: 270}
    assert c.post(f"/api/documents/{orig}/unsplit", headers=h).json()["restored"] == [orig]
    assert ids(arch, "Mietvertrag") == [orig]
