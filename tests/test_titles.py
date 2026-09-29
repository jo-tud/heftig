"""Consistent titles: normalisation, examples for the classifier, harmonisation proposals."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth, titles
from heftig import documents as docs
from heftig.providers import registry
from heftig.worker import run_until_idle

from .conftest import ScriptedClassifier, ingest_bytes, make_settings, process_all
from .helpers import text_pdf

PASSWORD = "richtig-langes-passwort"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Kontoabrechnung 3. Quartal 2020 Girokonto", "Kontoabrechnung Q3 2020 Girokonto"),
        ("Kontoabrechnung Girokonto III. Quartal 2019", "Kontoabrechnung Girokonto Q3 2019"),
        ("Abrechnung Quartal 4/2021", "Abrechnung Q4 2021"),
        ("Kontoabrechnung Q1/2022", "Kontoabrechnung Q1 2022"),
        ("Kontoabrechnung 2022 Q2", "Kontoabrechnung Q2 2022"),
        ("2023 Steuerbescheid", "Steuerbescheid 2023"),
        ("2020 - Steuerbescheid", "Steuerbescheid 2020"),
        ("2014 Steuerbescheid Beide 2015", "2014 Steuerbescheid Beide 2015"),  # ambiguous: keep
        ("  Lohnabrechnung   März 2025 - ", "Lohnabrechnung März 2025"),
        ("Einkommensteuerbescheid 2015", "Einkommensteuerbescheid 2015"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_title(raw, want):
    assert titles.normalize_title(raw) == want


def _bank_statement(archive, name, quarter, year, title):
    by = {name: {"title": title, "correspondent": "Beispielbank eG", "correspondent_confidence": 0.9,
                 "document_type": "Kontoauszug", "document_type_confidence": 0.9}}  # fmt: skip
    registry.override(classifier=ScriptedClassifier(by_filename=by))
    text = (
        f"Beispielbank eG\nWir informieren Sie - Ihre Kontoabrechnung\n{quarter}. Quartal {year}\n"
        "Girokonto Entgelte Kontoführung Buchungsposten Zinsen Abschluss"
    )
    r = ingest_bytes(archive, text_pdf([text]), name)
    process_all(archive)
    return r.doc_id


def test_classifier_gets_titles_of_similar_documents(archive):
    _bank_statement(archive, "a.pdf", 1, 2020, "Kontoabrechnung Girokonto Q1 2020")
    _bank_statement(archive, "b.pdf", 2, 2020, "Kontoabrechnung Girokonto Q2 2020")
    fake = ScriptedClassifier(default={"title": "Kontoabrechnung 3. Quartal 2020 Girokonto"})
    registry.override(classifier=fake)
    text = (
        "Beispielbank eG\nWir informieren Sie - Ihre Kontoabrechnung\n3. Quartal 2020\n"
        "Girokonto Entgelte Kontoführung Buchungsposten Zinsen Abschluss"
    )
    r = ingest_bytes(archive, text_pdf([text]), "c.pdf")
    process_all(archive)
    examples = fake.requests[-1].title_examples
    assert {e["title"] for e in examples} == {
        "Kontoabrechnung Girokonto Q1 2020",
        "Kontoabrechnung Girokonto Q2 2020",
    }
    assert examples[0]["correspondent"] == "Beispielbank eG"
    # the prompt carries the examples and the naming scheme
    from heftig.providers.prompt import classify_system, classify_user_message

    assert "Kontoabrechnung Girokonto Q1 2020" in classify_user_message(fake.requests[-1])
    # written in the installation's language (German here), one fixed prompt per language
    assert fake.requests[-1].language == "de"
    assert '"Q3 2020" (quarter)' in classify_system("de")
    assert "in German." in classify_system("de") and "in English." in classify_system("en")
    # AI titles are normalised
    assert docs.load_meta(archive, r.doc_id).title == "Kontoabrechnung Q3 2020 Girokonto"


class Harmonizer(ScriptedClassifier):
    def __init__(self, mapping):
        super().__init__()
        self.mapping = mapping
        self.calls = []

    def complete_json(self, system, user, schema, max_tokens=8000):
        groups = json.loads(user.split("\n", 1)[1])
        self.calls.append(groups)
        out = []
        for g in groups:
            for d in g["documents"]:
                if not d["fixed"]:
                    out.append({"id": d["id"], "title": self.mapping.get(d["title"], d["title"])})
        return {"titles": out}


def test_harmonise_proposals_accept_and_safety(archive):
    a = _bank_statement(archive, "a.pdf", 1, 2020, "Kontoabrechnung Girokonto Q1 2020")
    b = _bank_statement(archive, "b.pdf", 2, 2020, "Kontoabrechnung 2. Quartal 2020 Girokonto")
    c = _bank_statement(archive, "c.pdf", 3, 2020, "GLS Kontoabrechnung Q3 2020")
    d = _bank_statement(archive, "d.pdf", 4, 2020, "Kontoabrechnung Girokonto Q4 2020")
    docs.update_fields(archive, d, {"title": "Kontoabrechnung Girokonto Q4 2020"})  # locked
    h = Harmonizer({
        # (the AI title was already normalised when it was classified)
        "Kontoabrechnung Q2 2020 Girokonto": "Kontoabrechnung Girokonto Q2 2020",
        "GLS Kontoabrechnung Q3 2020": "Kontoabrechnung Girokonto 3. Quartal 2020",
    })  # fmt: skip
    registry.override(classifier=h)
    # only titles, dates and the two names are sent - no document text
    est = titles.estimate(archive.conn, "claude-opus-5-5")
    assert est["documents"] == 4 and est["requests"] == 1 and 0 < est["usd"] < 0.05
    titles.enqueue_job(archive)
    run_until_idle(archive)
    sent = json.dumps(h.calls)
    assert (
        "Girokonto Entgelte" not in sent and "Beispielbank eG" in sent and '"fixed": true' in sent
    )
    groups = titles.proposals(archive.conn)
    assert groups[0]["label"] == "Beispielbank eG · Kontoauszug"
    new = {p["doc_id"]: p["new_title"] for p in groups[0]["items"]}
    # unchanged titles are no proposal; results are normalised; the locked title is untouched
    assert new == {b: "Kontoabrechnung Girokonto Q2 2020", c: "Kontoabrechnung Girokonto Q3 2020"}
    # a title changed in the meantime is never overwritten
    docs.update_fields(archive, c, {"title": "Mein eigener Titel"})
    assert titles.accept(archive, [b, c]) == {"accepted": 1, "stale": 1}
    mb = docs.load_meta(archive, b)
    assert mb.title == "Kontoabrechnung Girokonto Q2 2020" and mb.locked("title")
    assert docs.load_meta(archive, c).title == "Mein eigener Titel"
    assert titles.pending_count(archive.conn) == 0
    assert docs.load_meta(archive, a).title == "Kontoabrechnung Girokonto Q1 2020"


def test_harmonise_needs_an_ai_provider(archive):
    registry.override(classifier=ScriptedClassifier())  # no complete_json
    with pytest.raises(ValueError, match="AI provider"):
        titles.generate(archive)


def test_titles_page_flow(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", PASSWORD)
    c = TestClient(app)
    csrf = c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD}).json()[
        "csrf_token"
    ]
    x = _bank_statement(arch, "x.pdf", 1, 2021, "Kontoabrechnung 1. Quartal 2021")
    y = _bank_statement(arch, "y.pdf", 2, 2021, "Kontoabrechnung Girokonto Q2 2021")
    page = c.get("/titles").text
    assert "Dafür ist ein KI-Anbieter" in page  # test settings use the local rules
    registry.override(classifier=Harmonizer({
        "Kontoabrechnung Q1 2021": "Kontoabrechnung Girokonto Q1 2021",
    }))  # fmt: skip
    assert c.post("/titles/action", data={"action": "generate"}).status_code == 403  # CSRF
    c.post("/titles/action", data={"csrf_token": csrf, "action": "generate"})
    assert "Vorschläge werden erstellt" in c.get("/titles").text
    run_until_idle(arch)
    page = c.get("/titles").text
    assert "Kontoabrechnung Girokonto Q1 2021" in page and "1 Vorschläge" in page
    assert "Titelvorschläge" in c.get("/inbox").text and "/titles" in c.get("/inbox").text
    r = c.post("/titles/action", data={"csrf_token": csrf, "action": "accept", "doc": [x]},
               follow_redirects=False)  # fmt: skip
    assert "1+Titel+%C3%BCbernommen" in r.headers["location"]
    assert docs.load_meta(arch, x).title == "Kontoabrechnung Girokonto Q1 2021"
    assert docs.load_meta(arch, y).title == "Kontoabrechnung Girokonto Q2 2021"
    arch.close()


def test_proposals_never_drop_the_sender(archive):
    """ "Aktienbuch (Cap Table) atpar AG 2019" -> "Aktienbuch (Cap Table) 2019" is refused: the
    sender makes a title quick to understand. An alias ("TK") counts as the sender."""
    from heftig import taxonomy as tax
    from heftig.db import write_tx
    from heftig.titles import drops_sender, sender_words

    with write_tx(archive.conn):
        tid = tax.get_or_create(archive.conn, "correspondent", "Techniker Krankenkasse")
        tax.add_alias(archive.conn, tid, "TK")
    words = sender_words(archive.conn, tid)
    assert {"techniker", "krankenkasse", "tk"} <= words
    assert drops_sender("Gesundheitskarte Techniker Krankenkasse", "Gesundheitskarte", words)
    assert not drops_sender("Gesundheitskarte Techniker Krankenkasse", "Gesundheitskarte TK",
                            words)  # fmt: skip
    assert not drops_sender("Beitragsbescheid 2025", "Beitragsbescheid TK 2025", words)
    assert not drops_sender("Beitragsbescheid 2025", "Beitragsbescheid 2025", words)
    atpar = sender_words(archive.conn, tax.get_or_create(archive.conn, "correspondent", "atpar AG"))
    assert atpar == {"atpar"}
    assert drops_sender(
        "Aktienbuch (Cap Table) atpar AG 2019", "Aktienbuch (Cap Table) 2019", atpar
    )
