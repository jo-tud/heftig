"""Comparing two documents: visual page differences, text differences, automatic resolution."""

import io
import logging

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from heftig import auth, trash
from heftig import documents as docs
from heftig.duplicates import open_pairs, resolve_identical
from heftig.pagediff import compare_images, text_diff

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_image, text_pdf

LETTER = (
    "\n".join(
        f"Sehr geehrter Herr Muster, Zeile {n} des Vertrags über die Leistung" for n in range(20)
    )
    + "\n\nMit freundlichen Grüßen\n\n\n\nMax Beispiel"
)
WORDS = "\n".join(f"Paragraph {n}: Vertragsbedingung und Kündigungsfrist" for n in range(12))


def page():
    return text_image(LETTER).convert("L")


def test_same_page_scanned_twice_is_same():
    a = page()
    shifted = Image.new("L", a.size, 255)
    shifted.paste(a.rotate(0.2, fillcolor=255), (7, -5))
    assert compare_images(a, shifted).status == "same"


def test_signature_is_found_on_the_signed_side():
    a = page()
    signed = a.copy()
    ImageDraw.Draw(signed).line(
        [(120, 1330), (200, 1290), (260, 1340), (330, 1280)], fill=0, width=4
    )
    d = compare_images(a, signed)
    assert d.status == "different" and d.more_ink == "b" and len(d.boxes_b) == 1
    x0, y0, x1, y1 = d.boxes_b[0]
    assert 0.05 < x0 < x1 < 0.4 and 0.65 < y0 < y1 < 0.85


def test_other_letter_is_not_comparable():
    other = text_image("\n".join(f"Ganz anderer Brief {n}" for n in range(30))).convert("L")
    assert compare_images(page(), other).status == "unaligned"


def test_text_diff():
    d = text_diff("Rechnung über 149,00 EUR vom 01.03.", "Rechnung über 151,20 EUR vom 01.03.")
    assert (
        not d["identical"] and d["changes"][0]["a"] == "149,00" and d["changes"][0]["b"] == "151,20"
    )
    assert text_diff("Müller Straße", "MUELLER strasse")["identical"]


def _pdf(text, extra=b""):
    return text_pdf([text]) + extra  # extra bytes: another file, same content


def test_identical_copies_are_resolved_automatically(archive):
    older = ingest_bytes(archive, _pdf(WORDS), "a.pdf").doc_id
    newer = ingest_bytes(archive, _pdf(WORDS, b"\n%andere Datei"), "b.pdf").doc_id
    process_all(archive)
    assert open_pairs(archive.conn) == []  # resolved right after processing
    listing = trash.listing(archive)
    assert listing[0]["batch"].startswith("auto-") and listing[0]["items"][0]["id"] == newer
    assert docs.load_meta(archive, older).status in ("done", "needs_review")


def test_the_copy_with_user_data_is_kept_and_both_with_data_stay(archive, monkeypatch):
    monkeypatch.setattr(archive.settings, "auto_resolve_identical", False)
    older = ingest_bytes(archive, _pdf(WORDS), "a.pdf").doc_id
    newer = ingest_bytes(archive, _pdf(WORDS, b"\n%x"), "b.pdf").doc_id
    process_all(archive)
    docs.add_note(archive, newer, "Bezahlt am 3.10.")
    monkeypatch.setattr(archive.settings, "auto_resolve_identical", True)
    assert resolve_identical(archive) == [{"kept": newer, "trashed": older}]
    # both carry user data: nothing happens automatically
    trash.restore(archive, older)
    from heftig.duplicates import check_document

    docs.add_note(archive, older, "Kopie")
    check_document(archive, older)
    assert resolve_identical(archive) == [] and len(open_pairs(archive.conn)) == 1


def test_a_copy_with_tag_decisions_is_kept(archive, monkeypatch):
    monkeypatch.setattr(archive.settings, "auto_resolve_identical", False)
    older = ingest_bytes(archive, _pdf(WORDS), "a.pdf").doc_id
    newer = ingest_bytes(archive, _pdf(WORDS, b"\n%x"), "b.pdf").doc_id
    process_all(archive)
    docs.update_fields(archive, newer, {"tags": ["Steuer 2025"]}, {})  # tags are not locked
    monkeypatch.setattr(archive.settings, "auto_resolve_identical", True)
    assert resolve_identical(archive) == [{"kept": newer, "trashed": older}]


def test_signed_copy_or_little_text_is_never_resolved(archive):
    few = "Kurzer Beleg"
    ingest_bytes(archive, _pdf(few), "a.pdf")
    ingest_bytes(archive, _pdf(few, b"\n%x"), "b.pdf")
    # the same text, but one copy is signed (image PDFs, text via the fake OCR)
    signed = page().convert("RGB")
    ImageDraw.Draw(signed).line(
        [(120, 1330), (200, 1290), (260, 1340), (330, 1280)], fill=0, width=4
    )
    for n, img in enumerate([page().convert("RGB"), signed]):
        buf = io.BytesIO()
        img.save(buf, format="PDF")
        ingest_bytes(archive, buf.getvalue(), f"scan{n}.pdf", source="scanner")
    process_all(archive)
    assert trash.listing(archive) == []


def test_compare_page(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path, auto_resolve_identical=False))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"})
    a = ingest_bytes(arch, _pdf(WORDS), "a.pdf").doc_id
    b = ingest_bytes(arch, _pdf(WORDS + " Nachtrag", b"\n%x"), "b.pdf").doc_id
    process_all(arch)
    pair = open_pairs(arch.conn)[0]
    html = c.get(f"/duplicates/{pair['a']['id']}/{pair['b']['id']}").text
    assert "Paar 1 von 1" in html and 'data-key="ArrowLeft"' in html and "Textunterschiede" in html
    # the pair order follows the (random) ids: the extra word is "only left" or "only right"
    assert ("<ins" in html or "<del" in html) and "Nachtrag" in html
    assert (
        c.get("/duplicates/next", follow_redirects=False)
        .headers["location"]
        .startswith("/duplicates/")
    )
    assert {a, b} == {pair["a"]["id"], pair["b"]["id"]}
    arch.close()


@pytest.fixture(autouse=True)
def _no_warmup_threads(monkeypatch):
    # the compare page warms the next pair's cache in a thread; not needed in tests
    monkeypatch.setattr("heftig.web.ui._warm_pagediff", lambda *a, **k: None)
