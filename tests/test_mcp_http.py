"""HTTP transport tests for brainctl-mcp-http.

Exercises the Starlette app built by :func:`agentmemory.mcp_http.create_app`
against an in-process ``httpx.AsyncClient`` — no real network binding,
no real cross-encoder, no real stdio server. We stub the allowlisted
tool's handler so we can assert dispatch reached the existing MCP app
without duplicating its surface here.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager  # type: ignore[import-not-found]
from starlette.applications import Starlette

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory import mcp_http  # noqa: E402  — path bootstrap above
from agentmemory.mcp_http import HTTPConfig, create_app  # noqa: E402


_VALID_TOKEN = "x" * 48  # >= 32 chars


def _make_config(allowed: tuple[str, ...] = ("memory_search",)) -> HTTPConfig:
    return HTTPConfig(
        token=_VALID_TOKEN,
        allowed_tools=frozenset(allowed),
        host="127.0.0.1",
        port=8080,
        log_level="warning",
    )


@pytest_asyncio.fixture
async def http_app() -> AsyncIterator[Starlette]:
    """Starlette app with the MCP bridge brought up via lifespan."""
    app = create_app(_make_config())
    async with LifespanManager(app):
        yield app


@pytest_asyncio.fixture
async def client(http_app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


def _auth() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_VALID_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


# ---------------------------------------------------------------------------
# Config boot-time validation
# ---------------------------------------------------------------------------


def test_config_rejects_short_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", "too-short")
    monkeypatch.setenv("BRAINCTL_HTTP_ALLOWED_TOOLS", "memory_search")
    with pytest.raises(ValueError, match="at least 32"):
        HTTPConfig.from_env()


def test_config_rejects_missing_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", _VALID_TOKEN)
    monkeypatch.delenv("BRAINCTL_HTTP_ALLOWED_TOOLS", raising=False)
    with pytest.raises(ValueError, match="ALLOWED_TOOLS"):
        HTTPConfig.from_env()


def test_config_parses_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("BRAINCTL_HTTP_ALLOWED_TOOLS", " memory_search , entity_search ")
    monkeypatch.setenv("BRAINCTL_HTTP_PORT", "9000")
    cfg = HTTPConfig.from_env()
    assert cfg.allowed_tools == {"memory_search", "entity_search"}
    assert cfg.port == 9000


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ok_without_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_auth_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_auth_returns_401(client: httpx.AsyncClient) -> None:
    headers = _auth() | {"Authorization": "Bearer wrong-token"}
    r = await client.post(
        "/mcp",
        headers=headers,
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Tools list filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_is_filtered_to_allowlist(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/mcp",
        headers=_auth(),
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert r.status_code == 200, r.text
    # Streamable HTTP with json_response=True returns application/json.
    payload = r.json()
    tools = payload.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    assert names == {"memory_search"}, (
        f"tools/list should expose only the allowlisted set, got {names}"
    )


# ---------------------------------------------------------------------------
# Tools call allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_non_allowlisted_rejected(
    client: httpx.AsyncClient,
) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "stats", "arguments": {}},
    }
    r = await client.post("/mcp", headers=_auth(), content=json.dumps(body))
    assert r.status_code == 200
    payload = r.json()
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32601
    assert "stats" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_tools_call_allowlisted_dispatches(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an allowlisted tool, the HTTP bridge forwards to the existing
    MCP dispatcher. We don't call into real SQLite — we patch the
    in-mcp_server ``tool_memory_search`` so the request succeeds
    deterministically."""
    import agentmemory.mcp_server as server

    fake_result = {
        "ok": True,
        "results": [{"id": 1, "content": "fixture row"}],
        "total": 1,
    }

    def fake_tool_memory_search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return fake_result

    monkeypatch.setattr(server, "tool_memory_search", fake_tool_memory_search)

    body = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": "memory_search", "arguments": {"query": "x"}},
    }
    r = await client.post("/mcp", headers=_auth(), content=json.dumps(body))
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["id"] == 42
    assert "error" not in payload, payload
    # The MCP dispatcher wraps tool returns in a CallToolResult with
    # content entries — the first entry's text is the JSON we returned.
    content = payload["result"]["content"]
    assert content, payload
    assert content[0]["type"] == "text"
    echoed = json.loads(content[0]["text"])
    assert echoed["results"][0]["content"] == "fixture row"


# ---------------------------------------------------------------------------
# Parse / body-size edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_returns_parse_error(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post("/mcp", headers=_auth(), content=b"{not json")
    assert r.status_code == 200
    payload = r.json()
    assert payload["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_body_over_one_mib_returns_413(
    client: httpx.AsyncClient,
) -> None:
    oversize = b"x" * (mcp_http._BODY_CAP_BYTES + 1)
    r = await client.post("/mcp", headers=_auth(), content=oversize)
    assert r.status_code == 413
