"""Papierkorb and bulk deletion from a search (with safeguards against accidents)."""

import io
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from heftig import auth, maintenance, trash
from heftig import documents as docs
from heftig.db import iso, utcnow
from heftig.search import SearchParams, search

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"


def ids(archive, q=""):
    return [i["id"] for i in search(archive.conn, SearchParams(q=q, per_page=100)).items]


def test_trash_restore_and_purge(archive):
    d = ingest_bytes(archive, text_pdf(["Kaufvertrag Fahrrad"]), "k.pdf").doc_id
    process_all(archive)
    docs.add_attachment(archive, d, io.BytesIO(b"Quittung"), "q.txt")
    meta = docs.load_meta(archive, d)
    orig = archive.paths.resolve(meta.original_relpath)
    att = archive.paths.resolve(meta.attachments[0].relpath)
    trash.trash_document(archive, d, reason="Test")
    assert ids(archive, "Kaufvertrag") == [] and orig.exists() and att.exists()
    assert (archive.paths.trash / d / "metadata.json").exists()
    assert maintenance.check(archive)["ok"]  # original held by the trash is no orphan
    with pytest.raises(docs.DocumentNotFound):
        docs.load_meta(archive, d)
    trash.restore(archive, d)
    assert ids(archive, "Kaufvertrag") == [d] and docs.load_meta(archive, d).trashed_at is None
    trash.trash_document(archive, d)
    trash.purge(archive, d)
    assert not orig.exists() and not att.exists() and trash.listing(archive) == []
    assert maintenance.check(archive)["ok"]


def test_same_file_again_while_in_trash(archive):
    pdf = text_pdf(["Mietvertrag"])
    d = ingest_bytes(archive, pdf, "m.pdf").doc_id
    trash.trash_document(archive, d)
    again = ingest_bytes(archive, pdf, "m.pdf").doc_id  # a new document, the original is shared
    assert again != d
    with pytest.raises(trash.TrashError, match="back in the archive"):
        trash.restore(archive, d)
    trash.purge(archive, d)  # must not delete the original the new document uses
    assert archive.paths.resolve(docs.load_meta(archive, again).original_relpath).exists()


