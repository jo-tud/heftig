"""Date evidence: the quote must back the date - in every format real documents use."""

import json

import pytest

from heftig import documents as docs
from heftig.classify import _evidence_matches_date
from heftig.db import write_tx
from heftig.models import Suggestion
from heftig.processing import revalidate_dates
from heftig.providers import registry

from .conftest import ScriptedClassifier, ingest_bytes, process_all
from .helpers import text_pdf


@pytest.mark.parametrize(
    ("evidence", "iso"),
    [
        ("03.09.2026", "2026-09-03"), ("15.09.26", "2026-09-15"), ("12.03.98", "1998-03-12"),
        ("05/08/2014", "2014-08-05"), ("Datum, 18-10-2016", "2016-10-18"),
        ("2026-09-15", "2026-09-15"), ("15. September 2026", "2026-09-15"),
        ("31. Maerz 2025", "2025-03-31"), ("D r e s d e n 1 1 J u n i 2 0 1 5", "2015-06-11"),
        ("June 11, 2021", "2021-06-11"), ("Jun-04-2021", "2021-06-04"),
        ("June 4th, 2021", "2021-06-04"), ("June, 2nd, 2021", "2021-06-02"),
        ("Zurich, March 4th, 2020", "2020-03-04"), ("4 Mar 2020", "2020-03-04"),
        ("the 1st of July 2022", "2022-07-01"),
    ],
)  # fmt: skip
def test_evidence_formats(evidence, iso):
    assert _evidence_matches_date(evidence, iso)


@pytest.mark.parametrize(
    ("evidence", "iso"),
    [("03.09.2026", "2026-09-04"), ("June 11, 2021", "2021-06-12"), ("Rechnung 2021", "2021-01-01"),
     ("Kundennummer 1234567", "2023-04-05")],
)  # fmt: skip
def test_evidence_must_match_the_date(evidence, iso):
    assert not _evidence_matches_date(evidence, iso)


def test_english_date_is_applied_directly(archive):
    registry.override(classifier=ScriptedClassifier(default={
        "title": "Agreement", "document_date": "2021-06-11",
        "document_date_evidence": "June 11, 2021", "document_date_confidence": 0.9,
    }))  # fmt: skip
    r = ingest_bytes(
        archive, text_pdf(["Loan agreement of June 11, 2021 between A and B"]), "a.pdf"
    )
    process_all(archive)
    m = docs.load_meta(archive, r.doc_id)
    assert m.document_date == "2021-06-11" and m.status == "done" and not m.suggestions


def test_revalidate_stored_suggestions_without_ai(archive):
    registry.override(classifier=ScriptedClassifier(default={"title": "Agreement"}))
    good = ingest_bytes(
        archive, text_pdf(["Agreement dated June 4th, 2021 between A and B"]), "g.pdf"
    ).doc_id
    bad = ingest_bytes(
        archive, text_pdf(["Agreement between A and B, signed later"]), "b.pdf"
    ).doc_id
    process_all(archive)
    # as left behind by the old rules: a suggestion + the stored classifier answer
    for doc_id in (good, bad):
        data = {"document_date": "2021-06-04", "document_date_evidence": "June 4th, 2021",
                "document_date_confidence": 0.9}  # fmt: skip
        with write_tx(archive.conn):
            m = docs.load_meta(archive, doc_id)
            m.suggestions = [Suggestion(field="document_date", value="2021-06-04",
                             reason="Datum nicht wörtlich im Text belegt – bitte prüfen")]  # fmt: skip
            m.status = "needs_review"
            docs.persist(archive, m)
            archive.conn.execute(
                "INSERT INTO processing_runs(doc_id, task, provider, status, suggestions, "
                "started_at, finished_at) VALUES(?, 'classify', 'x', 'ok', ?, 'a', 'b')",
                (doc_id, json.dumps({"data": data})),
            )
    assert revalidate_dates(archive) == {"checked": 2, "applied": 1}
    g, b = docs.load_meta(archive, good), docs.load_meta(archive, bad)
    assert g.document_date == "2021-06-04" and g.status == "done" and not g.suggestions
    assert b.document_date is None and b.status == "needs_review"  # quote not in its text
