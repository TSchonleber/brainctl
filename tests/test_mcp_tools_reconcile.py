"""Tests for mcp_tools_reconcile — cross-agent entity reconciliation MCP tools."""
from __future__ import annotations
import sys
import json
import sqlite3
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_reconcile as mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_db(tmp_path: Path) -> Path:
    """Create a fresh brain.db and redirect the module to it."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    mod.DB_PATH = db_file
    return db_file


def _ensure_agent(db_file: Path, agent_id: str) -> None:
    """Insert an agent row if it doesn't exist."""
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES (?,?,?,?,'2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')",
        (agent_id, agent_id, "test", "active"),
    )
    conn.commit()
    conn.close()


def _insert_entity(
    db_file: Path,
    agent_id: str,
    name: str,
    entity_type: str = "person",
    observations: list | None = None,
    properties: dict | None = None,
    scope: str = "global",
) -> int:
    """Insert an entity directly and return its id."""
    _ensure_agent(db_file, agent_id)
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "INSERT INTO entities (name, entity_type, observations, properties, agent_id, scope, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')",
        (
            name,
            entity_type,
            json.dumps(observations or []),
            json.dumps(properties or {}),
            agent_id,
            scope,
        ),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


def _insert_edge(db_file: Path, src_id: int, tgt_id: int, relation: str = "knows",
                 agent_id: str = "test-agent") -> int:
    _ensure_agent(db_file, agent_id)
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "INSERT OR IGNORE INTO knowledge_edges "
        "(source_table, source_id, target_table, target_id, relation_type, agent_id, created_at) "
        "VALUES ('entities', ?, 'entities', ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (src_id, tgt_id, relation, agent_id),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


# ---------------------------------------------------------------------------
# Module interface
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list_with_correct_length(self, tmp_path):
        assert isinstance(mod.TOOLS, list)
        assert len(mod.TOOLS) == 6

    def test_dispatch_is_dict_with_all_keys(self, tmp_path):
        assert isinstance(mod.DISPATCH, dict)
        expected = {
            "entity_duplicates_scan",
            "entity_merge",
            "entity_aliases",
            "entity_add_alias",
            "entity_cross_agent_view",
            "entity_reconcile_report",
        }
        assert set(mod.DISPATCH.keys()) == expected

    def test_tool_names_match_dispatch(self, tmp_path):
        tool_names = {t.name for t in mod.TOOLS}
        assert tool_names == set(mod.DISPATCH.keys())

    def test_all_tools_have_input_schema(self, tmp_path):
        for tool in mod.TOOLS:
            assert hasattr(tool, "inputSchema"), f"{tool.name} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"

    def test_dispatch_callables(self, tmp_path):
        for name, fn in mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH['{name}'] is not callable"


# ---------------------------------------------------------------------------
# entity_duplicates_scan
# ---------------------------------------------------------------------------

