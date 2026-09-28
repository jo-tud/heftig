"""Search acceptance tests (spec section 5)."""

import pytest

from heftig import maintenance
from heftig import taxonomy as tax
from heftig.search import SearchParams, parse_query, search

from .corpus import SCAN_NAME, load_corpus


@pytest.fixture
def corpus(archive):
    return load_corpus(archive)


def ids_for(archive, **kw):
    return [i["id"] for i in search(archive.conn, SearchParams(per_page=100, **kw)).items]


def test_all_documents_processed(archive, corpus):
    rows = archive.conn.execute("SELECT status, COUNT(*) FROM documents GROUP BY status").fetchall()
    assert dict((r[0], r[1]) for r in rows) == {"done": len(corpus)}


def test_typo_telekomm_rechnung(archive, corpus):
    res = search(archive.conn, SearchParams(q="Telekomm Rechnung"))
    assert ("telekomm", "telekom") in res.corrections
    top2 = {i["id"] for i in res.items[:2]}
    assert top2 == {corpus["telekom_2026_09.pdf"], corpus["telekom_2026_08.pdf"]}
    assert res.fuzzy_stats["candidates_checked"] < 200  # bounded, not a full-archive scan


def test_exact_contract_number_first(archive, corpus):
    res = search(archive.conn, SearchParams(q="83729381"))
    ids = [i["id"] for i in res.items]
    assert ids[0] == corpus["vodafone_vertrag.pdf"]
    # the number printed with a space in another letter is found too, but ranked lower
    assert corpus["vodafone_brief.pdf"] in ids[1:]
    # a longer number that merely starts with the same digits is not a match
    assert corpus["stadtwerke_abschlag.pdf"] not in ids
    assert "Custom field" in res.items[0]["reasons"] or "Number/ID" in res.items[0]["reasons"]
    assert not res.corrections


def test_allianz_versicherung_2025_prefers_correspondent_type_year(archive, corpus):
    ids = ids_for(archive, q="Allianz Versicherung 2025")
    assert ids[0] == corpus["allianz_hausrat_2025.pdf"]
    assert ids.index(corpus["tickets.pdf"]) > ids.index(corpus["allianz_hausrat_2025.pdf"])
    assert ids.index(corpus["tickets.pdf"]) > ids.index(corpus["allianz_kfz_2023.pdf"])


def test_phrase_search(archive, corpus):
    assert ids_for(archive, q='"Allianz Arena"') == [corpus["tickets.pdf"]]
    assert ids_for(archive, q='"Arena Allianz"') == []


def test_date_and_tag_filters_combined(archive, corpus):
    assert ids_for(archive, tags=["Telefon"], date_from="2026-09-01") == [
        corpus["telekom_2026_09.pdf"]
    ]
    assert set(
        ids_for(archive, tags=["Versicherung"], date_from="2024-01-01", date_to="2025-12-31")
    ) == {
        corpus["allianz_hausrat_2025.pdf"],
        corpus[SCAN_NAME],
    }
    # multiple tags: all must match
    assert ids_for(archive, tags=["Versicherung", "Auto"]) == [corpus["allianz_kfz_2023.pdf"]]
    # document date and received date are separate filters
    assert ids_for(archive, received_from="2000-01-01", date_to="2023-12-31") == [
        corpus["allianz_kfz_2023.pdf"]
    ]


def test_filter_and_free_text_combined(archive, corpus):
    assert ids_for(
        archive, q="rechnung", correspondent=["Telekom Deutschland GmbH"], date_to="2026-08-31"
    ) == [corpus["telekom_2026_08.pdf"]]


def test_umlauts_and_spelling_variants(archive, corpus):
    target = corpus["finanzamt_bescheid.pdf"]
    for q in [
        "Müllerstraße",
        "muellerstrasse",
        "MUELLERSTRASSE",
        "Beispielstadt-Sued",
        "Süd Einkommensteuer",
    ]:
        assert target in ids_for(archive, q=q), q


def test_ocr_fragment_hyphenation(archive, corpus):
    ids = ids_for(archive, q="Krankenversicherung")
    assert ids == [corpus[SCAN_NAME]]
    # page 2 of the scan is also searchable
    assert corpus[SCAN_NAME] in ids_for(archive, q="Datenschutz")


def test_snippet_highlight_is_escaped(archive, corpus):
    res = search(archive.conn, SearchParams(q="Telekom"))
    snip = res.items[0]["snippet_html"]
    assert "<mark>Telekom</mark>" in snip
    assert "<script" not in snip


