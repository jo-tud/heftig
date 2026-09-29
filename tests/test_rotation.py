"""Turning pages that were scanned the wrong way round - one page or all; the original stays."""

import io
import logging

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from heftig import auth
from heftig import documents as docs
from heftig.media import rotate_box
from heftig.providers import registry

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import scan_pdf, text_pdf

PASSWORD = "richtig-langes-passwort"
TEXT = "\n".join(
    f"Zeile {n}: Sehr geehrte Damen und Herren, anbei die Unterlagen" for n in range(8)
)


def test_boxes_turn_with_the_page():
    box = [0.1, 0.2, 0.3, 0.25]
    assert rotate_box(box, 180) == pytest.approx([0.7, 0.75, 0.9, 0.8])
    assert rotate_box(box, 90) == pytest.approx([0.75, 0.1, 0.8, 0.3])
    assert rotate_box(rotate_box(box, 90), 270) == pytest.approx(box)


def test_turning_one_page_or_all_and_back(archive):
    d = ingest_bytes(archive, text_pdf([TEXT, TEXT, TEXT]), "a.pdf").doc_id
    process_all(archive)
    original = docs.load_meta(archive, d).sha256
    docs.rotate_pages(archive, d, [2])
    assert docs.load_meta(archive, d).page_rotation == {2: 90}
    docs.rotate_pages(archive, d, None, 90)  # all pages
    assert docs.load_meta(archive, d).page_rotation == {1: 90, 2: 180, 3: 90}
    docs.rotate_pages(archive, d, None, -90)
    docs.rotate_pages(archive, d, [2], -90)
    meta = docs.load_meta(archive, d)
    assert meta.page_rotation == {} and meta.sha256 == original
    with pytest.raises(docs.EditError):
        docs.rotate_pages(archive, d, [7])


def test_a_turned_page_is_read_turned(archive):
    from .conftest import FakeExtractor

    class Sizes(FakeExtractor):
        seen: list = []

        def extract_page(self, image_png, page_number, languages, media_type="image/png"):
            self.seen.append(Image.open(io.BytesIO(image_png)).size)
            return super().extract_page(image_png, page_number, languages, media_type)

    ocr = Sizes(pages={1: "Seite eins"})
    registry.override(extractor=ocr)
    d = ingest_bytes(archive, scan_pdf([TEXT]), "scan.pdf").doc_id
    process_all(archive)
    w, h = ocr.seen[-1]
    assert h > w  # portrait
    docs.rotate_pages(archive, d, [1])
    from heftig.processing import reprocess

    reprocess(archive, [d], ["extract"])
    process_all(archive)
    w, h = ocr.seen[-1]
    assert w > h  # read in landscape, as turned
    assert docs.load_text_pages(archive, d).pages[0].turn == 90


def test_turned_words_for_search_marks(archive):
    from heftig.wordboxes import page_words

    d = ingest_bytes(archive, text_pdf([TEXT]), "a.pdf").doc_id
    process_all(archive)
    before = {w[0]: w[1:] for w in page_words(archive, docs.load_meta(archive, d), 1)[0]}
    docs.rotate_pages(archive, d, [1], 180)
    after = {w[0]: w[1:] for w in page_words(archive, docs.load_meta(archive, d), 1)[0]}
    assert after["Unterlagen"] == pytest.approx(rotate_box(before["Unterlagen"], 180))


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


def test_viewer_turns_a_page_and_offers_to_read_it_again(web):
    arch, c, csrf = web
    d = ingest_bytes(arch, scan_pdf([TEXT, TEXT]), "scan.pdf").doc_id
    process_all(arch)
    page = c.get(f"/documents/{d}").text
    assert 'value="rotate"' in page and "Alle Seiten nach rechts drehen" in page
    r = c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "rotate",
               "page": "2", "review": "1"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].endswith("#page-2") and "review=1" in r.headers["location"]
    page = c.get(f"/documents/{d}").text
    assert "pages/2.webp?w=960&amp;r=90" in page  # a new address: the browser loads it anew
    assert "Seite 2 wurde vor dem Drehen gelesen." in page
    img = Image.open(io.BytesIO(c.get(f"/documents/{d}/pages/2.webp?w=480&r=90").content))
    assert img.width == 480 and img.width > img.height
    c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "reprocess_extract"})
    process_all(arch)
    assert "vor dem Drehen gelesen" not in c.get(f"/documents/{d}").text
    # all pages back: the original way round everywhere
    c.post(f"/documents/{d}/action", data={"csrf_token": csrf, "action": "rotate_back",
           "page": "2"})  # fmt: skip
    assert docs.load_meta(arch, d).page_rotation == {}
