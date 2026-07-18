"""HTTP transport tests for brainctl-mcp-http.

Uses Starlette's ``TestClient`` (which wraps ``httpx`` but is
synchronous and drives the ASGI lifespan itself) so the test matrix
doesn't depend on ``pytest-asyncio``, ``httpx``, or ``asgi-lifespan``
being installed beyond the base ``[mcp]`` extra.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

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


@pytest.fixture
def client() -> Any:
    """Starlette TestClient with the HTTP app lifespan managed."""
    app = create_app(_make_config())
    with TestClient(app) as c:
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


def test_config_missing_allowlist_defaults_to_visible_v2_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-2.8.1 this was a boot error. The relaxation defaults to the
    full visible v2 surface so operators can run brainctl-mcp-http with
    just `BRAINCTL_HTTP_TOKEN` set (the common case — Grok and other
    remote-MCP clients narrow further on their side)."""
    from agentmemory.mcp_server import _VISIBLE_TOOL_NAMES

    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", _VALID_TOKEN)
    monkeypatch.delenv("BRAINCTL_HTTP_ALLOWED_TOOLS", raising=False)
    cfg = HTTPConfig.from_env()
    assert cfg.allowed_tools == frozenset(_VISIBLE_TOOL_NAMES)
    assert len(cfg.allowed_tools) >= 98  # visible v2 surface (offline-core: wallet tools stripped)


def test_config_empty_allowlist_also_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BRAINCTL_HTTP_ALLOWED_TOOLS=` (empty string) and whitespace-only
    should resolve like unset, not raise. This avoids the gotcha where a
    deploy template leaves the var declared but empty."""
    from agentmemory.mcp_server import _VISIBLE_TOOL_NAMES

    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("BRAINCTL_HTTP_ALLOWED_TOOLS", "   ")
    cfg = HTTPConfig.from_env()
    assert cfg.allowed_tools == frozenset(_VISIBLE_TOOL_NAMES)


def test_config_rejects_v1_deprecated_tool_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale allowlist containing v1-deprecated names like `lc_fire`
    would silently shrink the wire surface to zero (v1 names are
    callable but not visible). Hard-fail at boot with a migration hint
    instead — same contract as stdio's BRAINCTL_ALLOWED_TOOLS."""
    from agentmemory import mcp_server

    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", _VALID_TOKEN)
    deprecated_sample = next(iter(mcp_server._V2_DEPRECATED))
    monkeypatch.setenv("BRAINCTL_HTTP_ALLOWED_TOOLS", deprecated_sample)
    with pytest.raises(ValueError) as exc:
        HTTPConfig.from_env()
    msg = str(exc.value)
    assert deprecated_sample in msg
    assert "deprecated" in msg.lower()
    assert "TOOL_MIGRATION_V2" in msg


def test_config_rejects_unknown_tool_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINCTL_HTTP_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("BRAINCTL_HTTP_ALLOWED_TOOLS", "not_a_real_tool")
    with pytest.raises(ValueError, match="not_a_real_tool"):
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


def test_health_ok_without_auth(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert r.status_code == 401


def test_wrong_auth_returns_401(client: TestClient) -> None:
    headers = _auth() | {"Authorization": "Bearer wrong-token"}
    r = client.post(
        "/mcp",
        headers=headers,
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Tools list filtering
# ---------------------------------------------------------------------------


def test_tools_list_is_filtered_to_allowlist(client: TestClient) -> None:
    r = client.post(
        "/mcp",
        headers=_auth(),
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    tools = payload.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    assert names == {"memory_search"}, (
        f"tools/list should expose only the allowlisted set, got {names}"
    )


# ---------------------------------------------------------------------------
# Tools call allowlist
# ---------------------------------------------------------------------------


def test_tools_call_non_allowlisted_rejected(client: TestClient) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "stats", "arguments": {}},
    }
    r = client.post("/mcp", headers=_auth(), content=json.dumps(body))
    assert r.status_code == 200
    payload = r.json()
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32601
    assert "stats" in payload["error"]["message"]


def test_tools_call_allowlisted_dispatches(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an allowlisted tool, the HTTP bridge forwards to the existing
    MCP dispatcher. We patch the in-mcp_server ``tool_memory_search`` so
    the request succeeds deterministically."""
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
    r = client.post("/mcp", headers=_auth(), content=json.dumps(body))
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["id"] == 42
    assert "error" not in payload, payload
    content = payload["result"]["content"]
    assert content, payload
    assert content[0]["type"] == "text"
    echoed = json.loads(content[0]["text"])
    assert echoed["results"][0]["content"] == "fixture row"


# ---------------------------------------------------------------------------
# Parse / body-size edges
# ---------------------------------------------------------------------------


def test_malformed_json_returns_parse_error(client: TestClient) -> None:
    r = client.post("/mcp", headers=_auth(), content=b"{not json")
    assert r.status_code == 200
    payload = r.json()
    assert payload["error"]["code"] == -32700


def test_body_over_one_mib_returns_413(client: TestClient) -> None:
    oversize = b"x" * (mcp_http._BODY_CAP_BYTES + 1)
    r = client.post("/mcp", headers=_auth(), content=oversize)
    assert r.status_code == 413
