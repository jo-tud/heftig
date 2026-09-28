"""AI search: the model turns a request into filters of the ordinary search."""

import logging
from datetime import date

import pytest
from fastapi.testclient import TestClient

from heftig import aisearch, auth
from heftig import taxonomy as tax
from heftig.db import write_tx
from heftig.providers import registry
from heftig.providers.base import ProviderError
from heftig.providers.pricing import UsageMeter

from .conftest import make_settings
from .corpus import load_corpus

PASSWORD = "richtig-langes-passwort"


class FakePlanner:
    name, model, target, adapter_version = "anthropic", "claude-haiku-4-5", "api", "t"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []
        self.usage = UsageMeter()

    def complete_json(self, system, user, schema, max_tokens=8000, prefix=""):
        self.calls.append((system, user, prefix))
        self.usage.add(self.model, 300, 80, 0, 4000)
        if self.error:
            raise self.error
        return self.answer


ANSWER = {
    "text": "", "correspondent": ["Telekom Deutschland GmbH", "Vodafone GmbH", "Erfundene AG"],
    "document_type": ["Rechnung"], "tags": [], "tag_mode": "all", "date_from": "2026-01-01",
    "date_to": "2026-12-31", "amount_key": "betrag", "amount_min": "30", "amount_max": "",
    "sort": "document_date", "explanation": "Handyrechnungen 2026 ab 30 €.",
}  # fmt: skip


def test_plan_is_checked_against_the_archive(archive):
    load_corpus(archive)
    with write_tx(archive.conn):
        tax.add_alias(
            archive.conn, tax.find_term(archive.conn, "correspondent", "Vodafone GmbH"), "VF"
        )
    fake = FakePlanner(
        {**ANSWER, "correspondent": ["Telekom Deutschland GmbH", "VF", "Erfundene AG"]}
    )
    registry.override(search_planner=fake)
    plan = aisearch.plan(
        archive, "Handyrechnungen dieses Jahr über 30 Euro", today=date(2026, 9, 28)
    )
    items = dict.fromkeys(plan.items)
    assert ("correspondent", "Telekom Deutschland GmbH") in items
    assert ("correspondent", "Vodafone GmbH") in items  # alias resolved
    assert plan.dropped == ["Erfundene AG"]
    assert ("cf_key", "Betrag") in items and (
        "cf_min",
        "30",
    ) in items  # key matched, case-insensitive
    assert ("date_from", "2026-01-01") in items and ("sort", "document_date") in items
    # the model saw categories (cached part) and the request, never document text
    system, user, prefix = fake.calls[0]
    assert "Vodafone GmbH" in prefix and "Betrag" in prefix and "Monday" in user
    assert "Rechnungsbetrag" not in prefix + user
    assert "one short German sentence" in system  # the installation language (tests: de)
    # its cost appears in the AI cost overview
    row = archive.conn.execute(
        "SELECT task, cost_usd FROM processing_runs WHERE task='search'").fetchone()  # fmt: skip
    assert row and row[1] > 0


def test_malformed_values_are_dropped(archive):
    load_corpus(archive)
    registry.override(search_planner=FakePlanner({**ANSWER, "date_from": "letztes Jahr",
                      "date_to": "2026-02-30", "amount_key": "Unbekannt", "tags": ["Nein"]}))  # fmt: skip
    plan = aisearch.plan(archive, "x")
    keys = {k for k, _ in plan.items}
    assert not keys & {"date_from", "date_to", "cf_key", "tag"}


@pytest.fixture
def web(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    arch = app.state.archive
    auth.create_user(arch.conn, "jo", PASSWORD)
    load_corpus(arch)
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD})
    yield c
    arch.close()


def test_ai_search_page_flow(web):
    c = web
    registry.override(search_planner=None)
    assert "✦ KI" not in c.get("/").text  # no AI configured: no button
    registry.override(search_planner=FakePlanner(ANSWER))
    assert "✦ KI" in c.get("/").text
    r = c.get("/ai-search", params={"q": "Handyrechnungen über 30 €"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "correspondent=Telekom" in loc and "ai=Handyrechnungen" in loc
    page = c.get(loc).text
    assert (
        "KI-Suche „Handyrechnungen über 30 €“" in page and "nicht im Archiv: Erfundene AG" in page
    )
    assert "ohne KI suchen" in page
    # the banner belongs to this result only: filter links do not carry it along
    assert "ai=" not in page.split("ohne KI suchen", 1)[1]
    registry.override(search_planner=FakePlanner(error=ProviderError("anthropic: HTTP 529")))
    r = c.get("/ai-search", params={"q": "Strom"}, follow_redirects=False)
    assert (
        "KI-Suche+nicht+m%C3%B6glich" in r.headers["location"]
        and "q=Strom" in r.headers["location"]
    )
