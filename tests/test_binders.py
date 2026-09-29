"""Binders of the paper filing: the current one, the next one when full, taking a sheet out,
putting it back, moving it to another binder."""

import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth, binders
from heftig import documents as docs
from heftig.archive import Archive

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"


def paper(archive, text):
    r = ingest_bytes(archive, text_pdf([text]), f"{text}.pdf", source="scanner")
    return r.doc_id


def place(archive, doc_id):
    m = docs.load_meta(archive, doc_id)
    p = docs.filing_position(archive, m)
    return (p.binder, p.position_from_top, p.total_in_section) if p else None


def test_filing_goes_into_the_current_binder_and_continues_in_the_next(archive):
    a, b, c = (paper(archive, t) for t in ("Brief eins", "Brief zwei", "Brief drei"))
    process_all(archive)
    docs.mark_filed(archive, a)
    docs.mark_filed(archive, b)
    assert binders.current(archive) == "Ordner 1"
    assert place(archive, a) == ("Ordner 1", 2, 2) and place(archive, b) == ("Ordner 1", 1, 2)
    assert binders.start_next(archive) == "Ordner 2"
    docs.mark_filed(archive, c)
    assert place(archive, c) == ("Ordner 2", 1, 1)
    assert place(archive, a) == ("Ordner 1", 2, 2)  # the full binder is unchanged
    over = {o["name"]: o for o in binders.overview(archive)}
    assert over["Ordner 1"]["full_at"] and over["Ordner 1"]["sheets"] == 2
    assert over["Ordner 2"]["current"] and over["Ordner 2"]["sheets"] == 1


def test_taking_out_keeps_the_place_and_putting_back_returns_it(archive):
    a, b, c = (paper(archive, t) for t in ("Vertrag", "Rechnung", "Bescheid"))
    process_all(archive)
    for d in (a, b, c):
        docs.mark_filed(archive, d)
    docs.take_out(archive, b, "beim Steuerberater")
    assert place(archive, a) == ("Ordner 1", 2, 2)  # the sheet below moved up
    assert place(archive, b) == ("Ordner 1", 2, 3)  # the taken-out one keeps its slot
    assert docs.load_meta(archive, b).paper_location == "beim Steuerberater"
    docs.put_back(archive, b)
    assert place(archive, a) == ("Ordner 1", 3, 3) and place(archive, b) == ("Ordner 1", 2, 3)
    # "filed now" on a taken-out sheet of the current binder also puts it back in its place
    docs.take_out(archive, b)
    seq = docs.load_meta(archive, b).filing_sequence
    docs.mark_filed(archive, b)
    assert docs.load_meta(archive, b).filing_sequence == seq and place(archive, b)[1] == 2


def test_moving_to_another_binder_and_not_kept(archive):
    a, b = paper(archive, "Police"), paper(archive, "Überweisung")
    process_all(archive)
    docs.mark_filed(archive, a)
    docs.mark_filed(archive, b)
    binders.start_next(archive, "Versicherungen")
    docs.mark_filed(archive, a, binder="Versicherungen")
    assert place(archive, a) == ("Versicherungen", 1, 1) and place(archive, b) == ("Ordner 1", 1, 1)
    docs.set_paper_state(archive, b, discarded=True)
    m = docs.load_meta(archive, b)
    assert m.filing_sequence is None and m.filing_binder is None and m.paper_discarded_at


def test_rename_and_names(archive):
    a = paper(archive, "Brief")
    process_all(archive)
    docs.mark_filed(archive, a)
    assert binders.rename(archive, "Ordner 1", "Heftig 1") == 1
    assert docs.load_meta(archive, a).filing_binder == "Heftig 1"
    assert binders.next_name(binders.load(archive.paths), "de") == "Heftig 2"
    with pytest.raises(binders.BinderError):
        binders.start_next(archive, "heftig 1")  # names are unique regardless of case


def test_filings_from_before_binders_are_adopted(tmp_path):
    arch = Archive(make_settings(tmp_path))
    d = paper(arch, "Alter Brief")
    process_all(arch)
    docs.mark_filed(arch, d)
    with docs.write_tx(arch.conn):  # as filed by an older version
        meta = docs.load_meta(arch, d)
        meta.filing_binder = None
        docs.persist(arch, meta)
    (arch.paths.root / binders.FILENAME).unlink()
    arch.close()
    arch = Archive(make_settings(tmp_path))
    assert docs.load_meta(arch, d).filing_binder == "Ordner 1"
    assert binders.current(arch) == "Ordner 1"
    arch.close()


def test_binders_survive_rebuild_and_travel_with_exports(archive, tmp_path):
    from heftig.maintenance import export_archive, import_archive, rebuild_db

    a = paper(archive, "Brief")
    process_all(archive)
    docs.mark_filed(archive, a)
    binders.rename(archive, "Ordner 1", "Heftig 1")
    rebuild_db(archive)
    assert place(archive, a)[0] == "Heftig 1"
    out = export_archive(archive, tmp_path / "exports", as_zip=False)
    other = Archive(make_settings(tmp_path / "other"))
    import_archive(other, out)
    names = [b["name"] for b in binders.load(other.paths)]
    assert "Heftig 1" in names
    assert binders.current(other) != "Heftig 1"  # imported binders are complete
    other.close()


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


