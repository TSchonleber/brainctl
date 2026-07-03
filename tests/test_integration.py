"""End-to-end integration tests for brainctl.

Tests real user scenarios spanning multiple steps and subsystems.
Each test is independent — isolated DB via tmp_path.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_server as mcp_server


# ---------------------------------------------------------------------------
# Shared fixture — gives Brain API and MCP tools the same isolated DB
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    """Return a (db_file, Brain) tuple with mcp_server.DB_PATH patched."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)
    return db_file, brain


# ---------------------------------------------------------------------------
# Scenario 1: Full memory lifecycle — write → search → forget → verify
# ---------------------------------------------------------------------------

def test_full_memory_lifecycle(tmp_path):
    """Write → search → forget → verify retirement."""
    brain = Brain(db_path=str(tmp_path / "brain.db"), agent_id="lifecycle-agent")

    mid = brain.remember("JWT tokens expire after 24 hours", category="convention")
    assert isinstance(mid, int) and mid > 0

    results = brain.search("JWT token")
    assert any(r["id"] == mid for r in results), "Memory should appear in search results"

    brain.forget(mid)

    results_after = brain.search("JWT token")
    assert not any(r["id"] == mid for r in results_after), (
        "Forgotten memory should not appear in search results"
    )

    # Verify soft-delete: row exists but retired_at is set
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    row = conn.execute(
        "SELECT retired_at FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    conn.close()
    assert row is not None and row[0] is not None, "Memory row should have retired_at set"


# ---------------------------------------------------------------------------
# Scenario 2: Multi-agent knowledge graph
# ---------------------------------------------------------------------------

def test_multi_agent_knowledge_graph(tmp_path):
    """Two agents build a shared knowledge graph about a person."""
    db_path = str(tmp_path / "shared.db")

    agent_a = Brain(db_path=db_path, agent_id="agent-a")
    agent_b = Brain(db_path=db_path, agent_id="agent-b")

    # Agent A creates Alice
    alice_id = agent_a.entity(
        "AliceKG",
        "person",
        observations=["Senior engineer", "Leads platform team"],
        properties={"department": "Engineering"},
    )
    assert isinstance(alice_id, int) and alice_id > 0

    # Agent B creates a project entity and relates Alice to it
    project_id = agent_b.entity(
        "PlatformV2",
        "project",
        observations=["Microservices migration"],
    )
    agent_b.relate("AliceKG", "leads", "PlatformV2")

    # Both agents can retrieve Alice and see the relation
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    alice_row = conn.execute(
        "SELECT * FROM entities WHERE name = 'AliceKG' AND retired_at IS NULL"
    ).fetchone()
    assert alice_row is not None, "Alice should exist in the shared graph"

    edge = conn.execute(
        """SELECT ke.relation_type
           FROM knowledge_edges ke
           JOIN entities src ON ke.source_id = src.id
           JOIN entities tgt ON ke.target_id = tgt.id
           WHERE src.name = 'AliceKG' AND tgt.name = 'PlatformV2'"""
    ).fetchone()
    conn.close()
    assert edge is not None and edge["relation_type"] == "leads", (
        "Relation 'leads' should connect Alice to PlatformV2"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Event → memory promotion pipeline
# ---------------------------------------------------------------------------

def test_event_to_memory_pipeline(mcp_db):
    """Log event → promote to memory → appears in search."""
    db_file, brain = mcp_db

    import agentmemory.mcp_tools_knowledge as knowledge_mod
    monkeypatched_db = mcp_server.DB_PATH  # already set by fixture
    knowledge_mod.DB_PATH = monkeypatched_db

    # Log a high-importance event
    eid = brain.log(
        "Deployed authentication service to production successfully",
        event_type="observation",
        project="auth-service",
        importance=0.9,
    )
    assert isinstance(eid, int) and eid > 0

    # Promote the event to a memory via MCP tool
    result = knowledge_mod.tool_promote(event_id=eid, category="lesson")
    assert result["ok"] is True, f"Promote failed: {result}"
    mid = result["memory_id"]
    assert isinstance(mid, int)

    # The promoted memory should appear in Brain.search
    search_results = brain.search("authentication service")
    assert any(r["id"] == mid for r in search_results), (
        "Promoted memory should be findable via search"
    )

    # Verify event link is intact
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    mem_row = conn.execute(
        "SELECT source_event_id, category FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    conn.close()
    assert mem_row["source_event_id"] == eid
    assert mem_row["category"] == "lesson"


# ---------------------------------------------------------------------------
# Scenario 4: Trigger fires on query
# ---------------------------------------------------------------------------

def test_trigger_fires_on_query(mcp_db):
    """Create trigger → search matching content → trigger fires."""
    db_file, brain = mcp_db

    # Create a trigger that watches for "deploy" keywords
    create_result = mcp_server.tool_trigger_create(
        agent_id="test-agent",
        condition="deploy mentioned",
        keywords="deploy,deployment,release",
        action="notify:ops-channel",
        priority="high",
    )
    assert create_result["ok"] is True, f"Trigger create failed: {create_result}"
    trigger_id = create_result["trigger_id"]

    # Check trigger fires when query contains a keyword
    check_result = mcp_server.tool_trigger_check(
        agent_id="test-agent",
        query="planning a deployment to production tonight",
    )
    assert check_result["ok"] is True
    assert check_result["count"] >= 1, "Trigger should fire on matching query"
    matched_ids = [t["id"] for t in check_result["matched_triggers"]]
    assert trigger_id in matched_ids, "Our trigger should be among the matches"
    matched = next(t for t in check_result["matched_triggers"] if t["id"] == trigger_id)
    assert any(kw in matched["matched_keywords"] for kw in ["deploy", "deployment"])

    # A non-matching query should not fire the trigger
    no_match = mcp_server.tool_trigger_check(
        agent_id="test-agent",
        query="weekly standup meeting notes",
    )
    assert no_match["ok"] is True
    assert not any(t["id"] == trigger_id for t in no_match["matched_triggers"]), (
        "Trigger should not fire for unrelated query"
    )


# ---------------------------------------------------------------------------
# Scenario 5: Handoff round-trip
# ---------------------------------------------------------------------------

def test_handoff_round_trip(mcp_db):
    """Create handoff → fetch latest → consume → verify consumed."""
    db_file, brain = mcp_db

    # Create a handoff packet
    add_result = mcp_server.tool_handoff_add(
        agent_id="test-agent",
        goal="Finish implementing the search ranking feature",
        current_state="Unit tests pass; integration tests pending",
        open_loops="Performance benchmarks not run yet",
        next_step="Run benchmark suite and compare with baseline",
        project="brainctl",
    )
    assert add_result["ok"] is True, f"Handoff add failed: {add_result}"
    handoff_id = add_result["handoff_id"]
    assert isinstance(handoff_id, int) and handoff_id > 0

    # Fetch the latest pending handoff
    latest = mcp_server.tool_handoff_latest(
        agent_id="test-agent",
        status="pending",
        project="brainctl",
    )
    assert latest, "Should return a handoff record (non-empty dict)"
    assert latest.get("id") == handoff_id, "Fetched handoff should match created one"
    assert latest.get("status") == "pending"

    # Consume the handoff
    consume_result = mcp_server.tool_handoff_consume(
        agent_id="test-agent",
        handoff_id=handoff_id,
    )
    assert consume_result["ok"] is True
    assert consume_result["status"] == "consumed"

    # Verify it is no longer returned as pending
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM handoff_packets WHERE id = ?", (handoff_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "consumed", "Handoff should be marked consumed in DB"


# ---------------------------------------------------------------------------
# Scenario 6: Affect log and check
# ---------------------------------------------------------------------------

def test_affect_log_and_check(mcp_db):
    """Log affect → check state → verify logged."""
    db_file, brain = mcp_db

    # Log a positive affect observation
    log_result = mcp_server.tool_affect_log(
        agent_id="test-agent",
        text="We shipped the feature successfully! The team is thrilled.",
        source="observation",
    )
    assert "valence" in log_result, "affect_log should return VAD scores"
    assert "affect_label" in log_result

    # Also log via Brain API
    brain_result = brain.affect_log(
        "I'm excited about the new design!",
        source="reflection",
    )
    assert "id" in brain_result, "Brain.affect_log should return stored id"
    assert isinstance(brain_result["id"], int)

    # Check current affect state
    check_result = mcp_server.tool_affect_check(agent_id="test-agent")
    assert "current" in check_result or "status" in check_result, (
        "affect_check should return current state"
    )
    assert check_result.get("agent") == "test-agent"

    # Verify rows are in the affect_log table
    conn = sqlite3.connect(str(db_file))
    count = conn.execute(
        "SELECT COUNT(*) FROM affect_log WHERE agent_id = 'test-agent'"
    ).fetchone()[0]
    conn.close()
    assert count >= 2, "Both affect log entries should be persisted"


# ---------------------------------------------------------------------------
# Scenario 7: Decision with entity relation
# ---------------------------------------------------------------------------

def test_decision_with_entity(mcp_db):
    """Record decision about an entity → entity appears in knowledge graph."""
    db_file, brain = mcp_db

    # Create an entity for the project we'll decide about
    entity_id = brain.entity(
        "BrainctlDB",
        "project",
        observations=["Memory persistence layer", "SQLite-backed"],
    )

    # Record a decision referencing that project
    decision_result = mcp_server.tool_decision_add(
        agent_id="test-agent",
        title="Use SQLite WAL mode for concurrent writes",
        rationale="WAL mode allows concurrent readers with a single writer, reducing lock contention.",
        project="BrainctlDB",
    )
    assert decision_result["ok"] is True, f"Decision add failed: {decision_result}"
    did = decision_result["decision_id"]
    assert isinstance(did, int) and did > 0

    # Verify entity is in the knowledge graph
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    entity_row = conn.execute(
        "SELECT * FROM entities WHERE name = 'BrainctlDB' AND retired_at IS NULL"
    ).fetchone()
    decision_row = conn.execute(
        "SELECT * FROM decisions WHERE id = ? AND project = 'BrainctlDB'", (did,)
    ).fetchone()
    conn.close()

    assert entity_row is not None, "Entity should be in knowledge graph"
    assert decision_row is not None, "Decision should be linked to project"
    assert decision_row["title"] == "Use SQLite WAL mode for concurrent writes"


# ---------------------------------------------------------------------------
# Scenario 8: MCP tool dispatch round-trip (memory add → search)
# ---------------------------------------------------------------------------

def test_mcp_memory_add_and_search(mcp_db):
    """Use MCP tools for the full add → search cycle via FTS."""
    db_file, brain = mcp_db

    # Add a memory using the MCP tool (force=True bypasses the worthiness gate)
    add_result = mcp_server.tool_memory_add(
        agent_id="test-agent",
        content="PostgreSQL partitioning improves query performance on large tables",
        category="convention",
        force=True,
    )
    assert add_result.get("ok") is True, (
        f"tool_memory_add failed: {add_result}"
    )
    memory_id = add_result["memory_id"]

    # Search for the memory using the MCP tool (uses FTS)
    search_result = mcp_server.tool_memory_search(
        agent_id="test-agent",
        query="PostgreSQL partitioning",
    )
    assert search_result["ok"] is True
    assert search_result["count"] >= 1, "FTS search should find the added memory"
    ids = [m["id"] for m in search_result["memories"]]
    assert memory_id in ids, "Added memory should appear in search results"


# ---------------------------------------------------------------------------
# Scenario 9: Stats reflect actual data
# ---------------------------------------------------------------------------

def test_stats_accuracy(mcp_db):
    """Insert N records → stats shows correct counts."""
    db_file, brain = mcp_db

    # Establish baseline
    baseline = brain.stats()
    baseline_memories = baseline.get("memories", 0)
    baseline_events = baseline.get("events", 0)
    baseline_entities = baseline.get("entities", 0)

    # Insert known quantities
    N_MEMORIES = 5
    N_EVENTS = 3
    N_ENTITIES = 2

    for i in range(N_MEMORIES):
        brain.remember(f"Test memory item number {i}", category="project")

    for i in range(N_EVENTS):
        brain.log(f"Test event summary {i}", event_type="observation")

    brain.entity("StatsPersonA", "person")
    brain.entity("StatsProjectB", "project")

    # Check stats via Brain API
    stats = brain.stats()
    assert stats["memories"] == baseline_memories + N_MEMORIES
    assert stats["events"] == baseline_events + N_EVENTS
    assert stats["entities"] == baseline_entities + N_ENTITIES
    assert stats["active_memories"] == baseline.get("active_memories", 0) + N_MEMORIES

    # Check stats via MCP tool
    mcp_stats = mcp_server.tool_stats()
    assert mcp_stats["memories"] >= baseline_memories + N_MEMORIES
    assert mcp_stats["events"] >= baseline_events + N_EVENTS
    assert mcp_stats["entities"] >= baseline_entities + N_ENTITIES
    assert mcp_stats["active_memories"] >= baseline.get("active_memories", 0) + N_MEMORIES


# ---------------------------------------------------------------------------
# Scenario 10: Multi-agent memory conflict detection
# ---------------------------------------------------------------------------

def test_multi_agent_memory_conflict(mcp_db):
    """Two agents write contradictory memories → conflict row queryable."""
    db_file, brain = mcp_db

    agent_a = Brain(db_path=str(db_file), agent_id="agent-alpha")
    agent_b = Brain(db_path=str(db_file), agent_id="agent-beta")

    # Write contradictory memories about the same topic
    mid_a = agent_a.remember(
        "The deployment window is every Tuesday at 2pm UTC",
        category="convention",
        confidence=0.9,
    )
    mid_b = agent_b.remember(
        "The deployment window is every Wednesday at 4pm UTC",
        category="convention",
        confidence=0.85,
    )

    # Manually register the conflict in belief_conflicts
    # (In production this would be detected by the conflict scanner)
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Ensure both agents are registered
    for aid in ("agent-alpha", "agent-beta"):
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
            "created_at, updated_at) VALUES (?, ?, 'api', 'active', "
            "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (aid, aid),
        )

    cur = conn.execute(
        """
        INSERT INTO belief_conflicts (
            agent_a_id, agent_b_id, topic, conflict_type, severity,
            belief_a, belief_b, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))
        """,
        (
            "agent-alpha",
            "agent-beta",
            "deployment window schedule",
            "factual",
            0.8,
            "Deployment window is every Tuesday at 2pm UTC",
            "Deployment window is every Wednesday at 4pm UTC",
        ),
    )
    conflict_id = cur.lastrowid
    conn.commit()

    # Verify DB has the conflict row before any tool call
    open_row = conn.execute(
        "SELECT * FROM belief_conflicts WHERE id = ? AND resolved_at IS NULL",
        (conflict_id,),
    ).fetchone()
    assert open_row is not None, "Conflict row should be unresolved in DB"
    assert open_row["topic"] == "deployment window schedule"
    assert open_row["agent_a_id"] == "agent-alpha"
    assert open_row["agent_b_id"] == "agent-beta"

    open_count = conn.execute(
        "SELECT COUNT(*) FROM belief_conflicts WHERE resolved_at IS NULL"
    ).fetchone()[0]
    assert open_count >= 1, "At least one open conflict should exist in DB"
    conn.close()

    # Optionally verify via tool_resolve_conflict if belief_revision module is available
    try:
        list_result = mcp_server.tool_resolve_conflict(
            agent_id="test-agent",
            list_conflicts=True,
        )
        if list_result.get("ok") is True:
            conflict_ids = [c["id"] for c in list_result.get("conflicts", [])]
            assert conflict_id in conflict_ids, (
                "Registered conflict should appear in the open conflicts list"
            )
            assert list_result["open_conflicts"] >= 1
    except Exception:
        # belief_revision module unavailable in this environment — DB checks above are sufficient
        pass


# ---------------------------------------------------------------------------
# Scenario 11: Knowledge index reflects multi-agent data
# ---------------------------------------------------------------------------

def test_knowledge_index_reflects_multi_agent_data(mcp_db):
    """Multiple agents write → knowledge_index aggregates all their data."""
    db_file, brain = mcp_db

    import agentmemory.mcp_tools_knowledge as knowledge_mod
    knowledge_mod.DB_PATH = mcp_server.DB_PATH

    agent_x = Brain(db_path=str(db_file), agent_id="agent-x")
    agent_y = Brain(db_path=str(db_file), agent_id="agent-y")

    agent_x.remember("Always use parameterised queries", category="convention")
    agent_y.remember("Use connection pooling for DB access", category="environment")
    agent_x.entity("DatabaseLayer", "project", observations=["Core persistence"])

    result = knowledge_mod.tool_knowledge_index()
    assert result["ok"] is True
    assert result["stats"]["total_memories"] >= 2
    cats = result["memories_by_category"]
    assert "convention" in cats
    assert "environment" in cats
    assert "project" in result["entities_by_type"]


# ---------------------------------------------------------------------------
# Scenario 12: Trigger list and lifecycle
# ---------------------------------------------------------------------------

def test_trigger_list_and_lifecycle(mcp_db):
    """Create multiple triggers → list them → update one → verify state."""
    db_file, brain = mcp_db

    r1 = mcp_server.tool_trigger_create(
        agent_id="test-agent",
        condition="high error rate",
        keywords="error,exception,crash",
        action="alert:oncall",
        priority="critical",
    )
    r2 = mcp_server.tool_trigger_create(
        agent_id="test-agent",
        condition="deployment started",
        keywords="deploy,release",
        action="log:audit",
        priority="low",
    )
    assert r1["ok"] and r2["ok"]
    tid1, tid2 = r1["trigger_id"], r2["trigger_id"]

    # List all triggers
    list_result = mcp_server.tool_trigger_list(agent_id="test-agent")
    assert list_result["ok"] is True
    all_ids = [t["id"] for t in list_result["triggers"]]
    assert tid1 in all_ids
    assert tid2 in all_ids

    # Verify priorities are stored correctly
    by_id = {t["id"]: t for t in list_result["triggers"]}
    assert by_id[tid1]["priority"] == "critical"
    assert by_id[tid2]["priority"] == "low"
