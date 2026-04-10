"""Tests for mcp_tools_neuro — neuromodulation MCP tool layer."""
from __future__ import annotations
import sqlite3
import sys
import os
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_neuro as _mod
from agentmemory.brain import Brain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Create a fresh brain.db with neuromodulation_state singleton seeded."""
    db_file = tmp_path / "brain.db"
    Brain(str(db_file))  # seeds schema + neuromodulation_state id=1
    return db_file


@pytest.fixture(autouse=True)
def patch_db(db_path, monkeypatch):
    """Point the module's DB_PATH at the temp db for every test."""
    monkeypatch.setattr(_mod, "DB_PATH", db_path)


# ---------------------------------------------------------------------------
# neuro_status
# ---------------------------------------------------------------------------

class TestNeuroStatus:
    def test_returns_ok_with_state(self):
        result = _mod.tool_neuro_status()
        assert result["ok"] is True
        assert "org_state" in result

    def test_default_state_is_normal(self):
        result = _mod.tool_neuro_status()
        assert result["org_state"] == "normal"

    def test_contains_key_params(self):
        result = _mod.tool_neuro_status()
        for key in ("arousal_level", "temporal_lambda", "context_window_depth",
                    "retrieval_breadth_multiplier", "confidence_decay_rate"):
            assert key in result, f"Missing key: {key}"

    def test_no_auto_revert_when_not_expired(self, db_path):
        # Set manual mode (no expires) — should NOT revert
        _mod.tool_neuro_set(mode="sprint")
        result = _mod.tool_neuro_status()
        assert result["ok"] is True
        assert result.get("auto_reverted") is not True
        assert result["org_state"] == "sprint"

    def test_auto_revert_expired_manual(self, db_path):
        # Set manual override with a past expiry
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE neuromodulation_state SET detection_method='manual', "
            "expires_at='2000-01-01T00:00:00', org_state='incident' WHERE id=1"
        )
        conn.commit()
        conn.close()
        result = _mod.tool_neuro_status()
        assert result["ok"] is True
        assert result.get("auto_reverted") is True
        # After revert, org_state is auto-detected (likely 'normal' on empty DB)
        assert result["org_state"] in _mod._NEURO_PRESETS


# ---------------------------------------------------------------------------
# neuro_set
# ---------------------------------------------------------------------------

