"""Hippocampus consolidation scheduler daemon.

Provides configurable scheduling for brain.db consolidation cycles,
a status API, and daemon mode with threading.Event stop support.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import threading
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentmemory.paths import get_db_path

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_CONFIG_KEY = "hippocampus_schedule"
_DEFAULT_INTERVAL = 60  # minutes
_DEFAULT_AGENT_ID = "hippocampus"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_config_table(conn: sqlite3.Connection) -> None:
    """Create config table if it doesn't exist."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    """Auto-register an agent if not present (prevents FK violations)."""
    now = _now_iso()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at)"
            " VALUES (?, ?, 'daemon', 'active', ?, ?)",
            (agent_id, agent_id, now, now),
        )
        conn.commit()
    except Exception:
        pass


def _resolve_event_agent(conn: sqlite3.Connection, agent_id: str) -> str:
    """Find an existing agent id that can be used for event attribution."""
    row = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row:
        return agent_id
    for fallback in ("hippocampus", "consolidator"):
        row = conn.execute("SELECT id FROM agents WHERE id = ?", (fallback,)).fetchone()
        if row:
            return fallback
    row = conn.execute("SELECT id FROM agents ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row["id"]
    return agent_id  # will be auto-registered by _ensure_agent


# ---------------------------------------------------------------------------
# Public config API
# ---------------------------------------------------------------------------


def get_schedule_config(db_path: str) -> dict:
    """Read schedule config from the config table.

    Returns defaults when not configured.
    """
    conn = _connect(db_path)
    _ensure_config_table(conn)
    row = conn.execute("SELECT value FROM config WHERE key = ?", (_CONFIG_KEY,)).fetchone()
    conn.close()
    if row is None:
        return {
            "enabled": False,
            "interval_minutes": _DEFAULT_INTERVAL,
            "agent_id": _DEFAULT_AGENT_ID,
        }
    try:
        data = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        data = {}
    return {
        "enabled": data.get("enabled", False),
        "interval_minutes": data.get("interval_minutes", _DEFAULT_INTERVAL),
        "agent_id": data.get("agent_id", _DEFAULT_AGENT_ID),
    }


def set_schedule_config(db_path: str, interval_minutes: int, enabled: bool = True, agent_id: str = _DEFAULT_AGENT_ID) -> dict:
    """Persist schedule config to the config table.

    Returns the saved config dict.
    """
    config = {
        "enabled": enabled,
        "interval_minutes": int(interval_minutes),
        "agent_id": agent_id,
    }
    conn = _connect(db_path)
    _ensure_config_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (_CONFIG_KEY, json.dumps(config)),
    )
    conn.commit()
    conn.close()
    return config


# ---------------------------------------------------------------------------
# ConsolidationScheduler
# ---------------------------------------------------------------------------