class TestEntityDuplicatesScan:
    def test_empty_db_returns_empty_groups(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_entity_duplicates_scan()
        assert result["ok"] is True
        assert result["duplicate_groups"] == []

    def test_finds_substring_duplicate(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person")
        _insert_entity(db_file, "agent-b", "Alice Chen", "person", scope="agent:agent-b")

        result = mod.tool_entity_duplicates_scan(similarity_threshold=0.7)
        assert result["ok"] is True
        assert result["total_candidates"] >= 1
        group = result["duplicate_groups"][0]
        names = {group["primary"]["name"], group["duplicates"][0]["name"]}
        assert "Alice" in names
        assert "Alice Chen" in names

    def test_reason_is_name_substring(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Bob", "person")
        _insert_entity(db_file, "agent-b", "Bob Smith", "person", scope="agent:agent-b")

        result = mod.tool_entity_duplicates_scan(similarity_threshold=0.7)
        assert result["ok"] is True
        assert len(result["duplicate_groups"]) >= 1
        assert result["duplicate_groups"][0]["reason"] == "name_substring"

    def test_entity_type_filter_excludes_different_types(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person")
        _insert_entity(db_file, "agent-b", "Alice Project", "project", scope="agent:agent-b")

        result = mod.tool_entity_duplicates_scan(entity_type="person", similarity_threshold=0.5)
        assert result["ok"] is True
        # Alice (person) and Alice Project (project) should NOT match because types differ
        for group in result["duplicate_groups"]:
            primary_type = group["primary"]["entity_type"]
            for dup in group["duplicates"]:
                assert dup["entity_type"] == primary_type == "person"

    def test_high_threshold_reduces_candidates(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person")
        _insert_entity(db_file, "agent-b", "Alice Chen", "person", scope="agent:agent-b")
        _insert_entity(db_file, "agent-c", "Bob", "person", scope="agent:agent-c")
        _insert_entity(db_file, "agent-d", "Bobby", "person", scope="agent:agent-d")

        low = mod.tool_entity_duplicates_scan(similarity_threshold=0.5)
        high = mod.tool_entity_duplicates_scan(similarity_threshold=0.99)
        assert low["total_candidates"] >= high["total_candidates"]

    def test_limit_is_respected(self, tmp_path):
        db_file = _setup_db(tmp_path)
        for i in range(10):
            _insert_entity(db_file, "agent-a", f"Entity{i}", "concept")
            _insert_entity(db_file, "agent-b", f"Entity{i}X", "concept", scope=f"agent:agent-b-{i}")

        result = mod.tool_entity_duplicates_scan(similarity_threshold=0.5, limit=3)
        assert result["ok"] is True
        assert len(result["duplicate_groups"]) <= 3

    def test_observations_overlap_boosts_confidence(self, tmp_path):
        db_file = _setup_db(tmp_path)
        shared_obs = ["engineer", "Python developer", "remote worker"]
        _insert_entity(db_file, "agent-a", "Carol", "person", observations=shared_obs)
        _insert_entity(
            db_file, "agent-b", "Carol W", "person",
            observations=shared_obs + ["manager"],
            scope="agent:agent-b",
        )
        result = mod.tool_entity_duplicates_scan(similarity_threshold=0.5)
        assert result["ok"] is True
        assert len(result["duplicate_groups"]) >= 1
        group = result["duplicate_groups"][0]
        assert group["obs_overlap"] > 0


# ---------------------------------------------------------------------------
# entity_merge
# ---------------------------------------------------------------------------

class TestEntityMerge:
    def test_dry_run_returns_preview_without_writing(self, tmp_path):
        db_file = _setup_db(tmp_path)
        pid = _insert_entity(db_file, "agent-a", "Alice", "person", observations=["engineer"])
        did = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person",
            observations=["manager", "engineer"],
            scope="agent:agent-b",
        )

        result = mod.tool_entity_merge(primary_id=pid, duplicate_ids=[did], dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["primary_id"] == pid
        assert result["merged_count"] == 1

        # Verify nothing was written
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        dup = conn.execute("SELECT retired_at FROM entities WHERE id=?", (did,)).fetchone()
        conn.close()
        assert dup["retired_at"] is None

    def test_actual_merge_retires_duplicates(self, tmp_path):
        db_file = _setup_db(tmp_path)
        pid = _insert_entity(db_file, "agent-a", "Alice", "person", observations=["engineer"])
        did = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person",
            observations=["manager"],
            scope="agent:agent-b",
        )

        result = mod.tool_entity_merge(primary_id=pid, duplicate_ids=[did], dry_run=False)
        assert result["ok"] is True
        assert result["dry_run"] is False
        assert result["merged_count"] == 1

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        dup = conn.execute("SELECT retired_at FROM entities WHERE id=?", (did,)).fetchone()
        conn.close()
        assert dup["retired_at"] is not None

    def test_merge_combines_observations(self, tmp_path):
        db_file = _setup_db(tmp_path)
        pid = _insert_entity(db_file, "agent-a", "Alice", "person", observations=["engineer"])
        did = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person",
            observations=["manager", "engineer"],  # "engineer" is duplicate
            scope="agent:agent-b",
        )

        mod.tool_entity_merge(primary_id=pid, duplicate_ids=[did], dry_run=False)

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        primary = conn.execute("SELECT observations FROM entities WHERE id=?", (pid,)).fetchone()
        conn.close()
        obs = json.loads(primary["observations"])
        # Should have "engineer" once and "manager" once (deduplicated)
        assert "engineer" in obs
        assert "manager" in obs
        # engineer should appear exactly once
        assert obs.count("engineer") == 1

    def test_merge_redirects_edges(self, tmp_path):
        db_file = _setup_db(tmp_path)
        pid = _insert_entity(db_file, "agent-a", "Alice", "person")
        did = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person",
            scope="agent:agent-b",
        )
        other = _insert_entity(db_file, "agent-a", "BrainProject", "project")
        # Edge: duplicate -> other
        _insert_edge(db_file, did, other, "works_on")

        result = mod.tool_entity_merge(primary_id=pid, duplicate_ids=[did], dry_run=False)
        assert result["ok"] is True
        assert result["edges_redirected"] >= 1

        # Edge should now point from primary to other
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        edge = conn.execute(
            "SELECT * FROM knowledge_edges WHERE source_id=? AND relation_type='works_on'",
            (pid,),
        ).fetchone()
        conn.close()
        assert edge is not None

    def test_merge_logs_event(self, tmp_path):
        db_file = _setup_db(tmp_path)
        pid = _insert_entity(db_file, "agent-a", "Alice", "person")
        did = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person",
            scope="agent:agent-b",
        )

        mod.tool_entity_merge(
            agent_id="test-agent", primary_id=pid, duplicate_ids=[did], dry_run=False
        )

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        event = conn.execute(
            "SELECT * FROM events WHERE event_type='memory_merged'"
        ).fetchone()
        conn.close()
        assert event is not None

    def test_merge_primary_not_found_returns_error(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_entity_merge(primary_id=99999, duplicate_ids=[1], dry_run=True)
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_merge_properties_primary_wins(self, tmp_path):
        db_file = _setup_db(tmp_path)
        pid = _insert_entity(
            db_file, "agent-a", "Alice", "person",
            properties={"role": "lead", "team": "core"},
        )
        did = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person",
            properties={"role": "junior", "location": "NYC"},
            scope="agent:agent-b",
        )

        mod.tool_entity_merge(primary_id=pid, duplicate_ids=[did], dry_run=False)

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT properties FROM entities WHERE id=?", (pid,)).fetchone()
        conn.close()
        props = json.loads(row["properties"])
        # primary wins on "role"
        assert props["role"] == "lead"
        # dup-only key "location" should be added
        assert props["location"] == "NYC"


# ---------------------------------------------------------------------------
# entity_aliases / entity_add_alias
# ---------------------------------------------------------------------------

class TestEntityAliases:
    def test_add_alias_stores_in_properties(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_entity(db_file, "agent-a", "Alice", "person")

        result = mod.tool_entity_add_alias(entity_id=eid, alias_name="Alice Chen")
        assert result["ok"] is True
        assert "Alice Chen" in result["aliases"]

    def test_aliases_roundtrip(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_entity(db_file, "agent-a", "Alice", "person")

        mod.tool_entity_add_alias(entity_id=eid, alias_name="Alice Chen")
        mod.tool_entity_add_alias(entity_id=eid, alias_name="A. Chen")

        result = mod.tool_entity_aliases(entity_id=eid)
        assert result["ok"] is True
        assert "Alice Chen" in result["stored_aliases"]
        assert "A. Chen" in result["stored_aliases"]

    def test_add_alias_creates_edge_when_entity_exists(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_entity(db_file, "agent-a", "Alice", "person")
        alias_eid = _insert_entity(
            db_file, "agent-b", "Alice Chen", "person", scope="agent:agent-b"
        )

        result = mod.tool_entity_add_alias(
            agent_id="test-agent", entity_id=eid, alias_name="Alice Chen"
        )
        assert result["ok"] is True
        assert result["edge_created"] is True

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        edge = conn.execute(
            "SELECT * FROM knowledge_edges "
            "WHERE source_id=? AND target_id=? AND relation_type='alias_of'",
            (eid, alias_eid),
        ).fetchone()
        conn.close()
        assert edge is not None

    def test_aliases_no_entity_returns_error(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_entity_aliases(entity_id=99999)
        assert result["ok"] is False

    def test_add_alias_no_duplicate_entries(self, tmp_path):
        db_file = _setup_db(tmp_path)
        eid = _insert_entity(db_file, "agent-a", "Alice", "person")

        mod.tool_entity_add_alias(entity_id=eid, alias_name="Ali")
        mod.tool_entity_add_alias(entity_id=eid, alias_name="Ali")  # duplicate

        result = mod.tool_entity_aliases(entity_id=eid)
        assert result["ok"] is True
        assert result["stored_aliases"].count("Ali") == 1


# ---------------------------------------------------------------------------
# entity_cross_agent_view
# ---------------------------------------------------------------------------

class TestEntityCrossAgentView:
    def test_single_agent_view(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person", observations=["engineer"])

        result = mod.tool_entity_cross_agent_view(entity_name="Alice")
        assert result["ok"] is True
        assert result["entity_name"] == "Alice"
        assert result["agent_count"] == 1
        assert result["agents"][0]["agent_id"] == "agent-a"

    def test_multi_agent_view_merges_observations(self, tmp_path):
        db_file = _setup_db(tmp_path)
        # Same name, different scopes so unique index doesn't block
        _insert_entity(db_file, "agent-a", "Alice", "person",
                       observations=["engineer"], scope="agent:agent-a")
        _insert_entity(db_file, "agent-b", "Alice", "person",
                       observations=["manager", "engineer"], scope="agent:agent-b")

        result = mod.tool_entity_cross_agent_view(entity_name="Alice")
        assert result["ok"] is True
        assert result["agent_count"] == 2
        merged_obs = result["merged_view"]["all_observations"]
        obs_lower = [o.lower() for o in merged_obs]
        assert "engineer" in obs_lower
        assert "manager" in obs_lower
        # Deduplication: engineer appears once
        assert obs_lower.count("engineer") == 1

    def test_not_found_returns_empty(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_entity_cross_agent_view(entity_name="Nonexistent")
        assert result["ok"] is True
        assert result["agent_count"] == 0

    def test_missing_entity_name_returns_error(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_entity_cross_agent_view(entity_name="")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# entity_reconcile_report
# ---------------------------------------------------------------------------

class TestEntityReconcileReport:
    def test_empty_db(self, tmp_path):
        _setup_db(tmp_path)
        result = mod.tool_entity_reconcile_report()
        assert result["ok"] is True
        assert result["total_entities"] == 0
        assert result["unique_names"] == 0
        assert result["duplication_rate"] == 0.0

    def test_counts_entities_correctly(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person")
        _insert_entity(db_file, "agent-b", "Bob", "person", scope="agent:agent-b")
        _insert_entity(db_file, "agent-c", "Alice Chen", "person", scope="agent:agent-c")

        result = mod.tool_entity_reconcile_report()
        assert result["ok"] is True
        assert result["total_entities"] == 3
        assert result["unique_names"] == 3

    def test_duplication_rate_nonzero_for_same_name(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person", scope="agent:agent-a")
        _insert_entity(db_file, "agent-b", "Alice", "person", scope="agent:agent-b")
        _insert_entity(db_file, "agent-c", "Bob", "person", scope="agent:agent-c")

        result = mod.tool_entity_reconcile_report()
        assert result["ok"] is True
        # "Alice" appears twice, "Bob" once → 1 of 2 unique names is duplicated
        assert result["exact_duplicate_name_groups"] == 1
        assert result["duplication_rate"] > 0

    def test_entity_type_filter(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person")
        _insert_entity(db_file, "agent-b", "BrainProject", "project", scope="agent:agent-b")

        result = mod.tool_entity_reconcile_report(entity_type="person")
        assert result["ok"] is True
        assert result["total_entities"] == 1
        assert result["entity_type"] == "person"

    def test_top_duplicates_present(self, tmp_path):
        db_file = _setup_db(tmp_path)
        _insert_entity(db_file, "agent-a", "Alice", "person")
        _insert_entity(
            db_file, "agent-b", "Alice Chen", "person", scope="agent:agent-b"
        )

        result = mod.tool_entity_reconcile_report()
        assert result["ok"] is True
        assert isinstance(result["top_duplicates"], list)


# ---------------------------------------------------------------------------
# Similarity helpers unit tests
# ---------------------------------------------------------------------------

class TestSimilarityHelpers:
    def test_exact_match(self):
        conf, reason = mod._name_similarity("Alice", "alice")
        assert conf == 1.0
        assert reason == "exact_match"

    def test_substring_match(self):
        conf, reason = mod._name_similarity("Alice", "Alice Chen")
        assert reason == "name_substring"
        assert conf >= 0.8

    def test_fuzzy_below_threshold_for_unrelated(self):
        conf, _ = mod._name_similarity("Alpha", "Zeta")
        assert conf < 0.8

    def test_observations_overlap_full(self):
        obs = ["engineer", "Python", "remote"]
        score = mod._observations_overlap(obs, obs)
        assert score == pytest.approx(1.0)

    def test_observations_overlap_partial(self):
        score = mod._observations_overlap(["engineer", "manager"], ["engineer", "developer"])
        assert 0 < score < 1.0

    def test_observations_overlap_none(self):
        score = mod._observations_overlap(["engineer"], ["chef"])
        assert score == 0.0

    def test_observations_overlap_empty(self):
        assert mod._observations_overlap([], ["engineer"]) == 0.0
        assert mod._observations_overlap(["engineer"], []) == 0.0
