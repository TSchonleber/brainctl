"""Issue #97-6: ``gaps_scan`` reported severity-1.0 coverage holes for
every active agent because it compared ``agent:<id>`` formatted scopes
against the ``knowledge_coverage`` table — which is populated from
``memories.scope`` (``global``, ``project:foo``, etc.) and never holds
``agent:<id>`` entries. Result: agents with dozens of memories were all
flagged as uncovered.

This test pins the corrected behavior:
- An agent with at least one active memory is *not* a coverage hole.
- An agent with zero active memories *is* a coverage hole.
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
import agentmemory.mcp_tools_expertise as expertise


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    Brain(db_path=str(db), agent_id="boot-agent")  # ensure schema initialized
    # The tool modules cache DB_PATH at import time; patch the symbols
    # they actually read instead of relying on env vars.
    monkeypatch.setattr(mcp_server, "DB_PATH", db)
    monkeypatch.setattr(expertise, "DB_PATH", db)
    return db


def test_agent_with_memories_is_not_a_coverage_hole(isolated_db):
    """The original beta-test repro: claude-style agent with several
    memories should not appear in ``coverage_holes``."""
    db_path = isolated_db
    brain = Brain(db_path=str(db_path), agent_id="claude")
    for content in [
        "Kelly village notes",
        "Howler routine",
        "Pigendom analysis",
    ]:
        brain.remember(content, category="project")

    out = expertise.tool_gaps_scan()
    assert out["ok"] is True

    holes = {h["scope"] for h in out["coverage_holes"]}
    assert "agent:claude" not in holes, (
        f"Regression: agent with active memories flagged as coverage hole. "
        f"holes={holes}"
    )


def test_agent_without_memories_is_a_coverage_hole(isolated_db):
    """Genuine empty agent — should be flagged."""
    db_path = isolated_db
    # Register an agent but don't write any memories under it
    import sqlite3
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?, ?, 'mcp', 'active', ?, ?)",
        ("ghost-agent", "ghost-agent", now, now),
    )
    conn.commit()
    conn.close()

    out = expertise.tool_gaps_scan()
    assert out["ok"] is True

    holes = {h["scope"] for h in out["coverage_holes"]}
    assert "agent:ghost-agent" in holes, (
        f"Regression: empty agent should be a coverage hole. holes={holes}"
    )
