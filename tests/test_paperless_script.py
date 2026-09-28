"""contrib/paperless/heftig_to_paperless.py against a real Heftig export and a mocked API."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from heftig import maintenance

from .corpus import load_corpus

SCRIPT = Path(__file__).resolve().parent.parent / "contrib" / "paperless" / "heftig_to_paperless.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("heftig_to_paperless", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module to be registered
    spec.loader.exec_module(mod)
    return mod


h2p = _load_script()


@pytest.fixture
def export_dir(archive, tmp_path):
    ids = load_corpus(archive)
    path = maintenance.export_archive(archive, tmp_path / "exp")
    return path, ids


class FakePaperless:
    """Just enough of the Paperless-ngx REST API for the script."""

    def __init__(self, known_md5=(), correspondents=(), document_types=(), tags=()):
        self.known_md5 = set(known_md5)
        self.terms = {
            "correspondents": {n: i for i, n in enumerate(correspondents, 1)},
            "document_types": {n: i for i, n in enumerate(document_types, 1)},
            "tags": {n: i for i, n in enumerate(tags, 1)},
        }
        self.created: list[tuple[str, dict]] = []
        self.uploads: list[bytes] = []
        self.auth: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        parts = request.url.path.strip("/").split("/")  # ["api", "<kind>", ...]
        kind = parts[1]
        if kind == "documents" and len(parts) == 2 and request.method == "GET":
            md5 = request.url.params["checksum__iexact"]
            n = 1 if md5.lower() in self.known_md5 else 0
            return httpx.Response(200, json={"count": n, "results": [{"id": 99}] * n})
        if kind == "documents" and parts[2:] == ["post_document"]:
            self.uploads.append(request.read())
            return httpx.Response(200, json=f"task-{len(self.uploads)}")
        if kind in self.terms and request.method == "GET":
            name = request.url.params["name__iexact"]
            hits = [
                {"id": i, "name": n}
                for n, i in self.terms[kind].items()
                if n.casefold() == name.casefold()
            ]
            return httpx.Response(200, json={"count": len(hits), "results": hits})
        if kind in self.terms and request.method == "POST":
            body = json.loads(request.content)
            new_id = 100 + sum(len(t) for t in self.terms.values())
            self.terms[kind][body["name"]] = new_id
            self.created.append((kind, body))
            return httpx.Response(201, json={"id": new_id, **body})
        return httpx.Response(404, json={"detail": "not found"})


def _field(body: bytes, name: str) -> list[str]:
    """Values of a multipart form field (enough for these simple payloads)."""
    out = []
    marker = f'name="{name}"'.encode()
    for part in body.split(b"\r\n--"):
        head, _, value = part.partition(b"\r\n\r\n")
        if marker in head and b"filename=" not in head:
            out.append(value.rstrip(b"\r\n").decode())
    return out


def test_plan_maps_heftig_fields(export_dir):
    root, ids = export_dir
    metas = {m["id"]: m for m in h2p.read_export(root)}
    plan = h2p.plan_document(root, metas[ids["telekom_2026_09.pdf"]])
    assert plan.title == "Mobilfunkrechnung September 2026"
    assert plan.created == "2026-09-03"
    assert plan.correspondent == "Telekom Deutschland GmbH"
    assert plan.document_type == "Rechnung"
    assert plan.tags == ["Telefon"]
    original = Path(plan.path).read_bytes()
    assert plan.md5 == hashlib.md5(original, usedforsecurity=False).hexdigest()
    assert plan.not_mapped["heftig_id"] == ids["telekom_2026_09.pdf"]
    assert plan.not_mapped["received_at"]
    assert plan.not_mapped["custom_fields"]["Betrag"]["currency"] == "EUR"


def test_dry_run_prints_plans_and_sends_nothing(export_dir, capsys, monkeypatch):
    root, ids = export_dir
    monkeypatch.delenv("PAPERLESS_URL", raising=False)

    def no_network(request):
        raise AssertionError("dry run must not send requests")

    rc = h2p.main([str(root), "--dry-run"], transport=httpx.MockTransport(no_network))
    assert rc == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert {ln["heftig_id"] for ln in lines} == set(ids.values())
    assert all(ln["action"] == "upload" for ln in lines)


def test_upload_creates_missing_terms_and_skips_known_documents(export_dir, monkeypatch, capsys):
    root, ids = export_dir
    metas = {m["id"]: m for m in h2p.read_export(root)}
    already = h2p.plan_document(root, metas[ids["tickets.pdf"]]).md5
    api = FakePaperless(
        known_md5=[already], correspondents=["telekom deutschland gmbh"], tags=["Telefon"]
    )
    monkeypatch.setenv("PAPERLESS_URL", "https://paperless.example.org/")
    monkeypatch.setenv("PAPERLESS_TOKEN", "test-token")

    rc = h2p.main([str(root)], transport=httpx.MockTransport(api))
    assert rc == 0
    assert api.auth == {"Token test-token"}
    assert len(api.uploads) == len(ids) - 1  # tickets.pdf was already known
    # existing correspondent (case-insensitive) reused, missing types created once each
    created_kinds = [k for k, _ in api.created]
    assert "correspondents" not in [k for k, b in api.created if b["name"].startswith("Telekom")]
    uploaded_types = {
        m["document_type"]
        for i, m in metas.items()
        if m["document_type"] and i != ids["tickets.pdf"]
    }
    assert created_kinds.count("document_types") == len(uploaded_types)
    assert all(b["matching_algorithm"] == 0 for _, b in api.created)

    telekom = next(u for u in api.uploads if b"Mobilfunkrechnung September 2026" in u)
    assert _field(telekom, "created") == ["2026-09-03"]
    assert _field(telekom, "correspondent") == ["1"]
    assert _field(telekom, "tags") == ["1"]
    assert b'filename="telekom_2026_09.pdf"' in telekom

    # second run: everything is known now -> nothing uploaded again
    api.known_md5 |= {h2p.plan_document(root, m).md5 for m in metas.values()}
    before = len(api.uploads)
    assert h2p.main([str(root)], transport=httpx.MockTransport(api)) == 0
    assert len(api.uploads) == before


def test_corrupt_original_is_reported_not_uploaded(export_dir, monkeypatch):
    root, ids = export_dir
    meta = next(m for m in h2p.read_export(root) if m["id"] == ids["tickets.pdf"])
    orig = root / meta["original_relpath"]
    orig.chmod(0o600)
    orig.write_bytes(b"%PDF-1.4 tampered")
    api = FakePaperless()
    monkeypatch.setenv("PAPERLESS_URL", "https://paperless.example.org")
    monkeypatch.setenv("PAPERLESS_TOKEN", "test-token")
    report = h2p.migrate(
        root,
        h2p.PaperlessClient(
            "https://paperless.example.org", "t", transport=httpx.MockTransport(api)
        ),
        dry_run=False,
    )
    assert any("SHA-256" in e for e in report["errors"])
    assert report["uploaded"] == len(ids) - 1


def test_missing_credentials_and_bad_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERLESS_URL", raising=False)
    monkeypatch.delenv("PAPERLESS_TOKEN", raising=False)
    assert h2p.main([str(tmp_path)]) == 2
    assert h2p.main([str(tmp_path), "--dry-run"]) == 1  # no metadata.jsonl
