"""MCP server (read-only access for Claude) and the analysis endpoints behind it."""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from heftig import auth

from .conftest import make_settings
from .corpus import load_corpus

pytest.importorskip("mcp")

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def env(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from heftig.web.app import create_app

    app = create_app(make_settings(tmp_path))
    a = app.state.archive
    uid = auth.create_user(a.conn, "jo", PASSWORD)
    uid = uid if isinstance(uid, int) else a.conn.execute("SELECT id FROM users").fetchone()[0]
    corpus = load_corpus(a)
    _, read_token = auth.create_api_token(a.conn, uid, "Claude", "read")
    _, full_token = auth.create_api_token(a.conn, uid, "App")
    yield app, corpus, read_token, full_token
    a.close()


def client_for(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_read_only_token_can_read_but_not_write(env):
    app, corpus, read_token, full_token = env
    c = client_for(app, read_token)
    doc = corpus["telekom_2026_09.pdf"]
    assert c.get(f"/api/documents/{doc}").status_code == 200
    r = c.patch(f"/api/documents/{doc}", json={"title": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "read_only"
    assert c.delete(f"/api/documents/{doc}?confirm={doc}").status_code == 403
    assert c.post("/api/searches", json={"name": "a", "query": "q=a"}).status_code == 403
    w = client_for(app, full_token)
    assert w.patch(f"/api/documents/{doc}", json={"title": "Neu"}).status_code == 200
    scopes = {t["name"]: t["scope"] for t in auth.list_tokens(app.state.archive.conn)}
    assert scopes == {"Claude": "read", "App": "full"}


def test_lines_endpoint(env):
    app, corpus, read_token, _ = env
    c = client_for(app, read_token)
    r = c.get("/api/lines", params={"pattern": "kundennummer",
                                    "correspondent": "Telekom Deutschland GmbH"}).json()  # fmt: skip
    assert r["documents_matched"] == 2 and r["total_matches"] == 2
    m = r["matches"][0]
    assert m["page"] == 1 and "5566778899" in m["line"] and m["correspondent"].startswith("Telekom")
    # umlauts/case do not matter; date filters apply; context lines
    r = c.get("/api/lines", params={"pattern": "RECHNUNGSBETRAG", "date_from": "2026-09",
                                    "context": 1}).json()  # fmt: skip
    assert r["total_matches"] == 1 and "39,95" in r["matches"][0]["line"]
    assert r["matches"][0]["before"]
    # regular expressions for variants
    r = c.get("/api/lines", params={"pattern": r"betrag:\s*\d+,\d\d", "regex": "true"}).json()
    assert r["total_matches"] >= 2
    assert c.get("/api/lines", params={"pattern": "(", "regex": "true"}).status_code == 422
    # parts of compound words count: "nummer" finds "Kundennummer", "betrag" "Rechnungsbetrag"
    r = c.get("/api/lines", params={"pattern": "nummer",
                                    "correspondent": "Telekom Deutschland GmbH"}).json()  # fmt: skip
    assert r["documents_matched"] == 2 and "5566778899" in r["matches"][0]["line"]
    r = c.get("/api/lines", params={"pattern": "betrag", "date_from": "2026-09"}).json()
    assert r["total_matches"] >= 1 and "39,95" in r["matches"][0]["line"]


def test_fields_endpoint(env):
    app, corpus, read_token, _ = env
    c = client_for(app, read_token)
    keys = {f["key"] for f in c.get("/api/fields").json()["fields"]}
    assert {"Betrag", "Vertragsnummer"} <= keys
    vals = c.get(
        "/api/fields", params={"key": "betrag", "correspondent": "Telekom Deutschland GmbH"}
    ).json()
    assert sorted(v["number"] for v in vals["values"]) == [39.95, 44.95]
    assert {v["currency"] for v in vals["values"]} == {"EUR"}


def _server(app, token):
    from heftig.mcp_server import HeftigClient, build_server

    hc = HeftigClient("http://testserver", token, "https://heftig.example")
    hc.http = client_for(app, token)
    return build_server(hc)


def call(server, name, **args):
    res = asyncio.run(server.call_tool(name, args))
    assert not res.is_error, res.content
    return res


def data(res):
    return json.loads(res.content[0].text)


def test_mcp_tools(env):
    app, corpus, read_token, _ = env
    s = _server(app, read_token)
    tools = {t.name: t for t in asyncio.run(s.list_tools())}
    assert set(tools) == {
        "archive_overview", "search_documents", "find_text", "field_values", "get_document",
        "get_page_image", "similar_documents",
    }  # fmt: skip
    assert all(t.annotations.read_only_hint for t in tools.values())

    ov = data(call(s, "archive_overview"))
    assert ov["documents"] == len(corpus)
    assert any(c["name"] == "Telekom Deutschland GmbH" for c in ov["correspondents"])
    assert ov["years"]["2025"] >= 5 and any(f["key"] == "Betrag" for f in ov["custom_fields"])
    # within filters, every count stays within them (no other years or senders listed)
    ov = data(call(s, "archive_overview", date_from="2026", date_to="2026"))
    assert set(ov["years"]) == {"2026"} and sum(ov["years"].values()) == ov["documents"]
    ov = data(call(s, "archive_overview", correspondent=["Telekom Deutschland GmbH"]))
    assert [c["name"] for c in ov["correspondents"]] == ["Telekom Deutschland GmbH"]

    res = data(call(s, "search_documents", query="Vodafone letztes Jahr", limit=5))
    assert res["total"] == 2 and res["date_filter_from_query"]
    assert res["documents"][0]["url"].startswith("https://heftig.example/documents/")

    lines = data(call(s, "find_text", pattern="Rechnungsbetrag", document_type=["Rechnung"]))
    assert lines["total_matches"] == 2 and "#page-1" in lines["matches"][0]["url"]

    fv = data(call(s, "field_values", key="Vertragsnummer"))
    assert fv["values"][0]["value"] == "83729381"

    with pytest.raises(Exception, match="Invalid document ID"):  # never reaches a URL
        asyncio.run(s.call_tool("get_document", {"document_id": "../status"}))

    doc = corpus["telekom_2026_09.pdf"]
    d = data(call(s, "get_document", document_id=doc, pages="1"))
    assert d["correspondent"] == "Telekom Deutschland GmbH" and d["pages"][0]["page"] == 1
    assert "MagentaMobil" in d["pages"][0]["text"]

    img = call(s, "get_page_image", document_id=doc, page=1, width=500)
    assert img.content[0].type == "image" and img.content[0].mime_type == "image/webp"

    sim = data(call(s, "similar_documents", document_id=doc))
    assert sim["documents"][0]["id"] == corpus["telekom_2026_08.pdf"]


def test_mcp_errors_are_explained(env):
    from mcp.server.mcpserver.exceptions import ToolError

    app, corpus, read_token, _ = env
    # anticipated failures reach the model as readable tool errors (is_error + message)
    with pytest.raises(ToolError, match="API token"):
        asyncio.run(_server(app, "hft_wrong").call_tool("archive_overview", {}))
    missing = "00000000-0000-4000-8000-000000000000"
    with pytest.raises(ToolError, match="Not found"):
        asyncio.run(_server(app, read_token).call_tool("get_document", {"document_id": missing}))


def test_mcp_command_needs_a_token(monkeypatch, capsys):
    from heftig.mcp_server import run

    monkeypatch.delenv("HEFTIG_MCP_TOKEN", raising=False)
    monkeypatch.delenv("HEFTIG_MCP_TOKEN_FILE", raising=False)
    assert run("http://127.0.0.1:1") == 2
    assert "no API token" in capsys.readouterr().err
