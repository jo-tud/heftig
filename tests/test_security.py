"""Findings of the security audit (2026-09-28) that must stay fixed."""

import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from heftig import auth
from heftig.search import SearchParams, SearchSyntaxError, find_lines

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def app(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    yield app
    app.state.archive.close()


def test_catastrophic_regex_is_stopped(archive):
    ingest_bytes(archive, text_pdf(["x" * 60 + "\nRechnungsbetrag: 39,95 EUR"]), "a.pdf")
    process_all(archive)
    t = time.monotonic()
    with pytest.raises(SearchSyntaxError, match="too expensive"):
        find_lines(archive, SearchParams(), "(x+)+#", regex=True)
    assert time.monotonic() - t < 15
    # ordinary expressions still work
    r = find_lines(archive, SearchParams(), r"betrag:\s*\d+,\d\d", regex=True)
    assert r["total_matches"] == 1


def test_big_bodies_outside_uploads_are_refused_before_parsing(app):
    c = TestClient(app)
    big = "x" * (2 * 1024 * 1024)
    r = c.post("/api/auth/login", json={"username": big, "password": "y"})
    assert r.status_code == 413
    r = c.post("/api/auth/login", json={"username": "x" * 300, "password": "y"})
    assert r.status_code == 422  # field length limit
    assert app.state.archive.conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0


def test_login_limit_counts_parallel_attempts_and_stores_no_names(app):
    conn = app.state.archive.conn
    results = []

    def attempt():
        c = TestClient(app)
        results.append(
            c.post(
                "/api/auth/login", json={"username": "jo", "password": "falsch-falsch"}
            ).status_code
        )

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    limit = app.state.archive.settings.login_max_attempts
    assert results.count(401) <= limit  # the check and the count are one step
    keys = [r[0] for r in conn.execute("SELECT DISTINCT key FROM login_attempts")]
    assert not any(k.split(":", 1)[1] == "jo" for k in keys)  # the typed name is hashed


def test_document_ids_are_canonical():
    from pydantic import ValidationError

    from heftig.models import DocumentMetadata

    base = {"sha256": "a" * 64, "original_filename": "x.pdf", "original_relpath":
            "originals/aa/" + "a" * 64 + ".pdf", "mime_type": "application/pdf", "size_bytes": 1,
            "source": "web", "received_at": "2026-01-01T00:00:00Z", "ingest_sequence": 1,
            "updated_at": "2026-01-01T00:00:00Z"}  # fmt: skip
    DocumentMetadata(id="0f8fad5b-d9cb-469f-a165-70867728950e", **base)
    for alias in ("urn:uuid:0f8fad5b-d9cb-469f-a165-70867728950e",
                  "0F8FAD5B-D9CB-469F-A165-70867728950E", "../../etc"):  # fmt: skip
        with pytest.raises(ValidationError):
            DocumentMetadata(id=alias, **base)


def test_every_tiff_frame_is_checked(tmp_path):
    import io

    from PIL import Image

    from heftig.media import UnsupportedFileError, inspect_file

    buf = io.BytesIO()
    small, big = Image.new("L", (100, 100), 255), Image.new("1", (3000, 3000), 1)
    small.save(buf, format="TIFF", save_all=True, append_images=[big], compression="tiff_lzw")
    f = tmp_path / "bombe.tif"
    f.write_bytes(buf.getvalue())
    with pytest.raises(UnsupportedFileError, match="too large"):
        inspect_file(f, max_pages=10, max_megapixels=5)


def test_tokens_cannot_use_the_browser_pages(app):
    a = app.state.archive
    _, full = auth.create_api_token(a.conn, 1, "Scanner-App", "full")
    _, read = auth.create_api_token(a.conn, 1, "Claude", "read")
    doc = ingest_bytes(a, text_pdf(["Seite"]), "a.pdf").doc_id
    process_all(a)
    for token in (full, read):
        c = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        # a stolen token cannot create a second one through the settings form
        r = c.post("/settings/action", data={"action": "token_create", "token_name": "x"})
        assert r.status_code == 403
        for url in ("/settings", "/inbox", "/ai-search?q=Strom", "/"):
            assert c.get(url, follow_redirects=False).status_code == 403, url
        # the REST API and page images (the Claude connection shows pages) stay usable
        assert c.get(f"/api/documents/{doc}").status_code == 200
        assert c.get(f"/documents/{doc}/pages/1.webp").status_code == 200
    assert len(auth.list_tokens(a.conn)) == 2
