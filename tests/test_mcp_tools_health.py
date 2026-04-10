"""Tests for mcp_tools_health — health & maintenance MCP tools."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentmemory.brain import Brain
import agentmemory.mcp_tools_health as health_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_db(tmp_path: Path) -> Path:
    """Create a fresh brain.db using Brain, return its path."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")
    return db_file


def _patch_db(monkeypatch, db_file: Path) -> None:
    """Point the health module at the test DB."""
    monkeypatch.setattr(health_mod, "DB_PATH", db_file)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_fresh_db(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = health_mod._validate()

        assert result["ok"] is True
        assert result["valid"] is True
        assert result["issues"] == []

    def test_reports_missing_table(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        # Drop a required table
        conn = sqlite3.connect(str(db_file))
        conn.execute("DROP TABLE IF EXISTS memory_trust_scores")
        conn.commit()
        conn.close()

        result = health_mod._validate()

        assert result["ok"] is True
        assert result["valid"] is False
        assert any("memory_trust_scores" in i for i in result["issues"])

    def test_dispatch_validate(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = health_mod.DISPATCH["validate"]
        result = fn({})
        assert result["ok"] is True
        assert "valid" in result


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_ok_on_empty_db(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = health_mod._health(window_days=7)

        assert result["ok"] is True
        assert "composite_score" in result
        assert "overall" in result
        assert result["overall"] in ("healthy", "degraded", "critical")
        assert "metrics" in result
        assert "alerts" in result

    def test_window_days_respected(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        r1 = health_mod._health(window_days=1)
        r30 = health_mod._health(window_days=30)
        # Both should succeed; window is reflected in the response
        assert r1["ok"] is True
        assert r1["window_days"] == 1
        assert r30["window_days"] == 30

    def test_metrics_keys_present(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = health_mod._health()
        metrics = result["metrics"]

        for key in ("coverage", "coverage_hi", "engagement_rate", "avg_confidence",
                    "recall_gini", "category_hhi", "scope_hhi",
                    "vec_coverage", "contradictions", "bayesian_ab_coverage"):
            assert key in metrics, f"Missing metric key: {key}"

    def test_dispatch_health(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = health_mod.DISPATCH["health"]
        result = fn({"window_days": 14})
        assert result["ok"] is True
        assert result["window_days"] == 14


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

class TestLint:
    def test_clean_db_is_healthy(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = health_mod._lint()

        assert result["ok"] is True
        assert result["health"] == "healthy"
        assert result["issues"] == 0
        assert result["fixed"] == 0

    def test_low_confidence_memory_flagged(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        # Insert an agent and a low-confidence memory
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('tester', 'tester', 'test', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.execute(
            "INSERT INTO memories (agent_id, content, category, scope, confidence, recalled_count, created_at, updated_at) "
            "VALUES ('tester', 'Very uncertain fact', 'lesson', 'agent', 0.1, 0, strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.commit()
        conn.close()

        result = health_mod._lint()

        assert result["ok"] is True
        checks = {c["check"]: c for c in result["checks"]}
        assert "low_confidence" in checks
        assert checks["low_confidence"]["severity"] == "warning"

    def test_never_recalled_memory_flagged(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('tester', 'tester', 'test', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        # Insert memory with recalled_count = 0 and decent confidence
        conn.execute(
            "INSERT INTO memories (agent_id, content, category, scope, confidence, recalled_count, created_at, updated_at) "
            "VALUES ('tester', 'Never recalled memory', 'lesson', 'agent', 0.9, 0, strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.commit()
        conn.close()

        result = health_mod._lint()

        assert result["ok"] is True
        checks = {c["check"]: c for c in result["checks"]}
        assert "never_recalled" in checks

    def test_fix_flag_accepted(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = health_mod._lint(fix=True)
        assert result["ok"] is True
        assert "fixed" in result

    def test_dispatch_lint(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = health_mod.DISPATCH["lint"]
        result = fn({"fix": False})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

class TestBackup:
    def test_creates_backup_at_dest_path(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        dest = tmp_path / "backups" / "manual.db"
        result = health_mod._backup(dest_path=str(dest))

        assert result["ok"] is True
        assert Path(result["backup"]).exists()
        assert result["size_bytes"] > 0
        assert Path(result["backup"]) == dest

    def test_creates_timestamped_backup_by_default(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        # Override the backups dir via env var so it lands in tmp_path
        backups_dir = tmp_path / "backups"
        monkeypatch.setenv("BRAINCTL_BACKUPS_DIR", str(backups_dir))

        result = health_mod._backup()

        assert result["ok"] is True
        backup_path = Path(result["backup"])
        assert backup_path.exists()
        assert backup_path.suffix == ".db"
        assert "brain_" in backup_path.name

    def test_backup_is_valid_sqlite(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        dest = tmp_path / "copy.db"
        result = health_mod._backup(dest_path=str(dest))

        assert result["ok"] is True
        # Verify the backup is a readable SQLite file
        conn = sqlite3.connect(str(dest))
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        assert len(tables) > 0

    def test_dispatch_backup(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        dest = tmp_path / "dispatch_backup.db"
        fn = health_mod.DISPATCH["backup"]
        result = fn({"dest_path": str(dest)})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# budget_status
# ---------------------------------------------------------------------------

class TestBudgetStatus:
    def test_empty_db_returns_ok(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        result = health_mod._budget_status()

        assert result["ok"] is True
        assert "date" in result
        assert "fleet_total" in result
        assert result["fleet_total"] == 0
        assert result["agents"] == []
        assert result["at_cap"] == []

    def test_agent_token_consumption_aggregated(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, attention_budget_tier, created_at, updated_at) "
            "VALUES ('worker1', 'Worker One', 'test', 'active', 3, strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        # Insert two access_log entries today with tokens_consumed
        today = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
        for tokens in (100, 200):
            conn.execute(
                "INSERT INTO access_log (agent_id, action, tokens_consumed, created_at) "
                "VALUES ('worker1', 'search', ?, ?)",
                (tokens, f"{today} 10:00:00"),
            )
        conn.commit()
        conn.close()

        result = health_mod._budget_status()

        assert result["ok"] is True
        assert result["fleet_total"] == 300
        assert len(result["agents"]) == 1
        agent = result["agents"][0]
        assert agent["agent_id"] == "worker1"
        assert agent["tokens_today"] == 300
        assert agent["queries_today"] == 2
        assert agent["tier"] == 3
        assert agent["ceiling"] == 500  # Tier 3 ceiling

    def test_dispatch_budget_status(self, tmp_path, monkeypatch):
        db_file = _init_db(tmp_path)
        _patch_db(monkeypatch, db_file)

        fn = health_mod.DISPATCH["budget_status"]
        result = fn({})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_tools_list_has_five_entries(self):
        assert len(health_mod.TOOLS) == 5

    def test_tool_names_match_dispatch_keys(self):
        tool_names = {t.name for t in health_mod.TOOLS}
        dispatch_keys = set(health_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys == {"validate", "health", "lint", "backup", "budget_status"}

    def test_all_dispatch_values_are_callable(self):
        for name, fn in health_mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"

    def test_all_tools_have_input_schema(self):
        for tool in health_mod.TOOLS:
            assert tool.inputSchema is not None, f"Tool {tool.name!r} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"