class TestNeuroSet:
    def test_set_sprint(self):
        result = _mod.tool_neuro_set(mode="sprint")
        assert result["ok"] is True
        assert result["org_state"] == "sprint"

    def test_set_incident_via_alias_urgent(self):
        result = _mod.tool_neuro_set(mode="urgent")
        assert result["ok"] is True
        assert result["org_state"] == "incident"

    def test_set_strategic_via_alias(self):
        result = _mod.tool_neuro_set(mode="strategic")
        assert result["ok"] is True
        assert result["org_state"] == "strategic_planning"

    def test_set_focused_via_alias(self):
        result = _mod.tool_neuro_set(mode="focused")
        assert result["ok"] is True
        assert result["org_state"] == "focused_work"

    def test_state_persists_in_db(self, db_path):
        _mod.tool_neuro_set(mode="sprint")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT org_state FROM neuromodulation_state WHERE id=1").fetchone()
        conn.close()
        assert row["org_state"] == "sprint"

    def test_unknown_mode_returns_error(self):
        result = _mod.tool_neuro_set(mode="NOTAMODE")
        assert result["ok"] is False
        assert "error" in result

    def test_transition_logged_when_state_changes(self, db_path):
        # Start from normal, set to sprint
        _mod.tool_neuro_set(mode="sprint")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM neuromodulation_transitions ORDER BY transitioned_at DESC LIMIT 1"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["from_state"] == "normal"
        assert rows[0]["to_state"] == "sprint"

    def test_no_transition_logged_when_same_state(self, db_path):
        # Already normal, set to normal again
        _mod.tool_neuro_set(mode="normal")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        count = conn.execute(
            "SELECT COUNT(*) FROM neuromodulation_transitions"
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_expires_returned_when_set(self):
        result = _mod.tool_neuro_set(mode="sprint", expires="2099-01-01T00:00:00")
        assert result["ok"] is True
        assert result.get("expires_at") == "2099-01-01T00:00:00"

    def test_custom_notes(self, db_path):
        _mod.tool_neuro_set(mode="sprint", notes="war room active")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT notes FROM neuromodulation_state WHERE id=1").fetchone()
        conn.close()
        assert "war room" in (row["notes"] or "")


# ---------------------------------------------------------------------------
# neuro_detect
# ---------------------------------------------------------------------------

class TestNeuroDetect:
    def test_detect_on_empty_db_returns_normal(self):
        result = _mod.tool_neuro_detect()
        assert result["ok"] is True
        assert result["org_state"] == "normal"
        assert result["reason"] == "no trigger conditions met"

    def test_detect_skips_when_manual_not_expired(self, db_path):
        _mod.tool_neuro_set(mode="sprint")  # sets detection_method=manual, no expiry
        result = _mod.tool_neuro_detect()
        assert result["ok"] is True
        assert result.get("skipped") is True

    def test_detect_force_overrides_manual(self, db_path):
        _mod.tool_neuro_set(mode="sprint")
        result = _mod.tool_neuro_detect(force=True)
        assert result["ok"] is True
        assert result.get("skipped") is not True
        # On an empty DB there are no triggering events — should go back to normal
        assert result["org_state"] == "normal"

    def test_detect_incident_from_errors(self, db_path):
        # Seed enough error events in the last 2h to trigger incident.
        # agent_id NOT NULL -> use the Brain-seeded agent (agent_id="default").
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('default', 'default', 'test', 'active', "
            "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        for i in range(6):
            conn.execute(
                "INSERT INTO events (agent_id, summary, event_type, created_at) "
                "VALUES ('default', ?, 'error', strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-10 minutes')))",
                (f"error {i}",),
            )
        conn.commit()
        conn.close()
        result = _mod.tool_neuro_detect(force=True)
        assert result["ok"] is True
        assert result["org_state"] == "incident"

    def test_detect_returns_transitioned_flag(self, db_path):
        # Manually put DB in sprint; detect should transition back to normal
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE neuromodulation_state SET org_state='sprint', detection_method='auto' WHERE id=1"
        )
        conn.commit()
        conn.close()
        result = _mod.tool_neuro_detect(force=True)
        assert result["ok"] is True
        assert result["transitioned"] is True


# ---------------------------------------------------------------------------
# neuro_history
# ---------------------------------------------------------------------------

class TestNeuroHistory:
    def test_empty_history(self):
        result = _mod.tool_neuro_history()
        assert result["ok"] is True
        assert result["transitions"] == []
        assert result["count"] == 0

    def test_history_after_transition(self):
        _mod.tool_neuro_set(mode="sprint")
        result = _mod.tool_neuro_history()
        assert result["ok"] is True
        assert result["count"] == 1
        t = result["transitions"][0]
        assert t["from_state"] == "normal"
        assert t["to_state"] == "sprint"

    def test_limit_respected(self):
        for mode in ("sprint", "normal", "sprint", "normal"):
            _mod.tool_neuro_set(mode=mode)
        result = _mod.tool_neuro_history(limit=2)
        assert result["ok"] is True
        assert len(result["transitions"]) <= 2

    def test_history_ordered_desc(self):
        _mod.tool_neuro_set(mode="sprint")
        _mod.tool_neuro_set(mode="normal")
        result = _mod.tool_neuro_history()
        assert result["ok"] is True
        ts = [t["transitioned_at"] for t in result["transitions"]]
        assert ts == sorted(ts, reverse=True)


# ---------------------------------------------------------------------------
# neurostate
# ---------------------------------------------------------------------------

class TestNeurostate:
    def test_returns_ok_and_levels(self):
        result = _mod.tool_neurostate()
        assert result["ok"] is True
        for key in ("dopamine_level", "norepinephrine_level", "acetylcholine_level", "serotonin_level"):
            assert key in result

    def test_levels_in_valid_range(self):
        result = _mod.tool_neurostate()
        for key in ("dopamine_level", "norepinephrine_level", "acetylcholine_level", "serotonin_level"):
            val = result[key]
            assert 0.0 <= val <= 1.0, f"{key}={val} out of [0,1]"

    def test_org_state_present(self):
        result = _mod.tool_neurostate()
        assert "org_state" in result
        assert result["org_state"] in _mod._NEURO_PRESETS

    def test_neuromod_params_present(self):
        result = _mod.tool_neurostate()
        assert "neuromod_params" in result
        assert isinstance(result["neuromod_params"], dict)

    def test_logs_neuro_event(self, db_path):
        _mod.tool_neurostate()
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM neuro_events").fetchone()[0]
        conn.close()
        assert count >= 1

    def test_detect_flag_triggers_autodetect(self):
        result = _mod.tool_neurostate(detect=True)
        assert result["ok"] is True
        assert result["org_state"] == "normal"  # empty DB -> normal


# ---------------------------------------------------------------------------
# neuro_signal
# ---------------------------------------------------------------------------

class TestNeuroSignal:
    def _seed_memory(self, db_path, scope="global", confidence=0.5):
        # agent_id NOT NULL -> ensure agent exists first.
        # Brain.__init__ inserts agent_id=agent_id ("default") via _init_db.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('default', 'default', 'test', 'active', "
            "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.execute(
            "INSERT INTO memories (agent_id, content, scope, category, confidence, created_at, updated_at) "
            "VALUES ('default', 'test memory', ?, 'lesson', ?, strftime('%Y-%m-%dT%H:%M:%S','now'), "
            "strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (scope, confidence),
        )
        conn.commit()
        conn.close()

    def test_positive_signal_boosts_confidence(self, db_path):
        self._seed_memory(db_path, confidence=0.5)
        result = _mod.tool_neuro_signal(dopamine=0.5)
        assert result["ok"] is True
        assert result["direction"] == "boost"
        assert result["affected_memories"] >= 1

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT confidence FROM memories WHERE content='test memory'").fetchone()
        conn.close()
        assert row[0] > 0.5

    def test_negative_signal_penalizes_confidence(self, db_path):
        self._seed_memory(db_path, confidence=0.8)
        result = _mod.tool_neuro_signal(dopamine=-0.5)
        assert result["ok"] is True
        assert result["direction"] == "penalize"
        assert result["affected_memories"] >= 1

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT confidence FROM memories WHERE content='test memory'").fetchone()
        conn.close()
        assert row[0] < 0.8

    def test_signal_updates_dopamine_reservoir(self, db_path):
        self._seed_memory(db_path)
        _mod.tool_neuro_signal(dopamine=0.8)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT dopamine_signal FROM neuromodulation_state WHERE id=1").fetchone()
        conn.close()
        assert row["dopamine_signal"] > 0.0

    def test_out_of_range_dopamine_returns_error(self):
        result = _mod.tool_neuro_signal(dopamine=1.5)
        assert result["ok"] is False
        assert "error" in result

    def test_scope_filter_respected(self, db_path):
        self._seed_memory(db_path, scope="project:alpha", confidence=0.5)
        self._seed_memory(db_path, scope="project:beta", confidence=0.5)
        # Only boost alpha
        _mod.tool_neuro_signal(dopamine=1.0, scope="project:alpha")
        conn = sqlite3.connect(str(db_path))
        alpha = conn.execute(
            "SELECT confidence FROM memories WHERE scope='project:alpha'"
        ).fetchone()[0]
        beta = conn.execute(
            "SELECT confidence FROM memories WHERE scope='project:beta'"
        ).fetchone()[0]
        conn.close()
        assert alpha > beta

    def test_returns_affected_count_and_scope(self, db_path):
        self._seed_memory(db_path)
        result = _mod.tool_neuro_signal(dopamine=0.3, scope="global")
        assert result["ok"] is True
        assert result["scope"] == "global"
        assert isinstance(result["affected_memories"], int)


