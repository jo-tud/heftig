"""Search page UI: facets, timeline, saved searches, suggestions, hits in the viewer."""

import logging
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from heftig import auth, maintenance, saved_searches
from heftig.archive import Archive

from .conftest import make_settings
from .corpus import SCAN_NAME, load_corpus

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    a = app.state.archive
    auth.create_user(a.conn, "jo", PASSWORD)
    corpus = load_corpus(a)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    yield app, c, csrf, corpus
    a.close()


def test_facets_timeline_and_toggle_links(web):
    app, c, csrf, corpus = web
    html = c.get("/?q=Versicherung").text
    # facet values with counts, as links that add the filter
    assert re.search(r'href="/\?q=Versicherung&amp;correspondent=Allianz\+Versicherungs-AG"', html)
    assert "Dokumente pro Jahr" in html
    # selecting a year drills the timeline down to months
    html = c.get("/?date_from=2025&date_to=2025").text
    assert "Dokumente pro Monat 2025" in html and "Datum: 2025" in html
    assert 'title="März 2025: 1 Dokument"' in html
    # a selected value is shown as active and its link removes it again
    html = c.get("/?correspondent=Vodafone+GmbH").text
    assert 'class="fv on" href="/" aria-current="true"' in html


def test_date_phrase_notice_and_literal_link(web):
    app, c, csrf, corpus = web
    html = c.get("/?q=Rechnung+September+2026").text
    assert "Zeitraum erkannt: <strong>September 2026</strong>" in html
    assert "literal=1" in html
    assert "Zeitraum erkannt" not in c.get("/?q=Rechnung+September+2026&literal=1").text
    # the timeline replaces the recognised phrase instead of adding a contradicting range
    html = c.get("/?q=Vodafone+seit+2025").text
    assert 'href="/?q=Vodafone&amp;date_from=2025&amp;date_to=2025"' in html
    assert "seit+2025&amp;date_from" not in html


def test_tag_mode_switch(web):
    app, c, csrf, corpus = web
    html = c.get("/?tag=Telefon&tag=Internet").text
    assert "Keine Dokumente gefunden" in html and "mindestens einer" in html
    html = c.get("/?tag=Telefon&tag=Internet&tag_mode=any").text
    assert "<strong>3</strong> Dokumente" in html


def test_saved_searches_roundtrip(web, tmp_path):
    app, c, csrf, corpus = web
    r = c.post("/searches", data={"csrf_token": csrf, "action": "save", "name": "Handy",
               "query": "q=rechnung&tag=Telefon&page=3&evil=1"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and "Suche+gespeichert" in r.headers["location"]
    saved = saved_searches.load(app.state.archive.paths)
    assert [(s["name"], s["query"]) for s in saved] == [("Handy", "q=rechnung&tag=Telefon")]
    start = c.get("/").text
    assert "Gespeicherte Suchen" in start and "☆ Handy" in start
    assert "★ gespeichert als „Handy“" in c.get("/?q=rechnung&tag=Telefon").text
    # the confirmation message is not part of the search (recent searches, links)
    html = c.get("/?q=rechnung&tag=Telefon&msg=Suche+gespeichert.").text
    assert 'data-recent-query="q=rechnung&amp;tag=Telefon"' in html
    # API view and CSRF protection
    assert c.get("/api/searches").json()["searches"][0]["name"] == "Handy"
    assert (
        c.post("/searches", data={"action": "save", "name": "x", "query": "q=x"}).status_code == 403
    )
    # survives export/import
    exp = maintenance.export_archive(app.state.archive, tmp_path / "exp")
    target = Archive(make_settings(tmp_path / "t"))
    rep = maintenance.import_archive(target, exp)
    assert rep["saved_searches"] == 1 and saved_searches.load(target.paths)[0]["name"] == "Handy"
    target.close()
    sid = saved[0]["id"]
    c.post("/searches", data={"csrf_token": csrf, "action": "delete", "id": sid})
    assert saved_searches.load(app.state.archive.paths) == []


def test_suggest_api(web):
    app, c, csrf, corpus = web
    data = c.get("/api/suggest", params={"q": "stadtw"}).json()
    assert data["items"][0]["value"] == "Stadtwerke Beispielstadt"
    assert data["items"][0]["param"] == "correspondent"


def test_document_page_hits_and_similar(web):
    app, c, csrf, corpus = web
    doc = corpus["telekom_2026_09.pdf"]
    html = c.get(f"/documents/{doc}?q=Kundennummer").text
    assert "„Kundennummer“ auf" in html and 'data-hit-pages="1"' in html
    assert "Ähnliche Dokumente" in html
    boxes = c.get(f"/documents/{doc}/pages/1/hits", params={"q": "Kundennummer"}).json()
    assert boxes["source"] == "pdf" and len(boxes["boxes"]) == 1
    x0, y0, x1, y1 = boxes["boxes"][0]
    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    # only in metadata -> explained, no page links
    html = c.get(f"/documents/{doc}?q=Telefon").text
    assert "steht nicht im Text" in html
    assert c.get(f"/documents/{doc}/pages/9/hits?q=x").status_code == 404


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract fehlt")
def test_hits_on_scanned_pages_via_ocr_boxes(web):
    app, c, csrf, corpus = web
    doc = corpus[SCAN_NAME]
    r = c.get(f"/documents/{doc}/pages/1/hits", params={"q": "Bild"}).json()
    assert r["source"] == "ocr" and r["boxes"]
    # cached: the second call does not run Tesseract again
    cache = app.state.archive.paths.doc_dir(doc) / "cache" / "words-p1.json"
    assert cache.exists()


def test_every_filter_link_works(web):
    """Follow every search link (facets, chips, pills, timeline) from several states: none may
    fail or point somewhere odd (a dict method once ended up in a link)."""
    from heftig import documents as docs

    app, c, csrf, corpus = web
    a = app.state.archive
    some = a.conn.execute("SELECT id FROM documents ORDER BY ingest_sequence LIMIT 2").fetchall()
    for (doc_id,), tag in zip(some, ["Haus & Garten", "Kfz/Auto, Motorrad"], strict=True):
        docs.update_fields(a, doc_id, {"tags": [tag, "Wohnung+Miete"]}, {})
    states = [
        "/",
        "/?tag=Haus+%26+Garten&tag=Wohnung%2BMiete",
        "/?tag=Kfz%2FAuto%2C+Motorrad&tag_mode=all",
        "/?q=Rechnung+letztes+Jahr",
        "/?date_from=2025&date_to=2025&correspondent=Vodafone+GmbH",
        "/?date_from=2025-09-28",
        "/?received_from=2026-01-01&status=needs_review&filed=no",
    ]
    seen: set[str] = set()
    for state in states:
        html = c.get(state).text
        assert "built-in method" not in html
        for href in re.findall(r'href="(/\?[^"]*|/)"', html):
            href = href.replace("&amp;", "&")
            if href in seen:
                continue
            seen.add(href)
            r = c.get(href)
            assert r.status_code == 200, (state, href)
            assert "built-in method" not in r.text, href
    assert len(seen) > 40


def test_odd_filter_values_do_not_break_pages(web):
    app, c, csrf, corpus = web
    for url in ("/?date_from=2025-13", "/?received_to=2025-00", "/bulk-delete?q=x&source=foo",
                "/bulk-delete?date_from=kaputt", "/bulk-delete?q=year:zwanzig"):  # fmt: skip
        assert c.get(url).status_code < 500, url
