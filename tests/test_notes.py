"""User notes and attachments on documents."""

import hashlib
import io
import logging
import os

import pytest
from fastapi.testclient import TestClient

from heftig import auth, maintenance
from heftig import documents as docs
from heftig.archive import Archive
from heftig.db import set_meta, write_tx
from heftig.processing import reprocess
from heftig.providers import registry
from heftig.search import SearchParams, search
from heftig.web.app import create_app

from .conftest import ScriptedClassifier, ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(make_settings(tmp_path, max_upload_mb=1))
    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    doc_id = ingest_bytes(app.state.archive, text_pdf(["Rechnung Nr. 4711"]), "r.pdf").doc_id
    yield app, c, {"X-CSRF-Token": csrf}, doc_id
    app.state.archive.close()


def ids(archive, q):
    return [i["id"] for i in search(archive.conn, SearchParams(q=q)).items]


def test_notes_crud_search_and_ai_never_touches_them(archive):
    registry.override(classifier=ScriptedClassifier(default={"title": "KI"}))
    r = ingest_bytes(archive, text_pdf(["Bescheid über Grundsteuer"]), "b.pdf")
    process_all(archive)
    m = docs.add_note(archive, r.doc_id, "Widerspruch eingelegt am 3.10.\r\nAktenzeichen W-77")
    note = m.notes[0]
    assert note.text == "Widerspruch eingelegt am 3.10.\nAktenzeichen W-77"
    hits = search(archive.conn, SearchParams(q="Widerspruch")).items
    assert [h["id"] for h in hits] == [r.doc_id] and "Note/attachment" in hits[0]["reasons"]
    reprocess(archive, [r.doc_id], ["extract", "classify"])
    process_all(archive)
    assert docs.load_meta(archive, r.doc_id).notes[0].text == note.text
    docs.edit_note(archive, r.doc_id, note.id, "Widerspruch zurückgenommen")
    assert docs.load_meta(archive, r.doc_id).notes[0].updated_at
    assert ids(archive, "zurückgenommen") == [r.doc_id]
    docs.delete_note(archive, r.doc_id, note.id)
    assert ids(archive, "zurückgenommen") == []
    with pytest.raises(docs.EditError):
        docs.add_note(archive, r.doc_id, "   ")


def test_attachments_are_stored_byte_identical_and_served_safely(web):
    app, c, h, doc_id = web
    evil = b"<html><script>alert(1)</script></html>"
    pdf = text_pdf(["Kuendigungsbestaetigung"])
    r = c.post(f"/api/documents/{doc_id}/attachments", headers=h,
               files=[("files", ("bestaetigung.pdf", pdf, "application/pdf")),
                      ("files", ("seite.html", evil, "text/html"))],
               data={"description": "Bestätigung vom Anbieter"})  # fmt: skip
    assert r.status_code == 201
    a_pdf, a_html = r.json()["attachments"]
    assert (
        a_pdf["mime_type"] == "application/pdf"
        and a_html["mime_type"] == "application/octet-stream"
    )
    assert a_pdf["sha256"] == hashlib.sha256(pdf).hexdigest()
    got = c.get(f"/api/documents/{doc_id}/attachments/{a_pdf['id']}?inline=true")
    assert got.content == pdf and got.headers["content-disposition"].startswith("inline")
    html = c.get(f"/documents/{doc_id}/attachments/{a_html['id']}?inline=1")
    assert html.headers["content-type"] == "application/octet-stream"
    assert html.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in html.headers["content-security-policy"]
    # attachment names and descriptions are searchable
    assert doc_id in ids(app.state.archive, "Bestätigung")
    # limits and validation
    big = io.BytesIO(b"x" * (2 * 1024 * 1024))
    r = c.post(
        f"/api/documents/{doc_id}/attachments", headers=h, files=[("files", ("big.bin", big))]
    )
    assert r.status_code == 422 and "größer" in r.json()["error"]["message"]
    assert c.get(f"/api/documents/{doc_id}/attachments/{'0' * 32}").status_code == 404
    assert c.get(f"/api/documents/{doc_id}/attachments/..%2F..%2Fx").status_code in (404, 422)
    # delete one attachment, then the whole document
    path = docs.attachment_path(app.state.archive, doc_id, a_html["id"])
    assert (
        c.delete(f"/api/documents/{doc_id}/attachments/{a_html['id']}", headers=h).status_code
        == 200
    )
    assert not path.exists()
    ddir = app.state.archive.paths.doc_dir(doc_id)
    c.delete(f"/api/documents/{doc_id}?confirm={doc_id}", headers=h)
    assert not ddir.exists()


