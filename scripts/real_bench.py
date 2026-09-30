"""Search quality on real questions: the whole search over an archive built from public data.

    uv run --with pyarrow python scripts/real_bench.py munich            # German
    uv run --with pyarrow python scripts/real_bench.py fiqa              # English
    uv run --with pyarrow python scripts/real_bench.py munich --meaning  # with the built-in model
    uv run --with pyarrow python scripts/real_bench.py munich --part hold --limit 200

- munich: 1,491 citizen questions to the City of Munich, answered by one of 810 service
  articles (it-at-m/LHM-Dienstleistungen-QA and it-at-m/munich-public-services, MIT).
- fiqa: 648 finance forum questions, answered by posts among 2,000 (FiQA-2018 as published
  by BEIR / MTEB; the relevant posts plus random others).

The data is downloaded once into ~/.cache/heftig-bench (--data), the documents are ingested as
PDFs into an archive there (kept; --rebuild after changes to indexing). `--part tune` takes the
first two fifths of the questions, `--part hold` the rest (settings were chosen on tune and
checked on hold). See docs/search.md, "Measuring search quality".
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import shutil
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.conftest import (  # noqa: E402
    ScriptedClassifier,
    ingest_bytes,
    make_settings,
    process_all,
)
from tests.helpers import text_pdf  # noqa: E402

from heftig import searcheval  # noqa: E402
from heftig.archive import Archive  # noqa: E402
from heftig.providers import registry  # noqa: E402

HF = "https://huggingface.co/datasets"
MUNICH_ARTICLES = (
    f"{HF}/it-at-m/munich-public-services/resolve/main/data/train-00000-of-00001.parquet"
)
MUNICH_QA = [
    f"{HF}/it-at-m/LHM-Dienstleistungen-QA/resolve/main/data/train-00000-of-00001-c163f2d954c3ad2a.parquet",
    f"{HF}/it-at-m/LHM-Dienstleistungen-QA/resolve/main/data/test-00000-of-00001-e0c77dcb61c4adeb.parquet",
]
FIQA = f"{HF}/mteb/fiqa/resolve/main"
FIQA_DOCS = 2000


def _get(url: str) -> bytes:
    import httpx

    r = httpx.get(url, timeout=120, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _parquet(url: str) -> list[dict]:
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("needs pyarrow: uv run --with pyarrow python scripts/real_bench.py ...")
    return pq.read_table(io.BytesIO(_get(url))).to_pylist()


def munich() -> dict:
    articles = [a for a in _parquet(MUNICH_ARTICLES) if a["language"] == "de"]

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.replace("–", "-").replace(" ", " ")).strip().lower()

    by_name = {norm(a["name"]): a["id"] for a in articles}
    corpus = {a["id"]: [a["name"], re.sub(r"^\s*# .*\n", "", a["content"] or "")] for a in articles}
    queries, qrels = {}, {}
    for url in MUNICH_QA:
        for r in _parquet(url):
            doc = by_name.get(norm(r["title"]))  # the questions name their article by title
            if doc and r["question"].strip():
                queries[r["id"]] = r["question"].strip()
                qrels[r["id"]] = {doc: 1}
    return {"corpus": corpus, "queries": queries, "qrels": qrels}


def fiqa() -> dict:
    def jsonl(url: str) -> list[dict]:
        return [json.loads(line) for line in _get(url).decode().splitlines() if line.strip()]

    corpus = {r["_id"]: [r.get("title") or "", r["text"]] for r in jsonl(f"{FIQA}/corpus.jsonl")}
    qrels: dict[str, dict[str, int]] = {}
    for line in _get(f"{FIQA}/qrels/test.tsv").decode().splitlines()[1:]:
        q, d, score = line.split("\t")[:3]
        if int(score) > 0:
            qrels.setdefault(q, {})[d] = 1
    queries = {r["_id"]: r["text"] for r in jsonl(f"{FIQA}/queries.jsonl") if r["_id"] in qrels}
    keep = {d for rel in qrels.values() for d in rel if d in corpus}
    rest = sorted(set(corpus) - keep)
    random.Random(1).shuffle(rest)
    keep |= set(rest[: max(0, FIQA_DOCS - len(keep))])
    return {"corpus": {d: corpus[d] for d in sorted(keep)}, "queries": queries, "qrels": qrels}


SETS = {"munich": munich, "fiqa": fiqa}


def _pages(text: str) -> list[str]:
    lines = [line for para in text.splitlines() for line in (textwrap.wrap(para, 95) or [""])]
    return ["\n".join(lines[i : i + 48]) for i in range(0, max(1, len(lines)), 48)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=sorted(SETS))
    ap.add_argument("--data", type=Path, default=Path.home() / ".cache" / "heftig-bench")
    ap.add_argument("--part", choices=("all", "tune", "hold"), default="all")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rebuild", action="store_true", help="ingest the documents again")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--meaning",
        metavar="MODEL_DIR",
        nargs="?",
        const=str(Path.home() / ".cache" / "heftig-models"),
        help="search by meaning too, with the built-in model (downloaded into MODEL_DIR once)",
    )
    args = ap.parse_args()

    args.data.mkdir(parents=True, exist_ok=True)
    cached = args.data / f"{args.set}.json"
    if not cached.exists():
        print(f"downloading {args.set} ...", file=sys.stderr)
        cached.write_text(json.dumps(SETS[args.set]()), encoding="utf-8")
    data = json.loads(cached.read_text(encoding="utf-8"))

    root = args.data / f"archive-{args.set}"
    if args.rebuild and root.exists():
        shutil.rmtree(root)
    archive = Archive(make_settings(root))
    try:
        upload = {doc: f"doc_{i:05d}.pdf" for i, doc in enumerate(data["corpus"])}
        if not archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]:
            print(f"ingesting {len(upload)} documents ...", file=sys.stderr)
            titles = {upload[d]: {"title": t} for d, (t, _) in data["corpus"].items() if t}
            registry.override(classifier=ScriptedClassifier(by_filename=titles))
            for doc, (_, text) in data["corpus"].items():
                ingest_bytes(archive, text_pdf(_pages(text)), upload[doc])
            process_all(archive)
        id_of = {
            upload_name: doc_id
            for doc_id, upload_name in archive.conn.execute(
                "SELECT id, original_filename FROM documents"
            )
        }
        embedder = None
        if args.meaning:
            from heftig import semantic
            from heftig.local_embed import DEFAULT, LocalEmbedder

            embedder = LocalEmbedder(DEFAULT, Path(args.meaning))
            embedder.download()
            registry.override(embedder=embedder)
            print(semantic.catch_up(archive), file=sys.stderr)

        qids = list(data["queries"])
        cut = len(qids) * 2 // 5
        qids = {"all": qids, "tune": qids[:cut], "hold": qids[cut:]}[args.part][: args.limit]
        cases = [
            searcheval.Case(
                data["queries"][q],
                {id_of[upload[d]]: g for d, g in data["qrels"][q].items() if d in upload},
            )
            for q in qids
        ]
        summary, results = searcheval.evaluate(archive.conn, cases, None, embedder)
        if args.json:
            print(searcheval.as_json(summary, results))
        else:
            print(
                f"{args.set} ({args.part}{', words + meaning' if embedder else ', words'}): "
                + "  ".join(
                    f"{k} {v:.3f}" if isinstance(v, float) else f"{k} {v}"
                    for k, v in summary.items()
                )
            )
    finally:
        archive.close()
        registry.clear_overrides()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
