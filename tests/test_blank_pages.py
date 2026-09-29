"""Blank pages - the empty backs of duplex scans: detected, hidden in the viewer, correctable."""

import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth
from heftig import documents as docs
from heftig.maintenance import BLANK_CHECK_KEY, detect_blank_pages

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import scan_pdf

PASSWORD = "richtig-langes-passwort"
TEXT = "\n".join(
    f"Zeile {n}: Sehr geehrte Damen und Herren, anbei die Unterlagen" for n in range(8)
)


def duplex(archive, *pages):
    """A duplex scan: "" is an empty back side."""
    r = ingest_bytes(archive, scan_pdf(list(pages)), "scan.pdf", source="scanner")
    process_all(archive)
    return r.doc_id


def test_blank_backs_are_detected_and_nothing_is_hidden_completely(archive):
    d = duplex(archive, TEXT, "", TEXT, "")
    tp = docs.load_text_pages(archive, d)
    assert [p.blank for p in tp.pages] == [False, True, False, True]
    assert docs.blank_pages(tp, {}) == [2, 4]
    # the user decides otherwise - and a document never disappears completely
    assert docs.blank_pages(tp, {2: False, 3: True}) == [3, 4]
    assert docs.blank_pages(tp, {1: True, 3: True}) == []
    empty = duplex(archive, "", "")
    assert docs.blank_pages(docs.load_text_pages(archive, empty), {}) == []


def test_the_cover_is_the_first_page_that_is_not_blank(archive):
    d = duplex(archive, "", TEXT)
    meta = docs.load_meta(archive, d)
    assert docs.cover_page(archive, meta) == 1
    docs.set_page_blank(archive, d, 1, False)
    assert docs.cover_page(archive, docs.load_meta(archive, d)) == 0


def test_the_users_decision_is_kept_only_where_it_differs(archive):
    d = duplex(archive, TEXT, "", TEXT)
    docs.set_page_blank(archive, d, 3, True)
    docs.set_page_blank(archive, d, 2, False)
    assert docs.load_meta(archive, d).page_blank == {3: True, 2: False}
    docs.set_page_blank(archive, d, 2, True)  # back to what was detected
    assert docs.load_meta(archive, d).page_blank == {3: True}
    with pytest.raises(docs.EditError):
        docs.set_page_blank(archive, d, 9, True)
    # the decision survives re-extraction (it lives in the sidecar)
    from heftig.processing import reprocess

    reprocess(archive, [d], ["extract"])
    process_all(archive)
    tp = docs.load_text_pages(archive, d)
    assert docs.blank_pages(tp, docs.load_meta(archive, d).page_blank) == [2, 3]


def test_documents_from_before_are_checked_once(archive):
    d = duplex(archive, TEXT, "", TEXT)
    with docs.write_tx(archive.conn):  # as extracted by an older version
        tp = docs.load_text_pages(archive, d)
        tp.pages[1].blank = False
        docs.write_text(archive, d, tp)
    assert detect_blank_pages(archive) == {"documents": 1, "pages": 1}
    assert docs.load_text_pages(archive, d).pages[1].blank
    assert detect_blank_pages(archive) == {"documents": 0, "pages": 0}
    assert docs.get_meta(archive.conn, BLANK_CHECK_KEY)


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


def test_viewer_hides_blank_pages_and_the_user_can_switch_them(web):
    arch, c, csrf = web
    d = duplex(arch, TEXT, "", TEXT, "")
    page = c.get(f"/documents/{d}").text
    assert 'id="page-1"' in page and 'id="page-3"' in page
    assert 'id="page-2"' not in page and 'id="page-4"' not in page
    assert "4 Seiten (2 leer)" in page and "2 leere Seiten ausgeblendet (2, 4)" in page
    page = c.get(f"/documents/{d}?pages=all").text
    assert 'id="page-2"' in page and 'class="vpage blank"' in page
    assert "Nicht leer – zeigen" in page and "Leer – ausblenden" in page
    r = c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "page_blank",
               "page": "2", "review": "1"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].endswith("#page-2") and "review=1" in r.headers["location"]
    c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "page_blank",
           "page": "3", "blank": "1"})  # fmt: skip
    page = c.get(f"/documents/{d}").text
    assert 'id="page-2"' in page and 'id="page-3"' not in page
    assert "2 leere Seiten ausgeblendet (3, 4)" in page
    # a search hit on a hidden page shows that page
    assert 'id="page-3"' in c.get(f"/documents/{d}?q=MOCK").text  # (mock text on every page)


def test_single_pages_offer_no_hiding(web):
    arch, c, csrf = web
    d = duplex(arch, TEXT)
    page = c.get(f"/documents/{d}").text
    assert "Leere Seiten ausblenden" not in page and "leer)" not in page
