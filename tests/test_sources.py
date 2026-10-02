"""Source documents: what was combined into another document is kept for good, never purged."""

import io
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from heftig import auth, combine, maintenance, trash
from heftig import documents as docs
from heftig.archive import Archive
from heftig.db import iso, utcnow
from heftig.search import SearchParams, search

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"
LONG_AGO = iso(utcnow() - timedelta(days=400))


def ids(archive, q=""):
    return [i["id"] for i in search(archive.conn, SearchParams(q=q, per_page=100)).items]


def two_parts(archive, word="Mietvertrag"):
    a = ingest_bytes(archive, text_pdf([f"{word} Seite eins"]), "a.pdf").doc_id
    b = ingest_bytes(archive, text_pdf([f"{word} Seite zwei"]), "b.pdf").doc_id
    process_all(archive)
    return a, b


def original(archive, meta):
    return archive.paths.resolve(meta.original_relpath)


def test_combined_parts_are_kept_and_never_purged(archive):
    a, b = two_parts(archive)
    docs.add_note(archive, a, "Unterschrieben am Küchentisch")
    docs.add_attachment(archive, b, io.BytesIO(b"Quittung"), "q.txt")
    metas = [docs.load_meta(archive, i) for i in (a, b)]
    new = combine.combine(archive, [a, b])
    for m in metas:
        assert (archive.paths.sources / m.id / "metadata.json").exists()
        assert not (archive.paths.trash / m.id).exists() and original(archive, m).exists()
    kept = trash.source_meta(archive, a)
    assert kept.replaced_by == [new.id] and kept.replaced_at and kept.trashed_at is None
    assert [n.text for n in kept.notes] == ["Unterschrieben am Küchentisch"]  # all of it stays
    assert ids(archive, "Mietvertrag") == [new.id] and trash.listing(archive) == []
    # however long ago: neither the automatic purge nor emptying the trash touches them
    archive.conn.execute("UPDATE trash SET trashed_at=?", (LONG_AGO,))
    assert trash.purge_expired(archive) == 0 and trash.empty(archive) == 0
    with pytest.raises(trash.TrashError):
        trash.purge(archive, a)
    assert all(original(archive, m).exists() for m in metas)
    assert len(trash.sources_listing(archive)[0]["items"]) == 2
    assert maintenance.check(archive)["ok"]  # their originals are no orphans


def test_undo_brings_them_back(archive):
    a, b = two_parts(archive)
    new = combine.combine(archive, [a, b])
    r = combine.undo(archive, new.id)
    assert r["restored"] == 2 and set(ids(archive, "Mietvertrag")) == {a, b}
    meta = docs.load_meta(archive, a)
    assert meta.replaced_by == [] and meta.replaced_at is None
    assert trash.sources_listing(archive) == []
    assert [g["items"][0]["id"] for g in trash.listing(archive)] == [new.id]


def test_deleting_on_purpose_goes_through_the_trash(archive):
    a, b = two_parts(archive)
    meta = docs.load_meta(archive, a)
    combine.combine(archive, [a, b])
    trash.discard_source(archive, a)
    assert not (archive.paths.sources / a).exists()
    assert (archive.paths.trash / a / "metadata.json").exists()
    assert [g["items"][0]["id"] for g in trash.listing(archive)] == [a]
    assert [it["id"] for it in trash.sources_listing(archive)[0]["items"]] == [b]
    archive.conn.execute("UPDATE trash SET trashed_at=? WHERE id=?", (LONG_AGO, a))
    assert trash.purge_expired(archive) == 1 and not original(archive, meta).exists()
    with pytest.raises(trash.TrashError):
        trash.discard_source(archive, a)


def test_restore_one_when_the_combined_document_is_gone(archive):
    a, b = two_parts(archive)
    new = combine.combine(archive, [a, b])
    trash.trash_document(archive, new.id)
    group = trash.sources_listing(archive)[0]
    assert group["targets"] == []  # deleted meanwhile: no undo, each can come back on its own
    trash.restore(archive, b)
    assert ids(archive, "Mietvertrag") == [b]


