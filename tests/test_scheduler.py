"""Tests for agentmemory.scheduler and related MCP/CLI integration."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agentmemory.brain import Brain
import agentmemory.scheduler as sched_mod
import agentmemory.mcp_tools_scheduler as mcp_sched_mod
from agentmemory.scheduler import (
    ConsolidationScheduler,
    get_schedule_config,
    set_schedule_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_db(tmp_path: Path) -> Path:
    """Create a fresh brain.db, return its path."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="hippocampus")
    return db_file


def _patch_mcp_db(monkeypatch, db_file: Path) -> None:
    monkeypatch.setattr(mcp_sched_mod, "DB_PATH", db_file)
    monkeypatch.setenv("BRAIN_DB", str(db_file))


# ---------------------------------------------------------------------------
# ConsolidationScheduler unit tests
# ---------------------------------------------------------------------------


class TestSchedulerInit:
    def test_scheduler_init(self, tmp_path):
        """Creates instance with correct defaults."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))
        assert s.db_path == str(db_file)
        assert s.interval_minutes == 60
        assert s.agent_id == "hippocampus"
        assert s._runs_completed == 0
        assert s._last_run_at is None
        assert s._next_run_at is None
        assert s._errors == []

    def test_scheduler_custom_interval(self, tmp_path):
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file), interval_minutes=30)
        assert s.interval_minutes == 30

    def test_scheduler_custom_agent(self, tmp_path):
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file), agent_id="myagent")
        assert s.agent_id == "myagent"


class TestRunOnce:
    def test_run_once_returns_dict(self, tmp_path):
        """run_once() returns a dict with an 'ok' key."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle") as mock_fn:
            mock_fn.return_value = None
            result = s.run_once()

        assert isinstance(result, dict)
        assert "ok" in result

    def test_run_once_ok_true_on_success(self, tmp_path):
        """run_once() returns ok=True when consolidation succeeds."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle") as mock_fn:
            mock_fn.return_value = None
            result = s.run_once()

        assert result["ok"] is True

    def test_run_once_ok_false_on_error(self, tmp_path):
        """run_once() returns ok=False when consolidation raises."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle", side_effect=RuntimeError("boom")):
            result = s.run_once()

        assert result["ok"] is False
        assert "error" in result

    def test_run_once_increments_runs_completed(self, tmp_path):
        """run_once() increments _runs_completed after each call."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            s.run_once()
            s.run_once()

        assert s._runs_completed == 2

    def test_run_once_updates_last_run_at(self, tmp_path):
        """run_once() sets _last_run_at after completion."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))
        assert s._last_run_at is None

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            s.run_once()

        assert s._last_run_at is not None

    def test_run_once_sets_next_run_at(self, tmp_path):
        """run_once() sets _next_run_at after completion."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            s.run_once()

        assert s._next_run_at is not None

    def test_run_once_logs_event(self, tmp_path):
        """run_once() logs a consolidation_sweep event to the events table."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            s.run_once()

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT event_type FROM events WHERE event_type = 'consolidation_sweep'"
        ).fetchone()
        conn.close()
        assert row is not None, "consolidation_sweep event should be logged"

    def test_run_once_tracks_errors(self, tmp_path):
        """run_once() records errors in _errors list."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle", side_effect=ValueError("test error")):
            s.run_once()

        assert len(s._errors) == 1
        assert "test error" in s._errors[0]["error"]


# ---------------------------------------------------------------------------
# Config API tests
# ---------------------------------------------------------------------------


class TestGetScheduleConfig:
    def test_get_schedule_config_defaults(self, tmp_path):
        """Returns defaults when not configured."""
        db_file = _init_db(tmp_path)
        config = get_schedule_config(str(db_file))
        assert config["enabled"] is False
        assert config["interval_minutes"] == 60
        assert config["agent_id"] == "hippocampus"

    def test_get_schedule_config_creates_config_table(self, tmp_path):
        """get_schedule_config creates the config table if absent."""
        db_file = _init_db(tmp_path)
        get_schedule_config(str(db_file))
        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config'"
        ).fetchone()
        conn.close()
        assert row is not None


class TestSetScheduleConfig:
    def test_set_schedule_config_persists(self, tmp_path):
        """set then get returns the same values."""
        db_file = _init_db(tmp_path)
        set_schedule_config(str(db_file), interval_minutes=45, enabled=True)
        config = get_schedule_config(str(db_file))
        assert config["interval_minutes"] == 45
        assert config["enabled"] is True

    def test_set_schedule_config_enables(self, tmp_path):
        """set_schedule_config can enable the schedule."""
        db_file = _init_db(tmp_path)
        set_schedule_config(str(db_file), interval_minutes=30, enabled=True)
        config = get_schedule_config(str(db_file))
        assert config["enabled"] is True

    def test_set_schedule_config_disables(self, tmp_path):
        """set_schedule_config can disable the schedule."""
        db_file = _init_db(tmp_path)
        set_schedule_config(str(db_file), interval_minutes=30, enabled=True)
        set_schedule_config(str(db_file), interval_minutes=30, enabled=False)
        config = get_schedule_config(str(db_file))
        assert config["enabled"] is False

    def test_set_schedule_config_enables_disables(self, tmp_path):
        """Multiple toggles work correctly."""
        db_file = _init_db(tmp_path)
        set_schedule_config(str(db_file), interval_minutes=60, enabled=True)
        assert get_schedule_config(str(db_file))["enabled"] is True
        set_schedule_config(str(db_file), interval_minutes=60, enabled=False)
        assert get_schedule_config(str(db_file))["enabled"] is False

    def test_set_schedule_config_returns_dict(self, tmp_path):
        """set_schedule_config returns the saved config dict."""
        db_file = _init_db(tmp_path)
        result = set_schedule_config(str(db_file), interval_minutes=120, enabled=True)
        assert isinstance(result, dict)
        assert result["interval_minutes"] == 120
        assert result["enabled"] is True


# ---------------------------------------------------------------------------
# Status tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_required_fields(self, tmp_path):
        """status() returns all required fields."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))
        st = s.status()
        assert "last_run_at" in st
        assert "next_run_at" in st
        assert "interval_minutes" in st
        assert "enabled" in st
        assert "runs_completed" in st
        assert "errors" in st

    def test_status_initial_state(self, tmp_path):
        """status() initial values are correct before any run."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file), interval_minutes=30)
        st = s.status()
        assert st["last_run_at"] is None
        assert st["next_run_at"] is None
        assert st["runs_completed"] == 0
        assert st["errors"] == []
        assert st["interval_minutes"] == 30

    def test_status_after_run(self, tmp_path):
        """status() reflects state after run_once()."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            s.run_once()

        st = s.status()
        assert st["runs_completed"] == 1
        assert st["last_run_at"] is not None
        assert st["next_run_at"] is not None


