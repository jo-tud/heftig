"""Search by meaning (optional): documents and the query as vectors of an embedding model.

Finds documents that describe what was searched for in other words - "Wertpapiere" finds the
ETF statement, "Elektriker" the bill from "Elektro Schulz" - which no word rule can. Off by
default; switched on in the settings with an OpenAI-compatible embedding model: OpenAI's
``text-embedding-3-small`` (texts go to OpenAI, needs its own permission) or a local server such
as Ollama with ``bge-m3`` or ``embeddinggemma`` (nothing leaves the machine).

- The worker embeds every document in pieces (title, sender, type, tags and date, then the text
  in chunks of about 1,200 characters, at most 12 per document) and stores the vectors in
  SQLite (``doc_embeddings``, derived data like the word index). A document is embedded again
  when its text or description changes.
- The normal search never uses it. Only when the user asks for it (``meaning=1``, "Search by
  meaning"), the query is embedded - one call to the model - and compared with all stored
  vectors (cosine, in memory: no database extension; numpy if installed, else pure Python). The documents most alike by
  meaning are merged with the word search's ranking by Reciprocal Rank Fusion (Cormack et al.
  2009), so a document found both ways comes first and one found only by meaning is added.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import operator
import sqlite3
import threading
from array import array
from typing import Any

from .db import now_iso, write_tx

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200
MAX_CHUNKS = 12
DOCS_PER_ROUND = 32
NEAREST = 20  # documents by meaning merged into the results
RRF_K = 60
MIN_Z = 2.0  # a document counts as near when it is this many standard deviations above average
MIN_DOCS_FOR_CUTOFF = 10
MEANING_WEIGHT = 0.5  # the word search counts double: measured in docs/search.md

# Task prefixes some models need to embed a query differently from a document.
_PREFIXES = (
    ("e5", "query: ", "passage: "),
    ("nomic-embed", "search_query: ", "search_document: "),
    ("embeddinggemma", "task: search result | query: ", "title: none | text: "),
    ("qwen3-embedding", "Instruct: Find the document that answers the search\nQuery: ", ""),
)


def prefix(model: str, kind: str) -> str:
    m = model.lower()
    for key, query, document in _PREFIXES:
        if key in m:
            return query if kind == "query" else document
    return ""


def available(settings) -> bool:
    """Switched on and allowed (the worker embeds, the search offers it)."""
    return settings.embed_provider != "none" and not settings.embed_blocked_reason()


def model_name(settings) -> str:
    if settings.embed_model:
        return settings.embed_model
    return "text-embedding-3-small" if settings.embed_provider == "openai" else ""


def status(conn: sqlite3.Connection, settings) -> dict[str, Any]:
    """How many documents are embedded with the configured model."""
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM doc_embed_state s JOIN documents d ON d.id = s.doc_id "
        "WHERE s.model = ?",
        (model_name(settings),),
    ).fetchone()[0]
    return {"total": total, "done": done, "model": model_name(settings)}


# --- what is embedded ------------------------------------------------------------------------


def chunks(conn: sqlite3.Connection, doc_id: str) -> list[str]:
    """The pieces of a document that are embedded: its description, then its text."""
    row = conn.execute(
        "SELECT d.metadata_json, t.content FROM documents d "
        "LEFT JOIN document_text t ON t.doc_id = d.id WHERE d.id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        return []
    meta = json.loads(row[0])
    head = " · ".join(
        str(x)
        for x in (
            meta.get("title"),
            meta.get("correspondent"),
            meta.get("document_type"),
            ", ".join(meta.get("tags") or []),
            meta.get("document_date"),
        )
        if x
    )
    out = [head + ("\n" + meta["summary"] if meta.get("summary") else "")]
    text = " ".join((row[1] or "").split())
    while text and len(out) <= MAX_CHUNKS:
        if len(text) <= CHUNK_CHARS:
            piece, text = text, ""
        else:
            cut = text.rfind(" ", CHUNK_CHARS // 2, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            piece, text = text[:cut], text[cut:].lstrip()
        out.append(f"{head}\n{piece}" if head else piece)
    return [c for c in out if c.strip()]


def _normalized(v: list[float]) -> array:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", (x / norm for x in v))


# --- the worker: embed what is new or changed ----------------------------------------------


def pending(conn: sqlite3.Connection, model: str, limit: int) -> list[tuple[str, int]]:
    """Documents (id, revision) not embedded with `model` since their last change."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT d.id, d.revision FROM documents d "
            "LEFT JOIN doc_embed_state s ON s.doc_id = d.id "
            "WHERE d.status IN ('done', 'needs_review') AND (s.doc_id IS NULL OR s.model != ? "
            "OR s.revision != d.revision) ORDER BY d.ingest_sequence DESC LIMIT ?",
            (model, limit),
        )
    ]


