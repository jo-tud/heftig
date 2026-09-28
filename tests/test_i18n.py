"""Interface languages: English source texts, complete German catalogue, English pages."""

import importlib.util
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heftig import auth, i18n

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import text_pdf

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")


def _collector():
    spec = importlib.util.spec_from_file_location("i18n_script", ROOT / "scripts" / "i18n.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_text_has_a_german_translation():
    entries = _collector().collect()
    cat = i18n.catalogue.__wrapped__("de")
    missing = [e[0] for k, e in entries.items() if k not in cat]
    assert missing == []


def test_translations_keep_the_placeholders():
    cat = i18n.catalogue.__wrapped__("de")
    for msgid, msgstr in cat.items():
        forms = msgstr if isinstance(msgstr, list) else [msgstr]
        want = set(PLACEHOLDER.findall(msgid.split("\x04")[-1]))
        for form in forms:
            got = set(PLACEHOLDER.findall(form))
            # a singular may leave out the count ("ein Dokument")
            assert got == want or (isinstance(msgstr, list) and got <= want), (msgid, form)


def test_stored_english_texts_are_shown_translated():
    with i18n.language("en"):
        stored = i18n._("%(field)s: a similar name already exists", field=i18n._("Sender"))
    assert stored == "Sender: a similar name already exists"
    with i18n.language("de"):
        assert i18n.translate_text(stored).endswith(": ähnlicher Name existiert schon")
        assert i18n.translate_text("free text from a library") == "free text from a library"
    with i18n.language("en"):
        assert i18n.translate_text(stored) == stored


def test_formats():
    from datetime import date

    with i18n.language("en"):
        assert i18n.format_date(date(2026, 3, 5)) == "5 Mar 2026"
        assert i18n.format_number(1234.5) == "1,234.50"
    with i18n.language("de"):
        assert i18n.format_date(date(2026, 3, 5)) == "05.03.2026"
        assert i18n.format_number(1234.5) == "1.234,50"


def test_accept_language():
    assert i18n.pick("de-DE,de;q=0.9,en;q=0.8") == "de"
    assert i18n.pick("en-US,de;q=0.5") == "en"
    assert i18n.pick("fr-FR") == "en"


GERMAN = re.compile(r"\b(Dokumente|Einstellungen|Papierkorb|Eingang|Hinzufügen|Suchen|Absender|"
                    r"Speichern|Abbrechen|abgeheftet|Seite|Zusammenfügen|Vorschläge)\b")  # fmt: skip


@pytest.fixture
def english(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path, language="en"))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"})
    yield arch, c
    arch.close()


def test_pages_are_english(english):
    arch, c = english
    doc = ingest_bytes(arch, text_pdf(["Invoice 2026 from Example Ltd, amount 49.90"]), "a.pdf")
    process_all(arch)
    pages = ["/", "/?q=invoice", f"/documents/{doc.doc_id}", "/inbox", "/upload", "/scan",
             "/settings", "/categories", "/trash", "/suggestions", "/titles",
             "/settings/ai", "/settings/mail", "/settings/scanner"]  # fmt: skip
    for path in pages:
        r = c.get(path)
        assert r.status_code == 200, path
        text = re.sub(r"<script.*?</script>|<[^>]+>", " ", r.text, flags=re.S)
        found = GERMAN.findall(text)
        assert not found, (path, found)
        assert '<html lang="en">' in r.text
        assert 'id="i18n-messages"' not in r.text  # no translations needed for English


def test_german_pages_carry_the_script_texts(tmp_path):
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))  # language="de"
    auth.create_user(app.state.archive.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"})
    page = c.get("/").text
    assert '<html lang="de">' in page and 'id="i18n-messages"' in page and "Privat: aus" in page
    app.state.archive.close()
