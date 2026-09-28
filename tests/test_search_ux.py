"""Finding without syntax: date phrases, facets, tag modes, suggestions, similar documents."""

from datetime import date

import pytest

from heftig.datephrases import extract
from heftig.search import SearchParams, count_hits, highlight_terms, search, similar, suggest

from .corpus import SCAN_NAME, load_corpus

TODAY = date(2026, 9, 28)


@pytest.fixture
def corpus(archive):
    return load_corpus(archive)


def ids_for(archive, **kw):
    res = search(archive.conn, SearchParams(per_page=100, **kw), today=TODAY)
    return [i["id"] for i in res.items]


@pytest.mark.parametrize(
    ("q", "rest", "lo", "hi"),
    [
        ("Rechnung März 2025", "Rechnung", "2025-03-01", "2025-03-31"),
        ("maerz 2025", "", "2025-03-01", "2025-03-31"),
        ("Jan. 2026 Lohn", "Lohn", "2026-01-01", "2026-01-31"),
        ("telekom letztes Jahr", "telekom", "2025-01-01", "2025-12-31"),
        ("vorletztes Jahr", "", "2024-01-01", "2024-12-31"),
        ("im Jahr 2019 Steuer", "Steuer", "2019-01-01", "2019-12-31"),
        ("seit 2023 versicherung", "versicherung", "2023-01-01", "2026-09-28"),
        ("vor 2020", "", "1900-01-01", "2019-12-31"),
        ("von 2021 bis 2023", "", "2021-01-01", "2023-12-31"),
        ("Strom letzte 3 Monate", "Strom", "2026-06-28", "2026-09-28"),
        ("in den letzten 30 Tagen", "", "2026-08-29", "2026-09-28"),
        ("letztes Quartal Abrechnung", "Abrechnung", "2026-04-01", "2026-06-30"),
        ("dieser Monat", "", "2026-09-01", "2026-09-30"),
        ("von März 2024 bis Mai 2025", "", "2024-03-01", "2025-05-31"),
        ("Strom Mai bis Juli 2025", "Strom", "2025-05-01", "2025-07-31"),
        ("zwischen Januar und März 2025", "", "2025-01-01", "2025-03-31"),
        ("Heizung November bis Februar 2025", "Heizung", "2024-11-01", "2025-02-28"),
        ("zwischen 2020 und 2022", "", "2020-01-01", "2022-12-31"),
    ],
)
def test_date_phrases(q, rest, lo, hi):
    got_rest, phrase = extract(q, TODAY)
    assert (got_rest, phrase.date_from, phrase.date_to) == (rest, lo, hi)


@pytest.mark.parametrize(
    "q", ["Allianz Versicherung 2025", '"März 2025"', "83729381", "Mai", "2021 und 2023"]
)
def test_no_date_phrase(q):
    assert extract(q, TODAY) == (q, None)


def test_date_phrase_filters_and_can_be_switched_off(archive, corpus):
    res = search(archive.conn, SearchParams(q="Rechnung September 2026"), today=TODAY)
    assert res.date_phrase.label == "September 2026"
    assert [i["id"] for i in res.items] == [corpus["telekom_2026_09.pdf"]]
    assert set(ids_for(archive, q="Vodafone letztes Jahr")) == {
        corpus["vodafone_vertrag.pdf"],
        corpus["vodafone_brief.pdf"],
    }
    # literal: the words are searched as text (the September invoice mentions "September 2026")
    literal = search(archive.conn, SearchParams(q="Rechnung September 2026", literal=True))
    assert literal.date_phrase is None and corpus["telekom_2026_09.pdf"] in [
        i["id"] for i in literal.items
    ]


def test_tag_mode_any_and_all(archive, corpus):
    both = ids_for(archive, tags=["Versicherung", "Auto"])
    assert both == [corpus["allianz_kfz_2023.pdf"]]
    anyof = set(ids_for(archive, tags=["Telefon", "Internet"], tag_mode="any"))
    assert anyof == {
        corpus["telekom_2026_09.pdf"],
        corpus["telekom_2026_08.pdf"],
        corpus["vodafone_vertrag.pdf"],
    }


def test_facets_count_the_current_results(archive, corpus):
    res = search(archive.conn, SearchParams(q="Versicherung"), with_facets=True)
    f = res.facets
    corr = {x["name"]: x["count"] for x in f["correspondent"]}
    assert corr["Allianz Versicherungs-AG"] == 2
    assert sum(corr.values()) == res.total
    assert {x["name"] for x in f["tag"]} >= {"Versicherung", "Auto"}
    assert sum(f["months"].values()) + f["undated"] == res.total


def test_facets_or_groups_ignore_their_own_filter(archive, corpus):
    res = search(
        archive.conn, SearchParams(correspondent=["Vodafone GmbH"], date_from="2025"),
        with_facets=True,
    )  # fmt: skip
    corr = {x["name"]: x["count"] for x in res.facets["correspondent"]}
    # other correspondents stay visible with their count for 2025 onwards
    assert corr["Vodafone GmbH"] == 2 and corr["Telekom Deutschland GmbH"] == 2
    # the timeline ignores the date filter but keeps the correspondent filter
    assert res.facets["months"] == {"2025-03": 1, "2025-06": 1}
    # "all" tags narrow: only tags present in the current result are counted
    res = search(archive.conn, SearchParams(tags=["Versicherung"]), with_facets=True)
    assert {x["name"]: x["count"] for x in res.facets["tag"]} == {"Versicherung": 3, "Auto": 1}


