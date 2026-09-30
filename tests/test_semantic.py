"""Search by meaning (heftig.semantic): embedding in the worker, fusion with the word search,
settings. A fake embedding model maps words to a few "meanings"."""

import json
import math
import zlib

import httpx
import pytest

from heftig import semantic
from heftig.providers import registry
from heftig.providers.base import ProviderError
from heftig.search import SearchParams, search
from heftig.textnorm import tokens

from .conftest import ScriptedClassifier, ingest_bytes, make_settings, process_all
from .helpers import text_pdf

CONCEPTS = {
    "money": ("wertpapiere", "etf", "depot", "fonds", "aktien", "msci"),
    "electric": ("elektriker", "elektro", "sicherungskasten", "strom", "elektroarbeiten"),
    "health": ("arzt", "praxis", "zahnreinigung", "zahnarzt"),
}


class FakeEmbedder:
    name = "fake-embed"
    model = "fake-embed-1"
    target = "local"
    adapter_version = "fake"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise ProviderError("fake-embed: connection error", transient=True)
        out = []
        for t in texts:
            v = [0.0] * 16
            for tok in tokens(t):
                for i, words in enumerate(CONCEPTS.values()):
                    if any(tok.startswith(w) for w in words):
                        v[i] += 3.0
                v[3 + zlib.crc32(tok.encode()) % 13] += 0.2
            out.append(v)
        return out


DOCS = {
    "depot.pdf": ("Beispiel Direktbank\nDepotauszug\nMSCI World ETF 142 Stück", "Depotauszug 2024"),
    "elektro.pdf": (
        "Elektro Schulz GmbH\nRechnung\nAustausch Sicherungskasten",
        "Rechnung Elektroarbeiten",
    ),
    "zahn.pdf": ("Zahnarztpraxis Dr. Weber\nRechnung\nZahnreinigung", "Rechnung Zahnreinigung"),
    "miete.pdf": ("Hausverwaltung\nMietvertrag\nKaltmiete 780 EUR", "Mietvertrag"),
}


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def ids(archive, embedder):
    registry.override(
        classifier=ScriptedClassifier(by_filename={n: {"title": t} for n, (_, t) in DOCS.items()}),
        embedder=embedder,
    )
    out = {n: ingest_bytes(archive, text_pdf([text]), n).doc_id for n, (text, _) in DOCS.items()}
    process_all(archive)
    semantic.catch_up(archive)
    return out


def _ids(archive, q, embedder=None, **kw):
    res = search(archive.conn, SearchParams(q=q, per_page=50, **kw), embedder=embedder)
    return [i["id"] for i in res.items], res


def test_documents_are_embedded_once(archive, ids, embedder):
    assert semantic.status(archive.conn, archive.settings)["done"] == 0  # settings: model ""
    n = archive.conn.execute("SELECT COUNT(DISTINCT doc_id) FROM doc_embeddings").fetchone()[0]
    assert n == len(DOCS)
    calls = embedder.calls
    assert semantic.embed_pending(archive) == {"embedded": 0, "unchanged": 0}
    assert embedder.calls == calls
    # a change to the description embeds that document again, and only it
    from heftig import documents as docs

    docs.update_fields(archive, ids["miete.pdf"], {"title": "Mietvertrag Lindenweg"})
    assert semantic.embed_pending(archive)["embedded"] == 1
    # every chunk starts with the description
    assert semantic.chunks(archive.conn, ids["miete.pdf"])[1].startswith("Mietvertrag Lindenweg")


def test_meaning_finds_other_words(archive, ids, embedder):
    found, res = _ids(archive, "Wertpapiere")
    assert found == []  # no word matches
    found, res = _ids(archive, "Wertpapiere", embedder, meaning=True)
    assert found[0] == ids["depot.pdf"] and res.meaning
    assert "Meaning" in res.items[0]["reasons"]
    found, _ = _ids(archive, "Elektriker", embedder, meaning=True)
    assert found[0] == ids["elektro.pdf"]


def test_word_matches_stay_first(archive, ids, embedder):
    found, _ = _ids(archive, "Rechnung Zahnreinigung", embedder, meaning=True)
    assert found[0] == ids["zahn.pdf"]  # found by words and by meaning


def test_normal_search_never_calls_the_model(archive, ids, embedder):
    calls = embedder.calls
    _ids(archive, "Wertpapiere", embedder)  # meaning not asked for
    _ids(archive, "Wertpapiere", None, meaning=True)  # asked for, but no model given
    assert embedder.calls == calls


def test_filters_apply_to_meaning(archive, ids, embedder):
    found, _ = _ids(archive, "Wertpapiere", embedder, meaning=True, date_from="2030")
    assert found == []