def embed_pending(archive, max_docs: int = DOCS_PER_ROUND, stop=None) -> dict[str, Any]:
    """Embed up to `max_docs` new or changed documents. The cost goes into the AI cost
    overview (task "embed"). Raises ProviderError/ProviderUnavailable when the model fails."""
    from .providers import registry

    conn = archive.conn
    embedder = registry.get_embedder(archive.settings)
    if embedder is None:
        return {"embedded": 0, "unchanged": 0}
    model = embedder.model
    with write_tx(conn):  # documents that are gone
        conn.execute("DELETE FROM doc_embeddings WHERE doc_id NOT IN (SELECT id FROM documents)")
        conn.execute("DELETE FROM doc_embed_state WHERE doc_id NOT IN (SELECT id FROM documents)")
    embedded = unchanged = 0
    for doc_id, revision in pending(conn, model, max_docs):
        if stop is not None and stop.is_set():
            break
        pieces = chunks(conn, doc_id)
        digest = hashlib.sha256(json.dumps([model, pieces]).encode()).hexdigest()
        state = conn.execute(
            "SELECT content_hash FROM doc_embed_state WHERE doc_id = ? AND model = ?",
            (doc_id, model),
        ).fetchone()
        if state and state[0] == digest:  # a change that does not touch the text (filing ...)
            with write_tx(conn):
                conn.execute(
                    "UPDATE doc_embed_state SET revision = ? WHERE doc_id = ?",
                    (revision, doc_id),
                )
            unchanged += 1
            continue
        started = now_iso()
        before = embedder.usage.snapshot() if hasattr(embedder, "usage") else None
        vectors = embedder.embed([prefix(model, "document") + p for p in pieces])
        with write_tx(conn):
            conn.execute("DELETE FROM doc_embeddings WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT INTO doc_embeddings(doc_id, chunk, model, vector) VALUES(?, ?, ?, ?)",
                [(doc_id, i, model, _normalized(v).tobytes()) for i, v in enumerate(vectors)],
            )
            conn.execute(
                "INSERT INTO doc_embed_state(doc_id, model, content_hash, revision) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET model = excluded.model, "
                "content_hash = excluded.content_hash, "
                "revision = excluded.revision",
                (doc_id, model, digest, revision),
            )
            _record(conn, embedder, "embed", doc_id, started, before)
        embedded += 1
    return {"embedded": embedded, "unchanged": unchanged}


def _record(conn, embedder, task: str, doc_id: str, started: str, before) -> None:
    """Tokens and cost of the call in the AI cost overview (inside the caller's transaction)."""
    meter = getattr(embedder, "usage", None)
    if meter is None or before is None:
        return
    tin, tout, cost = (a - b for a, b in zip(meter.snapshot(), before, strict=True))
    if not getattr(meter, "priced", True):
        cost = None
    conn.execute(
        "INSERT INTO processing_runs(doc_id, task, provider, model, target, adapter_version, "
        "prompt_version, status, started_at, finished_at, input_tokens, output_tokens, "
        "cost_usd) VALUES(?, ?, ?, ?, ?, ?, '', 'ok', ?, ?, ?, ?, ?)",
        (doc_id, task, getattr(embedder, "name", ""), getattr(embedder, "model", ""),
         getattr(embedder, "target", ""), getattr(embedder, "adapter_version", ""),
         started, now_iso(), tin, tout, cost),
    )  # fmt: skip


# --- the search ---------------------------------------------------------------------------


class _Store:
    """All stored vectors of one model: a numpy matrix if numpy is installed (heftig[semantic],
    the container image), else plain float arrays (about 1 s per 30,000 chunks)."""

    def __init__(self, rows: list[tuple[str, array]]):
        self.ids = [d for d, _ in rows]
        self.matrix = None
        self.rows = rows
        dims = {len(v) for _, v in rows}
        try:
            import numpy as np
        except ImportError:
            return
        if len(dims) == 1:
            self.matrix = np.frombuffer(b"".join(v.tobytes() for _, v in rows), dtype=np.float32)
            self.matrix = self.matrix.reshape(len(rows), dims.pop())
            self.rows = []

    def similarities(self, q: array) -> list[tuple[str, float]]:
        if self.matrix is not None:
            import numpy as np

            if self.matrix.shape[1] != len(q):
                return []
            sims = self.matrix @ np.frombuffer(q.tobytes(), dtype=np.float32)
            return list(zip(self.ids, sims.tolist(), strict=True))
        return [
            (doc_id, sum(map(operator.mul, q, v))) for doc_id, v in self.rows if len(v) == len(q)
        ]


