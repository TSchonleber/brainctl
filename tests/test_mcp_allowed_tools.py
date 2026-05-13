"""Tests for BRAINCTL_ALLOWED_TOOLS (issue #114).

The stdio MCP server can be limited to a subset of its 201 tools via
the BRAINCTL_ALLOWED_TOOLS env var. Required for clients like Google's
Antigravity IDE that enforce a hard 100-tool MCP cap. Unset env =
backward-compatible behaviour (full surface exposed).

Unknown tool names in the env var are a HARD ERROR at process start —
not a silent skip — so a typo can't cause an invisible misconfiguration.

These tests exercise the pure resolver (`_resolve_allowed_tools`) and
patch the module-level `_ALLOWED_TOOLS` for the filter tests. We
deliberately avoid `importlib.reload(mcp_server)` because it disturbs
function identity for other tests in the suite.
"""
from __future__ import annotations

import pytest


pytest.importorskip("mcp", reason="brainctl[mcp] required for stdio server tests")
from agentmemory import mcp_server  # noqa: E402


class TestResolveAllowedTools:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("BRAINCTL_ALLOWED_TOOLS", raising=False)
        assert mcp_server._resolve_allowed_tools() is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "")
        assert mcp_server._resolve_allowed_tools() is None

    def test_whitespace_only_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "   ,   ")
        assert mcp_server._resolve_allowed_tools() is None

    def test_valid_names_pass_through(self, monkeypatch):
        monkeypatch.setenv(
            "BRAINCTL_ALLOWED_TOOLS", "memory_add,memory_search,stats"
        )
        result = mcp_server._resolve_allowed_tools()
        assert result == frozenset({"memory_add", "memory_search", "stats"})

    def test_trims_whitespace(self, monkeypatch):
        monkeypatch.setenv(
            "BRAINCTL_ALLOWED_TOOLS",
            " memory_add , memory_search ,  stats  ",
        )
        result = mcp_server._resolve_allowed_tools()
        assert result == frozenset({"memory_add", "memory_search", "stats"})

    def test_unknown_name_hard_exits(self, monkeypatch):
        monkeypatch.setenv(
            "BRAINCTL_ALLOWED_TOOLS", "memory_add,not_a_real_tool"
        )
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert "not_a_real_tool" in msg
        assert "BRAINCTL_ALLOWED_TOOLS" in msg

    def test_typo_gets_did_you_mean_suggestion(self, monkeypatch):
        """memory-add (hyphen) should suggest memory_add (underscore)."""
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "memory-add")
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert "memory-add" in msg
        assert "memory_add" in msg
        assert "did you mean" in msg.lower()

    def test_no_close_match_reported(self, monkeypatch):
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "xyzqwerty_no_match_at_all")
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert "no close match" in msg


class TestListToolsFiltering:
    @pytest.mark.asyncio
    async def test_unset_returns_full_surface(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", None)
        tools = await mcp_server.list_tools()
        assert len(tools) == len(mcp_server.TOOLS)

    @pytest.mark.asyncio
    async def test_allowlist_filters_surface(self, monkeypatch):
        allowlist = frozenset({"memory_add", "memory_search", "event_add", "stats"})
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", allowlist)
        tools = await mcp_server.list_tools()
        names = {t.name for t in tools}
        assert names == allowlist

    @pytest.mark.asyncio
    async def test_antigravity_subset_fits_under_100_cap(self, monkeypatch):
        antigravity_set = frozenset({
            "memory_add", "memory_search", "search", "event_add",
            "event_search", "entity_create", "entity_get", "entity_observe",
            "entity_relate", "entity_search", "decision_add", "handoff_add",
            "handoff_latest", "handoff_consume", "trigger_create",
            "trigger_list", "trigger_check", "stats", "agent_orient",
            "agent_wrap_up", "validate", "lint",
        })
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", antigravity_set)
        tools = await mcp_server.list_tools()
        assert len(tools) <= 100
        assert len(tools) == 22


class TestCallToolGating:
    @pytest.mark.asyncio
    async def test_disallowed_call_raises(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server, "_ALLOWED_TOOLS", frozenset({"memory_add"})
        )
        with pytest.raises(ValueError) as exc_info:
            await mcp_server.call_tool("stats", {})
        msg = str(exc_info.value)
        assert "stats" in msg
        assert "BRAINCTL_ALLOWED_TOOLS" in msg

    @pytest.mark.asyncio
    async def test_allowed_call_passes_gate(self, monkeypatch):
        """Calling an allowed tool must NOT be rejected by the gate.
        The handler may still error for its own reasons (DB missing,
        invalid args, etc.) but the error must not mention the allowlist
        env var."""
        monkeypatch.setattr(
            mcp_server, "_ALLOWED_TOOLS", frozenset({"stats"})
        )
        try:
            await mcp_server.call_tool("stats", {})
        except ValueError as e:
            assert "BRAINCTL_ALLOWED_TOOLS" not in str(e)
        except Exception:
            pass  # other errors (DB env, etc.) are out of scope

    @pytest.mark.asyncio
    async def test_unset_allowlist_does_not_gate(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", None)
        try:
            await mcp_server.call_tool("stats", {})
        except ValueError as e:
            assert "BRAINCTL_ALLOWED_TOOLS" not in str(e)
        except Exception:
            pass


class TestKnownToolNames:
    def test_module_exports_known_tool_set(self):
        assert hasattr(mcp_server, "_ALL_TOOL_NAMES")
        assert isinstance(mcp_server._ALL_TOOL_NAMES, frozenset)
        assert "memory_add" in mcp_server._ALL_TOOL_NAMES
        assert "stats" in mcp_server._ALL_TOOL_NAMES
        # Should reflect the actual 201-tool surface.
        assert len(mcp_server._ALL_TOOL_NAMES) == len(mcp_server.TOOLS)