class ConsolidationScheduler:
    """Daemon scheduler that periodically runs hippocampus consolidation cycles."""

    def __init__(
        self,
        db_path: str,
        interval_minutes: int = _DEFAULT_INTERVAL,
        agent_id: str = _DEFAULT_AGENT_ID,
    ) -> None:
        self.db_path = db_path
        self.interval_minutes = interval_minutes
        self.agent_id = agent_id

        # Runtime state
        self._last_run_at: str | None = None
        self._next_run_at: str | None = None
        self._runs_completed: int = 0
        self._errors: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # run_once
    # ------------------------------------------------------------------

    def run_once(self) -> dict:
        """Run one consolidation cycle. Returns a report dict with ok key."""
        import argparse

        started_at = _now_iso()
        buf = io.StringIO()

        # Set BRAIN_DB env so hippocampus.get_db() picks up the right path
        old_env = os.environ.get("BRAIN_DB")
        os.environ["BRAIN_DB"] = self.db_path

        try:
            # Build a minimal namespace matching what cmd_consolidation_cycle expects
            ns = argparse.Namespace(
                agent=self.agent_id,
                project="agentmemory",
                quiet=True,
            )

            from agentmemory.hippocampus import cmd_consolidation_cycle

            with redirect_stdout(buf):
                cmd_consolidation_cycle(ns)

            output = buf.getvalue().strip()
            try:
                cycle_report = json.loads(output) if output else {}
            except json.JSONDecodeError:
                cycle_report = {"raw_output": output}

            result: dict[str, Any] = {"ok": True, "started_at": started_at, "cycle": cycle_report}

        except Exception as exc:
            result = {"ok": False, "started_at": started_at, "error": str(exc)}
            with self._lock:
                self._errors.append({"at": started_at, "error": str(exc)})

        finally:
            # Restore BRAIN_DB
            if old_env is None:
                os.environ.pop("BRAIN_DB", None)
            else:
                os.environ["BRAIN_DB"] = old_env

        finished_at = _now_iso()
        result["finished_at"] = finished_at

        # Update runtime state
        with self._lock:
            self._last_run_at = finished_at
            self._runs_completed += 1
            next_dt = datetime.now(timezone.utc) + timedelta(minutes=self.interval_minutes)
            self._next_run_at = next_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        # Log a consolidation_sweep event (the scheduler-level wrapper event)
        try:
            self._log_sweep_event(started_at, result)
        except Exception:
            pass  # event logging is best-effort

        return result

    def _log_sweep_event(self, started_at: str, result: dict) -> None:
        """Log a consolidation_sweep event to the events table."""
        conn = _connect(self.db_path)
        _ensure_agent(conn, self.agent_id)
        agent_id = _resolve_event_agent(conn, self.agent_id)
        ok = result.get("ok", False)
        summary = (
            f"Scheduled consolidation_sweep at {started_at} — "
            f"{'ok' if ok else 'error'}: "
            + (result.get("error", "completed") if not ok else "completed")
        )
        conn.execute(
            """
            INSERT INTO events (agent_id, event_type, summary, detail, metadata, project, importance, created_at)
            VALUES (?, 'consolidation_sweep', ?, ?, ?, 'agentmemory', 0.5, ?)
            """,
            (
                agent_id,
                summary,
                json.dumps(result),
                json.dumps({"interval_minutes": self.interval_minutes}),
                started_at,
            ),
        )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # run_daemon
    # ------------------------------------------------------------------

    def run_daemon(self, stop_event: threading.Event | None = None) -> None:
        """Loop forever (or until stop_event is set), sleeping interval between runs."""
        if stop_event is None:
            stop_event = threading.Event()

        while not stop_event.is_set():
            self.run_once()
            # Wait for either the interval to expire or a stop signal
            stop_event.wait(timeout=self.interval_minutes * 60)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return scheduler status: last_run_at, next_run_at, runs_completed, errors, interval_minutes, enabled."""
        config = get_schedule_config(self.db_path)
        with self._lock:
            return {
                "last_run_at": self._last_run_at,
                "next_run_at": self._next_run_at,
                "runs_completed": self._runs_completed,
                "errors": list(self._errors),
                "interval_minutes": self.interval_minutes,
                "enabled": config.get("enabled", False),
            }


# ---------------------------------------------------------------------------
# Daemon entry point (subprocess mode)
# ---------------------------------------------------------------------------


def _daemon_main() -> None:
    """Entry point when running as a background daemon subprocess.

    Usage: python -m agentmemory.scheduler --db-path PATH --interval N --agent AGENT_ID
    """
    import argparse as _ap

    p = _ap.ArgumentParser(description="Hippocampus consolidation daemon")
    p.add_argument("--db-path", default=str(get_db_path()), help="Path to brain.db")
    p.add_argument("--interval", type=int, default=_DEFAULT_INTERVAL, help="Interval in minutes")
    p.add_argument("--agent", default=_DEFAULT_AGENT_ID, help="Agent ID for events")
    args = p.parse_args()

    scheduler = ConsolidationScheduler(
        db_path=args.db_path,
        interval_minutes=args.interval,
        agent_id=args.agent,
    )
    try:
        scheduler.run_daemon()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    _daemon_main()
