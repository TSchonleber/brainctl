"""call_tool's agent_id fallback when the client omits it.

Several agents (e.g. Claude Code and Grok) can share one brain.db, each
running its own brainctl-mcp with BRAINCTL_AGENT_ID set in its MCP config.
A tool call without an explicit agent_id must be attributed to that
configured identity, not collapsed into the anonymous "mcp-client".
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="brainctl[mcp] required for stdio server tests")
from agentmemory import mcp_server  # noqa: E402


def _captured_agent_id(monkeypatch, arguments: dict) -> str:
    seen: dict = {}

    def fake_handoff_latest(agent_id="mcp-client", **kw):
        seen["agent_id"] = agent_id
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "tool_handoff_latest", fake_handoff_latest)
    monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", None)
    asyncio.run(mcp_server.call_tool("handoff_latest", dict(arguments)))
    return seen["agent_id"]


def test_env_identity_used_when_agent_id_omitted(monkeypatch):
    monkeypatch.setenv("BRAINCTL_AGENT_ID", "grok")
    assert _captured_agent_id(monkeypatch, {}) == "grok"


def test_explicit_agent_id_wins_over_env(monkeypatch):
    monkeypatch.setenv("BRAINCTL_AGENT_ID", "grok")
    assert _captured_agent_id(monkeypatch, {"agent_id": "claude"}) == "claude"


def test_falls_back_to_mcp_client_without_env(monkeypatch):
    monkeypatch.delenv("BRAINCTL_AGENT_ID", raising=False)
    assert _captured_agent_id(monkeypatch, {}) == "mcp-client"