def test_failing_model_keeps_word_results(archive, ids):
    found, res = _ids(archive, "Mietvertrag", FakeEmbedder(fail=True), meaning=True)
    assert found == [ids["miete.pdf"]]
    assert any("meaning failed" in e for e in res.errors)


def test_fuse_rewards_both_lists():
    fused = [d for d, _ in semantic.fuse(["a", "b", "c"], ["c", "x"])]
    assert fused[0] == "c" and set(fused) == {"a", "b", "c", "x"}


def test_prefixes_for_models():
    assert semantic.prefix("multilingual-e5-small", "query") == "query: "
    assert semantic.prefix("nomic-embed-text-v2-moe", "document") == "search_document: "
    assert semantic.prefix("bge-m3", "query") == ""


def test_openai_embedder_request(monkeypatch):
    from heftig.providers.openai_compat import OpenAICompatEmbedder

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        n = len(seen["body"]["input"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0, 0.0, float(i)]} for i in reversed(range(n))
                ],
                "usage": {"prompt_tokens": 1000},
            },
        )

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)})
    )
    e = OpenAICompatEmbedder(
        "openai", "https://api.openai.com/v1", "sk-x", "text-embedding-3-small", 5
    )
    vectors = e.embed(["a", "b"])
    assert seen["url"] == "https://api.openai.com/v1/embeddings"
    assert seen["body"]["dimensions"] == 512
    assert vectors == [[1.0, 0.0, 0.0], [1.0, 0.0, 1.0]]  # in input order
    assert math.isclose(e.usage.snapshot()[2], 1000 / 1e6 * 0.02)


def test_meaning_settings(tmp_path):
    from heftig.web.ui import meaning_changes

    s = make_settings(tmp_path)
    changes, err = meaning_changes({"meaning_mode": "openai"}, s)
    assert err and "API" in err  # no key anywhere
    changes, err = meaning_changes({"meaning_mode": "openai", "meaning_api_key": "sk-1"}, s)
    assert err  # cloud without consent
    changes, err = meaning_changes(
        {"meaning_mode": "openai", "meaning_api_key": "sk-1", "meaning_consent": "1"}, s
    )
    assert (
        not err
        and changes["allow_cloud_embed"]
        and changes["embed_model"] == "text-embedding-3-small"
    )
    changes, err = meaning_changes(
        {
            "meaning_mode": "local",
            "meaning_base_url": "http://localhost:11434/v1",
            "meaning_model": "bge-m3",
        },
        s,
    )
    assert not err and not changes["allow_cloud_embed"]
    assert meaning_changes({"meaning_mode": "off"}, s)[0]["embed_provider"] == "none"


def test_settings_page_and_search_link(tmp_path):
    from fastapi.testclient import TestClient

    from heftig import auth
    from heftig.web.app import create_app

    app = create_app(
        make_settings(
            tmp_path,
            embed_provider="openai_compatible",
            embed_base_url="http://localhost:11434/v1",
            embed_model="fake-embed-1",
        )
    )
    registry.override(embedder=FakeEmbedder())
    auth.create_user(app.state.archive.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"})
    page = c.get("/settings").text
    assert "Suche nach Bedeutung" in page and "0 von 0 Dokumenten" in page
    r = c.get("/?q=Wertpapiere")
    assert "q=Wertpapiere&amp;meaning=1" in r.text and "Auch nach Bedeutung suchen" in r.text
    r = c.get("/?q=Wertpapiere&meaning=1")
    assert "Auch nach Bedeutung gesucht" in r.text
    app.state.archive.close()


def test_vector_store_without_numpy(monkeypatch):
    import sys
    from array import array

    rows = [("a", semantic._normalized([1.0, 0.0])), ("b", semantic._normalized([1.0, 1.0]))]
    q = semantic._normalized([1.0, 0.2])
    fast = semantic._Store(rows).similarities(q)
    monkeypatch.setitem(sys.modules, "numpy", None)  # import fails
    slow = semantic._Store(rows)
    assert slow.matrix is None
    for (d1, s1), (d2, s2) in zip(fast, slow.similarities(q), strict=True):
        assert d1 == d2 and abs(s1 - s2) < 1e-6
    assert slow.similarities(array("f", [1.0, 0.0, 0.0])) == []  # other dimensions


def test_only_documents_that_stand_out_count(archive, monkeypatch):
    """Every query is "similar" to something: with enough documents, only the ones well above
    the average similarity are taken."""

    class OneDirection(FakeEmbedder):
        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    rows = [(f"d{i}", semantic._normalized([0.2, 1.0, 0.1 * (i % 5), 0.3])) for i in range(30)]
    rows.append(("near", semantic._normalized([1.0, 0.1, 0.0, 0.0])))
    store = semantic._Store(rows)
    monkeypatch.setattr(semantic, "_vectors", lambda conn, model: store)
    assert [d for d, _ in semantic.nearest(archive.conn, OneDirection(), "x")] == ["near"]
