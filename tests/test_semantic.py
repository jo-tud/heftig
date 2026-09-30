"""Search by meaning (heftig.semantic): embedding in the worker, fusion with the word search,
settings. A fake embedding model maps words to a few "meanings"."""

import os
import zlib
from pathlib import Path

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


def test_long_texts_are_embedded_in_large_and_small_pieces(archive):
    short = ingest_bytes(archive, text_pdf(["Kurzer Brief " * 10]), "kurz.pdf")
    long = ingest_bytes(
        archive, text_pdf(["\n".join(["Lange Vertragsbedingungen gelten hier"] * 80)]), "lang.pdf"
    )
    process_all(archive)
    kinds = [k for k, _ in semantic.chunks(archive.conn, long.doc_id)]
    assert kinds[0] == semantic.DESCRIPTION
    assert 2 <= kinds.count(semantic.LARGE) < kinds.count(semantic.SMALL)
    kinds = [k for k, _ in semantic.chunks(archive.conn, short.doc_id)]
    assert kinds == [semantic.DESCRIPTION, semantic.LARGE]  # short: one piece is enough


def test_documents_are_embedded_once(archive, ids, embedder):
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
    assert semantic.chunks(archive.conn, ids["miete.pdf"])[1][1].startswith("Mietvertrag Lindenweg")


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


def test_meaning_counts_more_for_questions(archive, ids, embedder, monkeypatch):
    weights = []
    fuse = semantic.fuse
    monkeypatch.setattr(
        semantic, "fuse", lambda *a, weight=None, **kw: weights.append(weight) or fuse(*a, **kw)
    )
    _ids(archive, "Rechnung Zahnreinigung", embedder, meaning=True)
    _ids(archive, "wo ist die Rechnung für die Zahnreinigung beim Zahnarzt", embedder, meaning=True)
    assert weights == [semantic.MEANING_WEIGHT, semantic.MEANING_WEIGHT_QUESTION]
    assert semantic.MEANING_WEIGHT_QUESTION > semantic.MEANING_WEIGHT


@pytest.mark.parametrize(
    "q,question",
    [
        ("Wie beantrage ich einen Parkausweis?", True),
        ("wann bekomme ich den Bescheid für die Kur", True),
        ("What is the best way to save for retirement", True),
        ("Rechnung Telekom 2023", False),  # keywords
        ("die Rechnung vom Zahnarzt", False),  # two words with articles: still keywords
        ("Zahnarzt?", False),
        ("Kfz Versicherung Beitrag Allianz", False),
    ],
)
def test_written_questions_are_told_from_keywords(q, question):
    from heftig.search import parse_query, written_question

    assert written_question(q, parse_query(q)) is question


def test_vector_store_without_numpy(monkeypatch):
    import sys
    from array import array

    rows = [
        ("a", semantic.LARGE, *semantic._packed([1.0, 0.0])),
        ("b", semantic.DESCRIPTION, *semantic._packed([0.0, 1.0])),
        ("b", semantic.LARGE, *semantic._packed([1.0, 1.0])),
        ("b", semantic.SMALL, *semantic._packed([1.0, 0.1])),
    ]
    q = semantic._normalized([1.0, 0.2])
    fast = semantic._Store(rows).similarities(q, passages=True)
    monkeypatch.setitem(sys.modules, "numpy", None)  # import fails
    slow = semantic._Store(rows)
    assert slow.matrix is None
    for (d1, s1), (d2, s2) in zip(fast, slow.similarities(q, passages=True), strict=True):
        assert d1 == d2 and abs(s1 - s2) < 1e-5
    # int8 is close; "b": the mean of its best large piece and its best small piece
    norm = (1 + 0.04) ** 0.5
    assert abs(fast[0][1] - 1.0 / norm) < 0.01
    large, small = 1.2 / (2**0.5 * norm), 1.02 / ((1 + 0.01) ** 0.5 * norm)
    assert [d for d, _ in fast] == ["a", "b"] and abs(fast[1][1] - (large + small) / 2) < 0.01
    # keywords: the large pieces decide
    for store in (semantic._Store(rows), slow):
        assert abs(store.similarities(q)[1][1] - large) < 0.01
    assert slow.similarities(array("f", [1.0, 0.0, 0.0])) == []  # other dimensions