# ---------------------------------------------------------------------------
# Daemon tests
# ---------------------------------------------------------------------------


class TestDaemon:
    def test_daemon_runs_multiple_cycles(self, tmp_path):
        """run_daemon runs run_once multiple times until stop_event is set."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file), interval_minutes=1)
        stop = threading.Event()
        call_count = [0]

        def fake_run_once():
            call_count[0] += 1
            if call_count[0] >= 3:
                stop.set()
            return {"ok": True, "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z"}

        with patch.object(s, "run_once", side_effect=fake_run_once):
            # Use a very short wait so the test doesn't hang
            with patch.object(stop, "wait", side_effect=lambda timeout=None: None):
                s.run_daemon(stop_event=stop)

        assert call_count[0] >= 3

    def test_daemon_stops_on_event(self, tmp_path):
        """run_daemon stops when stop_event is set."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file), interval_minutes=60)
        stop = threading.Event()
        stop.set()  # already stopped

        call_count = [0]

        def fake_run_once():
            call_count[0] += 1
            return {"ok": True, "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z"}

        with patch.object(s, "run_once", side_effect=fake_run_once):
            s.run_daemon(stop_event=stop)

        # Because stop is already set, the while condition is False immediately
        # so run_once should NOT be called.
        assert call_count[0] == 0

    def test_daemon_uses_stop_event_wait(self, tmp_path):
        """run_daemon uses stop_event.wait() for sleep (not time.sleep)."""
        db_file = _init_db(tmp_path)
        s = ConsolidationScheduler(db_path=str(db_file), interval_minutes=30)
        stop = threading.Event()

        wait_calls = []
        original_wait = stop.wait

        def capturing_wait(timeout=None):
            wait_calls.append(timeout)
            stop.set()  # Stop after first wait
            return True

        with patch.object(s, "run_once", return_value={"ok": True,
                                                         "started_at": "2026-01-01T00:00:00Z",
                                                         "finished_at": "2026-01-01T00:00:01Z"}):
            stop.wait = capturing_wait
            s.run_daemon(stop_event=stop)

        # wait should have been called with the interval in seconds
        assert len(wait_calls) >= 1
        assert wait_calls[0] == 30 * 60


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


