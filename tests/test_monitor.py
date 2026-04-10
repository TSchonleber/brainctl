"""Tests for brainctl monitor command."""
import sys
import os
import sqlite3
import types
import argparse
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import re

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    """Build a minimal args namespace for cmd_monitor."""
    defaults = {
        "agent": None,
        "interval": 0.01,
        "tail": 5,
        "types": None,
    }
    defaults.update(kwargs)
    ns = argparse.Namespace(**defaults)
    return ns


def _setup_db(db_path):
    """Create a minimal brain schema with events, memories, and affect_log."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            display_name TEXT,
            agent_type TEXT DEFAULT 'default',
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            event_type TEXT,
            summary TEXT,
            detail TEXT,
            metadata TEXT,
            session_id TEXT,
            project TEXT,
            refs TEXT,
            importance REAL DEFAULT 0.5,
            caused_by_event_id INTEGER,
            causal_chain_root INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            category TEXT,
            scope TEXT DEFAULT 'global',
            content TEXT,
            confidence REAL DEFAULT 0.8,
            tags TEXT,
            supersedes_id INTEGER,
            retired_at TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );

        CREATE TABLE IF NOT EXISTS affect_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            valence REAL,
            arousal REAL,
            dominance REAL,
            affect_label TEXT,
            cluster TEXT,
            functional_state TEXT,
            safety_flag TEXT,
            trigger TEXT,
            source TEXT,
            metadata TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );

        INSERT OR IGNORE INTO agents(id, display_name, agent_type, created_at, updated_at)
            VALUES('agent-alpha', 'Alpha', 'test',
                   strftime('%Y-%m-%dT%H:%M:%S','now'),
                   strftime('%Y-%m-%dT%H:%M:%S','now'));
        INSERT OR IGNORE INTO agents(id, display_name, agent_type, created_at, updated_at)
            VALUES('agent-beta', 'Beta', 'test',
                   strftime('%Y-%m-%dT%H:%M:%S','now'),
                   strftime('%Y-%m-%dT%H:%M:%S','now'));
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Test 1: cmd_monitor exists and is callable
# ---------------------------------------------------------------------------

class TestCmdMonitorExists:
    def test_cmd_monitor_exists(self):
        """cmd_monitor must exist as a callable in _impl."""
        import agentmemory._impl as _impl
        assert hasattr(_impl, "cmd_monitor"), "cmd_monitor not found in _impl"
        assert callable(_impl.cmd_monitor), "cmd_monitor is not callable"


# ---------------------------------------------------------------------------
# Test 2: monitor subparser is registered in build_parser
# ---------------------------------------------------------------------------

class TestMonitorSubparserRegistered:
    def test_monitor_subparser_registered(self):
        """build_parser() must include a 'monitor' subcommand."""
        import agentmemory._impl as _impl
        parser = _impl.build_parser()
        # argparse stores subcommand choices on the subparsers action
        subparsers_actions = [
            a for a in parser._actions
            if hasattr(a, "_name_parser_map")
        ]
        assert subparsers_actions, "No subparsers found on parser"
        choices = subparsers_actions[0]._name_parser_map
        assert "monitor" in choices, f"'monitor' not in subcommands: {list(choices)}"

    def test_monitor_help_exits_zero(self, tmp_path):
        """brainctl monitor --help should exit 0."""
        import subprocess
        db_file = tmp_path / "brain.db"
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(SRC)!r}); "
             f"import agentmemory._impl as _i; "
             f"from pathlib import Path; "
             f"_i.DB_PATH = Path({str(db_file)!r}); "
             f"sys.argv = ['brainctl', 'monitor', '--help']; "
             f"_i.main()"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONPATH": str(SRC)},
        )
        assert result.returncode == 0, (
            f"brainctl monitor --help failed:\n{result.stdout}\n{result.stderr}"
        )
        assert "monitor" in result.stdout.lower() or "interval" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Test 3: tail shows recent events on startup
# ---------------------------------------------------------------------------

class TestTailShowsRecentEvents:
    def test_tail_shows_recent_events(self, tmp_path, capsys):
        """On startup, the tail logic should print the last N events."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        # Insert 3 events
        for i in range(3):
            conn.execute(
                "INSERT INTO events (agent_id, event_type, summary, created_at) "
                "VALUES ('agent-alpha', 'observation', ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
                (f"Event number {i}",)
            )
        conn.commit()
        conn.close()

        args = _make_args(tail=10)

        # Patch get_db to return our test DB, and time.sleep to raise
        # KeyboardInterrupt immediately after the first poll sleep.
        import agentmemory._impl as _impl

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        sleep_calls = []

        def fake_sleep(n):
            sleep_calls.append(n)
            raise KeyboardInterrupt

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = fake_sleep
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out

        # Should have printed 3 event lines
        assert "Event number 0" in out
        assert "Event number 1" in out
        assert "Event number 2" in out
        assert "[EVENT/" in out

    def test_tail_respects_n(self, tmp_path, capsys):
        """--tail N should limit startup output to N items."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        # Insert 10 events
        for i in range(10):
            conn.execute(
                "INSERT INTO events (agent_id, event_type, summary, created_at) "
                "VALUES ('agent-alpha', 'observation', ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
                (f"TailEvent {i}",)
            )
        conn.commit()
        conn.close()

        args = _make_args(tail=3)  # only last 3

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = KeyboardInterrupt
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out
        event_lines = [l for l in out.splitlines() if "[EVENT/" in l]
        assert len(event_lines) == 3


# ---------------------------------------------------------------------------
# Test 4: agent filter is respected
# ---------------------------------------------------------------------------

class TestAgentFilter:
    def test_agent_filter_events(self, tmp_path, capsys):
        """--agent filter should exclude events from other agents."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        conn.execute(
            "INSERT INTO events (agent_id, event_type, summary, created_at) "
            "VALUES ('agent-alpha', 'observation', 'Alpha event', strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.execute(
            "INSERT INTO events (agent_id, event_type, summary, created_at) "
            "VALUES ('agent-beta', 'observation', 'Beta event should be hidden', strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.commit()
        conn.close()

        args = _make_args(tail=20, agent="agent-alpha")

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = KeyboardInterrupt
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out
        assert "Alpha event" in out
        assert "Beta event should be hidden" not in out

    def test_agent_filter_memories(self, tmp_path, capsys):
        """--agent filter should also apply to memory tail output."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        conn.execute(
            "INSERT INTO memories (agent_id, category, content, created_at) "
            "VALUES ('agent-alpha', 'lesson', 'Alpha memory', strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.execute(
            "INSERT INTO memories (agent_id, category, content, created_at) "
            "VALUES ('agent-beta', 'lesson', 'Beta memory hidden', strftime('%Y-%m-%dT%H:%M:%S','now'))"
        )
        conn.commit()
        conn.close()

        args = _make_args(tail=20, agent="agent-alpha")

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = KeyboardInterrupt
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out
        assert "Alpha memory" in out
        assert "Beta memory hidden" not in out


# ---------------------------------------------------------------------------
# Test 5: output format matches expected pattern
# ---------------------------------------------------------------------------

class TestMonitorFormat:
    def test_event_format_matches_pattern(self, tmp_path, capsys):
        """Each EVENT line should match the expected format."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        conn.execute(
            "INSERT INTO events (agent_id, event_type, summary, created_at) "
            "VALUES ('agent-alpha', 'observation', 'Deployed v3 to production', '2026-04-09T12:34:56')"
        )
        conn.commit()
        conn.close()

        args = _make_args(tail=20)

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = KeyboardInterrupt
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out

        # Find the event line
        event_lines = [l for l in out.splitlines() if "Deployed v3" in l]
        assert event_lines, f"No line containing 'Deployed v3' found in output:\n{out}"
        line = event_lines[0]

        # Strip ANSI codes for pattern matching
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean = ansi_escape.sub("", line)

        # Should match: TIMESTAMP [EVENT/type] agent: "content"
        pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[EVENT/\w+\] .+: \".+\"$"
        )
        assert pattern.match(clean), (
            f"Line does not match expected format.\nLine: {clean!r}\n"
            f"Expected: YYYY-MM-DDTHH:MM:SS [EVENT/type] agent: \"content\""
        )

    def test_mem_format_matches_pattern(self, tmp_path, capsys):
        """Each MEM line should match the expected format."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        conn.execute(
            "INSERT INTO memories (agent_id, category, content, created_at) "
            "VALUES ('agent-alpha', 'lesson', 'Always run migrations in a transaction', '2026-04-09T12:34:58')"
        )
        conn.commit()
        conn.close()

        args = _make_args(tail=20)

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = KeyboardInterrupt
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out

        mem_lines = [l for l in out.splitlines() if "migrations" in l]
        assert mem_lines, f"No line containing 'migrations' found:\n{out}"
        line = mem_lines[0]

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean = ansi_escape.sub("", line)

        pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[MEM/\w+\] .+: \".+\"$"
        )
        assert pattern.match(clean), (
            f"Line does not match expected format.\nLine: {clean!r}"
        )

    def test_affect_format_matches_pattern(self, tmp_path, capsys):
        """Each AFFECT line should match the expected format."""
        import agentmemory._impl as _impl

        db_file = tmp_path / "brain.db"
        conn = _setup_db(db_file)
        conn.execute(
            "INSERT INTO affect_log (agent_id, affect_label, functional_state, created_at) "
            "VALUES ('agent-alpha', 'curious', 'exploring', '2026-04-09T12:35:00')"
        )
        conn.commit()
        conn.close()

        args = _make_args(tail=20)

        real_conn = sqlite3.connect(str(db_file))
        real_conn.row_factory = sqlite3.Row

        with patch.object(_impl, "get_db", return_value=real_conn):
            with patch("agentmemory._impl.time") as mock_time:
                mock_time.sleep.side_effect = KeyboardInterrupt
                _impl.cmd_monitor(args)

        real_conn.close()
        captured = capsys.readouterr()
        out = captured.out

        affect_lines = [l for l in out.splitlines() if "exploring" in l or "curious" in l]
        assert affect_lines, f"No AFFECT line found:\n{out}"
        line = affect_lines[0]

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean = ansi_escape.sub("", line)

        pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[AFFECT/\w+\] .+: \".+\"$"
        )
        assert pattern.match(clean), (
            f"AFFECT line does not match expected format.\nLine: {clean!r}"
        )
