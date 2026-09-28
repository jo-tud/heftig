from heftig import documents as docs
from heftig.search import SearchParams, search

from .conftest import ingest_bytes, process_all
from .helpers import text_pdf


def test_ingest_process_search(archive):
    pdf = text_pdf(
        ["Telekom Deutschland GmbH\nRechnung vom 15.09.2026\nRechnungsbetrag: 39,95 EUR"]
    )
    r = ingest_bytes(archive, pdf, "rechnung.pdf")
    assert r.status == "created"
    process_all(archive)
    meta = docs.load_meta(archive, r.doc_id)
    assert meta.text_status == "ok"
    assert meta.document_date == "2026-09-15"
    res = search(archive.conn, SearchParams(q="telekom"))
    assert [i["id"] for i in res.items] == [r.doc_id]
