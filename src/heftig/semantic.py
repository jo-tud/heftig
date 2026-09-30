"""Search by meaning: documents and the query as vectors of the built-in embedding model.

Finds documents that describe what was searched for in other words - "Wertpapiere" finds the
ETF statement, "Elektriker" the bill from "Elektro Schulz" - which no word rule can. The model
runs on this computer (local_embed.py); nothing is sent anywhere. Off unless switched on (the
setup assistant asks; Settings -> Search).

- The worker embeds every archived document in the background (worker.py): pieces with title,
  sender, type, tags and date, then the text in chunks of about 1,200 characters (at most 12),
  stored in SQLite (``doc_embeddings``, derived data like the word index). The model is
  downloaded when documents are embedded for the first time. A document is embedded again when
  the embedded pieces change.
- Every search with words also compares the query with the stored vectors, once the model is
  there and documents are embedded (cosine, in memory with numpy). Documents that stand out by
  meaning are merged with the word search's ranking by weighted Reciprocal Rank Fusion
  (Cormack et al. 2009): a document found both ways comes first, one found only by meaning is
  added, documents not embedded yet are still found by their words.
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
GENERATION_KEY = "embeddings_generation"  # meta: counts changes of doc_embeddings (cache key)
MIN_Z = 2.0  # a document counts as near when it is this many standard deviations above average
MIN_DOCS_FOR_CUTOFF = 10
MAX_BELOW_BEST = 0.15  # cosine: documents much less alike than the best one are left out
LOW_FACTOR = 0.7  # a document that stands out needs at least this share of min_similarity
MEANING_WEIGHT = 0.5  # keywords: the word search counts double (measured, docs/search.md)
# written-out questions (search.written_question): meaning counts more, and a small k gives the
# first places of both rankings more weight (measured on real questions, docs/search.md)
MEANING_WEIGHT_QUESTION = 2.0
RRF_K_QUESTION = 5


def _prefix(embedder, kind: str) -> str:
    """Task prefix of the model ("query: " / "passage: " for e5)."""
    spec = getattr(embedder, "spec", None)
    if spec is None:
        return ""
    return spec.query_prefix if kind == "query" else spec.document_prefix


def available(settings) -> bool:
    """Switched on: the worker embeds, the search uses what is embedded."""
    return bool(settings.semantic_search)


def embedder_for(settings):
    from .providers import registry

    return registry.get_embedder(settings)


def model_name(settings) -> str:
    e = embedder_for(settings) if available(settings) else None
    if e is not None:
        return e.model
    from .local_embed import DEFAULT

    return DEFAULT.name


def status(conn: sqlite3.Connection, settings) -> dict[str, Any]:
    """How many documents are embedded with the model, and whether it is downloaded."""
    model = model_name(settings)
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM doc_embed_state s JOIN documents d ON d.id = s.doc_id "
        "WHERE s.model = ?",
        (model,),
    ).fetchone()[0]
    e = embedder_for(settings) if available(settings) else None
    downloaded = bool(e is not None and getattr(e, "downloaded", lambda: True)())
    return {"total": total, "done": done, "model": model, "downloaded": downloaded}


def for_search(conn: sqlite3.Connection, settings):
    """The embedder for a search, if meaning can be used right now: switched on, the model
    downloaded, documents embedded. Never downloads anything."""
    if not available(settings):
        return None
    e = embedder_for(settings)
    if e is None or not getattr(e, "downloaded", lambda: True)():
        return None
    row = conn.execute("SELECT 1 FROM doc_embeddings WHERE model = ? LIMIT 1", (e.model,))
    return e if row.fetchone() else None


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


def _packed(v: list[float]) -> tuple[bytes, float]:
    """A vector normalised and stored as 8-bit integers with a scale: a quarter of the size of
    32-bit floats and the same ranking (measured, docs/search.md)."""
    n = _normalized(v)
    scale = (max(map(abs, n), default=0.0) or 1.0) / 127
    return array("b", (round(x / scale) for x in n)).tobytes(), scale


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


def _changed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET "
        "value = CAST(value AS INTEGER) + 1",
        (GENERATION_KEY,),
    )


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
        gone = conn.execute(
            "DELETE FROM doc_embeddings WHERE doc_id NOT IN (SELECT id FROM documents)"
        ).rowcount
        conn.execute("DELETE FROM doc_embed_state WHERE doc_id NOT IN (SELECT id FROM documents)")
        if gone:
            _changed(conn)
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
        vectors = embedder.embed([_prefix(embedder, "document") + p for p in pieces])
        with write_tx(conn):
            conn.execute("DELETE FROM doc_embeddings WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT INTO doc_embeddings(doc_id, chunk, model, vector, scale) "
                "VALUES(?, ?, ?, ?, ?)",
                [(doc_id, i, model, *_packed(v)) for i, v in enumerate(vectors)],
            )
            conn.execute(
                "INSERT INTO doc_embed_state(doc_id, model, content_hash, revision) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET model = excluded.model, "
                "content_hash = excluded.content_hash, "
                "revision = excluded.revision",
                (doc_id, model, digest, revision),
            )
            _record(conn, embedder, "embed", doc_id, started, before)
            _changed(conn)
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
    """All stored vectors of one model (8-bit integers and a scale per vector): a numpy matrix
    if numpy is installed (heftig[semantic], the container image), else plain arrays (about 1 s
    per 30,000 chunks)."""

    BLOCK = 8192  # rows converted to floats at a time: little memory, same speed

    def __init__(self, rows: list[tuple[str, bytes, float]]):
        self.ids = [d for d, _, _ in rows]
        self.matrix = None
        self.rows = [(d, array("b", v), s) for d, v, s in rows]
        dims = {len(v) for _, v, _ in rows}
        try:
            import numpy as np
        except ImportError:
            return
        if len(dims) == 1:
            self.matrix = np.frombuffer(b"".join(v for _, v, _ in rows), dtype=np.int8)
            self.matrix = self.matrix.reshape(len(rows), dims.pop())
            self.scales = np.array([s for _, _, s in rows], dtype=np.float32)
            self.rows = []

    def similarities(self, q: array) -> list[tuple[str, float]]:
        if self.matrix is not None:
            import numpy as np

            if self.matrix.shape[1] != len(q):
                return []
            qv = np.frombuffer(q.tobytes(), dtype=np.float32)
            sims = np.concatenate(
                [
                    self.matrix[i : i + self.BLOCK].astype(np.float32) @ qv
                    for i in range(0, len(self.ids), self.BLOCK)
                ]
            )
            return list(zip(self.ids, (sims * self.scales).tolist(), strict=True))
        return [
            (doc_id, sum(map(operator.mul, q, v)) * s)
            for doc_id, v, s in self.rows
            if len(v) == len(q)
        ]


_VECTORS: dict[tuple, _Store] = {}
_LOCK = threading.Lock()


def _vectors(conn: sqlite3.Connection, model: str) -> _Store:
    """All stored vectors of `model`, in memory until they change."""
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (GENERATION_KEY,)).fetchone()
    key = (db, model, row[0] if row else "0")
    with _LOCK:
        if key in _VECTORS:
            return _VECTORS[key]
    rows = conn.execute(
        "SELECT doc_id, vector, scale FROM doc_embeddings WHERE model = ?", (model,)
    ).fetchall()
    store = _Store([tuple(r) for r in rows])
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
    q = _normalized(embedder.embed([_prefix(embedder, "query") + query])[0])
    with write_tx(conn):
        _record(conn, embedder, "search", "", started, before)
    best: dict[str, float] = {}
    for doc_id, sim in store.similarities(q):
        if sim > best.get(doc_id, -2.0):
            best[doc_id] = sim
    ranked = sorted(best.items(), key=lambda x: -x[1])
    return _near_enough(ranked, getattr(embedder, "spec", None))[:limit]


def _near_enough(ranked: list[tuple[str, float]], spec) -> list[tuple[str, float]]:
    """The documents that count as near. Every query is similar to something; two rules decide
    (docs/search.md): a document **stands out** from the rest of the archive (at least
    max(2, sqrt(2 ln n) - 0.8) standard deviations above the average - the largest of n random
    values lies about sqrt(2 ln n) above it), or, for the calibrated built-in model, it is
    **similar enough** in absolute terms (several documents of one kind, none standing out).
    Nothing far below the best hit."""
    if not ranked:
        return []
    sims = [x[1] for x in ranked]
    stands_out: set[str] = set()
    if len(sims) >= MIN_DOCS_FOR_CUTOFF:
        mean = sum(sims) / len(sims)
        std = math.sqrt(sum((x - mean) ** 2 for x in sims) / len(sims)) or 1.0
        min_z = max(MIN_Z, math.sqrt(2 * math.log(len(sims))) - 0.8)
        floor = spec.min_similarity * LOW_FACTOR if spec is not None else -2.0
        stands_out = {d for d, sim in ranked if (sim - mean) / std >= min_z and sim >= floor}
    elif spec is None:
        return ranked  # too few documents to tell, and no calibration
    similar = spec.min_similarity if spec is not None and spec.min_similarity else 2.0
    best = ranked[0][1]
    return [
        (d, sim)
        for d, sim in ranked
        if (d in stands_out or sim >= similar) and sim >= best - MAX_BELOW_BEST
    ]


def fuse(
    lexical: list[str], meaning: list[str], k: int | None = None, weight: float | None = None
) -> list[tuple[str, float]]:
    """Weighted Reciprocal Rank Fusion of two rankings: sum of weight / (k + rank). The word
    search counts fully, the meaning with MEANING_WEIGHT; ties keep the word search's order."""
    score: dict[str, float] = {}
    w = MEANING_WEIGHT if weight is None else weight
    k = RRF_K if k is None else k
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
        e = embedder_for(archive.settings)
        # the model is downloaded when there is something to embed for the first time
        if (
            e is not None
            and hasattr(e, "download")
            and not e.downloaded()
            and pending(archive.conn, e.model, 1)
        ):
            log.info("search by meaning: downloading the model %s", e.model)
            e.download()
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