def test_only_documents_that_stand_out_count(archive, monkeypatch):
    """Every query is "similar" to something: with enough documents, only the ones well above
    the average similarity are taken."""

    class OneDirection(FakeEmbedder):
        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    rows = [
        (f"d{i:02}", semantic.LARGE, *semantic._packed([0.2, 1.0, 0.1 * (i % 5), 0.3]))
        for i in range(30)
    ]
    rows.append(("near", semantic.LARGE, *semantic._packed([1.0, 0.1, 0.0, 0.0])))
    store = semantic._Store(rows)
    monkeypatch.setattr(semantic, "_vectors", lambda conn, model: store)
    assert [d for d, _ in semantic.nearest(archive.conn, OneDirection(), "x")] == ["near"]


def test_worker_embeds_in_its_own_thread(tmp_path):
    from heftig.archive import Archive
    from heftig.worker import Worker

    a = Archive(
        make_settings(
            tmp_path,
            semantic_search=True,
        )
    )
    fake = FakeEmbedder()
    registry.override(
        classifier=ScriptedClassifier(default={"title": "Depotauszug"}), embedder=fake
    )
    ingest_bytes(a, text_pdf(["Depotauszug MSCI World ETF"]), "d.pdf")
    process_all(a)
    w = Worker(a)
    w._embed_new(1000.0)
    w._embed.join(timeout=30)
    st = semantic.status(a.conn, a.settings)
    assert (st["total"], st["done"], st["model"]) == (1, 1, "fake-embed-1")
    calls = fake.calls
    w._embed_new(1010.0)  # not again within 30 s
    assert w._embed is not None and fake.calls == calls
    a.close()


def test_embed_threads_do_not_leak_connections(tmp_path):
    from heftig.archive import Archive
    from heftig.worker import Worker

    a = Archive(
        make_settings(
            tmp_path,
            semantic_search=True,
        )
    )
    registry.override(embedder=FakeEmbedder())
    w = Worker(a)
    before = len(a._all)
    for i in range(5):
        w._embed_new(1000.0 + 60 * i)
        w._embed.join(timeout=30)
    assert len(a._all) == before
    a.close()


def test_re_embedding_refreshes_the_vectors_in_memory(archive, ids, embedder, monkeypatch):
    from heftig import documents as docs

    first = semantic._vectors(archive.conn, embedder.model)
    # same number of chunks, other content: the rows get the same rowids again
    docs.update_fields(archive, ids["miete.pdf"], {"title": "Depotauszug Wertpapiere"})
    assert semantic.embed_pending(archive)["embedded"] == 1
    second = semantic._vectors(archive.conn, embedder.model)
    assert second is not first
    found, _ = _ids(archive, "Wertpapiere", embedder, meaning=True)
    assert ids["miete.pdf"] in found


def test_words_only_when_asked_or_without_model(archive, ids, embedder):
    calls = embedder.calls
    _ids(archive, "Wertpapiere", embedder, meaning=False)
    _ids(archive, "Wertpapiere", None)
    assert embedder.calls == calls


def test_task_prefixes_of_the_model(tmp_path):
    from heftig.local_embed import LocalEmbedder, ModelSpec

    spec = ModelSpec("m", "r/m", "abc", {}, query_prefix="query: ", document_prefix="")
    e = LocalEmbedder(spec, tmp_path)
    assert semantic._prefix(e, "query") == "query: " and semantic._prefix(e, "document") == ""
    assert semantic._prefix(FakeEmbedder(), "query") == ""


