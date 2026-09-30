"""Word forms, compounds, synonyms and similar spellings (heftig.expand) and how the search
ranks them."""

from heftig import expand, synonyms
from heftig.search import SearchParams, parse_query, search

from .conftest import ScriptedClassifier, ingest_bytes, process_all
from .helpers import text_pdf


def test_stems_read_folded_umlauts():
    assert expand.stem("haeusern") == expand.stem("haus")
    assert expand.stem("aerzte") == expand.stem("arzt")
    assert expand.stem("kindern") == expand.stem("kind")
    assert expand.stem("vertraege") == expand.stem("vertrag")
    assert expand.stem("feuer") != expand.stem("fur")  # "ue" after a vowel is not an umlaut


def test_distance_counts_swaps_and_ocr_confusions_once():
    assert expand.distance("rechnugn", "rechnung", 2) == 1  # swapped letters
    assert expand.distance("kaltmiete", "kaltrniete", 2) == 1  # m read as rn
    assert expand.distance("kuendigung", "kuendiqunq", 2) == 2  # g read as q, twice
    assert expand.distance("miete", "biete", 1) == 1
    assert expand.distance("rechnung", "zeichnung", 1) == 2  # exceeded: max + 1


def test_allowed_edits_grow_with_the_word():
    assert [expand.allowed_edits(w) for w in ("haus", "miete", "rechnung", "versicherung")] == [
        0,
        1,
        1,
        2,
    ]


def test_synonyms_match_by_stem_and_phrase():
    assert ("mobilfunk",) in synonyms.alternatives(("handys",))
    assert ("kraftfahrzeugsteuer",) in synonyms.alternatives(("kfz", "steuer"))
    assert synonyms.alternatives(("rechnung", "2024")) == []  # numbers have no synonyms


def test_function_words_are_left_out_unless_that_is_all():
    assert [t.tokens for t in parse_query("die Rechnung vom Zahnarzt").terms] == [
        ["rechnung"],
        ["zahnarzt"],
    ]
    assert [t.tokens for t in parse_query("die").terms] == [["die"]]
    assert [t.tokens for t in parse_query('"die Zeit"').terms] == [["die", "zeit"]]


DOCS = {
    "a.pdf": (
        "Hausverwaltung Lindenhof\nAbrechnung der Nebenkosten für 2023\nNachzahlung 80 EUR",
        {
            "title": "Abrechnung Nebenkosten 2023",
            "correspondent": "Hausverwaltung Lindenhof",
            "correspondent_confidence": 0.95,
        },
    ),
    "b.pdf": ("Beispiel Versicherung\nVersicherunqsschein Hausrat\nKündiqunq zum Jahresende", {}),
    "c.pdf": (
        "Stadtwerke\nJahresabrechnung Strom 2023\nIhre Rechnung über 812 EUR",
        {"title": "Jahresabrechnung Strom 2023"},
    ),
    "d.pdf": (
        "Rundschreiben\nStrom sparen im Winter. Tipps für Ihre Rechnung finden Sie auf Seite 4. "
        "Allianz Arena und Versicherung: Angebote für Mitglieder.",
        {"title": "Rundschreiben Winter"},
    ),
    "e.pdf": (
        "Allianz Versicherungs-AG\nIhre Versicherung: Beitragsrechnung 2024",
        {"title": "Beitragsrechnung", "correspondent": "Allianz Versicherungs-AG"},
    ),
    "f.pdf": (
        "Kinderarztpraxis Dr. Klein\nRechnung\nUntersuchung Ihrer Kinder",
        {"title": "Rechnung Kinderarzt"},
    ),
}


def _archive_with(archive):
    registry_meta = {name: meta for name, (_, meta) in DOCS.items()}
    from heftig.providers import registry

    registry.override(classifier=ScriptedClassifier(by_filename=registry_meta))
    ids = {
        name: ingest_bytes(archive, text_pdf([text]), name).doc_id
        for name, (text, _) in DOCS.items()
    }
    process_all(archive)
    return ids


def _ids(archive, q, **kw):
    return [i["id"] for i in search(archive.conn, SearchParams(q=q, per_page=50, **kw)).items]


def test_split_compound_finds_its_parts(archive):
    ids = _archive_with(archive)
    found = _ids(archive, "Stromrechnung")
    assert found[0] == ids["c.pdf"]  # "Strom" and "Rechnung" in the title and text
    assert ids["d.pdf"] in found  # both words in passing: found, ranked lower


def test_ocr_errors_are_found_but_ranked_last(archive):
    ids = _archive_with(archive)
    assert _ids(archive, "Kündigung") == [ids["b.pdf"]]  # only as "Kündiqunq"
    res = search(archive.conn, SearchParams(q="Versicherungsschein Hausrat"))
    assert [i["id"] for i in res.items] == [ids["b.pdf"]]
    assert "<mark>Versicherunqsschein</mark>" in res.items[0]["snippet_html"]
    # the scan comes after documents with the word itself
    found = _ids(archive, "Rechnung")
    assert found[-1] != ids["b.pdf"] or ids["b.pdf"] not in found


def test_stem_forms_and_compounds(archive):
    ids = _archive_with(archive)
    assert _ids(archive, "Kindern")[0] == ids["f.pdf"]
    assert _ids(archive, "Ärzte") == [ids["f.pdf"]]  # Kinderarzt, Kinderarztpraxis


def test_close_words_rank_higher(archive):
    ids = _archive_with(archive)
    found = _ids(archive, "Allianz Versicherung")
    assert found.index(ids["e.pdf"]) < found.index(ids["d.pdf"])


def test_partial_matches_rank_by_number_of_words(archive):
    ids = _archive_with(archive)
    res = search(archive.conn, SearchParams(q="Strom Rechnung Allianz Arena Hamburg"))
    assert res.partial  # nothing has "Hamburg"
    found = [i["id"] for i in res.items]
    assert found[0] == ids["d.pdf"]  # four of the five words
    assert found.index(ids["c.pdf"]) < found.index(ids["a.pdf"])  # two words before one


def test_synonym_ranked_below_the_word_itself(archive):
    ids = _archive_with(archive)
    found = _ids(archive, "Betriebskosten")
    assert found == [ids["a.pdf"]]  # "Nebenkosten" stands for it
    reasons = search(archive.conn, SearchParams(q="Betriebskosten")).items[0]["reasons"]
    assert "Title" in reasons


def test_new_alias_is_searchable_at_once(archive):
    """The vocabulary caches follow every change of the index, also an alias (no persist)."""
    from heftig import documents as docs
    from heftig import taxonomy as tax

    _archive_with(archive)
    assert search(archive.conn, SearchParams(q="Lindenhofverwaltung")).total == 0
    term = tax.find_term(archive.conn, "correspondent", "Hausverwaltung Lindenhof")
    docs.add_term_alias(archive, term, "Lindenhofverwaltung")
    res = search(archive.conn, SearchParams(q="Lindenhofverwaltung"))
    assert res.total == 1 and not res.corrections


def test_quoted_function_word_is_kept():
    assert [t.tokens for t in parse_query('"die" Rechnung').terms] == [["die"], ["rechnung"]]
