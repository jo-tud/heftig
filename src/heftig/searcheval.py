"""Measuring search quality: queries with the documents that should be found.

Most searches in an archive look for one known document (the letter from the tax office, the
rental contract), so the leading figures are how high the first right document is (MRR@10) and
whether it is the first hit (Success@1) or on the first screen (Success@5). nDCG@10 also counts
the other right documents by grade, Recall@10 how many of the right ones are on the first page;
"noise" counts documents marked as wrong that come before the first right one.

`heftig search-eval FILE` runs a set of queries against a real archive (see docs/search.md,
"Measuring search quality"); the test suite uses the synthetic archive in
tests/search_bench.py.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .search import SearchParams, search

K = 10


@dataclass
class Case:
    q: str
    grades: dict[str, int]  # document id -> 2 (what was searched for) or 1 (also good)
    wrong: tuple[str, ...] = ()  # document ids that must not come before the right ones


@dataclass
class CaseResult:
    q: str
    ranked: list[str]
    rr: float
    success1: bool
    success5: bool
    ndcg: float
    recall: float
    noise: int
    first: int | None  # 1-based position of the first document with the highest grade
    ms: float
    total: int
    notes: list[str] = field(default_factory=list)


def evaluate_case(conn: sqlite3.Connection, case: Case, today: date | None = None) -> CaseResult:
    res = search(conn, SearchParams(q=case.q, per_page=K), today=today)
    ranked = [it["id"] for it in res.items]
    best = max(case.grades.values())
    first = next((i + 1 for i, d in enumerate(ranked) if case.grades.get(d) == best), None)
    any_first = next((i + 1 for i, d in enumerate(ranked) if d in case.grades), None)
    dcg = sum((2 ** case.grades.get(d, 0) - 1) / math.log2(i + 2) for i, d in enumerate(ranked[:K]))
    ideal = sorted(case.grades.values(), reverse=True)[:K]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    relevant = [d for d, g in case.grades.items() if g > 0]
    notes = [f"{a}->{b}" for a, b in res.corrections]
    return CaseResult(
        q=case.q,
        ranked=ranked,
        rr=1.0 / any_first if any_first else 0.0,
        success1=first == 1,
        success5=first is not None and first <= 5,
        ndcg=dcg / idcg if idcg else 0.0,
        recall=sum(1 for d in relevant if d in ranked[:K]) / len(relevant),
        noise=sum(1 for d in ranked[: (first or K + 1) - 1] if d in case.wrong),
        first=first,
        ms=res.took_ms,
        total=res.total,
        notes=notes,
    )


def summarize(results: list[CaseResult]) -> dict[str, float]:
    n = len(results) or 1
    times = sorted(r.ms for r in results) or [0.0]
    return {
        "queries": len(results),
        "mrr@10": sum(r.rr for r in results) / n,
        "success@1": sum(r.success1 for r in results) / n,
        "success@5": sum(r.success5 for r in results) / n,
        "ndcg@10": sum(r.ndcg for r in results) / n,
        "recall@10": sum(r.recall for r in results) / n,
        "noise": sum(r.noise for r in results),
        "zero_results": sum(1 for r in results if r.total == 0),
        "ms_median": times[len(times) // 2],
        "ms_max": times[-1],
    }


def evaluate(
    conn: sqlite3.Connection, cases: list[Case], today: date | None = None
) -> tuple[dict[str, float], list[CaseResult]]:
    results = [evaluate_case(conn, c, today) for c in cases]
    return summarize(results), results


def report(summary: dict[str, float], results: list[CaseResult], titles: dict[str, str]) -> str:
    lines = [
        f"{'':1}{'query':34} {'first':>5} {'nDCG':>5} {'hits':>5}  top 3",
    ]
    for r in results:
        mark = " " if r.success1 else ("~" if r.success5 else "!")
        top = " | ".join(titles.get(d, d)[:24] for d in r.ranked[:3])
        first = str(r.first) if r.first else "-"
        note = f"  ({', '.join(r.notes)})" if r.notes else ""
        lines.append(f"{mark}{r.q[:34]:34} {first:>5} {r.ndcg:5.2f} {r.total:5}  {top}{note}")
    lines.append("")
    lines.append(
        "  ".join(
            f"{k} {v:.3f}" if isinstance(v, float) and k not in ("ms_median", "ms_max") else
            f"{k} {v:.1f}" if isinstance(v, float) else f"{k} {v}"
            for k, v in summary.items()
        )
    )  # fmt: skip
    return "\n".join(lines)


def load_cases(conn: sqlite3.Connection, path: Path) -> tuple[list[Case], list[str]]:
    """Queries from a JSON file: a list of {"q": "...", "expect": [...], "also": [...],
    "wrong": [...]}. Documents are named by ID, by the start of the ID (8+ characters) or by
    their exact title. Returns the cases and the problems found (names that match no document
    or more than one)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    def resolve(name: str) -> str | None:
        rows = conn.execute(
            "SELECT id FROM documents WHERE id = ? OR (length(?) >= 8 AND id LIKE ? || '%') "
            "OR title = ? LIMIT 2",
            (name, name, name, name),
        ).fetchall()
        if len(rows) != 1:
            problems.append(
                f"“{name}”: " + ("no document" if not rows else "more than one document")
            )
            return None
        return rows[0][0]

    cases = []
    for item in data:
        grades: dict[str, int] = {}
        for grade, key in ((1, "also"), (2, "expect")):
            for name in item.get(key) or []:
                doc = resolve(name)
                if doc:
                    grades[doc] = grade
        wrong = tuple(d for d in (resolve(n) for n in item.get("wrong") or []) if d)
        if grades:
            cases.append(Case(item["q"], grades, wrong))
    return cases, problems


def titles(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r[0]: r[1] or r[2]
        for r in conn.execute("SELECT id, title, original_filename FROM documents")
    }


def as_json(summary: dict[str, Any], results: list[CaseResult]) -> str:
    return json.dumps(
        {
            "summary": summary,
            "queries": [
                {"q": r.q, "first": r.first, "ndcg": round(r.ndcg, 3), "top": r.ranked[:5]}
                for r in results
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