def test_suggest_filters_numbers_documents(archive, corpus):
    s = suggest(archive.conn, "telek")
    first = s["items"][0]
    assert (first["kind"], first["value"], first["count"]) == (
        "correspondent", "Telekom Deutschland GmbH", 2,
    )  # fmt: skip
    assert s["replace"] == "telek"
    # the last word is used when the whole input matches nothing
    s = suggest(archive.conn, "rechnung vodaf")
    assert s["replace"] == "vodaf" and s["items"][0]["value"] == "Vodafone GmbH"
    # middle of a name, and types/tags
    assert any(
        i["value"] == "Versicherungsschein" for i in suggest(archive.conn, "versich")["items"]
    )
    # numbers from custom fields lead straight to the document
    nums = [i for i in suggest(archive.conn, "8372")["items"] if i["kind"] == "number"]
    assert nums and nums[0]["href"].endswith(corpus["vodafone_vertrag.pdf"])
    docs = [
        i for i in suggest(archive.conn, "Krankenversicherung")["items"] if i["kind"] == "document"
    ]
    assert docs[0]["href"].endswith(corpus[SCAN_NAME])
    assert any(i["kind"] == "date" for i in suggest(archive.conn, "März 2025")["items"])
    assert suggest(archive.conn, "") == {"items": [], "replace": ""}


def test_similar_documents(archive, corpus):
    sims = similar(archive.conn, corpus["telekom_2026_09.pdf"])
    assert sims[0]["id"] == corpus["telekom_2026_08.pdf"]
    assert "same sender" in sims[0]["why"]
    assert corpus["telekom_2026_09.pdf"] not in [s["id"] for s in sims]


def test_highlight_terms_follow_the_search(archive, corpus):
    terms = highlight_terms(archive.conn, "Telekomm Rechnung März 2025")
    assert [t.tokens for t in terms] == [["telekom"], ["rechnung"]]
    assert count_hits("Ihre Rechnung von der Telekom, Rechnungsdatum", terms) == 3


def test_id_fragments_do_not_match_words_but_id_starts_find_the_document(archive, corpus):
    from heftig import documents as docs

    all_ids = [r[0] for r in archive.conn.execute("SELECT id FROM documents")]
    # a pure-letter piece of any ID ("bfae", "cafe") is not a search hit by itself
    for doc_id in all_ids:
        for seg in doc_id.split("-"):
            letters = "".join(c for c in seg if c.isalpha())[:4]
            if len(letters) >= 3:
                hits = ids_for(archive, q=letters)
                assert doc_id not in hits or letters in docs.get_text(archive, doc_id).lower()
    # the start of the ID or the hash (e.g. from below the page view) opens the document
    d = all_ids[0]
    sha = docs.load_meta(archive, d).sha256
    assert ids_for(archive, q=sha[:16]) == [d]
    assert ids_for(archive, q=d[:13]) == [d] or not any(c.isalpha() for c in d[:13])


def test_german_word_forms_and_compounds(archive):
    from heftig import documents as docs

    from .conftest import ingest_bytes, process_all
    from .helpers import text_pdf

    def doc(text, title, dtype):
        d = ingest_bytes(archive, text_pdf([text]), f"{title}.pdf").doc_id
        process_all(archive)
        docs.update_fields(archive, d, {"title": title, "document_type": dtype}, {})
        return d

    bescheid = doc(
        "Festsetzung der Einkommensteuer 2023", "Einkommensteuerbescheid 2023", "Bescheid"
    )
    brief = doc("Anbei die Steuerbescheide der letzten Jahre zur Kenntnis", "Brief Steuerberater",
                "Brief")  # fmt: skip
    vertrag = doc("Mietsache Wohnung, Kaltmiete 500 Euro", "Mietvertrag Musterstraße", "Vertrag")
    auszug = doc("Buchungen im Juli", "Kontoauszug Juli", "Kontoauszug")
    # the assessment itself first, the letter that mentions assessments after it
    assert ids_for(archive, q="steuerbescheide") == [bescheid, brief]
    assert ids_for(archive, q="Steuerbescheid")[0] == bescheid
    assert ids_for(archive, q="verträge") == [vertrag]  # plural with umlaut, compound
    assert ids_for(archive, q="kontoauszüge") == [auszug]
    assert ids_for(archive, q="miete") == [vertrag]  # "Kaltmiete" in the text
    # words shorter than 6 letters are not reduced (no "Bad" -> "ba*")
    assert ids_for(archive, q="juli") == [auszug]
    terms = highlight_terms(archive.conn, "steuerbescheide")
    assert count_hits("Ihr Einkommensteuerbescheid und der Steuerbescheid", terms) == 2


@pytest.mark.parametrize(
    ("q", "rest", "span"),
    [
        ("invoice march 2025", "invoice", ("2025-03-01", "2025-03-31")),
        ("phone bills last year", "phone bills", ("2025-01-01", "2025-12-31")),
        ("since 2023", "", ("2023-01-01", "2026-09-28")),
        ("in the last 30 days", "", ("2026-08-29", "2026-09-28")),
        ("between January and March 2025", "", ("2025-01-01", "2025-03-31")),
        ("from 2021 to 2023", "", ("2021-01-01", "2023-12-31")),
        ("rent until 2024", "rent", ("1900-01-01", "2024-12-31")),
        ("this quarter", "", ("2026-07-01", "2026-09-30")),
    ],
)
def test_english_date_phrases(q, rest, span):
    from heftig.datephrases import extract

    got_rest, phrase = extract(q, date(2026, 9, 28))
    assert got_rest == rest and (phrase.date_from, phrase.date_to) == span


def test_english_words_that_are_no_date_phrase():
    from heftig.datephrases import extract

    for q in ("2021 and 2023", "may i ask", "past year"):
        assert extract(q, date(2026, 9, 28))[1] is None
