"""Who sent a document by e-mail: the address on the document and in the list, the filter
"E-mail from", and names for addresses (senders.json)."""

import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth, maintenance, senders
from heftig.search import SearchParams, search
from heftig.web.app import create_app

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"


def _mail(archive, text, name, sender, subject="Unterlagen"):
    return ingest_bytes(
        archive, text_pdf([text]), name, "email",
        source_details={"from": sender, "subject": subject, "message_id": f"<{name}@example.org>"},
    ).doc_id  # fmt: skip


@pytest.fixture
def docs(archive):
    ids = {
        "kita": _mail(archive, "Kita Sonnenschein Elternbeitrag", "kita.pdf", "Anna.Beispiel@Example.org"),
        "arzt": _mail(archive, "Rechnung Zahnarzt Zahnreinigung", "arzt.pdf", "anna.beispiel@example.org"),
        "strom": _mail(archive, "Stadtwerke Abschlag Strom", "strom.pdf", "info@stadtwerke.example"),
        "scan": ingest_bytes(archive, text_pdf(["Mietvertrag Kaltmiete"]), "scan.pdf", "scanner").doc_id,
    }  # fmt: skip
    process_all(archive)
    return ids


def test_filter_and_counts_by_sender_address(archive, docs):
    # upper and lower case are the same address
    res = search(archive.conn, SearchParams(email_from=["ANNA.BEISPIEL@example.org"]),
                 with_facets=True)  # fmt: skip
    assert {i["id"] for i in res.items} == {docs["kita"], docs["arzt"]}
    assert all(i["email_from"].lower() == "anna.beispiel@example.org" for i in res.items)
    # the counts show the other addresses too (a group combined with OR)
    counts = {f["value"]: f["count"] for f in res.facets["email_from"]}
    assert counts == {"anna.beispiel@example.org": 2, "info@stadtwerke.example": 1}
    res = search(archive.conn, SearchParams(q="Zahnarzt", email_from=["info@stadtwerke.example"]))
    assert res.total == 0


def test_names_are_cleaned_and_merged(archive):
    saved = senders.save(archive.paths, {" Anna.Beispiel@Example.org ": "  Anna  ", "kein-at": "X",
                                         "leer@example.org": " "})  # fmt: skip
    assert saved == {"anna.beispiel@example.org": "Anna"}
    assert senders.label(saved, "ANNA.beispiel@example.org") == "Anna"
    assert senders.label(saved, "info@stadtwerke.example") == "info@stadtwerke.example"
    # an import adds names for new addresses, never overwrites
    assert senders.merge(archive.paths, {"anna.beispiel@example.org": "Mama",
                                         "opa@example.org": "Opa"}) == 1  # fmt: skip
    assert senders.load(archive.paths) == {
        "anna.beispiel@example.org": "Anna",
        "opa@example.org": "Opa",
    }
    senders.save(archive.paths, {})
    assert not (archive.paths.root / senders.FILENAME).exists()


def test_names_travel_with_export_and_backup(archive, docs, tmp_path):
    senders.save(archive.paths, {"anna.beispiel@example.org": "Anna"})
    out = maintenance.export_archive(archive, tmp_path / "export")
    assert (out / senders.FILENAME).exists()
    backup = maintenance.backup(archive, tmp_path / "backup")
    assert (backup / senders.FILENAME).exists()
    from heftig.archive import Archive

    other = Archive(make_settings(tmp_path / "other"))
    try:
        report = maintenance.import_archive(other, out)
        assert report["senders"] == 1
        assert senders.load(other.paths) == {"anna.beispiel@example.org": "Anna"}
    finally:
        other.close()


@pytest.fixture
def client(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(make_settings(tmp_path))
    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    c = TestClient(app)
    c.csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    yield c
    app.state.archive.close()


def test_sender_shown_named_and_filtered_in_the_web(client):
    a = client.app.state.archive
    kita = _mail(
        a, "Kita Sonnenschein Elternbeitrag", "kita.pdf", "Anna.Beispiel@Example.org", "Kita"
    )
    _mail(a, "Stadtwerke Abschlag Strom", "strom.pdf", "info@stadtwerke.example")
    process_all(a)
    # the document page: from whom, the subject, a link to all documents from the address
    page = client.get(f"/documents/{kita}").text
    assert "Per E-Mail von" in page and "Anna.Beispiel@Example.org" in page and "„Kita“" in page
    assert "/?email_from=Anna.Beispiel%40Example.org" in page
    assert "<dt>Betreff</dt>" in page  # readable labels under Origin
    # the list: the address when hovering over "E-Mail"; the filter with counts
    page = client.get("/").text
    assert 'title="Per E-Mail von Anna.Beispiel@Example.org"' in page
    assert "Per E-Mail von" in page and "/settings/senders" in page
    # a name for the address
    page = client.get("/settings/senders").text
    assert "anna.beispiel@example.org" in page and "info@stadtwerke.example" in page
    r = client.post("/settings/senders", follow_redirects=False, data={
        "csrf_token": client.csrf,
        "address": ["anna.beispiel@example.org", "info@stadtwerke.example"],
        "name": ["Anna", ""],
    })  # fmt: skip
    assert r.status_code == 303
    assert senders.load(a.paths) == {"anna.beispiel@example.org": "Anna"}
    page = client.get("/").text
    assert 'title="Per E-Mail von Anna &lt;Anna.Beispiel@Example.org&gt;"' in page
    assert ">Anna</span>" in page  # the filter shows the name
    page = client.get("/?email_from=anna.beispiel%40example.org").text
    assert "Per E-Mail von: Anna" in page  # the chip
    page = client.get(f"/documents/{kita}").text
    assert ">Anna</a>" in page and "Namen vergeben" not in page.split("</header>")[0]
    # the API filters too
    r = client.get("/api/documents", params={"email_from": "anna.beispiel@example.org"})
    assert [i["id"] for i in r.json()["items"]] == [kita]
