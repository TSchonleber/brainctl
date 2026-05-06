"""Issue #97-3: handoff_latest must not silently skip pinned packets
when called with the default (no status argument).

Pinned handoffs are explicitly retained for cross-session continuity.
The prior default of ``status="pending"`` filtered them out at the very
moment they were most needed — session-start orientation. This test
locks in the new behavior: when the caller doesn't specify a status,
both pending and pinned packets are eligible, and pinned wins ties.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_server as mcp_server


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)
    return db_file, brain


def _add_handoff(agent_id: str, *, project: str = "p", goal: str = "g") -> int:
    out = mcp_server.tool_handoff_add(
        agent_id=agent_id,
        goal=goal,
        current_state="state",
        open_loops="loops",
        next_step="next",
        project=project,
    )
    assert out["ok"] is True, f"handoff_add failed: {out}"
    return out["handoff_id"]


def test_default_status_returns_pinned_when_no_pending(mcp_db):
    """Original beta-test repro from #97: only handoff is pinned, default
    status="pending" returned {} silently."""
    h_id = _add_handoff("test-agent")
    pin = mcp_server.tool_handoff_pin(agent_id="test-agent", handoff_id=h_id)
    assert pin["ok"] is True

    # Old behavior: status="pending" default would return {}
    out_explicit_pending = mcp_server.tool_handoff_latest(
        agent_id="test-agent", status="pending",
    )
    assert out_explicit_pending == {}

    # New behavior: omitting status looks at pending OR pinned
    out_default = mcp_server.tool_handoff_latest(agent_id="test-agent")
    assert out_default.get("id") == h_id
    assert out_default.get("status") == "pinned"


def test_default_status_prefers_pinned_over_pending_on_tie(mcp_db):
    """When both states exist, pinned wins (it was explicitly retained)."""
    pending_id = _add_handoff("test-agent", goal="pending packet")
    pinned_id = _add_handoff("test-agent", goal="pinned packet")
    pin = mcp_server.tool_handoff_pin(agent_id="test-agent", handoff_id=pinned_id)
    assert pin["ok"] is True

    out = mcp_server.tool_handoff_latest(agent_id="test-agent")
    assert out.get("id") == pinned_id, (
        "When omitting status, the pinned handoff must win even if a newer "
        "pending one exists — pinning is an explicit signal of importance."
    )
    # And pending is still reachable explicitly
    out_explicit = mcp_server.tool_handoff_latest(
        agent_id="test-agent", status="pending",
    )
    assert out_explicit.get("id") == pending_id


def test_explicit_status_pending_unchanged(mcp_db):
    """Backwards-compat: explicit status='pending' keeps single-state behavior."""
    h_id = _add_handoff("test-agent")
    out = mcp_server.tool_handoff_latest(
        agent_id="test-agent", status="pending",
    )
    assert out.get("id") == h_id
    assert out.get("status") == "pending"


def test_consumed_packets_still_filtered_by_default(mcp_db):
    """Consumed/expired packets must NOT show up in the default response."""
    h_id = _add_handoff("test-agent")
    consume = mcp_server.tool_handoff_consume(agent_id="test-agent", handoff_id=h_id)
    assert consume["ok"] is True

    out = mcp_server.tool_handoff_latest(agent_id="test-agent")
    assert out == {}, "Consumed packets must not be returned by default"

    # But they remain reachable with explicit status filter
    out_explicit = mcp_server.tool_handoff_latest(
        agent_id="test-agent", status="consumed",
    )
    assert out_explicit.get("id") == h_id