def test_search_uses_meaning_by_itself_when_documents_are_embedded(tmp_path):
    """No button: with the search by meaning on and documents embedded, every search uses it."""
    from fastapi.testclient import TestClient

    from heftig import auth
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path, semantic_search=True))
    a = app.state.archive
    fake = FakeEmbedder()
    registry.override(
        classifier=ScriptedClassifier(by_filename={n: {"title": t} for n, (_, t) in DOCS.items()}),
        embedder=fake,
    )
    auth.create_user(a.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"})
    for n, (text, _) in DOCS.items():
        ingest_bytes(a, text_pdf([text]), n)
    process_all(a)
    assert semantic.for_search(a.conn, a.settings) is None  # nothing embedded yet
    assert "Depotauszug" not in c.get("/?q=Wertpapiere").text
    semantic.catch_up(a)
    page = c.get("/?q=Wertpapiere").text
    assert "Depotauszug" in page and "Bedeutung" in page
    assert "Depotauszug" not in c.get("/?q=Wertpapiere&meaning=0").text
    r = c.get("/api/documents", params={"q": "Wertpapiere"}).json()
    assert r["meaning"] and r["items"][0]["title"] == "Depotauszug 2024"
    app.state.archive.close()


def test_setup_assistant_asks_and_settings_can_switch_it_off(tmp_path):
    from fastapi.testclient import TestClient

    from heftig import auth
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    a = app.state.archive
    assert not a.settings.semantic_search  # without the assistant: off
    auth.create_user(a.conn, "jo", "richtig-langes-passwort")
    c = TestClient(app)
    csrf = c.post(
        "/api/auth/login", json={"username": "jo", "password": "richtig-langes-passwort"}
    ).json()["csrf_token"]
    # the step after the AI
    assert 'href="/settings/search?setup=1"' in c.get("/settings/ai?setup=1").text
    page = c.get("/settings/search?setup=1").text
    assert "Suche nach Bedeutung verwenden?" in page
    assert 'name="semantic" value="1" checked' in page  # suggested in the assistant
    r = c.post("/settings/search?setup=1", data={"csrf_token": csrf, "semantic": "1"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/settings/mail?setup=1"
    a.refresh_settings()
    assert a.settings.semantic_search
    page = c.get("/settings").text
    assert "das Modell wird geladen" in page
    assert '<span data-meaning="done">0</span>' in page  # updated in place (app.js)
    progress = c.get("/settings/search/progress").json()
    assert progress["active"] and progress["done"] == 0 and progress["total"] == 0
    c.post("/settings/search", data={"csrf_token": csrf, "semantic": "0"})
    a.refresh_settings()
    assert not a.settings.semantic_search
    assert 'name="semantic" value="0" checked' in c.get("/settings/search").text
    app.state.archive.close()


def test_local_model_download_is_checked(tmp_path, monkeypatch):
    import hashlib

    import httpx

    from heftig.local_embed import LocalEmbedder, ModelSpec

    files = {"model.onnx": b"onnx-bytes", "tokenizer.json": b"{}"}

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=files[name])

    real = httpx.stream
    monkeypatch.setattr(
        httpx, "stream",
        lambda method, url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).stream(
            method, url
        ),
    )  # fmt: skip
    good = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
    spec = ModelSpec("m", "r/m", "abc", {k: k for k in files}, sha256=good)
    e = LocalEmbedder(spec, tmp_path)
    assert not e.downloaded()
    e.download()
    assert e.downloaded() and (e.directory / "model.onnx").read_bytes() == b"onnx-bytes"
    bad = ModelSpec("m2", "r/m", "abc", {k: k for k in files}, sha256={"model.onnx": "0" * 64})
    e2 = LocalEmbedder(bad, tmp_path)
    with pytest.raises(ProviderError):
        e2.download()
    assert not e2.downloaded() and not list(e2.directory.glob(".*part"))
    assert real is not None


def test_local_model_pools_token_vectors(tmp_path, monkeypatch):
    """Mean or CLS pooling over the ONNX output, padding masked out, input order kept."""
    import numpy as np
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    from heftig.local_embed import LocalEmbedder, ModelSpec

    vocab = {"[PAD]": 0, "[UNK]": 1, "a": 2, "b": 3, "c": 4}
    tok = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    d = tmp_path / "m"
    d.mkdir()
    tok.save(str(d / "tokenizer.json"))
    (d / "model.onnx").write_bytes(b"x")

    class Session:
        def get_inputs(self):
            return [type("I", (), {"name": n})() for n in ("input_ids", "attention_mask")]

        def get_outputs(self):
            return [type("O", (), {"name": "last_hidden_state"})()]

        def run(self, _outputs, feed):
            ids = feed["input_ids"].astype(np.float32)
            return [np.stack([ids, ids * 0 + 1], axis=-1)]  # token vector (id, 1)

    import onnxruntime

    monkeypatch.setattr(onnxruntime, "InferenceSession", lambda *a, **k: Session())
    e = LocalEmbedder(ModelSpec("m", "r", "x", {"model.onnx": "", "tokenizer.json": ""}), tmp_path)
    out = e.embed(["a b c", "c"])
    assert out[0] == [3.0, 1.0] and out[1] == [4.0, 1.0]  # mean of ids 2,3,4 / only 4
    cls = LocalEmbedder(
        ModelSpec("m", "r", "x", {"model.onnx": "", "tokenizer.json": ""}, pooling="cls"), tmp_path
    )
    assert cls.embed(["b c", "a"]) == [[3.0, 1.0], [2.0, 1.0]]
    last = LocalEmbedder(
        ModelSpec("m", "r", "x", {"model.onnx": "", "tokenizer.json": ""}, pooling="last"), tmp_path
    )
    assert last.embed(["a b c", "b"]) == [[4.0, 1.0], [3.0, 1.0]]  # padding is not the last


MODEL_CACHE = Path(
    os.environ.get("HEFTIG_TEST_MODEL_DIR", Path.home() / ".cache" / "heftig-models")
)


@pytest.mark.skipif(
    not (MODEL_CACHE / "snowflake-arctic-embed-m-v2.0-int8" / "model.onnx").exists(),
    reason="the built-in model is not downloaded (scripts/search_bench.py --meaning does it)",
)
def test_built_in_model_finds_by_meaning(archive):
    """The real model on the benchmark archive (61 documents): words it cannot find."""
    from heftig.local_embed import DEFAULT, LocalEmbedder

    from . import search_bench

    ids = search_bench.load(archive)
    model = LocalEmbedder(DEFAULT, MODEL_CACHE, threads=2)
    registry.override(embedder=model)
    assert semantic.catch_up(archive)["embedded"] == len(ids)
    for q, doc in (("Wertpapiere", "depot_2024.pdf"), ("Elektriker", "elektriker.pdf")):
        found, res = _ids(archive, q, model)
        assert found and found[0] == ids[doc] and "Meaning" in res.items[0]["reasons"], q
    assert semantic.nearest(archive.conn, model, "Rezept Apfelkuchen") == []


def test_model_is_unloaded_when_idle(tmp_path, monkeypatch):
    import time

    import numpy as np
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    from heftig.local_embed import LocalEmbedder, ModelSpec

    tok = Tokenizer(WordLevel({"[PAD]": 0, "[UNK]": 1, "a": 2}, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    d = tmp_path / "m"
    d.mkdir()
    tok.save(str(d / "tokenizer.json"))
    (d / "model.onnx").write_bytes(b"x")
    loads = []

    class Session:
        def get_inputs(self):
            return [type("I", (), {"name": n})() for n in ("input_ids", "attention_mask")]

        def get_outputs(self):
            return [type("O", (), {"name": "sentence_embedding"})()]

        def run(self, _outputs, feed):
            return [np.ones((feed["input_ids"].shape[0], 2), dtype=np.float32)]

    import onnxruntime

    monkeypatch.setattr(
        onnxruntime, "InferenceSession", lambda *a, **k: loads.append(1) or Session()
    )
    e = LocalEmbedder(ModelSpec("m", "r", "x", {"model.onnx": "", "tokenizer.json": ""}), tmp_path)
    e.idle_seconds = 0.3
    e.embed(["a"])
    assert e.loaded()
    e.embed(["a"])  # used again: stays loaded, not loaded twice
    assert loads == [1]
    time.sleep(0.8)
    assert not e.loaded()  # idle: unloaded
    e.embed(["a"])  # and loaded again when needed
    assert e.loaded() and loads == [1, 1]
    e.unload()


@pytest.mark.skipif(
    not (MODEL_CACHE / "snowflake-arctic-embed-m-v2.0-int8" / "model.onnx").exists(),
    reason="the built-in model is not downloaded",
)
def test_unloading_gives_memory_back():
    import time

    from heftig.local_embed import DEFAULT, LocalEmbedder

    def rss():
        return int(open("/proc/self/status").read().split("VmRSS:")[1].split()[0]) // 1024

    e = LocalEmbedder(DEFAULT, MODEL_CACHE, threads=2)
    e.idle_seconds = 1
    before = rss()
    e.embed(["Rechnung Stromlieferung " * 40] * 8)
    loaded = rss()
    time.sleep(2)
    assert not e.loaded()
    assert rss() < before + (loaded - before) * 0.3  # most of it is returned