_VECTORS: dict[tuple, _Store] = {}
_LOCK = threading.Lock()


def _vectors(conn: sqlite3.Connection, model: str) -> _Store:
    """All stored vectors of `model`, in memory until they change."""
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    count, last = conn.execute(
        "SELECT COUNT(*), MAX(rowid) FROM doc_embeddings WHERE model = ?", (model,)
    ).fetchone()
    key = (db, model, count, last)
    with _LOCK:
        if key in _VECTORS:
            return _VECTORS[key]
    rows = []
    for doc_id, blob in conn.execute(
        "SELECT doc_id, vector FROM doc_embeddings WHERE model = ?", (model,)
    ):
        v = array("f")
        v.frombytes(blob)
        rows.append((doc_id, v))
    store = _Store(rows)
    with _LOCK:
        for k in [k for k in _VECTORS if k[0] == db]:
            del _VECTORS[k]  # one generation per archive
        _VECTORS[key] = store
    return store


def nearest(
    conn: sqlite3.Connection, embedder, query: str, limit: int = NEAREST
) -> list[tuple[str, float]]:
    """The documents most alike `query` by meaning: (id, cosine of the best chunk)."""
    model = embedder.model
    store = _vectors(conn, model)
    if not store.ids or not query.strip():
        return []
    started = now_iso()
    before = embedder.usage.snapshot() if hasattr(embedder, "usage") else None
    q = _normalized(embedder.embed([prefix(model, "query") + query])[0])
    with write_tx(conn):
        _record(conn, embedder, "search", "", started, before)
    best: dict[str, float] = {}
    for doc_id, sim in store.similarities(q):
        if sim > best.get(doc_id, -2.0):
            best[doc_id] = sim
    ranked = sorted(best.items(), key=lambda x: -x[1])
    if len(ranked) >= MIN_DOCS_FOR_CUTOFF:
        # only documents that stand out from the rest: every query is "similar" to something,
        # and how similar unrelated texts are differs from model to model
        sims = [x[1] for x in ranked]
        mean = sum(sims) / len(sims)
        std = math.sqrt(sum((x - mean) ** 2 for x in sims) / len(sims)) or 1.0
        # the largest of n random values lies about sqrt(2 ln n) deviations above average
        min_z = max(MIN_Z, math.sqrt(2 * math.log(len(sims))) - 0.8)
        ranked = [(d, sim) for d, sim in ranked if (sim - mean) / std >= min_z]
    return ranked[:limit]


def fuse(
    lexical: list[str], meaning: list[str], k: int = RRF_K, weight: float | None = None
) -> list[tuple[str, float]]:
    """Weighted Reciprocal Rank Fusion of two rankings: sum of weight / (k + rank). The word
    search counts fully, the meaning with MEANING_WEIGHT; ties keep the word search's order."""
    score: dict[str, float] = {}
    w = MEANING_WEIGHT if weight is None else weight
    for ranking, factor in ((lexical, 1.0), (meaning, w)):
        for rank, doc_id in enumerate(ranking, 1):
            score[doc_id] = score.get(doc_id, 0.0) + factor / (k + rank)
    order = {d: i for i, d in enumerate([*lexical, *meaning])}
    return sorted(score.items(), key=lambda x: (-x[1], order[x[0]]))


ERROR_KEY = "semantic_error"  # meta: the last failure of the embedding model, shown in settings


def catch_up(archive, stop=None, rounds: int = 1000) -> dict[str, Any]:
    """Embed everything that is pending, round by round (worker thread, `heftig embed`). A
    failing model ends the run; its message is kept for the settings page."""
    from .db import set_meta
    from .providers.base import ProviderError, ProviderUnavailable

    total = {"embedded": 0, "unchanged": 0}
    try:
        for _ in range(rounds):
            r = embed_pending(archive, stop=stop)
            total["embedded"] += r["embedded"]
            total["unchanged"] += r["unchanged"]
            if not r["embedded"] and not r["unchanged"]:
                break
            if stop is not None and stop.is_set():
                break
    except (ProviderError, ProviderUnavailable) as e:
        with write_tx(archive.conn):
            set_meta(archive.conn, ERROR_KEY, str(e)[:300])
        total["error"] = str(e)
        return total
    from .db import get_meta

    if get_meta(archive.conn, ERROR_KEY):
        with write_tx(archive.conn):
            set_meta(archive.conn, ERROR_KEY, "")
    return total