def test_combine_batches_already_in_the_trash_are_adopted(archive):
    """Archives from before the source documents: the parts went to the Papierkorb."""
    a, b = two_parts(archive)
    c = ingest_bytes(archive, text_pdf(["Werbung"]), "c.pdf").doc_id
    d, e = two_parts(archive, "Kündigung")
    process_all(archive)
    new = combine.combine(archive, [a, b])
    gone = combine.combine(archive, [d, e])
    for part in (a, b, d, e):  # back to how it was before: in the Papierkorb
        row = archive.conn.execute("SELECT batch FROM trash WHERE id=?", (part,)).fetchone()
        trash.restore(archive, part)
        trash.trash_document(archive, part, reason="combined", batch=row[0])
    trash.trash_document(archive, gone.id)  # its combined document deleted too
    trash.trash_document(archive, c)
    archive.conn.execute("UPDATE trash SET trashed_at=?", (LONG_AGO,))
    archive.conn.execute("DELETE FROM meta WHERE key=?", (trash.SOURCES_ADOPTED_KEY,))
    assert trash.purge_expired(archive) == 4  # c, the deleted combined document and its parts
    kept = trash.sources_listing(archive)
    assert len(kept) == 1 and {it["id"] for it in kept[0]["items"]} == {a, b}
    assert kept[0]["targets"][0]["id"] == new.id
    assert trash.source_meta(archive, a).replaced_by == [new.id]
    assert maintenance.check(archive)["ok"]


def test_rebuild_check_backup_export_and_import(archive, tmp_path):
    a, b = two_parts(archive)
    new = combine.combine(archive, [a, b])
    maintenance.rebuild_db(archive)
    assert {it["id"] for it in trash.sources_listing(archive)[0]["items"]} == {a, b}
    assert trash.listing(archive) == [] and maintenance.check(archive)["ok"]
    backup = maintenance.backup(archive, tmp_path / "backups")
    assert (backup / "sources" / a / "metadata.json").exists()
    path = maintenance.export_archive(archive, tmp_path / "exports")
    target = Archive(make_settings(tmp_path / "target"))
    try:
        report = maintenance.import_archive(target, path)
        assert report["sources"] == 2 and not report["conflicts"]
        group = trash.sources_listing(target)[0]
        assert {it["id"] for it in group["items"]} == {a, b}
        assert group["targets"][0]["id"] == new.id
        meta = trash.source_meta(target, a)
        assert original(target, meta).exists() and maintenance.check(target)["ok"]
        assert maintenance.import_archive(target, path)["sources"] == 0  # again: nothing new
    finally:
        target.close()


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    yield arch, c, csrf
    arch.close()


def test_sources_page(web):
    arch, c, csrf = web
    a, b = two_parts(arch)
    pdf = original(arch, docs.load_meta(arch, a)).read_bytes()
    r = c.post("/combine", data={"csrf_token": csrf, "ids": [a, b]}, follow_redirects=False)
    page = c.get(r.headers["location"]).text
    assert "Ausgangsdokumente" in page and 'href="/sources"' in page  # undo bar
    new = ids(arch, "Mietvertrag")[0]
    assert f'href="/sources#combine-{new}"' in c.get(f"/documents/{new}").text
    assert 'href="/sources"' in c.get("/settings").text
    page = c.get("/sources").text
    assert "Zusammengefügt zu" in page and f'href="/documents/{new}"' in page
    assert f"/sources/{a}/original" in page
    r = c.get(f"/sources/{a}/original")
    assert (
        r.status_code == 200
        and r.content == pdf
        and "attachment" in r.headers["content-disposition"]
    )
    assert c.get(f"/sources/{new}/original").status_code == 404  # a live document: not here
    assert c.get(f"/api/sources/{a}/original").content == pdf
    assert len(c.get("/api/sources").json()["groups"][0]["items"]) == 2
    # delete one on purpose: into the trash
    r = c.post("/sources/action?action=delete", data={"csrf_token": csrf, "target": b},
               follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and b in c.get("/trash").text
    # undo the combining: the remaining one comes back, the combined document goes
    r = c.post("/sources/action?action=undo",
               data={"csrf_token": csrf, "target": f"combine-{new}"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith(f"/documents/{a}")
    assert ids(arch, "Mietvertrag") == [a]
    assert "Noch keine Ausgangsdokumente" in c.get("/sources").text


def test_sources_need_login_and_csrf(web):
    arch, c, csrf = web
    a, b = two_parts(arch)
    combine.combine(arch, [a, b])
    r = c.post("/sources/action?action=delete", data={"target": a}, follow_redirects=False)
    assert r.status_code == 403 and trash.sources_listing(arch)
    c.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert c.get("/sources", follow_redirects=False).status_code in (302, 303, 401)
    assert c.get(f"/sources/{a}/original", follow_redirects=False).status_code in (302, 303, 401)
