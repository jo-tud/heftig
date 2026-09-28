import hashlib
import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth
from heftig.web.app import create_app

from .conftest import make_settings, process_all
from .helpers import image_bytes, text_image, text_pdf

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def app(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(make_settings(tmp_path, max_upload_mb=2))
    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    yield app
    app.state.archive.close()


@pytest.fixture
def client(app):
    return TestClient(app)


def login(client) -> str:
    r = client.post("/api/auth/login", json={"username": "jo", "password": PASSWORD})
    assert r.status_code == 200
    return r.json()["csrf_token"]


def upload(client, csrf, data=None, name="a.pdf", kind="digital", headers=None):
    h = {"X-CSRF-Token": csrf} if csrf else {}
    h.update(headers or {})
    return client.post(
        "/api/documents",
        files=[("files", (name, data or text_pdf(["Hallo Welt"]), "application/pdf"))],
        data={"kind": kind},
        headers=h,
    )


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"


def test_ready_detects_unwritable_archive(client, app):
    tmp = app.state.archive.paths.tmp
    tmp.chmod(0o500)
    try:
        r = client.get("/ready")
        assert r.status_code == 503 and r.json()["checks"]["archive_writable"].startswith("error")
    finally:
        tmp.chmod(0o700)


def test_everything_requires_login(client):
    assert client.get("/api/documents").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.post("/api/documents", files=[("files", ("a.pdf", b"x"))]).status_code == 401


def test_login_cookie_flags_and_rate_limit(client):
    for _ in range(5):
        r = client.post("/api/auth/login", json={"username": "jo", "password": "falsch"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "jo", "password": PASSWORD})
    assert r.status_code == 429  # locked for this address/user
    c2 = TestClient(client.app)
    c2.app.state.archive.conn.execute("DELETE FROM login_attempts")
    r = c2.post("/api/auth/login", json={"username": "jo", "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Secure" not in cookie  # plain http in tests; set automatically behind HTTPS


def test_csrf_is_required_for_session_writes(client):
    csrf = login(client)
    assert upload(client, None).status_code == 403
    assert upload(client, "falsch").status_code == 403
    r = upload(client, csrf, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = upload(client, csrf)
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "created"


def test_upload_detail_original_and_duplicate(client):
    csrf = login(client)
    data = text_pdf(["Rechnung Nr. 4711 vom 01.03.2025"])
    r = upload(client, csrf, data, name="rechnung.pdf", kind="paper").json()["results"][0]
    doc_id = r["document_id"]
    dup = upload(client, csrf, data, name="nochmal.pdf").json()["results"][0]
    assert dup["status"] == "duplicate" and dup["document_id"] == doc_id
    process_all(client.app.state.archive)
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["metadata"]["paper"] is True and d["metadata"]["source"] == "web"
    orig = client.get(f"/api/documents/{doc_id}/original")
    assert orig.content == data and hashlib.sha256(orig.content).hexdigest() == r["sha256"]
    assert orig.headers["x-content-type-options"] == "nosniff"
    assert orig.headers["content-disposition"].startswith("attachment")
    inline = client.get(f"/documents/{doc_id}/original?inline=1")
    assert inline.headers["content-disposition"].startswith("inline")
    assert "4711" in client.get(f"/api/documents/{doc_id}/text").text
    res = client.get("/api/documents", params={"q": "4711"}).json()
    assert [i["id"] for i in res["items"]] == [doc_id]
    html = client.get(f"/documents/{doc_id}").text
    assert "Jetzt abgeheftet" in html


def test_unsupported_upload_is_rejected_friendly(client):
    csrf = login(client)
    r = upload(client, csrf, b"hello", name="notiz.txt")
    assert r.status_code == 422
    assert "nicht unterstützt" in r.json()["results"][0]["message"]


def test_upload_size_limits(client):
    csrf = login(client)
    big = b"%PDF-1.4\n" + b"0" * (3 * 1024 * 1024)
    r = upload(client, csrf, big)
    assert r.status_code == 422 and "größer" in r.json()["results"][0]["message"]
    huge = b"%PDF-1.4\n" + b"0" * (12 * 1024 * 1024)
    r = upload(client, csrf, huge)
    assert r.status_code == 413


def test_api_token_flow(client):
    csrf = login(client)
    t = client.post("/api/tokens", json={"name": "Scan-App"}, headers={"X-CSRF-Token": csrf}).json()
    token = t["token"]
    anon = TestClient(client.app)
    h = {"Authorization": f"Bearer {token}"}
    r = anon.post("/api/documents", files=[("files", ("x.png", image_bytes(text_image("Foto")), "image/png"))],
                  headers=h)  # fmt: skip
    res = r.json()["results"][0]
    assert r.status_code == 200 and res["status"] == "created"
    d = anon.get(f"/api/documents/{res['document_id']}", headers=h).json()
    assert d["metadata"]["source"] == "api"
    assert anon.post("/api/tokens", json={"name": "x"}, headers=h).status_code == 403
    listed = client.get("/api/tokens").json()
    assert listed[0]["token_prefix"] == token[:10] and "token_hash" not in listed[0]
    assert (
        client.delete(f"/api/tokens/{t['id']}", headers={"X-CSRF-Token": csrf}).status_code == 200
    )
    assert anon.get("/api/documents", headers=h).status_code == 401


def test_patch_locks_and_delete_needs_confirmation(client):
    csrf = login(client)
    h = {"X-CSRF-Token": csrf}
    doc_id = upload(client, csrf).json()["results"][0]["document_id"]
    process_all(client.app.state.archive)
    r = client.patch(f"/api/documents/{doc_id}", json={"title": "Neu", "document_date": "2025-02-03",
                                                        "locks": {"summary": True}}, headers=h)  # fmt: skip
    m = r.json()
    assert m["title"] == "Neu" and m["field_locks"] == {
        "title": True,
        "document_date": True,
        "summary": True,
    }
    assert (
        client.patch(
            f"/api/documents/{doc_id}", json={"document_date": "03.02.2025"}, headers=h
        ).status_code
        == 422
    )
    assert client.delete(f"/api/documents/{doc_id}", headers=h).status_code == 422
    assert client.delete(f"/api/documents/{doc_id}?confirm={doc_id}", headers=h).status_code == 200
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/original").status_code == 404


def test_filing_via_api(client):
    csrf = login(client)
    h = {"X-CSRF-Token": csrf}
    ids = [upload(client, csrf, text_pdf([f"Papier {i}"]), kind="paper").json()["results"][0]["document_id"]
           for i in range(3)]  # fmt: skip
    for i in ids:
        client.post(f"/api/documents/{i}/filing", json={"action": "file"}, headers=h)
    pos = client.get(f"/api/documents/{ids[0]}").json()["filing_position"]
    assert pos["position_from_top"] == 3 and pos["total_in_section"] == 3
    assert [x["id"] for x in pos["above"]] == [ids[2], ids[1]]


def test_html_is_escaped(client):
    csrf = login(client)
    doc_id = upload(client, csrf).json()["results"][0]["document_id"]
    client.patch(f"/api/documents/{doc_id}", json={"title": "<script>alert(1)</script>"},
                 headers={"X-CSRF-Token": csrf})  # fmt: skip
    for url in ("/", f"/documents/{doc_id}"):
        html = client.get(url).text
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_ui_pages_render(client):
    csrf = login(client)
    upload(client, csrf)
    for url in (
        "/",
        "/inbox",
        "/upload",
        "/settings",
        "/?q=hallo&tag=x&filed=no",
        "/inbox/progress",
    ):
        assert client.get(url).status_code == 200, url


def test_ui_form_edit_with_csrf(client):
    csrf = login(client)
    doc_id = upload(client, csrf).json()["results"][0]["document_id"]
    r = client.post(f"/documents/{doc_id}/edit", data={"title": "Per Formular", "tags": "A, B"},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 403
    r = client.post(f"/documents/{doc_id}/edit",
                    data={"csrf_token": csrf, "title": "Per Formular", "tags": "A, B", "lock_title": "on"},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    m = client.get(f"/api/documents/{doc_id}").json()["metadata"]
    assert m["title"] == "Per Formular" and m["tags"] == ["A", "B"] and m["field_locks"]["title"]


def test_german_amounts_tag_lock_and_stale_review(client, app):
    from heftig import documents as docs
    from heftig.db import write_tx
    from heftig.models import Suggestion

    csrf = login(client)
    doc_id = upload(client, csrf).json()["results"][0]["document_id"]
    process_all(app.state.archive)
    form = {"csrf_token": csrf, "cf_key_0": "Betrag", "cf_type_0": "monetary",
            "cf_value_0": "1.234", "cf_currency_0": "EUR", "tags": "Steuer", "lock_tags": "on"}  # fmt: skip
    client.post(f"/documents/{doc_id}/edit", data=form)
    m = client.get(f"/api/documents/{doc_id}").json()["metadata"]
    assert m["custom_fields"]["Betrag"]["value"] == 1234  # not 1.234
    assert m["field_locks"].get("tags")  # the lock ticked while editing the tags counts
    # an amount filter that cannot be read is reported instead of showing everything
    assert "nicht verstanden" in client.get("/?cf_key=Betrag&cf_min=zwölf").text
    assert f"/documents/{doc_id}" in client.get("/?cf_key=Betrag&cf_min=1.000").text  # 1000
    assert f"/documents/{doc_id}" not in client.get("/?cf_key=Betrag&cf_min=1.300,50").text
    # accepting a tag suggestion settles it
    a = app.state.archive
    with write_tx(a.conn):
        meta = docs.load_meta(a, doc_id)
        meta.suggestions = [Suggestion(field="tags", value=["Haus"], reason="unsicher")]
        meta.status = "needs_review"
        docs.persist(a, meta)
    meta = docs.accept_suggestion(a, doc_id, 0)
    assert "Haus" in meta.tags and meta.suggestions == [] and meta.status == "done"
    # "Alles geprüft" from an outdated page confirms nothing
    r = client.post(f"/documents/{doc_id}/action", data={
        "csrf_token": csrf, "action": "reviewed", "revision": str(meta.revision - 1)})  # fmt: skip
    assert "inzwischen verändert" in r.text


def test_taxonomy_merge_api(client):
    csrf = login(client)
    h = {"X-CSRF-Token": csrf}
    d1 = upload(client, csrf, text_pdf(["eins"])).json()["results"][0]["document_id"]
    d2 = upload(client, csrf, text_pdf(["zwei"])).json()["results"][0]["document_id"]
    client.patch(f"/api/documents/{d1}", json={"correspondent": "Telekom"}, headers=h)
    client.patch(f"/api/documents/{d2}", json={"correspondent": "Deutsche Telekom AG"}, headers=h)
    terms = {t["name"]: t["id"] for t in client.get("/api/taxonomy?kind=correspondent").json()}
    r = client.post(
        f"/api/taxonomy/{terms['Telekom']}/merge",
        json={"into": terms["Deutsche Telekom AG"]},
        headers=h,
    )
    assert r.json()["documents_updated"] == 1
    assert (
        client.get(f"/api/documents/{d1}").json()["metadata"]["correspondent"]
        == "Deutsche Telekom AG"
    )
    t = client.get("/api/taxonomy?kind=correspondent").json()
    assert len(t) == 1 and t[0]["aliases"] == ["Telekom"] and t[0]["doc_count"] == 2
    res = client.get("/api/documents", params={"correspondent": "Telekom"}).json()
    assert res["total"] == 2  # old name still works as alias


def test_openapi_schema(client):
    spec = client.get("/api/openapi.json").json()
    for path in ("/api/documents", "/api/documents/{doc_id}", "/api/documents/{doc_id}/original",
                 "/api/documents/{doc_id}/filing", "/api/jobs/{job_id}/retry", "/api/taxonomy/{term_id}/merge",
                 "/api/export", "/api/import", "/api/index/rebuild", "/api/auth/login", "/api/tokens"):  # fmt: skip
        assert path in spec["paths"], path


def test_ui_edit_locks_changed_fields_even_without_checkbox(client):
    csrf = login(client)
    doc_id = upload(client, csrf).json()["results"][0]["document_id"]
    client.post(f"/documents/{doc_id}/edit", data={"csrf_token": csrf, "title": "Manuell"},
                follow_redirects=False)  # fmt: skip
    m = client.get(f"/api/documents/{doc_id}").json()["metadata"]
    assert m["title"] == "Manuell" and m["field_locks"] == {"title": True}
    # unticking the box of an unchanged field unlocks it
    client.post(f"/documents/{doc_id}/edit", data={"csrf_token": csrf, "title": "Manuell"},
                follow_redirects=False)  # fmt: skip
    assert client.get(f"/api/documents/{doc_id}").json()["metadata"]["field_locks"] == {}


def test_scan_page_and_manifest(client):
    login(client)
    r = client.get("/scan")
    assert r.status_code == 200 and "opencv-4.7.0.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "'wasm-unsafe-eval'" in csp and "connect-src 'self' data:" in csp
    assert "camera=(self)" in r.headers["permissions-policy"]
    # everywhere else the camera stays off and eval stays forbidden
    other = client.get("/")
    assert "camera=()" in other.headers["permissions-policy"]
    assert "unsafe-eval" not in other.headers["content-security-policy"]
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["start_url"] == "/scan"
    v = client.get("/static/vendor/opencv-4.7.0.js")
    assert v.status_code == 200 and "immutable" in v.headers["cache-control"]


def test_privacy_mode_markup(client):
    csrf = login(client)
    doc_id = upload(client, csrf).json()["results"][0]["document_id"]
    client.patch(f"/api/documents/{doc_id}", headers={"X-CSRF-Token": csrf}, json={
        "custom_fields": {"Vertragsnummer": {"type": "string", "value": "</script><b>8372"}}})  # fmt: skip
    html = client.get(f"/documents/{doc_id}").text
    assert '<script src="/static/privacy-init.js?v=' in html  # before first paint (not deferred)
    assert 'id="privacy-toggle"' in html and 'class="pv-media viewer-wrap"' in html
    # values are embedded as JSON without breaking out of the script element
    assert "</script><b>" not in html and "\\u003c/script\\u003e" in html
    assert "data-pv" in client.get("/").text


def test_page_viewer_renders_pages(client, app):
    csrf = login(client)
    r = client.post(
        "/api/documents",
        files=[("files", ("drei.pdf", text_pdf(["Eins", "Zwei", "Drei"]), "application/pdf"))],
        headers={"X-CSRF-Token": csrf},
    )
    doc_id = r.json()["results"][0]["document_id"]
    html = client.get(f"/documents/{doc_id}").text
    assert html.count('class="vpage"') == 3 and "iframe" not in html
    img = client.get(f"/documents/{doc_id}/pages/2.webp?w=900")
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    import io

    from PIL import Image

    assert Image.open(io.BytesIO(img.content)).width == 960  # snapped to the next size step
    etag = img.headers["etag"]
    assert (
        client.get(
            f"/documents/{doc_id}/pages/2.webp?w=900", headers={"If-None-Match": etag}
        ).status_code
        == 304
    )
    assert client.get(f"/documents/{doc_id}/pages/4.webp").status_code == 404
    cache = app.state.archive.paths.doc_dir(doc_id) / "cache"
    assert (cache / "p2-960.webp").exists()
    TestClient(app).cookies.clear()
    assert TestClient(app).get(
        f"/documents/{doc_id}/pages/1.webp", follow_redirects=False
    ).status_code in (303, 401)


def test_review_flow_one_after_the_other(client, app):
    from heftig import documents as docs
    from heftig.db import write_tx
    from heftig.models import Suggestion

    csrf = login(client)
    ids = [upload(client, csrf, text_pdf([f"Brief {n}"]), f"b{n}.pdf").json()["results"][0]
           ["document_id"] for n in range(3)]  # fmt: skip
    process_all(app.state.archive)
    a = app.state.archive
    for d in ids[:2]:
        with write_tx(a.conn):
            m = docs.load_meta(a, d)
            m.status = "needs_review"
            m.suggestions = [Suggestion(field="document_type", value="Brief", reason="unsicher")]
            docs.persist(a, m)
    inbox = client.get("/inbox").text
    assert "Dokumente prüfen" in inbox and 'href="/review/next"' in inbox
    first = client.get("/review/next", follow_redirects=False).headers["location"]
    assert first == f"/documents/{ids[1]}?review=1"  # newest first
    page = client.get(first).text
    assert "Geprüft → nächstes" in page and "noch <strong>2</strong>" in page
    assert "1 offener Vorschlag" in page
    rev = docs.load_meta(a, ids[1]).revision
    r = client.post(f"/documents/{ids[1]}/edit", data={
        "csrf_token": csrf, "revision": str(rev), "title": "Brief vom Amt", "then": "review_next",
        "review": "1"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == f"/review/next?after={ids[1]}"
    m = docs.load_meta(a, ids[1])
    assert m.title == "Brief vom Amt" and m.status == "done" and m.suggestions == []
    nxt = client.get(r.headers["location"], follow_redirects=False).headers["location"]
    assert nxt == f"/documents/{ids[0]}?review=1"
    # actions on the page keep the review mode; skipping comes back round to the same one
    r = client.post(f"/documents/{ids[0]}/action", data={
        "csrf_token": csrf, "action": "accept_0", "revision": str(docs.load_meta(a, ids[0]).revision),
        "review": "1"}, follow_redirects=False)  # fmt: skip
    assert "review=1" in r.headers["location"]
    skip = client.get(f"/review/next?after={ids[0]}", follow_redirects=False).headers["location"]
    assert skip.startswith("/inbox") or skip == f"/documents/{ids[0]}?review=1"
    r = client.post(f"/documents/{ids[0]}/action", data={
        "csrf_token": csrf, "action": "reviewed", "review": "1",
        "revision": str(docs.load_meta(a, ids[0]).revision)}, follow_redirects=False)  # fmt: skip
    done = client.get(r.headers["location"], follow_redirects=False).headers["location"]
    assert done.startswith("/inbox") and "Dokumente prüfen" not in client.get("/inbox").text


def test_suggestions_in_bulk(client, app):
    from heftig import documents as docs
    from heftig.db import write_tx
    from heftig.models import Suggestion

    csrf = login(client)
    ids = [upload(client, csrf, text_pdf([f"Schreiben Nummer {n}"]), f"s{n}.pdf").json()["results"][0]
           ["document_id"] for n in range(4)]  # fmt: skip
    process_all(app.state.archive)
    a = app.state.archive
    for n, d in enumerate(ids):
        with write_tx(a.conn):
            m = docs.load_meta(a, d)
            m.status = "needs_review"
            m.suggestions = [
                Suggestion(
                    field="document_type",
                    value="Schreiben",
                    reason="Zuordnung unsicher",
                    confidence=0.4,
                )
            ]
            if n == 0:  # a competing proposal for the same field
                m.suggestions.append(
                    Suggestion(field="document_type", value="Brief", reason="Neuer Eintrag")
                )
            docs.persist(a, m)
    assert "gleiche Vorschläge auf mehreren Dokumenten" in client.get("/inbox").text
    page = client.get("/suggestions").text
    assert "Schreiben" in page and "auch vorgeschlagen: Brief" in page and "Ø 40 %" in page
    value = page.split('name="value" value="')[1].split('"')[0].replace("&#34;", '"')
    # one document is deselected: it keeps its suggestion
    r = client.post("/suggestions/action", data={"csrf_token": csrf, "action": "accept",
                    "field": "document_type", "value": value, "doc": ids[:3]}, follow_redirects=False)  # fmt: skip
    assert "3+Dokumenten+%C3%BCbernommen" in r.headers["location"]
    for d in ids[:3]:
        m = docs.load_meta(a, d)
        assert (
            m.document_type == "Schreiben" and m.field_locks["document_type"] and not m.suggestions
        )
    assert docs.load_meta(a, ids[3]).suggestions  # not touched
    client.post("/suggestions/action", data={"csrf_token": csrf, "action": "dismiss",
                "field": "document_type", "value": value, "doc": [ids[3]]})  # fmt: skip
    m = docs.load_meta(a, ids[3])
    assert m.document_type is None and not m.suggestions and m.status == "done"
    assert "Keine offenen Vorschläge" in client.get("/suggestions").text


def test_thumbnails_in_exact_sizes(client, app):
    import io

    from PIL import Image

    csrf = login(client)
    doc = upload(client, csrf).json()["results"][0]["document_id"]
    process_all(app.state.archive)
    for w in (72, 144, 216, 54):
        r = client.get(f"/documents/{doc}/preview.webp?w={w}")
        assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).width == w
    r = client.get(f"/documents/{doc}/preview.webp?w=100")  # the next step up
    assert Image.open(io.BytesIO(r.content)).width == 108
    etag = r.headers["etag"]
    assert (
        client.get(
            f"/documents/{doc}/preview.webp?w=100", headers={"if-none-match": etag}
        ).status_code
        == 304
    )
    page = client.get("/?q=Hallo").text
    assert f"/documents/{doc}/preview.webp?w=144&amp;t=" in page and " 2x" in page


def test_categories_page(client, app):
    from heftig import taxonomy as tax
    from heftig.db import write_tx

    csrf = login(client)
    a = app.state.archive
    with write_tx(a.conn):
        for name in ("Telekom Deutschland GmbH", "Deutsche Telekom AG", "Vodafone GmbH"):
            tax.get_or_create(a.conn, "correspondent", name)
    settings = client.get("/settings").text
    assert "3 Absender" in settings and 'href="/categories"' in settings
    assert settings.count("<option") < 20  # no list of every name per row any more
    page = client.get("/categories?kind=correspondent&q=telekom").text
    assert (
        "Deutsche Telekom AG" in page
        and "Vodafone GmbH</span>" not in page.split('id="dl-terms"')[0]
    )
    tid = tax.find_term(a.conn, "correspondent", "Telekom Deutschland GmbH")
    r = client.post("/categories/action", data={
        "csrf_token": csrf, "action": "term_merge", "kind": "correspondent", "term_id": str(tid),
        "into": "Deutsche Telekom AG", "back_q": "telekom"}, follow_redirects=False)  # fmt: skip
    assert "Zusammengef" in r.headers["location"] and "q=telekom" in r.headers["location"]
    names = {t.name: t.aliases for t in tax.list_terms(a.conn, "correspondent")}
    assert set(names) == {"Deutsche Telekom AG", "Vodafone GmbH"}
    assert "Telekom Deutschland GmbH" in names["Deutsche Telekom AG"]
    r = client.post("/categories/action", data={
        "csrf_token": csrf, "action": "term_merge", "kind": "correspondent",
        "term_id": str(tax.find_term(a.conn, "correspondent", "Vodafone GmbH")),
        "into": "Gibt es nicht"}, follow_redirects=False)  # fmt: skip
    assert "Fehler" in r.headers["location"]