def test_rebuild_and_expiry(archive):
    d = ingest_bytes(archive, text_pdf(["Altes Schreiben"]), "a.pdf").doc_id
    process_all(archive)
    trash.trash_document(archive, d)
    maintenance.rebuild_db(archive)
    assert [g["items"][0]["id"] for g in trash.listing(archive)] == [d]
    assert ids(archive, "Schreiben") == []
    archive.conn.execute(
        "UPDATE trash SET trashed_at=? WHERE id=?", (iso(utcnow() - timedelta(days=31)), d)
    )
    assert trash.purge_expired(archive) == 1 and trash.listing(archive) == []


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    a = app.state.archive
    auth.create_user(a.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    yield app, c, csrf
    a.close()


def test_delete_goes_to_trash_with_undo(web):
    app, c, csrf = web
    d = ingest_bytes(
        app.state.archive, text_pdf(["Werbung Zeitschrift"]), "Werbung Zeitschrift.pdf"
    ).doc_id
    r = c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "delete"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == f"/?undo=doc%3A{d}"
    page = c.get(r.headers["location"]).text
    assert "„Werbung Zeitschrift“ in den Papierkorb verschoben." in page and "Rückgängig" in page
    # the bar belongs to this one page: search and filter links do not carry it along
    assert "undo=" not in page
    assert "Werbung Zeitschrift" in c.get("/trash").text
    r = c.post("/trash/action", data={"csrf_token": csrf, "action": "restore", "target": f"doc:{d}"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/documents/{d}")
    # deleting from a search result goes back to that search (not the whole archive)
    r = c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "delete",
               "back": "/?q=Werbung&tag=Post"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == f"/?q=Werbung&tag=Post&undo=doc%3A{d}"
    c.post("/trash/action", data={"csrf_token": csrf, "action": "restore", "target": f"doc:{d}"})
    r = c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "delete",
               "back": "//evil.example/"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == f"/?undo=doc%3A{d}"  # never somewhere else
    c.post("/trash/action", data={"csrf_token": csrf, "action": "restore", "target": f"doc:{d}"})
    # API: delete = trash, restore, purge
    assert c.delete(f"/api/documents/{d}?confirm={d}", headers={"X-CSRF-Token": csrf}).json()[
        "trashed"
    ]
    assert c.post(f"/api/trash/{d}/restore", headers={"X-CSRF-Token": csrf}).json() == {
        "restored": d
    }


def test_bulk_delete_needs_matching_count_and_unchanged_results(web):
    app, c, csrf = web
    a = app.state.archive
    ads = [ingest_bytes(a, text_pdf([f"Werbeprospekt Nummer {n}"]), f"w{n}.pdf").doc_id
           for n in range(3)]  # fmt: skip
    keep = ingest_bytes(a, text_pdf(["Arbeitsvertrag"]), "v.pdf").doc_id
    process_all(a)
    # never for the whole archive
    assert c.get("/bulk-delete").status_code == 400
    page = c.get("/bulk-delete?q=Werbeprospekt").text
    assert "3 Dokumente in den Papierkorb?" in page and "Arbeitsvertrag" not in page
    fp = page.split('name="fingerprint" value="')[1].split('"')[0]
    form = {"csrf_token": csrf, "query": "q=Werbeprospekt", "fingerprint": fp}
    # wrong count: nothing happens
    r = c.post("/bulk-delete", data={**form, "count": "4"}, follow_redirects=False)
    assert "Anzahl+stimmt+nicht" in r.headers["location"] and len(ids(a, "Werbeprospekt")) == 3
    # the result list changed in between: nothing happens
    ingest_bytes(a, text_pdf(["Werbeprospekt Nummer 9"]), "w9.pdf")
    process_all(a)
    r = c.post("/bulk-delete", data={**form, "count": "3"}, follow_redirects=False)
    assert "ge%C3%A4ndert" in r.headers["location"] and len(ids(a, "Werbeprospekt")) == 4
    # confirmed with the current list
    page = c.get("/bulk-delete?q=Werbeprospekt").text
    fp = page.split('name="fingerprint" value="')[1].split('"')[0]
    r = c.post(
        "/bulk-delete", data={**form, "fingerprint": fp, "count": "4"}, follow_redirects=False
    )
    assert "undo=batch" in r.headers["location"]
    assert ids(a, "Werbeprospekt") == [] and ids(a, "Arbeitsvertrag") == [keep]
    groups = trash.listing(a)
    assert len(groups) == 1 and len(groups[0]["items"]) == 4
    # one click restores the whole batch
    batch = groups[0]["batch"]
    c.post(
        "/trash/action", data={"csrf_token": csrf, "action": "restore", "target": f"batch:{batch}"}
    )
    assert len(ids(a, "Werbeprospekt")) == 4 and set(ads) <= set(ids(a, "Werbeprospekt"))


def test_failed_restore_leaves_everything_in_the_trash(archive, monkeypatch):
    d = ingest_bytes(archive, text_pdf(["Versicherungsschein"]), "v.pdf").doc_id
    process_all(archive)
    trash.trash_document(archive, d)

    def broken(*a, **k):
        raise OSError("Platte voll")

    monkeypatch.setattr(trash.fts, "index_document", broken)
    with pytest.raises(OSError):
        trash.restore(archive, d)
    monkeypatch.undo()
    # nothing half-restored: folder and sidecar back in the trash, the original untouched
    assert (archive.paths.trash / d / "metadata.json").exists()
    assert not docs.files(archive, d).dir.exists()
    assert trash.listing(archive)[0]["items"][0]["id"] == d
    assert trash.restore(archive, d).trashed_at is None
    assert maintenance.check(archive)["ok"]


def test_failed_delete_leaves_the_document_live(archive, monkeypatch):
    d = ingest_bytes(archive, text_pdf(["Arztbrief"]), "a.pdf").doc_id
    process_all(archive)
    monkeypatch.setattr(
        trash, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
    )
    with pytest.raises(OSError):
        trash.trash_document(archive, d)
    monkeypatch.undo()
    assert docs.load_meta(archive, d).trashed_at is None and ids(archive, "Arztbrief") == [d]
    assert (docs.files(archive, d).dir / "text_pages.json").exists()
    assert maintenance.check(archive)["ok"]


def test_late_cache_write_and_undo_of_a_waiting_document(archive):
    d = ingest_bytes(archive, text_pdf(["Frisch gescannt"]), "f.pdf").doc_id
    trash.trash_document(archive, d)  # still waiting for processing
    # a page image / OCR page finishing after the deletion must not recreate the folder
    assert not docs.cache_writable(archive, d)
    (docs.files(archive, d).dir / "cache").mkdir(parents=True)  # a racing writer did anyway
    trash.restore(archive, d)
    assert (
        archive.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE doc_id=? AND status='queued'", (d,)
        ).fetchone()[0]
        == 1
    )
    process_all(archive)
    assert docs.load_meta(archive, d).text_status == "ok"


def test_restore_when_the_filing_position_is_taken(archive):
    from heftig import combine

    a = ingest_bytes(archive, text_pdf(["Brief Seite eins"]), "a.pdf").doc_id
    b = ingest_bytes(archive, text_pdf(["Brief Seite zwei"]), "b.pdf").doc_id
    process_all(archive)
    docs.mark_filed(archive, a)
    new = combine.combine(archive, [a, b])
    meta = trash.restore(archive, a)  # only one part back: its position belongs to `new`
    assert meta.filing_sequence is None and any("filing position" in r for r in meta.review_reasons)
    assert docs.load_meta(archive, new.id).filing_sequence is not None
    assert maintenance.check(archive)["ok"]


def test_numbers_of_trashed_documents_are_not_reused_after_rebuild(archive):
    ingest_bytes(archive, text_pdf(["Eins"]), "1.pdf")
    d = ingest_bytes(archive, text_pdf(["Zwei"]), "2.pdf").doc_id
    process_all(archive)
    seq = docs.load_meta(archive, d).ingest_sequence
    trash.trash_document(archive, d)
    archive.conn.execute("DELETE FROM meta WHERE key LIKE 'last_%'")  # as after a lost DB
    new = ingest_bytes(archive, text_pdf(["Drei"]), "3.pdf").doc_id
    assert docs.load_meta(archive, new).ingest_sequence > seq
    trash.restore(archive, d)