def test_field_syntax(archive, corpus):
    assert set(ids_for(archive, q='correspondent:"Telekom Deutschland GmbH" year:2026')) == {
        corpus["telekom_2026_09.pdf"],
        corpus["telekom_2026_08.pdf"],
    }
    assert ids_for(archive, q="type:Vertrag") == [corpus["vodafone_vertrag.pdf"]]
    assert ids_for(archive, q="tag:Steuer") == [corpus["finanzamt_bescheid.pdf"]]
    assert ids_for(archive, q="source:scanner") == [corpus[SCAN_NAME]]
    assert len(ids_for(archive, q="received:2000..2100")) == len(corpus)


def test_invalid_syntax_is_reported_not_raised(archive, corpus):
    res = search(archive.conn, SearchParams(q="year:zwanzig"))
    assert res.errors and "please enter a year" in res.errors[0]
    res = search(archive.conn, SearchParams(q='"Allianz Arena'))
    assert res.errors  # unbalanced quote -> searched as plain text
    assert corpus["tickets.pdf"] in [i["id"] for i in res.items]
    # SQL/FTS metacharacters are harmless
    for q in ["'; DROP TABLE documents; --", "NEAR(a b)", "a OR", "*", "title:x AND"]:
        search(archive.conn, SearchParams(q=q))
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == len(corpus)


def test_unknown_field_prefix_is_plain_text(archive, corpus):
    p = parse_query("Vertragsnr:83729381")
    assert not p.filters and p.terms


def test_exact_id_and_hash(archive, corpus):
    doc_id = corpus["tickets.pdf"]
    sha = archive.conn.execute("SELECT sha256 FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    for q in (doc_id, sha, sha.upper()):
        res = search(archive.conn, SearchParams(q=q))
        assert res.exact and [i["id"] for i in res.items] == [doc_id]


def test_default_sort_and_stable_pagination(archive, corpus):
    all_ids = ids_for(archive)
    seqs = [
        archive.conn.execute("SELECT ingest_sequence FROM documents WHERE id=?", (i,)).fetchone()[0]
        for i in all_ids
    ]
    assert seqs == sorted(seqs, reverse=True)
    paged = []
    for page in range(1, 10):
        paged += [i["id"] for i in search(archive.conn, SearchParams(page=page, per_page=3)).items]
    assert paged == all_ids
    q_all = ids_for(archive, q="gmbh")
    q_paged = []
    for page in range(1, 5):
        q_paged += [
            i["id"]
            for i in search(archive.conn, SearchParams(q="gmbh", page=page, per_page=2)).items
        ]
    assert q_paged == q_all


def test_custom_field_amount_filter(archive, corpus):
    assert set(ids_for(archive, cf_key="Betrag", cf_min=40)) == {
        corpus["telekom_2026_08.pdf"],
        corpus["stadtwerke_abschlag.pdf"],
        corpus["allianz_hausrat_2025.pdf"],
    }
    assert ids_for(archive, cf_key="Betrag", cf_min=40, cf_max=50) == [
        corpus["telekom_2026_08.pdf"]
    ]


def test_alias_is_searchable(archive, corpus):
    from heftig import documents as docs

    tid = tax.find_term(archive.conn, "correspondent", "Telekom Deutschland GmbH")
    docs.add_term_alias(archive, tid, "DTAG")
    assert len(ids_for(archive, q="dtag")) == 2
    assert len(ids_for(archive, correspondent=["DTAG"])) == 2


def test_or_fallback_when_not_all_terms_match(archive, corpus):
    res = search(archive.conn, SearchParams(q="Telekom Zebrastreifen"))
    assert res.partial and res.items


QUERIES = [
    "Telekomm Rechnung",
    "83729381",
    "Allianz Versicherung 2025",
    '"Allianz Arena"',
    "Müllerstraße",
    "Krankenversicherung",
    "type:Rechnung",
    "",
]


def _snapshot(archive):
    return {q: ids_for(archive, q=q) for q in QUERIES}


def test_reindex_and_rebuild_keep_results(archive, corpus):
    before = _snapshot(archive)
    maintenance.reindex(archive)
    assert _snapshot(archive) == before
    report = maintenance.rebuild_db(archive)
    assert report["documents"] == len(corpus) and not report["errors"]
    assert _snapshot(archive) == before
