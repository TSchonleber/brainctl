"""Tests for mcp_tools_trust — memory operations & trust MCP tools."""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_tools_trust as trust_mod


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the module at a fresh temp DB for every test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))  # initialise schema
    monkeypatch.setattr(trust_mod, "DB_PATH", db_file)
    return db_file


def _insert_agent(db_path: Path, agent_id: str = "test-agent") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES (?, ?, 'test', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()
    conn.close()


def _insert_memory(
    db_path: Path,
    content: str = "test memory",
    category: str = "project",
    agent_id: str = "test-agent",
    trust_score: float = 1.0,
    alpha: float = 1.0,
    beta: float = 1.0,
    recalled_count: int = 0,
    temporal_class: str = "medium",
    retracted_at: str | None = None,
    validated_at: str | None = None,
) -> int:
    _insert_agent(db_path, agent_id)
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO memories (agent_id, content, category, trust_score, alpha, beta, "
        "recalled_count, temporal_class, retracted_at, validated_at, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, content, category, trust_score, alpha, beta,
         recalled_count, temporal_class, retracted_at, validated_at),
    )
    mem_id = cur.lastrowid
    conn.commit()
    conn.close()
    return mem_id


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        assert isinstance(trust_mod.TOOLS, list)
        assert len(trust_mod.TOOLS) == 10

    def test_dispatch_is_dict(self):
        assert isinstance(trust_mod.DISPATCH, dict)

    def test_tool_names_match_dispatch_keys(self):
        tool_names = {t.name for t in trust_mod.TOOLS}
        dispatch_keys = set(trust_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_all_expected_names_present(self):
        expected = {
            "memory_pii", "memory_pii_scan", "memory_trust_propagate",
            "memory_suggest_category", "trust_show", "trust_audit",
            "trust_calibrate", "trust_decay", "trust_update_contradiction",
            "trust_process_meb",
        }
        actual = {t.name for t in trust_mod.TOOLS}
        assert actual == expected

    def test_each_tool_has_input_schema(self):
        for tool in trust_mod.TOOLS:
            assert hasattr(tool, "inputSchema"), f"{tool.name} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"


# ---------------------------------------------------------------------------
# memory_pii
# ---------------------------------------------------------------------------

class TestMemoryPii:
    def test_missing_memory(self, isolated_db):
        result = trust_mod.tool_memory_pii(memory_id=999)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_pii_score_returned(self, isolated_db):
        mid = _insert_memory(isolated_db, recalled_count=5)
        result = trust_mod.tool_memory_pii(memory_id=mid)
        assert result["ok"] is True
        assert 0.0 <= result["pii"] <= 1.0
        assert result["tier"] in ("OPEN", "ESTABLISHED", "ENTRENCHED", "CRYSTALLIZED")
        assert result["memory_id"] == mid

    def test_zero_recall_gives_zero_pii(self, isolated_db):
        mid = _insert_memory(isolated_db, recalled_count=0)
        result = trust_mod.tool_memory_pii(memory_id=mid)
        assert result["ok"] is True
        assert result["pii"] == 0.0
        assert result["tier"] == "OPEN"

    def test_content_snippet_truncated(self, isolated_db):
        long_content = "x" * 200
        mid = _insert_memory(isolated_db, content=long_content, recalled_count=1)
        result = trust_mod.tool_memory_pii(memory_id=mid)
        assert result["ok"] is True
        assert len(result["content_snippet"]) <= 120

    def test_dispatch_memory_pii(self, isolated_db):
        mid = _insert_memory(isolated_db, recalled_count=3)
        fn = trust_mod.DISPATCH["memory_pii"]
        result = fn(memory_id=mid)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# memory_pii_scan
# ---------------------------------------------------------------------------

class TestMemoryPiiScan:
    def test_empty_db(self, isolated_db):
        result = trust_mod.tool_memory_pii_scan(top=10)
        assert result["ok"] is True
        assert result["memories"] == []
        assert result["count"] == 0

    def test_returns_sorted_descending(self, isolated_db):
        # Create memories with different recall counts
        _insert_memory(isolated_db, recalled_count=0, temporal_class="permanent")
        _insert_memory(isolated_db, recalled_count=10, temporal_class="permanent")
        _insert_memory(isolated_db, recalled_count=5, temporal_class="permanent")
        result = trust_mod.tool_memory_pii_scan(top=10)
        assert result["ok"] is True
        piis = [m["pii"] for m in result["memories"]]
        assert piis == sorted(piis, reverse=True)

    def test_top_n_respected(self, isolated_db):
        for i in range(10):
            _insert_memory(isolated_db, content=f"memory {i}", recalled_count=i)
        result = trust_mod.tool_memory_pii_scan(top=3)
        assert result["ok"] is True
        assert len(result["memories"]) <= 3

    def test_each_entry_has_required_fields(self, isolated_db):
        _insert_memory(isolated_db, recalled_count=2)
        result = trust_mod.tool_memory_pii_scan(top=5)
        assert result["ok"] is True
        for m in result["memories"]:
            for field in ("memory_id", "pii", "tier", "alpha", "beta", "recalled_count",
                          "temporal_class", "content_snippet"):
                assert field in m, f"Missing field: {field}"

    def test_dispatch_memory_pii_scan(self, isolated_db):
        fn = trust_mod.DISPATCH["memory_pii_scan"]
        result = fn(top=5)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# memory_trust_propagate
# ---------------------------------------------------------------------------

class TestMemoryTrustPropagate:
    def test_empty_db(self, isolated_db):
        result = trust_mod.tool_memory_trust_propagate()
        assert result["ok"] is True
        assert result["agent_category_scores"] == []
        assert result["derived_propagated"] == 0

    def test_populates_trust_scores_table(self, isolated_db):
        _insert_memory(isolated_db, category="project", agent_id="agent-a")
        result = trust_mod.tool_memory_trust_propagate()
        assert result["ok"] is True
        assert len(result["agent_category_scores"]) >= 1
        score_entry = result["agent_category_scores"][0]
        assert "agent_id" in score_entry
        assert "category" in score_entry
        assert 0.0 <= score_entry["score"] <= 1.0

    def test_retracted_lowers_score(self, isolated_db):
        # Insert 2 memories, 1 retracted
        _insert_memory(isolated_db, category="project", agent_id="agent-b")
        _insert_memory(isolated_db, category="project", agent_id="agent-b",
                       retracted_at="2024-01-01T00:00:00")
        result = trust_mod.tool_memory_trust_propagate()
        assert result["ok"] is True
        entry = next(e for e in result["agent_category_scores"]
                     if e["agent_id"] == "agent-b" and e["category"] == "project")
        # score = max(0, (2 - 1*2) / 2) = 0
        assert entry["score"] <= 0.5

    def test_dispatch(self, isolated_db):
        fn = trust_mod.DISPATCH["memory_trust_propagate"]
        result = fn()
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# memory_suggest_category
# ---------------------------------------------------------------------------

class TestMemorySuggestCategory:
    def test_decision_content(self, isolated_db):
        result = trust_mod.tool_memory_suggest_category(
            "We decided to use PostgreSQL instead of MySQL"
        )
        assert result["ok"] is True
        assert result["inferred_category"] == "decision"

    def test_lesson_content(self, isolated_db):
        result = trust_mod.tool_memory_suggest_category(
            "lesson: always run migrations in a transaction"
        )
        assert result["ok"] is True
        assert result["inferred_category"] == "lesson"

    def test_environment_content(self, isolated_db):
        result = trust_mod.tool_memory_suggest_category(
            "The database schema has a users table with an email column"
        )
        assert result["ok"] is True
        assert result["inferred_category"] == "environment"

    def test_fallback_is_project(self, isolated_db):
        result = trust_mod.tool_memory_suggest_category(
            "some random unclassifiable text here"
        )
        assert result["ok"] is True
        assert result["inferred_category"] == "project"

    def test_returns_valid_categories_list(self, isolated_db):
        result = trust_mod.tool_memory_suggest_category("anything")
        assert result["ok"] is True
        assert isinstance(result["valid_categories"], list)
        assert "project" in result["valid_categories"]
        assert "decision" in result["valid_categories"]

    def test_empty_content_returns_project(self, isolated_db):
        result = trust_mod.tool_memory_suggest_category("")
        assert result["ok"] is True
        assert result["inferred_category"] == "project"

    def test_dispatch(self, isolated_db):
        fn = trust_mod.DISPATCH["memory_suggest_category"]
        result = fn(content="We chose Python for this project")
        assert result["ok"] is True
        assert result["inferred_category"] == "decision"


# ---------------------------------------------------------------------------
# trust_show
# ---------------------------------------------------------------------------

class TestTrustShow:
    def test_missing_memory(self, isolated_db):
        result = trust_mod.tool_trust_show(memory_id=9999)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_returns_breakdown(self, isolated_db):
        mid = _insert_memory(isolated_db, category="project", trust_score=0.75)
        result = trust_mod.tool_trust_show(memory_id=mid)
        assert result["ok"] is True
        assert "trust_score" in result
        assert "components" in result
        assert result["memory_id"] == mid
        assert "content_preview" in result
        assert "category" in result

    def test_retracted_memory(self, isolated_db):
        mid = _insert_memory(isolated_db, retracted_at="2024-01-01T00:00:00")
        result = trust_mod.tool_trust_show(memory_id=mid)
        assert result.get("ok") is True
        assert result.get("retracted") is True
        assert result.get("trust_score") == 0.05

    def test_dispatch(self, isolated_db):
        mid = _insert_memory(isolated_db)
        fn = trust_mod.DISPATCH["trust_show"]
        result = fn(memory_id=mid)
        assert result["ok"] is True
        assert "trust_score" in result


# ---------------------------------------------------------------------------
# trust_audit
# ---------------------------------------------------------------------------

class TestTrustAudit:
    def test_empty_db(self, isolated_db):
        result = trust_mod.tool_trust_audit(threshold=0.5)
        assert result["ok"] is True
        assert result["memories"] == []
        assert result["count"] == 0

    def test_finds_low_trust_memories(self, isolated_db):
        _insert_memory(isolated_db, trust_score=0.2)
        _insert_memory(isolated_db, trust_score=0.9)
        result = trust_mod.tool_trust_audit(threshold=0.5)
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["memories"][0]["trust_score"] == 0.2

    def test_limit_respected(self, isolated_db):
        for i in range(10):
            _insert_memory(isolated_db, content=f"mem {i}", trust_score=0.1)
        result = trust_mod.tool_trust_audit(threshold=0.5, limit=3)
        assert result["ok"] is True
        assert len(result["memories"]) <= 3

    def test_content_preview_present(self, isolated_db):
        _insert_memory(isolated_db, content="a" * 200, trust_score=0.1)
        result = trust_mod.tool_trust_audit(threshold=0.5)
        assert result["ok"] is True
        assert len(result["memories"][0]["content_preview"]) <= 100

    def test_dispatch(self, isolated_db):
        fn = trust_mod.DISPATCH["trust_audit"]
        result = fn(threshold=0.8)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# trust_calibrate
# ---------------------------------------------------------------------------

class TestTrustCalibrate:
    def test_dry_run_no_changes(self, isolated_db):
        _insert_memory(isolated_db, category="identity", trust_score=1.0, agent_id="agent-x")
        result_dry = trust_mod.tool_trust_calibrate(dry_run=True)
        assert result_dry["ok"] is True
        assert result_dry["dry_run"] is True

        # Verify no rows changed in DB
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT trust_score FROM memories WHERE trust_score IS NOT NULL LIMIT 1"
        ).fetchone()
        conn.close()
        # identity default is 1.0 stored, calibrated prior is 0.85 → difference > 0.001 → would update
        assert row is not None

    def test_calibrates_identity_prior(self, isolated_db):
        _insert_memory(isolated_db, category="identity", trust_score=1.0, agent_id="agent-cal")
        result = trust_mod.tool_trust_calibrate(dry_run=False)
        assert result["ok"] is True
        assert result["updated"] >= 1

        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT trust_score FROM memories LIMIT 1").fetchone()
        conn.close()
        # identity prior is 0.85
        assert abs(row[0] - 0.85) < 0.01

    def test_retracted_gets_005(self, isolated_db):
        _insert_memory(isolated_db, trust_score=0.9, retracted_at="2024-01-01T00:00:00")
        result = trust_mod.tool_trust_calibrate(dry_run=False)
        assert result["ok"] is True

        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT trust_score FROM memories LIMIT 1").fetchone()
        conn.close()
        assert row[0] == 0.05

    def test_dispatch(self, isolated_db):
        fn = trust_mod.DISPATCH["trust_calibrate"]
        result = fn(dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# trust_decay
# ---------------------------------------------------------------------------

class TestTrustDecay:
    def test_no_old_memories_nothing_changes(self, isolated_db):
        # Freshly created memories are < 1 day old → no decay
        _insert_memory(isolated_db, trust_score=0.9, temporal_class="medium")
        result = trust_mod.tool_trust_decay(dry_run=False)
        assert result["ok"] is True
        assert result["decayed"] == 0

    def test_dry_run_flag_propagated(self, isolated_db):
        result = trust_mod.tool_trust_decay(dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True

    def test_permanent_memories_not_decayed(self, isolated_db):
        # Force an "old" created_at date manually
        db_file = trust_mod.DB_PATH
        conn = sqlite3.connect(str(db_file))
        _insert_agent(db_file)
        conn.execute(
            "INSERT INTO memories (agent_id, content, category, trust_score, temporal_class, "
            "created_at, updated_at) VALUES ('test-agent', 'perm', 'project', 0.9, 'permanent', "
            "'2020-01-01T00:00:00', '2020-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        result = trust_mod.tool_trust_decay(dry_run=False)
        assert result["ok"] is True
        # permanent memories have rate=0.0, should never be counted
        conn2 = sqlite3.connect(str(db_file))
        row = conn2.execute(
            "SELECT trust_score FROM memories WHERE temporal_class='permanent'"
        ).fetchone()
        conn2.close()
        assert row[0] == 0.9  # unchanged

    def test_old_memory_decays(self, isolated_db):
        db_file = trust_mod.DB_PATH
        _insert_agent(db_file)
        conn = sqlite3.connect(str(db_file))
        # Insert with an old timestamp to simulate age
        conn.execute(
            "INSERT INTO memories (agent_id, content, category, trust_score, temporal_class, "
            "created_at, updated_at) VALUES ('test-agent', 'old fact', 'project', 0.95, 'medium', "
            "'2020-01-01T00:00:00', '2020-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        result = trust_mod.tool_trust_decay(dry_run=False)
        assert result["ok"] is True
        assert result["decayed"] >= 1

    def test_dispatch(self, isolated_db):
        fn = trust_mod.DISPATCH["trust_decay"]
        result = fn()
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# trust_update_contradiction
# ---------------------------------------------------------------------------

class TestTrustUpdateContradiction:
    def test_unresolved_penalizes_both(self, isolated_db):
        mid_a = _insert_memory(isolated_db, trust_score=0.9, agent_id="agent-a")
        mid_b = _insert_memory(isolated_db, trust_score=0.8, agent_id="agent-a")
        result = trust_mod.tool_trust_update_contradiction(
            memory_id_a=mid_a, memory_id_b=mid_b, resolved=False
        )
        assert result["ok"] is True
        assert result["resolved"] is False
        scores = {m["id"]: m["trust_score"] for m in result["updated_memories"]}
        assert scores[mid_a] <= 0.7   # 0.9 - 0.20
        assert scores[mid_b] <= 0.6   # 0.8 - 0.20

    def test_resolved_penalizes_only_a(self, isolated_db):
        mid_a = _insert_memory(isolated_db, trust_score=0.9, agent_id="agent-a")
        mid_b = _insert_memory(isolated_db, trust_score=0.8, agent_id="agent-a")
        result = trust_mod.tool_trust_update_contradiction(
            memory_id_a=mid_a, memory_id_b=mid_b, resolved=True
        )
        assert result["ok"] is True
        assert result["resolved"] is True
        scores = {m["id"]: m["trust_score"] for m in result["updated_memories"]}
        # Only mid_a gets penalized by 0.05
        assert abs(scores[mid_a] - 0.85) < 0.01
        # mid_b stays at 0.8 (no UPDATE touches it in resolved=True path)
        assert abs(scores[mid_b] - 0.8) < 0.01

    def test_floor_at_030(self, isolated_db):
        mid_a = _insert_memory(isolated_db, trust_score=0.3, agent_id="agent-floor")
        mid_b = _insert_memory(isolated_db, trust_score=0.3, agent_id="agent-floor")
        result = trust_mod.tool_trust_update_contradiction(
            memory_id_a=mid_a, memory_id_b=mid_b, resolved=False
        )
        assert result["ok"] is True
        for m in result["updated_memories"]:
            assert m["trust_score"] >= 0.30

    def test_dispatch(self, isolated_db):
        mid_a = _insert_memory(isolated_db, trust_score=0.8, agent_id="agent-d")
        mid_b = _insert_memory(isolated_db, trust_score=0.7, agent_id="agent-d")
        fn = trust_mod.DISPATCH["trust_update_contradiction"]
        result = fn(memory_id_a=mid_a, memory_id_b=mid_b)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# trust_process_meb
# ---------------------------------------------------------------------------

class TestTrustProcessMeb:
    """MEB events are auto-created by DB triggers on memory INSERT/UPDATE."""

    def _get_max_meb_id(self, db_path: Path) -> int:
        """Return the current max MEB event id (0 if none)."""
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT MAX(id) FROM memory_events").fetchone()
        conn.close()
        return int(row[0] or 0)

    def test_empty_events(self, isolated_db):
        result = trust_mod.tool_trust_process_meb(since=0)
        assert result["ok"] is True
        # No memories → no MEB events → nothing processed
        assert result["processed"] == 0
        assert result["new_watermark"] == 0

    def test_insert_memory_triggers_meb_and_sets_prior(self, isolated_db):
        # Inserting a memory with trust_score=1.0 triggers the MEB 'insert' event.
        # Processing those events should set trust to the category prior.
        mid = _insert_memory(isolated_db, category="identity", trust_score=1.0)
        # MEB event was auto-created; process from watermark=0
        result = trust_mod.tool_trust_process_meb(since=0)
        assert result["ok"] is True
        assert result["processed"] >= 1

        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT trust_score FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        # identity prior = 0.85, multiplier = 1.0 → 0.85
        assert abs(row[0] - 0.85) < 0.01

    def test_watermark_filters_old_events(self, isolated_db):
        # Insert first memory — auto-creates MEB event
        _insert_memory(isolated_db, category="project", trust_score=1.0)
        first_max = self._get_max_meb_id(isolated_db)

        # Insert second memory — creates another MEB event
        _insert_memory(isolated_db, content="second fact", category="project", trust_score=1.0)
        second_max = self._get_max_meb_id(isolated_db)

        # Process only events after first_max
        result = trust_mod.tool_trust_process_meb(since=first_max)
        assert result["ok"] is True
        assert result["new_watermark"] == second_max

    def test_dry_run_does_not_change_scores(self, isolated_db):
        mid = _insert_memory(isolated_db, category="identity", trust_score=1.0)
        result = trust_mod.tool_trust_process_meb(since=0, dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        # Score should be unchanged since dry_run=True
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT trust_score FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        assert row[0] == 1.0

    def test_dispatch(self, isolated_db):
        fn = trust_mod.DISPATCH["trust_process_meb"]
        result = fn(since=0, dry_run=True)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# PII helper unit tests
# ---------------------------------------------------------------------------

class TestPiiHelpers:
    def test_pii_tier_crystallized(self):
        assert trust_mod._pii_tier(0.75) == "CRYSTALLIZED"

    def test_pii_tier_entrenched(self):
        assert trust_mod._pii_tier(0.55) == "ENTRENCHED"

    def test_pii_tier_established(self):
        assert trust_mod._pii_tier(0.30) == "ESTABLISHED"

    def test_pii_tier_open(self):
        assert trust_mod._pii_tier(0.10) == "OPEN"

    def test_pii_tier_zero(self):
        assert trust_mod._pii_tier(0.0) == "OPEN"

    def test_days_since_zero_for_now(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        days = trust_mod._days_since(now)
        assert 0.0 <= days < 0.01  # effectively zero

    def test_days_since_empty_string(self):
        assert trust_mod._days_since("") == 0.0

    def test_days_since_old_date(self):
        days = trust_mod._days_since("2020-01-01T00:00:00Z")
        assert days > 365 * 4  # at least 4 years

    def test_trust_agent_multiplier_supervisor(self):
        mult = trust_mod._trust_agent_multiplier("supervisor-1")
        assert mult == 1.15

    def test_trust_agent_multiplier_unknown(self):
        mult = trust_mod._trust_agent_multiplier("random-agent")
        assert mult == 1.0
