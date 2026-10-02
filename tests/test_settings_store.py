"""Settings changed in the web interface: stored in the archive, environment wins."""

import pytest

from heftig import settings_store as store
from heftig.archive import Archive

from .conftest import make_settings


@pytest.fixture
def arch(tmp_path):
    a = Archive(make_settings(tmp_path))
    yield a
    a.close()


def test_stored_values_apply_and_survive_a_restart(arch, tmp_path):
    base = arch.base_settings
    new = store.save(arch.conn, base, {"imap_host": "imap.example.org", "imap_port": 143})
    assert new.imap_host == "imap.example.org" and new.imap_port == 143
    assert store.revision(arch.conn) == 1
    again = Archive(make_settings(tmp_path))
    assert again.settings.imap_host == "imap.example.org"
    again.close()


def test_environment_wins_and_is_fixed(arch):
    base = arch.base_settings  # built with language="de" and ocr_provider="mock"
    assert {"language", "ocr_provider"} <= store.fixed(base)
    with pytest.raises(store.SettingsError, match="environment"):
        store.save(arch.conn, base, {"language": "en"})
    with pytest.raises(store.SettingsError, match="not editable"):
        store.save(arch.conn, base, {"archive_dir": "/tmp"})


def test_invalid_values_are_refused(arch):
    with pytest.raises(store.SettingsError, match="imap_port"):
        store.save(arch.conn, arch.base_settings, {"imap_port": "many"})
    assert store.stored(arch.conn) == {}


def test_secrets_are_kept_when_left_empty_and_removed_with_none(arch):
    base = arch.base_settings
    store.save(arch.conn, base, {"imap_password": "geheim"})
    s = store.save(arch.conn, base, {"imap_password": "", "imap_user": "me"})
    assert s.secret("imap_password") == "geheim" and s.imap_user == "me"
    s = store.save(arch.conn, base, {"imap_password": None})
    assert not s.secret("imap_password")


def test_refresh_follows_the_revision(arch, tmp_path):
    other = Archive(make_settings(tmp_path))  # e.g. the worker
    store.save(arch.conn, arch.base_settings, {"trash_retention_days": 60})
    assert other.settings.trash_retention_days == 30
    assert other.refresh_settings() and other.settings.trash_retention_days == 60
    assert not other.refresh_settings()
    other.close()


def test_exports_do_not_contain_stored_secrets(arch, tmp_path):
    from heftig.maintenance import export_archive

    store.save(arch.conn, arch.base_settings, {"imap_password": "geheim-1234", "imap_user": "me",
               "classify_headers": "x-session: geheim-1234"})  # fmt: skip
    out = export_archive(arch, tmp_path / "exports", as_zip=False)
    for f in out.rglob("*"):
        if f.is_file():
            assert b"geheim-1234" not in f.read_bytes(), f


def test_additional_headers_are_checked_and_never_echoed(arch):
    with pytest.raises(store.SettingsError, match="classify_headers") as e:
        store.save(arch.conn, arch.base_settings, {"classify_headers": "Bearer geheim-1234"})
    assert "geheim-1234" not in str(e.value) and store.stored(arch.conn) == {}
    with pytest.raises(store.SettingsError, match="Host"):
        store.save(arch.conn, arch.base_settings, {"classify_headers": "Host: evil.example"})
    s = store.save(arch.conn, arch.base_settings, {"classify_headers": "x-session: 1"})
    assert s.provider_headers("classify") == {"x-session": "1"}
    assert "x-session" not in repr(s) and "1" not in str(s.classify_headers)
    s = store.save(arch.conn, arch.base_settings, {"classify_headers": ""})  # kept
    assert s.provider_headers("classify") == {"x-session": "1"}
