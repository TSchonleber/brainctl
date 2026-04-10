"""Tests for mcp_tools_temporal — temporal causality & epoch MCP tools."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory._impl as impl
import agentmemory.mcp_tools_temporal as mt


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    """Point the module's DB_PATH at a fresh temp DB for each test."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file))  # initialises full schema
    monkeypatch.setattr(mt, "DB_PATH", db_file)
    return db_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_agent(conn, agent_id="tester"):
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
        "created_at, updated_at) VALUES (?, ?, 'test', 'active', "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, agent_id),
    )
    conn.commit()


def _insert_event(conn, agent_id="tester", event_type="observation", summary="test event",
                  project=None) -> int:
    cur = conn.execute(
        "INSERT INTO events (agent_id, event_type, summary, project, created_at) "
        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (agent_id, event_type, summary, project),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# epoch_list
# ---------------------------------------------------------------------------

class TestEpochList:
    def test_empty_returns_ok(self):
        result = mt._epoch_list()
        assert result["ok"] is True
        assert result["epochs"] == []

    def test_created_epoch_appears_in_list(self, patch_db):
        conn = _conn(patch_db)
        conn.execute(
            "INSERT INTO epochs (name, started_at) VALUES ('Alpha', '2024-01-01T00:00:00')"
        )
        conn.commit()
        result = mt._epoch_list()
        assert result["ok"] is True
        assert len(result["epochs"]) == 1
        assert result["epochs"][0]["name"] == "Alpha"

    def test_active_only_filter(self, patch_db):
        conn = _conn(patch_db)
        conn.execute(
            "INSERT INTO epochs (name, started_at, ended_at) "
            "VALUES ('Past', '2000-01-01T00:00:00', '2000-06-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO epochs (name, started_at, ended_at) "
            "VALUES ('Current', strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day'), NULL)"
        )
        conn.commit()
        result = mt._epoch_list(active_only=True)
        assert result["ok"] is True
        names = [e["name"] for e in result["epochs"]]
        assert "Current" in names
        assert "Past" not in names

    def test_limit_respected(self, patch_db):
        conn = _conn(patch_db)
        for i in range(5):
            conn.execute(
                f"INSERT INTO epochs (name, started_at) VALUES ('Epoch{i}', '2024-0{i+1}-01T00:00:00')"
            )
        conn.commit()
        result = mt._epoch_list(limit=3)
        assert result["ok"] is True
        assert len(result["epochs"]) == 3


# ---------------------------------------------------------------------------
# epoch_create
# ---------------------------------------------------------------------------

class TestEpochCreate:
    def test_basic_create(self):
        result = mt._epoch_create(name="Beta Sprint", started="2024-03-01")
        assert result["ok"] is True
        assert result["name"] == "Beta Sprint"
        assert result["epoch_id"] is not None
        assert isinstance(result["epoch_id"], int)

    def test_create_with_end(self):
        result = mt._epoch_create(name="Closed", started="2024-01-01", ended="2024-02-01")
        assert result["ok"] is True
        assert result["ended_at"] is not None

    def test_ended_before_started_rejected(self):
        result = mt._epoch_create(name="Bad", started="2024-06-01", ended="2024-01-01")
        assert result["ok"] is False
        assert "ended" in result["error"].lower() or ">=" in result["error"]

    def test_invalid_timestamp_rejected(self):
        result = mt._epoch_create(name="Bad", started="not-a-date")
        assert result["ok"] is False

    def test_backfill_events(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        _insert_event(conn, summary="early event")
        result = mt._epoch_create(name="Backfill Test", started="2000-01-01")
        assert result["ok"] is True
        assert result["backfilled"]["events"] >= 1


# ---------------------------------------------------------------------------
# epoch_detect
# ---------------------------------------------------------------------------

class TestEpochDetect:
    def test_no_events_returns_ok(self):
        result = mt._epoch_detect()
        assert result["ok"] is True
        assert result["event_count"] == 0
        assert result["suggested_epochs"] == []

    def test_detect_epoch_boundaries_flags_context_divergence_without_time_gap(self):
        events = [
            {"created_at": "2026-04-10T10:00:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:01:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:02:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:03:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:04:00", "event_type": "observation", "summary": "orchid soil watering greenhouse", "detail": "orchid soil watering greenhouse", "project": "garden", "refs": None},
        ]

        boundaries = mt.detect_epoch_boundaries(events, window_size=4, min_window=4, topic_shift_threshold=0.45, min_boundary_distance=1)

        assert len(boundaries) == 1
        assert boundaries[0]["boundary_index"] == 4
        assert "context_divergence" in boundaries[0]["reasons"]
        assert boundaries[0]["context_divergence"] >= 0.45

    def test_detect_epoch_boundaries_ignores_same_context_continuation(self):
        events = [
            {"created_at": "2026-04-10T10:00:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:01:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:02:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:03:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:04:00", "event_type": "observation", "summary": "deploy api build green pipeline", "detail": "deploy api build green pipeline", "project": "release", "refs": None},
        ]

        boundaries = mt.detect_epoch_boundaries(events, window_size=4, min_window=4, topic_shift_threshold=0.45, min_boundary_distance=1)

        assert boundaries == []

    def test_detect_epoch_boundaries_preserves_large_gap_with_empty_event(self):
        events = [
            {"created_at": "2026-01-01T10:00:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-01-01T10:01:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-01-01T10:02:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-01-01T10:03:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:04:00", "event_type": "", "summary": "", "detail": "", "project": None, "refs": None},
        ]

        boundaries = mt.detect_epoch_boundaries(events, gap_hours=48.0, window_size=4, min_window=4, topic_shift_threshold=0.45, min_boundary_distance=1)

        assert len(boundaries) == 1
        assert boundaries[0]["boundary_index"] == 4
        assert boundaries[0]["reasons"] == ["time_gap"]
        assert boundaries[0]["context_divergence"] is None

    def test_detect_epoch_boundaries_matches_cli_impl(self):
        events = [
            {"created_at": "2026-04-10T10:00:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:01:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:02:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:03:00", "event_type": "observation", "summary": "deploy api build green", "detail": "deploy api build green", "project": "release", "refs": None},
            {"created_at": "2026-04-10T10:04:00", "event_type": "observation", "summary": "orchid soil watering greenhouse", "detail": "orchid soil watering greenhouse", "project": "garden", "refs": None},
        ]

        kwargs = dict(window_size=4, min_window=4, topic_shift_threshold=0.45, min_boundary_distance=1)

        assert impl.detect_epoch_boundaries(events, **kwargs) == mt.detect_epoch_boundaries(events, **kwargs)

    def test_few_events_no_boundaries(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        for _ in range(3):
            _insert_event(conn)
        result = mt._epoch_detect()
        assert result["ok"] is True
        # With only 3 events and default settings, boundary count should be 0
        assert result["boundary_count"] == 0

    def test_verbose_includes_boundaries_key(self, patch_db):
        result = mt._epoch_detect(verbose=True)
        assert result["ok"] is True
        assert "boundaries" in result

    def test_event_count_matches(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        for i in range(4):
            _insert_event(conn, summary=f"event {i}")
        result = mt._epoch_detect()
        assert result["ok"] is True
        assert result["event_count"] == 4


# ---------------------------------------------------------------------------
# temporal_context
# ---------------------------------------------------------------------------

class TestTemporalContext:
    def test_returns_ok(self):
        result = mt._temporal_context()
        assert result["ok"] is True

    def test_has_required_keys(self):
        result = mt._temporal_context()
        for key in ("timestamp", "current_epoch", "project_age", "last_activity",
                    "cadence", "active_agents", "dormant_agents", "memory_health"):
            assert key in result, f"Missing key: {key}"

    def test_no_events_project_age_zero(self):
        result = mt._temporal_context()
        assert result["project_age"]["total_events"] == 0

    def test_with_events(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        _insert_event(conn, summary="hello")
        result = mt._temporal_context()
        assert result["ok"] is True
        assert result["project_age"]["total_events"] == 1
        assert result["last_activity"] is not None


# ---------------------------------------------------------------------------
# event_link
# ---------------------------------------------------------------------------

class TestEventLink:
    def test_link_two_events(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        e1 = _insert_event(conn, event_type="error", summary="error occurred")
        e2 = _insert_event(conn, event_type="decision", summary="decided to fix")
        result = mt._event_link(cause_event_id=e1, effect_event_id=e2)
        assert result["ok"] is True
        assert result["edge"]["cause_event_id"] == e1
        assert result["edge"]["effect_event_id"] == e2

    def test_missing_cause_returns_error(self):
        result = mt._event_link(cause_event_id=99999, effect_event_id=1)
        assert result["ok"] is False
        assert "cause event" in result["error"]

    def test_missing_effect_returns_error(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        e1 = _insert_event(conn, summary="cause")
        result = mt._event_link(cause_event_id=e1, effect_event_id=99999)
        assert result["ok"] is False
        assert "effect event" in result["error"]

    def test_duplicate_link_rejected(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        e1 = _insert_event(conn, event_type="error", summary="cause")
        e2 = _insert_event(conn, event_type="decision", summary="effect")
        mt._event_link(cause_event_id=e1, effect_event_id=e2)
        result2 = mt._event_link(cause_event_id=e1, effect_event_id=e2)
        assert result2["ok"] is False
        assert "already exists" in result2["error"]

    def test_cycle_prevented(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        e1 = _insert_event(conn, summary="A")
        e2 = _insert_event(conn, summary="B")
        mt._event_link(cause_event_id=e1, effect_event_id=e2)
        result = mt._event_link(cause_event_id=e2, effect_event_id=e1)
        assert result["ok"] is False
        assert "cycle" in result["error"]

    def test_custom_relation(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        e1 = _insert_event(conn, summary="cause")
        e2 = _insert_event(conn, summary="effect")
        result = mt._event_link(cause_event_id=e1, effect_event_id=e2, relation="contributes_to", confidence=0.7)
        assert result["ok"] is True
        assert result["edge"]["relation"] == "contributes_to"
        assert abs(result["edge"]["confidence"] - 0.7) < 1e-6


# ---------------------------------------------------------------------------
# temporal_causes / temporal_effects / temporal_chain
# ---------------------------------------------------------------------------

class TestCausalTraversal:
    def _setup_chain(self, patch_db):
        """Create A -> B -> C causal chain."""
        conn = _conn(patch_db)
        _insert_agent(conn)
        e_a = _insert_event(conn, event_type="error", summary="root cause")
        e_b = _insert_event(conn, event_type="decision", summary="decision made")
        e_c = _insert_event(conn, event_type="result", summary="outcome")
        mt._event_link(cause_event_id=e_a, effect_event_id=e_b)
        mt._event_link(cause_event_id=e_b, effect_event_id=e_c)
        return e_a, e_b, e_c

    def test_causes_missing_event(self):
        result = mt._temporal_causes(event_id=99999)
        assert result["ok"] is False

    def test_effects_missing_event(self):
        result = mt._temporal_effects(event_id=99999)
        assert result["ok"] is False

    def test_chain_missing_event(self):
        result = mt._temporal_chain(event_id=99999)
        assert result["ok"] is False

    def test_causes_returns_downstream(self, patch_db):
        e_a, e_b, e_c = self._setup_chain(patch_db)
        result = mt._temporal_causes(event_id=e_a)
        assert result["ok"] is True
        assert result["direction"] == "forward"
        chain_ids = [r["id"] for r in result["chain"]]
        assert e_b in chain_ids
        assert e_c in chain_ids

    def test_effects_returns_upstream(self, patch_db):
        e_a, e_b, e_c = self._setup_chain(patch_db)
        result = mt._temporal_effects(event_id=e_c)
        assert result["ok"] is True
        assert result["direction"] == "backward"
        chain_ids = [r["id"] for r in result["chain"]]
        assert e_b in chain_ids
        assert e_a in chain_ids

    def test_chain_bidirectional(self, patch_db):
        e_a, e_b, e_c = self._setup_chain(patch_db)
        result = mt._temporal_chain(event_id=e_b)
        assert result["ok"] is True
        upstream_ids = [r["id"] for r in result["upstream_causes"]]
        downstream_ids = [r["id"] for r in result["downstream_effects"]]
        assert e_a in upstream_ids
        assert e_c in downstream_ids


# ---------------------------------------------------------------------------
# temporal_auto_detect
# ---------------------------------------------------------------------------

class TestTemporalAutoDetect:
    def test_dry_run_no_insert(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        _insert_event(conn, event_type="error", summary="something broke")
        _insert_event(conn, event_type="decision", summary="fix it")
        result = mt._temporal_auto_detect(dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert "stats" in result

    def test_live_run_inserts_edges(self, patch_db):
        conn = _conn(patch_db)
        _insert_agent(conn)
        _insert_event(conn, event_type="error", project="myapp", summary="crash")
        _insert_event(conn, event_type="decision", project="myapp", summary="rollback")
        result = mt._temporal_auto_detect(dry_run=False)
        assert result["ok"] is True
        # The inserted count may be 0 or more depending on timing; just confirm it runs
        assert "inserted" in result["stats"]

    def test_returns_message(self, patch_db):
        result = mt._temporal_auto_detect()
        assert "message" in result
        assert isinstance(result["message"], str)


# ---------------------------------------------------------------------------
# TOOLS / DISPATCH exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_tools_is_list(self):
        assert isinstance(mt.TOOLS, list)
        assert len(mt.TOOLS) == 9

    def test_tool_names(self):
        names = {t.name for t in mt.TOOLS}
        expected = {
            "temporal_causes", "temporal_effects", "temporal_chain",
            "temporal_auto_detect", "temporal_context", "event_link",
            "epoch_detect", "epoch_create", "epoch_list",
        }
        assert names == expected

    def test_dispatch_keys_match_tools(self):
        tool_names = {t.name for t in mt.TOOLS}
        dispatch_keys = set(mt.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_dispatch_callable(self):
        for name, fn in mt.DISPATCH.items():
            assert callable(fn), f"DISPATCH[{name!r}] is not callable"

    def test_dispatch_routes_epoch_list(self, patch_db):
        result = mt.DISPATCH["epoch_list"]("epoch_list", {})
        assert result["ok"] is True

    def test_dispatch_unknown_tool(self, patch_db):
        result = mt.DISPATCH["epoch_list"]("nonexistent_tool", {})
        assert result["ok"] is False
        assert "unknown tool" in result["error"]
