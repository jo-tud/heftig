"""Search quality benchmark on the synthetic German archive (tests/search_bench.py).

    uv run python scripts/search_bench.py            # table per query + summary
    uv run python scripts/search_bench.py --json     # machine-readable
    uv run python scripts/search_bench.py -q Kaltmiete   # only queries containing the text

Lines start with ' ' (first hit is right), '~' (a right one among the first five) or '!'
(neither). See docs/search.md, "Measuring search quality".
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests import search_bench  # noqa: E402
from tests.conftest import make_settings  # noqa: E402

from heftig import searcheval  # noqa: E402
from heftig.archive import Archive  # noqa: E402
from heftig.providers import registry  # noqa: E402

TODAY = date(2026, 9, 29)


def cases(ids: dict[str, str], only: str | None = None) -> list[searcheval.Case]:
    out = []
    for q, grades, wrong in search_bench.QUERIES:
        if only and only.lower() not in q.lower():
            continue
        out.append(
            searcheval.Case(q, {ids[n]: g for n, g in grades.items()}, tuple(ids[n] for n in wrong))
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-q", dest="only")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        archive = Archive(make_settings(Path(tmp)))
        try:
            ids = search_bench.load(archive)
            summary, results = searcheval.evaluate(archive.conn, cases(ids, args.only), TODAY)
            if args.json:
                print(searcheval.as_json(summary, results))
            else:
                print(searcheval.report(summary, results, searcheval.titles(archive.conn)))
        finally:
            archive.close()
            registry.clear_overrides()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
