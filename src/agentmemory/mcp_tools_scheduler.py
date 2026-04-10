"""brainctl MCP tools — hippocampus consolidation scheduler."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.scheduler import (
    ConsolidationScheduler,
    get_schedule_config,
    set_schedule_config,
)

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db_path_str() -> str:
    """Return the active DB path (respects BRAIN_DB env var)."""
    env = os.environ.get("BRAIN_DB")
    if env:
        return env
    return str(DB_PATH)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_schedule_status(args: dict) -> dict:
    """Return current schedule configuration and last/next run times."""
    db_path = args.get("db_path") or _db_path_str()
    config = get_schedule_config(db_path)
    return {
        "ok": True,
        "enabled": config["enabled"],
        "interval_minutes": config["interval_minutes"],
        "agent_id": config.get("agent_id", "hippocampus"),
        "db_path": db_path,
        # Note: last_run_at / next_run_at are only tracked by a live scheduler instance.
        # The config table persists static config; runtime state lives in-process.
        "last_run_at": None,
        "next_run_at": None,
    }


def handle_schedule_run(args: dict) -> dict:
    """Trigger one consolidation cycle immediately."""
    db_path = args.get("db_path") or _db_path_str()
    agent_id = args.get("agent_id") or get_schedule_config(db_path).get("agent_id", "hippocampus")
    interval = get_schedule_config(db_path).get("interval_minutes", 60)

    scheduler = ConsolidationScheduler(
        db_path=db_path,
        interval_minutes=interval,
        agent_id=agent_id,
    )
    result = scheduler.run_once()
    return result


def handle_schedule_set(args: dict) -> dict:
    """Update schedule configuration (interval and/or enabled state)."""
    db_path = args.get("db_path") or _db_path_str()
    current = get_schedule_config(db_path)

    interval_minutes = args.get("interval_minutes")
    if interval_minutes is None:
        interval_minutes = current["interval_minutes"]
    else:
        interval_minutes = int(interval_minutes)
        if interval_minutes < 1:
            return {"ok": False, "error": "interval_minutes must be >= 1"}

    enabled = args.get("enabled")
    if enabled is None:
        enabled = current["enabled"]
    else:
        enabled = bool(enabled)

    agent_id = args.get("agent_id") or current.get("agent_id", "hippocampus")

    config = set_schedule_config(
        db_path=db_path,
        interval_minutes=interval_minutes,
        enabled=enabled,
        agent_id=agent_id,
    )
    return {"ok": True, "config": config}


# ---------------------------------------------------------------------------
# TOOLS and DISPATCH
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="schedule_status",
        description=(
            "Get current hippocampus consolidation schedule config and status. "
            "Returns enabled state, interval, and last/next run times."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Path to brain.db (defaults to BRAIN_DB env or ~/agentmemory/db/brain.db)",
                },
            },
        },
    ),
    Tool(
        name="schedule_run",
        description=(
            "Trigger one hippocampus consolidation cycle immediately. "
            "Runs the full consolidation pipeline (decay, demotion, merge, compress, etc.) "
            "and logs a consolidation_sweep event."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Path to brain.db (defaults to BRAIN_DB env or ~/agentmemory/db/brain.db)",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID for event attribution (default: hippocampus)",
                },
            },
        },
    ),
    Tool(
        name="schedule_set",
        description=(
            "Update hippocampus consolidation schedule configuration. "
            "Set the interval in minutes and/or enable/disable the scheduled daemon."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Path to brain.db (defaults to BRAIN_DB env or ~/agentmemory/db/brain.db)",
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "How often to run consolidation (in minutes, minimum 1)",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the daemon is enabled",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID for event attribution (default: hippocampus)",
                },
            },
        },
    ),
]

DISPATCH: dict = {
    "schedule_status": handle_schedule_status,
    "schedule_run": handle_schedule_run,
    "schedule_set": handle_schedule_set,
}
