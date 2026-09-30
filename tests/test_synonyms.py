"""The archive's own synonyms (synonyms.json) and `heftig search-eval`."""

import json

from heftig import maintenance, synonyms
from heftig.search import SearchParams, search

from .conftest import ScriptedClassifier, ingest_bytes, make_settings, process_all
from .helpers import text_pdf


def _docs(archive):
    from heftig.providers import registry

    registry.override(
        classifier=ScriptedClassifier(
            by_filename={
                "kita.pdf": {"title": "Elternbeitrag Sonnenschein"},
                "oma.pdf": {"title": "Brief von Oma Hilde"},
            }
        )
    )
    ids = {
        "kita.pdf": ingest_bytes(
            archive, text_pdf(["Kinderhaus Sonnenschein\nElternbeitrag August"]), "kita.pdf"
        ).doc_id,
        "oma.pdf": ingest_bytes(archive, text_pdf(["Liebe Grüße von Oma"]), "oma.pdf").doc_id,
    }
    process_all(archive)
    return ids


def test_parse_and_clean_groups():
    groups = synonyms.parse_text(
        "Kita, Kinderhaus Sonnenschein\n\n  Oma ;Großmutter, Oma \nnur ein Wort\n123, 456"
    )
    assert groups == [["Kita", "Kinderhaus Sonnenschein"], ["Oma", "Großmutter"]]
    assert synonyms.as_text(groups) == "Kita, Kinderhaus Sonnenschein\nOma, Großmutter"


def test_own_groups_are_searched(archive):
    ids = _docs(archive)
    assert search(archive.conn, SearchParams(q="Großmutter")).total == 0
    synonyms.save(archive.paths, [["Großmutter", "Oma"], ["Kita", "Kinderhaus Sonnenschein"]])
    assert [i["id"] for i in search(archive.conn, SearchParams(q="Großmutter")).items] == [
        ids["oma.pdf"]
    ]
    # a phrase as the other word; the built-in group Kita/Kindergarten still applies too
    assert search(archive.conn, SearchParams(q="Kindergarten")).items[0]["id"] == ids["kita.pdf"]
    # removing the groups takes effect at once
    synonyms.save(archive.paths, [])
    assert not (archive.paths.root / synonyms.FILENAME).exists()
    assert search(archive.conn, SearchParams(q="Großmutter")).total == 0


def test_own_groups_travel_with_export(archive, tmp_path):
    _docs(archive)
    synonyms.save(archive.paths, [["Großmutter", "Oma"]])
    path = maintenance.export_archive(archive, tmp_path / "exports")
    from heftig.archive import Archive

    target = Archive(make_settings(tmp_path / "target"))
    synonyms.save(target.paths, [["Kita", "Krippe"]])
    report = maintenance.import_archive(target, path)
    assert report["synonyms"] == 1
    assert synonyms.load(target.paths) == [["Kita", "Krippe"], ["Großmutter", "Oma"]]
    target.close()


def test_settings_page_saves_groups(tmp_path):
    from fastapi.testclient import TestClient

    from heftig import auth
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    auth.create_user(app.state.archive.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    csrf = c.post(
        "/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"}
    ).json()["csrf_token"]
    page = c.get("/settings").text
    assert "Suche: Wörter mit gleicher Bedeutung" in page and "Nebenkosten, Betriebskosten" in page
    r = c.post(
        "/settings/synonyms",
        data={"synonyms": "Oma, Großmutter", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Gruppe+gespeichert" in r.headers["location"]
    assert synonyms.load(app.state.archive.paths) == [["Oma", "Großmutter"]]
    assert "Oma, Großmutter</textarea>" in c.get("/settings").text
    app.state.archive.close()


def test_search_eval_cli(archive, tmp_path, capsys, monkeypatch):
    from heftig import cli

    ids = _docs(archive)
    queries = tmp_path / "queries.json"
    queries.write_text(
        json.dumps(
            [
                {"q": "Elternbeitrag", "expect": ["Elternbeitrag Sonnenschein"]},
                {
                    "q": "Oma",
                    "expect": [ids["oma.pdf"][:8]],
                    "wrong": ["Elternbeitrag Sonnenschein"],
                },
                {"q": "gibt es nicht", "expect": ["Kein solcher Titel"]},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_archive", lambda: archive)
    assert cli.main(["search-eval", str(queries)]) == 0
    captured = capsys.readouterr()
    assert "Kein solcher Titel" in captured.err  # names that match no document are reported
    assert "mrr@10 1.000" in captured.out and "queries 2" in captured.out
    assert cli.main(["search-eval", str(queries), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["success@1"] == 1.0