def test_ui_forms_for_notes_and_attachments(web):
    app, c, h, doc_id = web
    csrf = h["X-CSRF-Token"]
    r = c.post(f"/documents/{doc_id}/notes", data={"csrf_token": csrf, "action": "add", "text": "bezahlt am 1.10."},
               follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    r = c.post(f"/documents/{doc_id}/notes", data={"csrf_token": csrf, "action": "attach", "description": "Foto"},
               files=[("files", ("beleg.png", b"\x89PNG\r\n\x1a\nxxxx", "image/png"))], follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    page = c.get(f"/documents/{doc_id}").text
    assert "bezahlt am 1.10." in page and "beleg.png" in page and "ansehen" in page


def test_export_import_check_include_notes_and_attachments(archive, tmp_path):
    r = ingest_bytes(archive, text_pdf(["Vertrag"]), "v.pdf")
    docs.add_note(archive, r.doc_id, "Kündigungsfrist 3 Monate")
    docs.add_attachment(archive, r.doc_id, io.BytesIO(b"Anlage A"), "anlage.txt", "AGB")
    assert maintenance.check(archive)["ok"]
    exp = maintenance.export_archive(archive, tmp_path / "exp")
    target = Archive(make_settings(tmp_path / "t"))
    rep = maintenance.import_archive(target, exp)
    assert rep["imported"] == 1 and not rep["warnings"]
    m = docs.load_meta(target, r.doc_id)
    assert m.notes[0].text == "Kündigungsfrist 3 Monate"
    assert docs.attachment_path(target, r.doc_id, m.attachments[0].id).read_bytes() == b"Anlage A"
    assert ids(target, "Kündigungsfrist") == [r.doc_id]
    assert maintenance.check(target)["ok"]
    # tampering is detected
    p = docs.attachment_path(target, r.doc_id, m.attachments[0].id)
    os.chmod(p, 0o600)
    p.write_bytes(b"manipuliert")
    assert "attachment_hash_mismatch" in {i["kind"] for i in maintenance.check(target)["issues"]}
    target.close()


def test_index_is_rebuilt_after_schema_migration(tmp_path):
    s = make_settings(tmp_path)
    a = Archive(s)
    r = ingest_bytes(a, text_pdf(["Wichtiger Brief"]), "w.pdf")
    docs.add_note(a, r.doc_id, "Notiztext")
    with write_tx(a.conn):
        a.conn.execute("DELETE FROM doc_fts")
        set_meta(a.conn, "index_rebuild_required", "1")
    a.close()
    b = Archive(s)
    assert ids(b, "Notiztext") == [r.doc_id]
    b.close()


def test_attachments_live_next_to_originals_and_are_shared(archive):
    a = ingest_bytes(archive, text_pdf(["A"]), "a.pdf").doc_id
    b = ingest_bytes(archive, text_pdf(["B"]), "b.pdf").doc_id
    data = b"gemeinsame Anlage"
    ma = docs.add_attachment(archive, a, io.BytesIO(data), "anlage.txt")
    mb = docs.add_attachment(archive, b, io.BytesIO(data), "anlage.txt")
    rel = ma.attachments[0].relpath
    assert rel == mb.attachments[0].relpath
    assert rel.startswith("originals/attachments/") and rel.endswith(".txt")
    path = archive.paths.resolve(rel)
    assert path.read_bytes() == data
    docs.delete_attachment(archive, a, ma.attachments[0].id)
    assert path.exists()  # still used by document b
    docs.delete_document(archive, b)
    assert not path.exists()  # last reference gone
    assert maintenance.check(archive)["ok"]


def test_note_autosave_adds_then_edits_the_same_note(web):
    """The page saves a note when its field is left: the first save adds it, later ones change
    that note (no second copy), an emptied note is deleted. The answer carries the revision,
    which the other forms on the page need."""
    app, c, h, doc_id = web
    a = app.state.archive
    form = {"csrf_token": h["X-CSRF-Token"], "autosave": "1"}
    r = c.post(f"/documents/{doc_id}/notes", data={**form, "action": "add", "text": "Widerspruch"})
    data = r.json()
    m = docs.load_meta(a, doc_id)
    assert data["ok"] and data["note_id"] == m.notes[0].id and data["revision"] == m.revision
    r = c.post(f"/documents/{doc_id}/notes",
               data={**form, "action": "save", "note_id": data["note_id"], "text": "Widerspruch am 3.10."})  # fmt: skip
    assert r.json()["ok"] and not r.json()["deleted"]
    assert [n.text for n in docs.load_meta(a, doc_id).notes] == ["Widerspruch am 3.10."]
    r = c.post(f"/documents/{doc_id}/notes", data={**form, "action": "add", "text": "  "})
    assert r.status_code == 400 and r.json()["error"]
    r = c.post(f"/documents/{doc_id}/notes",
               data={**form, "action": "save", "note_id": data["note_id"], "text": ""})  # fmt: skip
    assert r.json()["deleted"] and r.json()["note_id"] is None
    assert docs.load_meta(a, doc_id).notes == []
    page = c.get(f"/documents/{doc_id}").text
    assert 'class="note-add"' in page and "tagbox.js" in page and "data-tagbox" in page
