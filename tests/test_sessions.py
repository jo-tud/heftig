"""Scan sessions: a stack of paper from one existing folder."""

import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth, maintenance, sessions
from heftig import documents as docs
from heftig.providers import registry
from heftig.search import SearchParams, search

from .conftest import ScriptedClassifier, ingest_bytes, make_settings, process_all
from .helpers import scan_pdf, text_pdf

PASSWORD = "richtig-langes-passwort"


def scan(archive, text, name):
    return ingest_bytes(archive, scan_pdf([text]), name, source="scanner").doc_id


def ids(archive, **kw):
    return [i["id"] for i in search(archive.conn, SearchParams(per_page=100, **kw)).items]


def test_folder_mode_records_where_the_paper_is(archive):
    s = sessions.start(archive, "  Versicherungen ", "folder")
    a = scan(archive, "Hausrat", "a.pdf")
    digital = ingest_bytes(archive, text_pdf(["Mail"]), "m.pdf", source="email").doc_id
    m = docs.load_meta(archive, a)
    assert m.scan_session.name == "Versicherungen" and m.paper_location == "Binder Versicherungen"
    assert docs.load_meta(archive, digital).scan_session is None  # digital mail never joins
    assert ids(archive, session=s["id"]) == [a]
    assert a not in ids(archive, filed="no")  # nothing left to file
    # survives a database rebuild (the sidecar is authoritative)
    maintenance.rebuild_db(archive)
    assert ids(archive, session=s["id"]) == [a]


def test_refile_mode_files_the_stack_as_it_comes_out_of_the_scanner(archive):
    s = sessions.start(archive, "Bank", "refile")
    first, second = scan(archive, "Eins", "1.pdf"), scan(archive, "Zwei", "2.pdf")
    assert set(ids(archive, filed="no")) == {first, second}
    assert sessions.summary(archive, s["id"])["pending"][0]["id"] == first
    assert sessions.file_all(archive, s["id"]) == 2
    f1, f2 = docs.load_meta(archive, first), docs.load_meta(archive, second)
    assert f1.filing_sequence > f2.filing_sequence  # the first scanned sheet lies on top
    assert docs.filing_position(archive, f1).position_from_top == 1
    assert sessions.summary(archive, s["id"])["done"]


def test_sort_mode_suggests_what_to_keep_and_applies_it(archive):
    by = {
        "vertrag.pdf": {"title": "Mietvertrag", "document_type": "Vertrag",
                        "document_type_confidence": 0.9, "keep_original": True,
                        "keep_original_reason": "Vertrag"},
        "rechnung.pdf": {"title": "Rechnung", "document_type": "Rechnung",
                         "document_type_confidence": 0.9, "keep_original": False},
        "bescheid.pdf": {"title": "Bescheid", "document_type": "Steuerbescheid",
                         "document_type_confidence": 0.9},  # no AI answer: type rule
    }  # fmt: skip
    fake = ScriptedClassifier(by_filename=by)
    registry.override(classifier=fake)
    s = sessions.start(archive, "Wohnung", "sort")
    v, r, b = (scan(archive, n, n) for n in ("vertrag.pdf", "rechnung.pdf", "bescheid.pdf"))
    process_all(archive)
    # the classifier is told that there is paper and from which folder
    assert fake.requests[0].paper and fake.requests[0].paper_folder == "Wohnung"
    sm = sessions.summary(archive, s["id"])
    assert [d["id"] for d in sm["keep"]] == [v, b] and [d["id"] for d in sm["discard"]] == [r]
    assert [d["n"] for d in sm["keep"]] == [1, 3]  # position in the scanned stack
    # the user overrides one suggestion; a reprocess never undoes that
    docs.set_keep_original(archive, r, True)
    registry.override(classifier=ScriptedClassifier(by_filename=by))
    from heftig.processing import reprocess

    reprocess(archive, [r], ["classify"])
    process_all(archive)
    assert docs.load_meta(archive, r).keep_original is True
    docs.set_keep_original(archive, r, False)
    kept, gone = sessions.apply_sort(archive, s["id"])
    assert (kept, gone) == (2, 1)
    assert docs.load_meta(archive, v).filing_sequence is not None
    assert docs.load_meta(archive, r).paper_discarded_at
    assert ids(archive, filed="no") == []


def test_sort_only_applies_to_what_the_user_saw(archive):
    s = sessions.start(archive, "Keller", "sort")
    seen = scan(archive, "Werbung", "w.pdf")
    process_all(archive)
    shown = {d["id"] for d in sessions.summary(archive, s["id"])["pending"]}
    late = scan(archive, "Kaufvertrag Haus", "k.pdf")  # scanned after the card was shown
    with pytest.raises(sessions.SessionError):  # still being processed: no decision yet
        sessions.apply_sort(archive, s["id"], shown)
    process_all(archive)
    sessions.apply_sort(archive, s["id"], shown)
    assert docs.load_meta(archive, seen).paper_discarded_at
    late_meta = docs.load_meta(archive, late)
    assert late_meta.paper_discarded_at is None and late_meta.filing_sequence is None


def test_forgotten_sessions_end_and_do_not_grab_new_mail(archive):
    s = sessions.start(archive, "Auto", "folder")
    archive.conn.execute(
        "UPDATE scan_sessions SET last_activity_at='2000-01-01T00:00:00Z' WHERE id=?", (s["id"],)
    )
    late = scan(archive, "Brief", "late.pdf")
    assert docs.load_meta(archive, late).scan_session is None
    assert sessions.end_idle(archive) == 1 and sessions.active(archive.conn) is None


def test_starting_a_new_session_ends_the_old_one(archive):
    a = sessions.start(archive, "A", "folder")
    b = sessions.start(archive, "B", "refile")
    assert sessions.active(archive.conn)["id"] == b["id"]
    assert sessions.get(archive.conn, a["id"])["ended_at"]
    with pytest.raises(sessions.SessionError):
        sessions.start(archive, " ", "folder")


def test_paper_states_on_the_document(archive):
    d = scan(archive, "Brief", "b.pdf")
    docs.set_paper_state(archive, d, location="Ordner Auto")
    assert d not in ids(archive, filed="no")
    docs.set_paper_state(archive, d, discarded=True)
    m = docs.load_meta(archive, d)
    assert m.paper_discarded_at and m.paper_location is None
    docs.set_paper_state(archive, d)
    assert d in ids(archive, filed="no")
    docs.mark_filed(archive, d)
    assert docs.load_meta(archive, d).paper_location is None


def test_inbox_card_flow(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    assert "Scan-Sitzung starten" in c.get("/inbox").text
    assert (
        c.post(
            "/sessions/action", data={"action": "start", "name": "X", "mode": "folder"}
        ).status_code
        == 403
    )
    r = c.post("/sessions/action", data={"csrf_token": csrf, "action": "start",
               "name": "Steuer", "mode": "refile"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and r.headers["location"].startswith("/inbox")
    scan(arch, "Bescheid", "s.pdf")
    page = c.get("/inbox").text
    assert "Scan-Sitzung „Steuer“" in page and "Alle 1 abgeheftet, so wie sie aus dem Scanner kamen" in page
    assert "Steuer" in c.get("/scan").text
    sid = sessions.active(arch.conn)["id"]
    assert "Sitzung: Steuer" in c.get(f"/?session={sid}").text
    c.post("/sessions/action", data={"csrf_token": csrf, "action": "file_all", "session": sid})
    c.post("/sessions/action", data={"csrf_token": csrf, "action": "end", "session": sid})
    assert "Scan-Sitzung starten" in c.get("/inbox").text  # nothing open: the card is idle
    arch.close()
