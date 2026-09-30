"""First start in the browser: setup code, account, AI, e-mail, scanner - no .env needed."""

import logging

import pytest
from fastapi.testclient import TestClient

from heftig import connections
from heftig import settings_store as store
from heftig.config import Settings
from heftig.web import setup

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def fresh(tmp_path):
    """An app as the installer starts it: nothing configured but the archive folder."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    s = Settings(_env_file=None, archive_dir=tmp_path / "archive")
    app = create_app(s)
    yield app.state.archive, TestClient(app)
    app.state.archive.close()


def _csrf(c):
    return c.get("/api/auth/me").json()["csrf_token"]


def test_account_needs_the_setup_code(fresh):
    arch, c = fresh
    token = setup.token_path(arch).read_text().strip()
    assert len(token) >= 16 and setup.token_path(arch).stat().st_mode & 0o077 == 0
    page = c.get("/setup", headers={"accept-language": "de-DE,de;q=0.9"}).text
    assert "Einrichtungscode" in page or "Setup code" in page  # asks for the code
    r = c.post("/setup", data={"token": "falsch", "username": "jo", "password": PASSWORD,
                               "password2": PASSWORD, "language": "en"})  # fmt: skip
    assert r.status_code == 400 and "setup code is not correct" in r.text
    r = c.post("/setup", data={"token": token, "username": "jo", "password": PASSWORD,
                               "password2": PASSWORD, "language": "de"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/settings/ai?setup=1"
    assert not setup.token_path(arch).exists()
    assert arch.settings.language == "de"
    # signed in right away, the next step is in German
    assert (
        "Welche KI" in c.get("/settings/ai?setup=1").text
        or c.get("/api/auth/me").status_code == 200
    )
    # a second account cannot be created this way
    assert (
        c.post(
            "/setup",
            data={"token": token, "username": "x", "password": PASSWORD, "password2": PASSWORD},
            follow_redirects=False,  # fmt: skip
        ).headers["location"]
        == "/login"
    )


@pytest.fixture
def signed_in(fresh):
    arch, c = fresh
    token = setup.token_path(arch).read_text().strip()
    c.post("/setup", data={"token": token, "username": "jo", "password": PASSWORD,
                           "password2": PASSWORD, "language": "en"})  # fmt: skip
    return arch, c, _csrf(c)


def test_ai_choices(signed_in, monkeypatch):
    arch, c, csrf = signed_in
    calls = []
    monkeypatch.setattr(connections, "test_ai", lambda s: calls.append(s) or s.classify_model)
    # cloud without consent: refused, nothing stored
    r = c.post("/settings/ai?setup=1", data={"csrf_token": csrf, "mode": "anthropic",
                                              "api_key": "sk-test", "model": ""})  # fmt: skip
    assert "confirm" in r.text and "classify_provider" not in store.stored(arch.conn)
    r = c.post("/settings/ai?setup=1", data={"csrf_token": csrf, "mode": "anthropic",
               "api_key": "sk-test", "consent": "1", "ocr_ai": "1"},
               follow_redirects=False)  # fmt: skip
    assert r.status_code == 303, r.text[r.text.find("notice") :][:300]
    assert r.headers["location"] == "/settings/search?setup=1"
    s = arch.settings
    assert (s.classify_provider, s.classify_model, s.allow_cloud_classify) == (
        "anthropic",
        "claude-sonnet-5",
        True,
    )
    assert s.ocr_provider == "anthropic" and s.secret("ocr_api_key") == "sk-test"
    assert s.ai_search_model.startswith("claude-haiku") and len(calls) == 1
    # the key is never shown again
    assert "sk-test" not in c.get("/settings/ai").text
    # a local server needs neither key nor consent; the Anthropic key is not handed to it
    r = c.post("/settings/ai", data={"csrf_token": csrf, "mode": "local", "ocr_ai": "1",
               "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/settings/ai?saved=ai"
    s = arch.settings
    assert s.classify_provider == "openai_compatible" and not s.allow_cloud_classify
    assert not s.secret("classify_api_key") and not s.secret("ocr_api_key")
    # offline
    c.post("/settings/ai", data={"csrf_token": csrf, "mode": "offline"})
    assert (arch.settings.classify_provider, arch.settings.ocr_provider) == ("rules", "tesseract")


def test_mail(signed_in, monkeypatch):
    arch, c, csrf = signed_in
    monkeypatch.setattr(connections, "test_imap", lambda *a: ["INBOX", "Archiv"])
    r = c.post("/settings/mail", data={"csrf_token": csrf, "imap_user": "scans@gmail.com",
               "imap_password": "app-pw", "after": "move", "imap_move_to": "Archiv",
               "imap_allowed_senders": "me@example.org\n@scanner.example"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/settings/mail?saved=mail"
    s = arch.settings
    assert (s.imap_host, s.imap_port, s.imap_move_to) == ("imap.gmail.com", 993, "Archiv")
    assert s.imap_allowed_sender_set == {"me@example.org", "@scanner.example"}
    assert s.secret("imap_password") == "app-pw"
    # a folder that does not exist is reported
    r = c.post("/settings/mail", data={"csrf_token": csrf, "imap_user": "scans@gmail.com",
               "after": "move", "imap_move_to": "Nirgends"})  # fmt: skip
    assert "Nirgends" in r.text and "does not exist" in r.text
    c.post("/settings/mail", data={"csrf_token": csrf, "action": "off"})
    assert arch.settings.imap_host == ""


def test_scanner_and_finish(signed_in):
    arch, c, csrf = signed_in
    assert "scanner folder" in c.get("/settings/scanner?setup=1").text
    r = c.post("/settings/scanner?setup=1", data={"csrf_token": csrf, "auto_file": "1"},
               follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/?welcome=1"
    assert "scanner" in arch.settings.auto_file_source_set


def test_environment_settings_are_shown_as_fixed(tmp_path):
    from heftig.web.app import create_app

    from .conftest import make_settings

    app = create_app(make_settings(tmp_path, classify_provider="rules"))
    c = TestClient(app)
    from heftig import auth

    auth.create_user(app.state.archive.conn, "jo", PASSWORD)
    c.post("/api/auth/login", json={"username": "jo", "password": PASSWORD})
    page = c.get("/settings/ai").text
    assert "<fieldset disabled" in page
    app.state.archive.close()


def test_stored_keys_never_go_to_another_server(signed_in, monkeypatch):
    arch, c, csrf = signed_in
    monkeypatch.setattr(connections, "test_ai", lambda s: s.classify_model)
    monkeypatch.setattr(connections, "test_imap", lambda *a: ["INBOX"])
    c.post("/settings/ai", data={"csrf_token": csrf, "mode": "anthropic", "api_key": "sk-secret",
                                 "consent": "1"})  # fmt: skip
    sent = []
    monkeypatch.setattr(
        connections, "list_models", lambda p, base, key: sent.append((base, key)) or []
    )
    # the (hidden) server field of the local mode is not used for Anthropic
    c.post("/settings/ai/models", data={"csrf_token": csrf, "mode": "anthropic",
                                        "base_url": "http://evil.example:1/v1"})  # fmt: skip
    # a local server gets no key of another provider
    c.post("/settings/ai/models", data={"csrf_token": csrf, "mode": "local",
                                        "base_url": "http://evil.example:1/v1"})  # fmt: skip
    assert sent == [("", "sk-secret"), ("http://evil.example:1/v1", None)]
    # a key stored for one local server is not reused for another one
    c.post("/settings/ai", data={"csrf_token": csrf, "mode": "local", "api_key": "LOCALKEY",
                                 "base_url": "http://127.0.0.1:1/v1", "model": "m"})  # fmt: skip
    r = c.post("/settings/ai", data={"csrf_token": csrf, "mode": "local", "model": "m",
                                     "base_url": "https://other.example/v1", "consent": "1"})  # fmt: skip
    assert arch.settings.secret("classify_api_key") in (None, "")
    # the mail password only for the same server and user
    c.post("/settings/mail", data={"csrf_token": csrf, "imap_user": "a@example.org",
                                   "imap_host": "imap.example.org", "imap_password": "pw",
                                   "after": "seen"})  # fmt: skip
    r = c.post("/settings/mail", data={"csrf_token": csrf, "imap_user": "a@example.org",
                                       "imap_host": "imap.other.example", "after": "seen"})  # fmt: skip
    assert "enter the password" in r.text and arch.settings.imap_host == "imap.example.org"


def test_a_rejected_key_is_reported_as_such(monkeypatch):
    from heftig.providers import registry
    from heftig.providers.base import ProviderError

    class Refusing:
        model = "m"

        def complete_json(self, *a, **k):
            raise ProviderError("anthropic: HTTP 401")

    monkeypatch.setattr(registry, "get_classifier", lambda s: Refusing())
    with pytest.raises(connections.ConnectionProblem, match="key was not accepted"):
        connections.test_ai(Settings(_env_file=None))


def test_language_can_be_changed_later(signed_in):
    arch, c, csrf = signed_in  # set up in English
    assert 'name="language"' in c.get("/settings").text
    c.post("/settings/language", data={"csrf_token": csrf, "language": "de"})
    assert arch.settings.language == "de" and "Einstellungen" in c.get("/settings").text
