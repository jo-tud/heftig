import json

import pytest

from heftig import documents as docs
from heftig.archive import Archive
from heftig.consume import ConsumeWatcher

from .conftest import make_settings
from .helpers import scan_pdf, text_pdf


@pytest.fixture
def watcher(archive):
    archive.settings.consume_path.mkdir(parents=True, exist_ok=True)
    return ConsumeWatcher(archive)


def drop(archive, name: str, data: bytes):
    p = archive.settings.consume_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_file_is_taken_only_when_stable_and_then_removed(archive, watcher):
    p = drop(archive, "scan_001.pdf", scan_pdf(["Seite"]))
    assert watcher.poll() == []  # first sighting
    assert p.exists()
    res = watcher.poll()
    assert [r["status"] for r in res] == ["created"]
    assert not p.exists()
    meta = docs.load_meta(archive, res[0]["document_id"])
    assert meta.source == "scanner" and meta.paper
    assert meta.source_details == {"path": "scan_001.pdf"}


def test_growing_file_waits(archive, watcher):
    p = drop(archive, "gross.pdf", b"%PDF-1.4\n")
    watcher.poll()
    with open(p, "ab") as f:  # scanner still writing
        f.write(b"more")
    assert watcher.poll() == []
    p.write_bytes(text_pdf(["fertig"]))
    watcher.poll()
    assert [r["status"] for r in watcher.poll()] == ["created"]


def test_multi_page_scan_with_a_pause_is_not_quarantined(archive, watcher, monkeypatch):
    """The scanner pauses between pages: the file is stable for a while but not finished."""
    full = scan_pdf(["Seite 1", "Seite 2", "Seite 3"])
    p = drop(archive, "scan_mehrseitig.pdf", full[: full.rindex(b"xref")])
    for _ in range(4):
        assert watcher.poll() == []  # stable, but no %%EOF yet: keep waiting
    assert p.exists() and not list(archive.paths.quarantine.glob("*"))
    p.write_bytes(full)  # the last page arrives
    watcher.poll()
    assert [r["status"] for r in watcher.poll()] == ["created"]
    # a file that never gets finished is taken after the waiting time (and quarantined)
    monkeypatch.setattr(archive.settings, "consume_incomplete_wait_seconds", 0)
    drop(archive, "abgebrochen.pdf", full[:2000])
    watcher.poll()
    assert [r["status"] for r in watcher.poll()] == ["rejected"]


def test_temporary_names_are_ignored(archive, watcher):
    for name in (".scan.pdf", "scan.pdf.part", "~$scan.pdf", "scan.tmp", "x.crdownload"):
        drop(archive, name, text_pdf([name]))
    watcher.poll()
    assert watcher.poll() == []
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_subfolders_are_scanned(archive, watcher):
    drop(archive, "2026/09/a.pdf", text_pdf(["a"]))
    watcher.poll()
    assert [r["path"] for r in watcher.poll()] == ["2026/09/a.pdf"]


def test_unsupported_file_goes_to_quarantine_with_reason(archive, watcher):
    p = drop(archive, "notiz.txt", b"just text")
    watcher.poll()
    res = watcher.poll()
    assert res[0]["status"] == "rejected"
    assert not p.exists()
    q = list(archive.paths.quarantine.glob("*notiz.txt"))
    assert len(q) == 1 and q[0].read_bytes() == b"just text"
    reason = json.loads(q[0].with_name(q[0].name + ".reason.json").read_text())
    assert "not supported" in reason["reason"]
    # not picked up again
    watcher.poll()
    assert watcher.poll() == []


def test_files_present_before_start_are_imported(tmp_path):
    """Files that arrived while the laptop was off are picked up by a fresh watcher."""
    settings = make_settings(tmp_path)
    a = Archive(settings)
    settings.consume_path.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (settings.consume_path / f"scan_{i}.pdf").write_bytes(scan_pdf([f"S{i}"]))
    a.close()
    a2 = Archive(settings)
    w = ConsumeWatcher(a2)
    w.poll()
    assert sorted(r["status"] for r in w.poll()) == ["created"] * 3
    a2.close()


def test_source_stays_when_archive_commit_fails(archive, watcher, monkeypatch):
    p = drop(archive, "wichtig.pdf", text_pdf(["wichtig"]))
    watcher.poll()
    calls = {"n": 0}
    real = docs.persist

    def boom(*a, **kw):
        calls["n"] += 1
        raise OSError("Datenträger voll")

    monkeypatch.setattr(docs, "persist", boom)
    res = watcher.poll()
    assert res[0]["status"] == "error"
    assert p.exists()  # never lost
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    monkeypatch.setattr(docs, "persist", real)
    watcher.poll()
    res = watcher.poll()
    assert res and res[0]["status"] == "created"
    assert not p.exists()
    # the original written during the failed attempt was reused, not duplicated
    assert len(list(archive.paths.originals.rglob("*.pdf"))) == 1