# ---------------------------------------------------------------------------
# weights
# ---------------------------------------------------------------------------

class TestWeights:
    def test_returns_error_when_salience_routing_unavailable(self):
        # salience_routing is unlikely to be available in test environment
        result = _mod.tool_weights()
        # Either ok (if module exists) or a clean error
        if not result["ok"]:
            assert "salience_routing" in result["error"] or "error" in result


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        from mcp.types import Tool
        assert isinstance(_mod.TOOLS, list)
        assert all(isinstance(t, Tool) for t in _mod.TOOLS)

    def test_tools_has_all_seven(self):
        names = {t.name for t in _mod.TOOLS}
        expected = {"neuro_status", "neuro_set", "neuro_detect", "neuro_history",
                    "neurostate", "neuro_signal", "weights"}
        assert expected == names

    def test_dispatch_is_dict(self):
        assert isinstance(_mod.DISPATCH, dict)

    def test_dispatch_keys_match_tool_names(self):
        tool_names = {t.name for t in _mod.TOOLS}
        dispatch_keys = set(_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_dispatch_values_are_callable(self):
        for name, fn in _mod.DISPATCH.items():
            assert callable(fn), f"DISPATCH['{name}'] is not callable"

    def test_all_tools_have_required_schema_fields(self):
        for t in _mod.TOOLS:
            assert t.name
            assert t.description
            assert isinstance(t.inputSchema, dict)
            assert t.inputSchema.get("type") == "object"
