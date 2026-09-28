import hashlib

import pytest

from heftig import documents as docs
from heftig.archive import Archive
from heftig.storage import sha256_file

from .conftest import ingest_bytes, make_settings, process_all
from .helpers import image_bytes, multipage_tiff, scan_pdf, text_image, text_pdf


@pytest.mark.parametrize("source", ["web", "api", "folder", "scanner", "email"])
def test_every_source_stores_byte_identical_original(archive, source):
    data = text_pdf([f"Dokument aus Quelle {source}"])
    r = ingest_bytes(archive, data, "x.pdf", source=source)
    assert r.status == "created"
    meta = docs.load_meta(archive, r.doc_id)
    assert meta.source == source
    assert meta.sha256 == hashlib.sha256(data).hexdigest()
    path = archive.paths.resolve(meta.original_relpath)
    assert path.read_bytes() == data
    assert meta.original_relpath == f"originals/{meta.sha256[:2]}/{meta.sha256}.pdf"
    assert meta.paper == (source == "scanner")


def test_supported_formats(archive):
    img = text_image("Hallo")
    cases = {
        "a.pdf": (text_pdf(["x"]), "application/pdf"),
        "b.jpg": (image_bytes(img, "JPEG"), "image/jpeg"),
        "c.png": (image_bytes(img, "PNG"), "image/png"),
        "d.tif": (multipage_tiff(["S1", "S2"]), "image/tiff"),
    }
    for name, (data, mime) in cases.items():
        r = ingest_bytes(archive, data, name)
        assert r.status == "created", r.message
        assert docs.load_meta(archive, r.doc_id).mime_type == mime
    tif = archive.conn.execute(
        "SELECT page_count FROM documents WHERE mime_type='image/tiff'"
    ).fetchone()
    assert tif[0] == 2


def test_type_is_detected_by_content_not_extension(archive):
    r = ingest_bytes(archive, text_pdf(["x"]), "getarnt.jpg")
    assert r.status == "created"
    assert docs.load_meta(archive, r.doc_id).mime_type == "application/pdf"
    r = ingest_bytes(archive, b"MZ\x90\x00 not a pdf", "rechnung.pdf")
    assert r.status == "rejected" and "not supported" in r.message
    r = ingest_bytes(archive, b"%PDF-1.4 kaputt", "kaputt.pdf")
    assert r.status == "rejected"
    ev = archive.conn.execute(
        "SELECT COUNT(*) FROM ingest_events WHERE result='rejected'"
    ).fetchone()
    assert ev[0] == 2
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_duplicate_keeps_one_original_and_logs_event(archive):
    data = text_pdf(["Einmalig"])
    r1 = ingest_bytes(archive, data, "a.pdf", source="web")
    r2 = ingest_bytes(archive, data, "b.pdf", source="email", source_details={"subject": "Fwd"})
    assert r2.status == "duplicate" and r2.doc_id == r1.doc_id
    assert "already archived" in r2.message
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    originals = list(archive.paths.originals.rglob("*.pdf"))
    assert len(originals) == 1
    meta = docs.load_meta(archive, r1.doc_id)
    assert [e.result for e in meta.ingest_events] == ["created", "duplicate"]
    assert meta.ingest_events[1].source == "email"
    # received_at / sequence belong to the first arrival
    assert meta.original_filename == "a.pdf"


def test_different_files_same_text_are_not_merged(archive):
    a = ingest_bytes(archive, text_pdf(["Gleicher Text"]), "a.pdf")
    b = ingest_bytes(archive, image_bytes(text_image("Gleicher Text")), "b.png")
    assert a.status == b.status == "created" and a.doc_id != b.doc_id


def test_size_limit(tmp_path):
    a = Archive(make_settings(tmp_path, max_upload_mb=1))
    big = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
    r = ingest_bytes(a, big, "gross.pdf")
    assert r.status == "rejected" and "larger" in r.message
    assert not list(a.paths.tmp.iterdir())  # temp file cleaned up
    a.close()


def test_page_limit(tmp_path):
    a = Archive(make_settings(tmp_path, max_pages=2))
    r = ingest_bytes(a, text_pdf(["1", "2", "3"]), "drei.pdf")
    assert r.status == "rejected" and "pages" in r.message
    a.close()


def test_sequences_are_monotonic_and_received_at_is_kept(archive):
    ids = [ingest_bytes(archive, text_pdf([f"Nr {i}"]), f"{i}.pdf").doc_id for i in range(3)]
    seqs = [docs.load_meta(archive, i).ingest_sequence for i in ids]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    before = docs.load_meta(archive, ids[0]).received_at
    process_all(archive)
    from heftig.processing import reprocess

    reprocess(archive, ids, ["extract", "classify"])
    process_all(archive)
    assert docs.load_meta(archive, ids[0]).received_at == before
    # deleting the newest does not make its number reusable
    docs.delete_document(archive, ids[2])
    new = ingest_bytes(archive, text_pdf(["neu"]), "neu.pdf").doc_id
    assert docs.load_meta(archive, new).ingest_sequence > seqs[2]


def test_auto_filing_for_scanner(tmp_path):
    a = Archive(make_settings(tmp_path, auto_file_sources="scanner"))
    r1 = ingest_bytes(a, scan_pdf(["A"]), "a.pdf", source="scanner")
    r2 = ingest_bytes(a, scan_pdf(["B"]), "b.pdf", source="scanner")
    r3 = ingest_bytes(a, text_pdf(["digital"]), "c.pdf", source="web")
    m1, m2, m3 = (docs.load_meta(a, r.doc_id) for r in (r1, r2, r3))
    assert m1.filing_sequence < m2.filing_sequence
    assert m1.filing_section == m2.filing_section
    assert m3.filed_at is None and not m3.paper
    pos = docs.filing_position(a, m1)
    assert pos.position_from_top == 2 and pos.total_in_section == 2
    assert pos.above[0]["id"] == r2.doc_id
    a.close()


def test_manual_filing_and_dates_are_independent(archive):
    r = ingest_bytes(
        archive, text_pdf(["Brief vom 01.02.2020"]), "alt.pdf", source="web", paper=True
    )
    process_all(archive)
    meta = docs.load_meta(archive, r.doc_id)
    assert meta.document_date == "2020-02-01"
    assert meta.filed_at is None and docs.filing_position(archive, meta) is None
    meta = docs.mark_filed(archive, r.doc_id)
    # an old letter date does not move the paper into an old section
    assert meta.filing_section == docs.filing_section_for(archive, meta.filed_at)
    assert not meta.filing_section.startswith("2020")
    again = docs.mark_filed(archive, r.doc_id)
    assert again.filing_sequence == meta.filing_sequence  # idempotent


def test_original_files_are_read_only(archive):
    r = ingest_bytes(archive, text_pdf(["x"]), "x.pdf")
    p = archive.paths.resolve(docs.load_meta(archive, r.doc_id).original_relpath)
    assert oct(p.stat().st_mode & 0o777) == "0o400"
    assert sha256_file(p) == r.sha256
