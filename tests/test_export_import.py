import json
import zipfile

import pytest

from heftig import documents as docs
from heftig import maintenance
from heftig import taxonomy as tax
from heftig.archive import Archive
from heftig.search import SearchParams, search

from .conftest import make_settings
from .corpus import load_corpus


@pytest.fixture
def source(archive):
    ids = load_corpus(archive)
    # paper filing order, locks, aliases, tag decisions
    for name in ("allianz_kfz_2023.pdf", "telekom_2026_09.pdf", "finanzamt_bescheid.pdf"):
        docs.mark_filed(archive, ids[name])
    docs.update_fields(
        archive, ids["tickets.pdf"], {"title": "Tickets (manuell)", "tags": ["Freizeit", "Fußball"]}
    )
    docs.add_term_alias(
        archive, tax.find_term(archive.conn, "correspondent", "Vodafone GmbH"), "VF"
    )
    return ids


def fresh(tmp_path, name):
    return Archive(make_settings(tmp_path / name))


def snapshot(a: Archive) -> dict:
    conn = a.conn
    rows = conn.execute("SELECT id FROM documents ORDER BY ingest_sequence").fetchall()
    out = {"docs": {}, "order": [], "filing": []}
    for r in rows:
        m = docs.load_meta(a, r[0])
        d = m.model_dump(mode="json")
        for k in ("revision", "updated_at"):
            d.pop(k)
        d["text"] = docs.get_text(a, m.id)
        d["original_sha"] = __import__("heftig.storage", fromlist=["x"]).sha256_file(
            a.paths.resolve(m.original_relpath)
        )
        out["docs"][m.id] = d
        out["order"].append((m.ingest_sequence, m.id))
    out["filing"] = [
        tuple(r) for r in conn.execute(
            "SELECT filing_section, filing_sequence, id FROM documents WHERE filing_sequence IS NOT NULL "
            "ORDER BY filing_sequence"
        )
    ]  # fmt: skip
    out["taxonomy"] = [
        (t.kind, t.name, tuple(t.aliases), t.doc_count) for t in tax.list_terms(conn)
    ]
    out["search"] = {
        q: [i["id"] for i in search(conn, SearchParams(q=q, per_page=50)).items]
        for q in ("Telekomm Rechnung", "83729381", "Allianz Versicherung 2025", "vf", "")
    }
    return out


@pytest.mark.parametrize("as_zip", [False, True])
def test_export_import_roundtrip(archive, source, tmp_path, as_zip):
    before = snapshot(archive)
    path = maintenance.export_archive(archive, tmp_path / "exports", as_zip=as_zip)
    target = fresh(tmp_path, "target")
    report = maintenance.import_archive(target, path)
    assert (
        report["imported"] == len(source) and not report["conflicts"] and not report["renumbered"]
    )
    after = snapshot(target)
    assert after == before
    # locks survive
    t = docs.load_meta(target, source["tickets.pdf"])
    assert t.field_locks["title"] and t.tag_overrides.added == ["Fußball"]
    # importing the same export again is a no-op
    again = maintenance.import_archive(target, path)
    assert again["imported"] == 0 and again["unchanged"] == len(source) and not again["conflicts"]
    assert snapshot(target) == before
    assert maintenance.check(target)["ok"]
    target.close()


def test_export_contents_are_open_and_without_secrets(archive, source, tmp_path):
    from heftig import auth

    auth.create_user(archive.conn, "jo", "geheimes-passwort-123")
    _, token = auth.create_api_token(archive.conn, 1, "App")
    path = maintenance.export_archive(archive, tmp_path / "exp")
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["format"] == "heftig-export" and manifest["document_count"] == len(source)
    names = {f["path"] for f in manifest["files"]}
    assert (
        "metadata.jsonl" in names and "taxonomy.json" in names and "state/sequences.json" in names
    )
    assert sum(1 for n in names if n.startswith("originals/")) == len(source)
    blob = "".join(p.read_text(errors="ignore") for p in path.rglob("*.json*"))
    # the token prefix (first 10 characters) is exported on purpose, the token itself never
    assert "scrypt$" not in blob and token not in blob and token[10:] not in blob
    assert "token_hash" not in blob
    users = json.loads((path / "state/users.json").read_text())
    assert users["users"][0]["username"] == "jo" and "password_hash" not in users["users"][0]
    lines = (path / "metadata.jsonl").read_text().splitlines()
    seqs = [json.loads(ln)["ingest_sequence"] for ln in lines]
    assert seqs == sorted(seqs)


def test_conflicts_are_reported_not_overwritten(archive, source, tmp_path):
    path = maintenance.export_archive(archive, tmp_path / "exp")
    target = fresh(tmp_path, "t")
    maintenance.import_archive(target, path)
    docs.update_fields(target, source["vodafone_vertrag.pdf"], {"title": "lokal geändert"})
    rep = maintenance.import_archive(target, path)
    assert [c["id"] for c in rep["conflicts"]] == [source["vodafone_vertrag.pdf"]]
    assert docs.load_meta(target, source["vodafone_vertrag.pdf"]).title == "lokal geändert"
    target.close()


def test_import_into_non_empty_archive_renumbers(archive, source, tmp_path):
    from .conftest import ingest_bytes
    from .helpers import text_pdf

    path = maintenance.export_archive(archive, tmp_path / "exp")
    target = fresh(tmp_path, "t")
    ingest_bytes(target, text_pdf(["schon da"]), "vorher.pdf")
    rep = maintenance.import_archive(target, path)
    assert rep["imported"] == len(source) and rep["renumbered"]
    seqs = [r[0] for r in target.conn.execute("SELECT ingest_sequence FROM documents")]
    assert len(seqs) == len(set(seqs))
    target.close()


def test_tampered_export_is_refused(archive, source, tmp_path):
    path = maintenance.export_archive(archive, tmp_path / "exp")
    orig = next((path / "originals").rglob("*.pdf"))
    orig.write_bytes(orig.read_bytes() + b"x")
    target = fresh(tmp_path, "t")
    with pytest.raises(maintenance.MaintenanceError, match="Checksum"):
        maintenance.import_archive(target, path)
    assert target.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    target.close()


def test_zip_slip_and_bombs_are_refused(tmp_path):
    target = fresh(tmp_path, "t")
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../etc/x", "boom")
        z.writestr("manifest.json", "{}")
    with pytest.raises(maintenance.MaintenanceError, match="Unsafe path"):
        maintenance.import_archive(target, evil)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("x/manifest.json", "{}")
        z.writestr("x/zeros.bin", b"\0" * (20 * 1024 * 1024))
    with pytest.raises(maintenance.MaintenanceError, match="compression ratio"):
        maintenance.import_archive(target, bomb)
    target.close()
