"""brainctl MCP tools — unified telemetry dashboard."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.telemetry import get_dashboard

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_telemetry(
    agent_id: str | None = None,
) -> dict:
    """Return the unified health dashboard for brain.db."""
    try:
        result = get_dashboard(str(DB_PATH), agent_id=agent_id)
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="telemetry",
        description=(
            "Unified health dashboard for brain.db — single-pane-of-glass view combining "
            "memory stats, event activity, entity counts, decisions, affect state, and budget. "
            "Returns a composite health_score (0–1), letter grade (A/B/C/D/F), per-section "
            "metrics, and actionable alerts. Optionally filter to a single agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter all metrics to this agent ID only. Omit for fleet-wide view.",
                },
            },
            "required": [],
        },
    ),
]

DISPATCH: dict = {
    "telemetry": lambda agent_id=None, **kw: tool_telemetry(agent_id=agent_id),
}