def test_repeated_failures_end_in_quarantine(archive, watcher, monkeypatch):
    p = drop(archive, "problem.pdf", text_pdf(["x"]))
    import heftig.consume as consume_mod

    def fail(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(consume_mod, "ingest_stream", fail)
    watcher.poll()
    statuses = []
    for _ in range(archive.settings.consume_max_failures):
        statuses += [r["status"] for r in watcher.poll()]
        watcher.poll()
    assert "rejected" in statuses
    assert not p.exists()
    assert list(archive.paths.quarantine.glob("*problem.pdf"))


def test_move_mode(tmp_path):
    s = make_settings(tmp_path, consume_after="move")
    a = Archive(s)
    s.consume_path.mkdir(parents=True)
    (s.consume_path / "a.pdf").write_bytes(text_pdf(["a"]))
    w = ConsumeWatcher(a)
    w.poll()
    w.poll()
    moved = list((s.consume_path / ".heftig-verarbeitet").iterdir())
    assert len(moved) == 1 and not (s.consume_path / "a.pdf").exists()
    a.close()


def test_missing_consume_folder_is_reported(archive):
    w = ConsumeWatcher(archive)
    assert w.poll() == []
    assert "cannot be reached" in w.last_error


def test_unreachable_share_is_reported_not_raised(archive, monkeypatch):
    import errno
    from pathlib import Path

    def host_down(self, *a, **kw):
        raise OSError(errno.EHOSTDOWN, "Host is down", str(self))

    monkeypatch.setattr(Path, "stat", host_down)
    w = ConsumeWatcher(archive)
    assert w.poll() == []
    assert "cannot be reached" in w.last_error


def test_local_folder_takes_digital_files(tmp_path):
    from heftig.worker import Worker

    local = tmp_path / "Heftig-Eingang"
    local.mkdir()
    a = Archive(make_settings(tmp_path, folder_dir=local))
    (local / "kontoauszug.pdf").write_bytes(text_pdf(["Kontoauszug"]))
    w = Worker(a)
    assert w.folder is not None and w.folder.root == local
    w.folder.poll()
    res = w.folder.poll()
    assert res[0]["status"] == "created" and not (local / "kontoauszug.pdf").exists()
    m = docs.load_meta(a, res[0]["document_id"])
    assert m.source == "folder" and m.paper is False
    a.close()


def test_quarantine_retry_and_hide(archive, watcher, monkeypatch):
    from heftig.consume import hide_quarantined, list_quarantine, retry_quarantined

    monkeypatch.setattr(archive.settings, "consume_incomplete_wait_seconds", 0)
    drop(archive, "kaputt.pdf", b"%PDF-1.4\nkaputt")
    drop(archive, "notiz.txt", b"just text")
    watcher.poll()
    watcher.poll()
    q = {i["original_name"]: i["file"] for i in list_quarantine(archive)}
    assert set(q) == {"kaputt.pdf", "notiz.txt"}
    # still broken: stays, with the new reason
    assert retry_quarantined(archive, q["kaputt.pdf"])["status"] == "rejected"
    # the file was fine after all (e.g. after an update): it is imported and leaves the list
    (archive.paths.quarantine / q["notiz.txt"]).write_bytes(text_pdf(["Doch lesbar"]))
    r = retry_quarantined(archive, q["notiz.txt"])
    assert r["status"] == "created"
    assert docs.load_meta(archive, r["document_id"]).original_filename == "notiz.txt"
    hide_quarantined(archive, q["kaputt.pdf"])
    assert list_quarantine(archive) == []
    assert (archive.paths.quarantine / "ausgeblendet" / q["kaputt.pdf"]).exists()
    with pytest.raises(ValueError):
        retry_quarantined(archive, "../index.sqlite")


def test_file_swapped_for_a_link_or_fifo_is_never_read(archive, watcher, tmp_path, monkeypatch):
    import os

    secret = tmp_path / "privat.pdf"
    secret.write_bytes(text_pdf(["PRIVATE FILE OUTSIDE THE CONSUME FOLDER"]))
    p = drop(archive, "a.pdf", text_pdf(["harmlos"]))
    q = drop(archive, "b.pdf", text_pdf(["auch harmlos"]))
    real_take = watcher._take

    def swap_then_take(path):  # the swap happens between the checks and the read
        path.unlink()
        if path.name == "a.pdf":
            path.symlink_to(secret)
        else:
            os.mkfifo(path)
        return real_take(path)

    monkeypatch.setattr(watcher, "_take", swap_then_take)
    watcher.poll()
    res = watcher.poll()  # a FIFO would block here forever
    assert {r["status"] for r in res} == {"skipped"}
    assert archive.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert not any(
        b"PRIVATE" in f.read_bytes() for f in archive.paths.quarantine.glob("*") if f.is_file()
    )
    assert p.is_symlink() and secret.exists() and q.exists()


def test_oversized_file_stays_in_the_consume_folder(tmp_path):
    from heftig.consume import hide_quarantined, list_quarantine, retry_quarantined

    a = Archive(make_settings(tmp_path, max_upload_mb=1))
    root = a.settings.consume_path
    root.mkdir(parents=True)
    (root / "riesig.pdf").write_bytes(text_pdf(["x"]) + b"0" * (2 * 1024 * 1024) + b"\n%%EOF\n")
    w = ConsumeWatcher(a)
    w.poll()
    assert w.poll()[0]["status"] == "rejected"
    assert not list(a.paths.quarantine.glob("*riesig.pdf"))  # no copy on the archive disk
    kept = list((root / ".heftig-abgelehnt").glob("*riesig.pdf"))
    assert len(kept) == 1
    entry = list_quarantine(a)[0]
    assert entry["kept_in"].startswith(".heftig-abgelehnt/")
    with pytest.raises(ValueError, match="input folder"):
        retry_quarantined(a, entry["file"])
    hide_quarantined(a, entry["file"])
    assert list_quarantine(a) == [] and kept[0].exists()
    assert w.poll() == [] and w.poll() == []  # the moved-aside file is not picked up again
    a.close()