class TestMcpScheduleStatus:
    def test_mcp_schedule_status(self, tmp_path, monkeypatch):
        """schedule_status MCP tool returns ok and required fields."""
        db_file = _init_db(tmp_path)
        _patch_mcp_db(monkeypatch, db_file)

        fn = mcp_sched_mod.DISPATCH["schedule_status"]
        result = fn({"db_path": str(db_file)})

        assert result["ok"] is True
        assert "enabled" in result
        assert "interval_minutes" in result
        assert "agent_id" in result


class TestMcpScheduleRun:
    def test_mcp_schedule_run(self, tmp_path, monkeypatch):
        """schedule_run MCP tool triggers one cycle and returns ok."""
        db_file = _init_db(tmp_path)
        _patch_mcp_db(monkeypatch, db_file)

        fn = mcp_sched_mod.DISPATCH["schedule_run"]

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            result = fn({"db_path": str(db_file)})

        assert "ok" in result

    def test_mcp_schedule_run_logs_event(self, tmp_path, monkeypatch):
        """schedule_run logs a consolidation_sweep event."""
        db_file = _init_db(tmp_path)
        _patch_mcp_db(monkeypatch, db_file)

        fn = mcp_sched_mod.DISPATCH["schedule_run"]
        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            fn({"db_path": str(db_file)})

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT event_type FROM events WHERE event_type = 'consolidation_sweep'"
        ).fetchone()
        conn.close()
        assert row is not None


class TestMcpScheduleSet:
    def test_mcp_schedule_set(self, tmp_path, monkeypatch):
        """schedule_set MCP tool persists config."""
        db_file = _init_db(tmp_path)
        _patch_mcp_db(monkeypatch, db_file)

        fn = mcp_sched_mod.DISPATCH["schedule_set"]
        result = fn({"db_path": str(db_file), "interval_minutes": 45, "enabled": True})

        assert result["ok"] is True
        assert result["config"]["interval_minutes"] == 45
        assert result["config"]["enabled"] is True

    def test_mcp_schedule_set_invalid_interval(self, tmp_path, monkeypatch):
        """schedule_set rejects interval_minutes < 1."""
        db_file = _init_db(tmp_path)
        _patch_mcp_db(monkeypatch, db_file)

        fn = mcp_sched_mod.DISPATCH["schedule_set"]
        result = fn({"db_path": str(db_file), "interval_minutes": 0})

        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# CLI / parser tests
# ---------------------------------------------------------------------------


class TestScheduleSubparser:
    def test_schedule_subparser_registered(self):
        """'schedule' subparser is registered in build_parser()."""
        import agentmemory._impl as impl_mod
        parser = impl_mod.build_parser()
        # Parse a schedule status command to verify the subparser exists
        args = parser.parse_args(["schedule", "status"])
        assert args.command == "schedule"
        assert args.sched_cmd == "status"

    def test_schedule_set_parser(self):
        """schedule set --interval --enabled parses correctly."""
        import agentmemory._impl as impl_mod
        parser = impl_mod.build_parser()
        args = parser.parse_args(["schedule", "set", "--interval", "30", "--enabled"])
        assert args.sched_cmd == "set"
        assert args.interval == 30
        assert args.enabled is True

    def test_schedule_start_parser(self):
        """schedule start --daemon parses correctly."""
        import agentmemory._impl as impl_mod
        parser = impl_mod.build_parser()
        args = parser.parse_args(["schedule", "start", "--daemon"])
        assert args.sched_cmd == "start"
        assert args.daemon is True


class TestScheduleStatusOutput:
    def test_schedule_status_output(self, tmp_path, monkeypatch, capsys):
        """brainctl schedule status outputs JSON with expected fields."""
        import agentmemory._impl as impl_mod
        db_file = _init_db(tmp_path)
        monkeypatch.setattr(impl_mod, "DB_PATH", db_file)
        monkeypatch.setenv("BRAIN_DB", str(db_file))

        parser = impl_mod.build_parser()
        args = parser.parse_args(["schedule", "status"])
        impl_mod.cmd_schedule(args)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert "enabled" in data
        assert "interval_minutes" in data


class TestScheduleRunOutput:
    def test_schedule_run_output(self, tmp_path, monkeypatch, capsys):
        """brainctl schedule run outputs JSON with ok key."""
        import agentmemory._impl as impl_mod
        db_file = _init_db(tmp_path)
        monkeypatch.setattr(impl_mod, "DB_PATH", db_file)
        monkeypatch.setenv("BRAIN_DB", str(db_file))

        with patch("agentmemory.hippocampus.cmd_consolidation_cycle"):
            parser = impl_mod.build_parser()
            args = parser.parse_args(["schedule", "run"])
            impl_mod.cmd_schedule(args)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ok" in data
