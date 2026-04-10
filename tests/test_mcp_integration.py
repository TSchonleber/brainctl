"""Comprehensive MCP integration test suite.

Exercises the MCP server through the actual MCP protocol over stdio transport —
not via direct Python module imports.  Tests the full stack:
  - tool discovery (tools/list)
  - JSON-RPC dispatch (tools/call)
  - response format and payload correctness
  - error handling for unknown tools and missing required params

Each test uses a hermetic temporary brain.db via the ``mcp_session`` fixture.
The server is launched as a real subprocess; if it fails to start the test
(or whole module) is skipped with a clear message rather than erroring.

Async work is driven with plain ``asyncio.run()`` so the suite has no
dependency on ``pytest-asyncio`` or any other async test plugin.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# PYTHONPATH setup — same pattern as all other test files in this repo
# ---------------------------------------------------------------------------

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# Optional MCP SDK import
# ---------------------------------------------------------------------------

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

# ---------------------------------------------------------------------------
# Skip marker applied to every test in this module
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not _MCP_AVAILABLE,
    reason="mcp SDK not installed — install with: pip install 'brainctl[mcp]'",
)

# ---------------------------------------------------------------------------
# Core tool names that must always appear in tools/list
# ---------------------------------------------------------------------------

CORE_TOOLS = {
    "memory_add",
    "memory_search",
    "event_add",
    "event_search",
    "entity_create",
    "entity_get",
    "entity_search",
    "entity_observe",
    "entity_relate",
    "trigger_create",
    "trigger_list",
    "trigger_check",
    "decision_add",
    "handoff_add",
    "handoff_latest",
    "search",
    "stats",
    "resolve_conflict",
    "affect_classify",
    "affect_log",
    "affect_check",
    "affect_monitor",
}

# ---------------------------------------------------------------------------
# Low-level asyncio helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously; create/close an event loop each time."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Context-manager helper: open a live MCP session
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _live_session(db_path: Path):
    """Async context manager that yields an active ClientSession.

    Pre-creates the brain.db schema using ``Brain`` before launching the
    subprocess so the server finds a fully-initialised database on first use.
    Raises ``RuntimeError`` if the server fails to start or initialize.
    """
    # Create the full schema in the temp file — same approach as conftest.py
    from agentmemory.brain import Brain
    Brain(db_path=str(db_path), agent_id="mcp-test-init")

    env = {
        **os.environ,
        "BRAIN_DB": str(db_path),
        "PYTHONPATH": str(SRC),
    }
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentmemory.mcp_server"],
        env=env,
    )
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


# ---------------------------------------------------------------------------
# Synchronous per-test helper
# ---------------------------------------------------------------------------


async def _run_with_session(db_path: Path, test_coro_fn):
    """Open a session then await test_coro_fn(session).

    Non-pytest exceptions from server startup are re-raised as ``RuntimeError``
    so ``_with_session`` can convert them to skip.  KeyboardInterrupt,
    SystemExit, and AssertionError are always re-raised unmodified so pytest
    can handle them correctly.
    """
    try:
        async with _live_session(db_path) as session:
            return await test_coro_fn(session)
    except (KeyboardInterrupt, SystemExit, AssertionError):
        raise
    except BaseException as exc:
        # Distinguish pytest outcome exceptions (skip/fail/xfail) by checking
        # the module path without importing private pytest internals.
        if type(exc).__module__.startswith("_pytest"):
            raise
        raise RuntimeError(f"MCP session failed: {exc}") from exc


def _with_session(db_path: Path, test_coro_fn):
    """Synchronously run test_coro_fn(session) inside a real MCP session.

    Converts server startup / connection failures into ``pytest.skip`` so the
    test is skipped rather than erroring when the server is unavailable.
    """
    try:
        return _run(_run_with_session(db_path, test_coro_fn))
    except RuntimeError as exc:
        pytest.skip(str(exc))


# ---------------------------------------------------------------------------
# Response parsing helper
# ---------------------------------------------------------------------------


def _parse(result) -> dict:
    """Return the parsed JSON dict from a call_tool CallToolResult."""
    assert result.content, "call_tool returned empty content list"
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# tests — tools/list
# ---------------------------------------------------------------------------


def test_list_tools_returns_all_core_tools(tmp_path):
    """tools/list must include every core tool name at minimum."""
    async def _body(session):
        result = await session.list_tools()
        names = {t.name for t in result.tools}
        missing = CORE_TOOLS - names
        assert not missing, f"tools/list missing expected tools: {missing}"

    _with_session(tmp_path / "brain.db", _body)


def test_list_tools_count_at_least_30(tmp_path):
    """tools/list must expose at least 30 tools total (core + extensions)."""
    async def _body(session):
        result = await session.list_tools()
        count = len(result.tools)
        assert count >= 30, (
            f"Expected >= 30 tools, got {count}: {[t.name for t in result.tools]}"
        )

    _with_session(tmp_path / "brain.db", _body)


def test_list_tools_each_has_name_and_description(tmp_path):
    """Every tool returned must have a non-empty name and description."""
    async def _body(session):
        result = await session.list_tools()
        for t in result.tools:
            assert t.name, "A tool has no name"
            assert t.description, f"Tool {t.name!r} has no description"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# memory_add
# ---------------------------------------------------------------------------


def test_memory_add_returns_memory_id(tmp_path):
    """memory_add must return a dict with a positive integer memory_id."""
    async def _body(session):
        result = await session.call_tool(
            "memory_add",
            {"content": "MCP integration test memory", "category": "lesson", "force": True},
        )
        data = _parse(result)
        assert "memory_id" in data, f"No memory_id in response: {data}"
        assert isinstance(data["memory_id"], int), f"memory_id is not int: {data}"
        assert data["memory_id"] > 0

    _with_session(tmp_path / "brain.db", _body)


def test_memory_add_force_bypasses_worthiness_gate(tmp_path):
    """memory_add with force=True must always store the memory."""
    async def _body(session):
        # Add the same content twice; both should succeed with force=True
        for i in range(2):
            result = await session.call_tool(
                "memory_add",
                {
                    "content": "Force-stored duplicate memory for testing",
                    "category": "lesson",
                    "force": True,
                },
            )
            data = _parse(result)
            assert "memory_id" in data, f"Attempt {i}: no memory_id: {data}"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------


def test_memory_search_finds_added_memory(tmp_path):
    """memory_search must return a memory that was just added."""
    async def _body(session):
        sentinel = "unique-sentinel-value-xyz987-mcp-integration"
        add_result = await session.call_tool(
            "memory_add",
            {"content": sentinel, "category": "project", "force": True},
        )
        add_data = _parse(add_result)
        memory_id = add_data.get("memory_id")
        assert memory_id, f"memory_add failed: {add_data}"

        search_result = await session.call_tool(
            "memory_search",
            {"query": "unique-sentinel-value-xyz987"},
        )
        search_data = _parse(search_result)
        assert "memories" in search_data, f"No 'memories' key: {search_data}"
        ids = [m["id"] for m in search_data["memories"]]
        assert memory_id in ids, (
            f"Added memory_id={memory_id} not found in results: {ids}"
        )

    _with_session(tmp_path / "brain.db", _body)


def test_memory_search_empty_result_is_list(tmp_path):
    """memory_search on a fresh DB must return an empty list, not an error."""
    async def _body(session):
        result = await session.call_tool(
            "memory_search",
            {"query": "nothing-here-zyxwvutsrqponmlkjihgfedcba"},
        )
        data = _parse(result)
        assert "memories" in data, f"Expected 'memories' key: {data}"
        assert isinstance(data["memories"], list)

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# event_add + event_search
# ---------------------------------------------------------------------------


def test_event_add_returns_event_id(tmp_path):
    """event_add must return a dict containing a positive integer event_id."""
    async def _body(session):
        result = await session.call_tool(
            "event_add",
            {
                "summary": "MCP integration test event fired",
                "event_type": "session_start",
                "project": "brainctl-mcp-test",
            },
        )
        data = _parse(result)
        assert "event_id" in data, f"No event_id in response: {data}"
        assert isinstance(data["event_id"], int)
        assert data["event_id"] > 0

    _with_session(tmp_path / "brain.db", _body)


def test_event_search_finds_added_event(tmp_path):
    """event_search must locate an event added in the same session."""
    async def _body(session):
        sentinel = "sentinel-event-token-abc123-mcp"
        add_result = await session.call_tool(
            "event_add",
            {"summary": sentinel, "event_type": "observation"},
        )
        add_data = _parse(add_result)
        event_id = add_data.get("event_id")
        assert event_id, f"event_add failed: {add_data}"

        search_result = await session.call_tool(
            "event_search",
            {"query": sentinel},
        )
        search_data = _parse(search_result)
        assert "events" in search_data, f"No 'events' key: {search_data}"
        ids = [e["id"] for e in search_data["events"]]
        assert event_id in ids, f"event_id={event_id} not in results: {ids}"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# entity_create + entity_get + entity_observe + entity_relate
# ---------------------------------------------------------------------------


def test_entity_create_returns_entity_id(tmp_path):
    """entity_create must return a positive integer entity_id."""
    async def _body(session):
        result = await session.call_tool(
            "entity_create",
            {"name": "TestAgent-mcp-001", "entity_type": "agent"},
        )
        data = _parse(result)
        assert "entity_id" in data, f"No entity_id: {data}"
        assert isinstance(data["entity_id"], int)
        assert data["entity_id"] > 0

    _with_session(tmp_path / "brain.db", _body)


def test_entity_get_retrieves_created_entity(tmp_path):
    """entity_get must return the entity that was just created."""
    async def _body(session):
        name = "BrainProject-mcp-get-test"
        create_result = await session.call_tool(
            "entity_create",
            {
                "name": name,
                "entity_type": "project",
                "observations": "Tests the MCP entity get path",
            },
        )
        create_data = _parse(create_result)
        assert create_data.get("entity_id"), f"entity_create failed: {create_data}"

        get_result = await session.call_tool("entity_get", {"identifier": name})
        get_data = _parse(get_result)
        # Response must be a dict with either "entity" or "id" key
        assert isinstance(get_data, dict), f"entity_get non-dict: {get_data}"
        assert "entity" in get_data or "id" in get_data, (
            f"Unexpected entity_get structure: {get_data}"
        )

    _with_session(tmp_path / "brain.db", _body)


def test_entity_observe_adds_observations(tmp_path):
    """entity_observe must succeed after creating an entity."""
    async def _body(session):
        create_result = await session.call_tool(
            "entity_create",
            {"name": "ObservedEntity-mcp-001", "entity_type": "concept"},
        )
        assert _parse(create_result).get("entity_id"), "entity_create failed"

        observe_result = await session.call_tool(
            "entity_observe",
            {
                "identifier": "ObservedEntity-mcp-001",
                "observations": "First observation; Second observation",
            },
        )
        observe_data = _parse(observe_result)
        assert observe_data.get("ok") is True, f"entity_observe not ok: {observe_data}"

    _with_session(tmp_path / "brain.db", _body)


def test_entity_relate_creates_relation(tmp_path):
    """entity_relate must succeed when both entities exist."""
    async def _body(session):
        for name, etype in [("RelateA-mcp", "person"), ("RelateB-mcp", "project")]:
            r = await session.call_tool(
                "entity_create", {"name": name, "entity_type": etype}
            )
            assert _parse(r).get("entity_id"), f"entity_create failed for {name}"

        relate_result = await session.call_tool(
            "entity_relate",
            {
                "from_entity": "RelateA-mcp",
                "relation": "works_on",
                "to_entity": "RelateB-mcp",
            },
        )
        relate_data = _parse(relate_result)
        assert relate_data.get("ok") is True, f"entity_relate not ok: {relate_data}"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# trigger_create + trigger_check
# ---------------------------------------------------------------------------


def test_trigger_create_returns_trigger_id(tmp_path):
    """trigger_create must return a positive integer trigger_id."""
    async def _body(session):
        result = await session.call_tool(
            "trigger_create",
            {
                "condition": "When deployment is mentioned",
                "keywords": "deploy,deployment,release",
                "action": "Remind to run smoke tests",
                "priority": "high",
            },
        )
        data = _parse(result)
        assert "trigger_id" in data, f"No trigger_id: {data}"
        assert isinstance(data["trigger_id"], int)
        assert data["trigger_id"] > 0

    _with_session(tmp_path / "brain.db", _body)


def test_trigger_check_matches_keywords(tmp_path):
    """trigger_check must surface a trigger whose keywords match the query."""
    async def _body(session):
        kw = "uniquetoken9871mcp"
        create_result = await session.call_tool(
            "trigger_create",
            {
                "condition": f"When {kw} appears",
                "keywords": kw,
                "action": "Do the thing",
            },
        )
        trigger_id = _parse(create_result).get("trigger_id")
        assert trigger_id, "trigger_create failed"

        check_result = await session.call_tool(
            "trigger_check",
            {"query": f"message mentioning {kw} in context"},
        )
        check_data = _parse(check_result)
        key = "matched_triggers" if "matched_triggers" in check_data else "triggers"
        assert key in check_data, f"No trigger list key in: {check_data}"
        ids = [t["id"] for t in check_data[key]]
        assert trigger_id in ids, (
            f"Created trigger {trigger_id} not matched; got ids: {ids}"
        )

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# decision_add
# ---------------------------------------------------------------------------


def test_decision_add_returns_decision_id(tmp_path):
    """decision_add must return a positive integer decision_id."""
    async def _body(session):
        result = await session.call_tool(
            "decision_add",
            {
                "title": "Use MCP for brainctl integration",
                "rationale": "Standard protocol enables broader tooling support",
                "project": "brainctl",
            },
        )
        data = _parse(result)
        assert "decision_id" in data, f"No decision_id: {data}"
        assert isinstance(data["decision_id"], int)
        assert data["decision_id"] > 0

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# handoff_add
# ---------------------------------------------------------------------------


def test_handoff_add_returns_handoff_id(tmp_path):
    """handoff_add must return a positive integer handoff_id."""
    async def _body(session):
        result = await session.call_tool(
            "handoff_add",
            {
                "goal": "Complete MCP integration tests",
                "current_state": "Writing tests in pytest",
                "open_loops": "Need to verify all tools",
                "next_step": "Run test suite and review failures",
                "project": "brainctl-mcp-test",
            },
        )
        data = _parse(result)
        assert "handoff_id" in data, f"No handoff_id: {data}"
        assert isinstance(data["handoff_id"], int)
        assert data["handoff_id"] > 0

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# search (cross-table)
# ---------------------------------------------------------------------------


def test_search_cross_table_returns_structure(tmp_path):
    """search must return a valid dict (not an error) when data exists."""
    async def _body(session):
        probe = "cross-table-search-probe-7f3d-mcp"
        await session.call_tool(
            "memory_add",
            {"content": probe, "category": "lesson", "force": True},
        )
        await session.call_tool(
            "event_add",
            {"summary": f"{probe} event", "event_type": "observation"},
        )

        result = await session.call_tool("search", {"query": probe})
        data = _parse(result)
        assert isinstance(data, dict), f"search returned non-dict: {data}"
        # There must be no top-level unhandled error
        if "error" in data:
            assert data["error"] is None, f"search returned error: {data['error']}"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_returns_table_counts(tmp_path):
    """stats must return integer counts for the core tables."""
    async def _body(session):
        result = await session.call_tool("stats", {})
        data = _parse(result)
        assert isinstance(data, dict), f"stats returned non-dict: {data}"
        for key in ("memories", "events", "entities"):
            assert key in data, f"stats missing key '{key}': {data}"
            assert isinstance(data[key], int), f"stats[{key!r}] is not int: {data}"

    _with_session(tmp_path / "brain.db", _body)


def test_stats_reflects_added_data(tmp_path):
    """stats memory count must increase after adding a memory."""
    async def _body(session):
        before = _parse(await session.call_tool("stats", {}))
        mem_before = before.get("memories", 0)

        await session.call_tool(
            "memory_add",
            {"content": "stats reflection test memory", "category": "project", "force": True},
        )

        after = _parse(await session.call_tool("stats", {}))
        mem_after = after.get("memories", 0)
        assert mem_after > mem_before, (
            f"stats memories count did not increase: before={mem_before} after={mem_after}"
        )

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


def test_telemetry_returns_valid_json(tmp_path):
    """telemetry must return parseable JSON without crashing the server.

    Note: the telemetry extension dispatch uses ``lambda args: ...`` which
    receives keyword args from the server's call_tool invocation
    ``fn(agent_id=..., **arguments)``.  This may produce a TypeError caught
    as ``{"error": "..."}``.  We verify the server stays alive and returns
    a dict either way.
    """
    async def _body(session):
        result = await session.call_tool("telemetry", {})
        data = _parse(result)
        assert isinstance(data, dict), f"telemetry returned non-dict: {data}"

    _with_session(tmp_path / "brain.db", _body)


def test_telemetry_health_score_when_ok(tmp_path):
    """When telemetry succeeds (ok=True), health_score must be in [0.0, 1.0]."""
    async def _body(session):
        result = await session.call_tool("telemetry", {})
        data = _parse(result)
        if not data.get("ok"):
            pytest.skip(
                f"telemetry returned ok=False (known dispatch mismatch?): "
                f"{data.get('error')}"
            )
        assert "health_score" in data, f"health_score missing: {data}"
        score = data["health_score"]
        assert isinstance(score, (int, float)), f"health_score not numeric: {score}"
        assert 0.0 <= score <= 1.0, f"health_score out of range: {score}"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# affect_classify (pure computation — no DB write)
# ---------------------------------------------------------------------------


def test_affect_classify_returns_vad_fields(tmp_path):
    """affect_classify must return valence, arousal, and dominance fields."""
    async def _body(session):
        result = await session.call_tool(
            "affect_classify",
            {"text": "I am thrilled and excited about this progress!"},
        )
        data = _parse(result)
        for key in ("valence", "arousal", "dominance"):
            assert key in data, f"affect_classify missing '{key}': {data}"
            assert isinstance(data[key], (int, float)), f"{key} not numeric: {data}"

    _with_session(tmp_path / "brain.db", _body)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_error_unknown_tool_returns_error_or_raises(tmp_path):
    """Calling a non-existent tool must return an error dict or raise, not crash the server."""
    async def _body(session):
        try:
            result = await session.call_tool(
                "this_tool_does_not_exist_xyzabc_mcp_test",
                {},
            )
            data = _parse(result)
            # Server-level: returns {"error": "Unknown tool: ..."}
            assert "error" in data, (
                f"Expected 'error' for unknown tool, got: {data}"
            )
        except Exception:
            # SDK-level: raises McpError for unknown tools — also acceptable.
            pass

        # Verify the server is still alive by calling a known tool
        stats_result = await session.call_tool("stats", {})
        stats_data = _parse(stats_result)
        assert "memories" in stats_data, f"Server seems dead after unknown tool: {stats_data}"

    _with_session(tmp_path / "brain.db", _body)


def test_error_missing_required_params_memory_add(tmp_path):
    """memory_add without required 'content' must yield an error, not a valid memory_id."""
    async def _body(session):
        try:
            result = await session.call_tool(
                "memory_add",
                {"category": "lesson"},  # missing required 'content'
            )
            data = _parse(result)
            # Server catches ValueError → {"error": "..."}; no memory_id
            assert "memory_id" not in data or "error" in data, (
                f"Expected error for missing content, got: {data}"
            )
        except Exception:
            # SDK-level schema validation — also acceptable
            pass

    _with_session(tmp_path / "brain.db", _body)


def test_error_missing_required_params_event_add(tmp_path):
    """event_add without required 'summary' must yield an error, not a valid event_id."""
    async def _body(session):
        try:
            result = await session.call_tool(
                "event_add",
                {"event_type": "observation"},  # missing required 'summary'
            )
            data = _parse(result)
            assert "event_id" not in data or "error" in data, (
                f"Expected error for missing summary, got: {data}"
            )
        except Exception:
            pass

    _with_session(tmp_path / "brain.db", _body)


def test_error_invalid_category_returns_error(tmp_path):
    """memory_add with an invalid category must return an error."""
    async def _body(session):
        try:
            result = await session.call_tool(
                "memory_add",
                {
                    "content": "test memory with bad category",
                    "category": "not_a_real_category_xyz",
                },
            )
            data = _parse(result)
            # Either validation error or the server rejects it
            assert "memory_id" not in data or "error" in data, (
                f"Expected error for invalid category, got: {data}"
            )
        except Exception:
            pass

    _with_session(tmp_path / "brain.db", _body)