def test_pages(web):
    arch, c, csrf = web
    d = paper(arch, "Brief an den Ordner")
    process_all(arch)
    assert "In Ordner <strong>Ordner 1</strong>" in c.get("/inbox").text
    c.post("/inbox/action", data={"csrf_token": csrf, "action": f"file_{d}"})
    page = c.get(f"/documents/{d}").text
    assert "Ordner <strong>Ordner 1</strong>" in page and 'value="take_out"' in page
    c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "take_out",
           "where": "beim Steuerberater", "revision": docs.load_meta(arch, d).revision})  # fmt: skip
    page = c.get(f"/documents/{d}").text
    assert "herausgenommen" in page and "beim Steuerberater" in page
    assert 'value="put_back"' in page
    # binder full: the next one, then renaming it
    r = c.post("/binders", data={"csrf_token": csrf, "action": "next", "name": "Heftig 2"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/binders?done=next" and binders.current(arch) == "Heftig 2"
    page = c.get("/binders").text
    assert "Heftig 2" in page and "voll seit" in page
    r = c.post("/binders", data={"csrf_token": csrf, "action": "rename", "old": "Ordner 1",
                                 "name": "Heftig 2"})  # fmt: skip
    assert r.status_code == 400 and "schon einen Ordner" in r.text
    assert c.get("/?filing_binder=Ordner+1").status_code == 200


def test_deleted_sheet_taken_out_or_left_in_the_binder(archive):
    from heftig import trash

    a, b, c = (paper(archive, t) for t in ("Unten", "Mitte", "Oben"))
    process_all(archive)
    for d in (a, b, c):
        docs.mark_filed(archive, d)
    trash.trash_document(archive, b)
    # default: the sheet is out - the one below moves up
    assert place(archive, a) == ("Ordner 1", 2, 2)
    # "leave it in the binder": its place keeps counting, even after the trash is emptied
    gone = docs.DocumentMetadata.model_validate_json(archive.conn.execute(
        "SELECT metadata_json FROM trash WHERE id=?", (b,)).fetchone()[0])  # fmt: skip
    assert binders.keep_sheet(archive, gone)
    assert place(archive, a) == ("Ordner 1", 3, 3)
    pos = docs.filing_position(archive, docs.load_meta(archive, a))
    assert [x.get("kept", False) for x in pos.above] == [False, True]  # listed from the top
    trash.purge(archive, b)
    assert place(archive, a) == ("Ordner 1", 3, 3)
    assert binders.overview(archive)[0]["sheets"] == 3
    binders.sheet_taken_out(archive, b)
    assert place(archive, a) == ("Ordner 1", 2, 2)


def test_restoring_a_left_sheet_gives_the_place_back_to_the_document(archive):
    from heftig import trash

    a, b = paper(archive, "Brief A"), paper(archive, "Brief B")
    process_all(archive)
    docs.mark_filed(archive, a)
    docs.mark_filed(archive, b)
    trash.trash_document(archive, a)
    gone = docs.DocumentMetadata.model_validate_json(archive.conn.execute(
        "SELECT metadata_json FROM trash WHERE id=?", (a,)).fetchone()[0])  # fmt: skip
    binders.keep_sheet(archive, gone)
    trash.restore(archive, a)
    assert not binders.sheet_kept(archive.paths, a)
    assert place(archive, a) == ("Ordner 1", 2, 2) and place(archive, b) == ("Ordner 1", 1, 2)


def test_pages_for_deleted_filed_documents(web):
    arch, c, csrf = web
    d = paper(arch, "Werbung im Ordner")
    process_all(arch)
    docs.mark_filed(arch, d)
    r = c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "delete"},
               follow_redirects=False)  # fmt: skip
    loc = r.headers["location"]
    page = c.get(loc).text
    assert "Papier aus Ordner <strong>Ordner 1</strong> herausnehmen" in page
    assert 'value="paper_stays"' in page
    r = c.post("/trash/action", data={"csrf_token": csrf, "target": f"doc:{d}",
               "action": "paper_stays", "back": loc}, follow_redirects=False)  # fmt: skip
    assert binders.sheet_kept(arch.paths, d)
    assert "Das Papier bleibt in Ordner" in c.get(r.headers["location"]).text
    assert "Papier bleibt in Ordner" in c.get("/trash").text
    assert "1 Blatt ohne Dokument im Archiv" in c.get("/binders").text


def test_a_kept_duplicate_takes_over_the_binder_place(web, monkeypatch):
    arch, c, csrf = web
    monkeypatch.setattr(arch.settings, "auto_resolve_identical", False)
    text = "\n".join(f"Paragraph {n}: Vertragsbedingung und Kündigungsfrist" for n in range(12))
    a = ingest_bytes(arch, text_pdf([text]), "a.pdf", source="scanner").doc_id
    b = ingest_bytes(arch, text_pdf([text]) + b"\n%x", "b.pdf").doc_id
    process_all(arch)
    filed = docs.mark_filed(arch, a)
    from heftig.duplicates import open_pairs

    pair = open_pairs(arch.conn)[0]
    victim = "delete_a" if pair["a"]["id"] == a else "delete_b"
    r = c.post("/duplicates/action", data={"csrf_token": csrf, "a": pair["a"]["id"],
               "b": pair["b"]["id"], "action": victim}, follow_redirects=False)  # fmt: skip
    kept = docs.load_meta(arch, b)
    assert kept.filing_sequence == filed.filing_sequence and kept.filing_binder == "Ordner 1"
    page = c.get(r.headers["location"]).text
    assert "übernimmt ihren Platz" in page and "herausnehmen" not in page


def test_a_stack_filed_the_other_way_round_is_reversed(archive):
    a, b, c, d = (paper(archive, t) for t in ("Erster", "Zweiter", "Dritter", "Später"))
    process_all(archive)
    for x in (a, b, c, d):
        docs.mark_filed(archive, x)
    assert docs.reverse_stack(archive, [a, b, c]) == 3
    # the later sheet on top keeps its place; the stack below is turned round
    assert [place(archive, x)[1] for x in (d, a, b, c)] == [1, 2, 3, 4]
