"""Search quality on the synthetic German household archive (tests/search_bench.py).

The thresholds are a floor, set a little below what the search reaches: a change that makes
the ranking worse fails here. `uv run python scripts/search_bench.py` shows every query.
"""

from datetime import date

import pytest

from heftig import searcheval

from . import search_bench

TODAY = date(2026, 9, 29)


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    from heftig.archive import Archive
    from heftig.providers import registry

    from .conftest import make_settings

    archive = Archive(make_settings(tmp_path_factory.mktemp("bench")))
    try:
        ids = search_bench.load(archive)
        cases = [
            searcheval.Case(q, {ids[n]: g for n, g in grades.items()}, tuple(ids[n] for n in wrong))
            for q, grades, wrong in search_bench.QUERIES
        ]
        summary, results = searcheval.evaluate(archive.conn, cases, TODAY)
        yield summary, results
    finally:
        archive.close()
        registry.clear_overrides()


def test_quality_floor(bench):
    summary, results = bench
    failed = [r.q for r in results if not r.success5]
    assert summary["mrr@10"] >= 0.95, failed
    assert summary["success@1"] >= 0.90, failed
    assert summary["success@5"] >= 0.97, failed
    assert summary["ndcg@10"] >= 0.93
    assert summary["recall@10"] >= 0.93
    assert summary["zero_results"] <= 2
    assert summary["noise"] == 0


def test_every_kind_of_query_is_covered(bench):
    """Each group of the benchmark finds what it looks for, at least on the first screen."""
    _summary, results = bench
    by_q = {r.q: r for r in results}
    for q in (
        "Kindern",  # stem: Kind, Kinder
        "Ärzte",  # umlaut plural
        "Stromrechnung",  # compound split: Strom + Rechnung
        "Rentenversicherungsnummer",
        "Nebenkostenabrechnung",  # synonym inside a split: Betriebskostenabrechnung
        "Krankschreibung",  # synonym
        "Kündigung Fitnessstudio",  # OCR error in the scan: Kündiqunq
        "Wohngebäudeversicherung",  # OCR error in the scan
        "die Rechnung vom Zahnarzt",  # function words
        "Wodafone",  # typo in the first letter
        "Rechnugn Telekom",  # swapped letters
    ):
        assert by_q[q].first is not None and by_q[q].first <= 3, (q, by_q[q].ranked)
