"""brainctl MCP tools — merge two brain.db files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory import merge as _merge_mod
from agentmemory.paths import get_db_path

DB_PATH = Path(os.environ.get("BRAIN_DB", str(get_db_path())))


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_merge_status(
    agent_id: str = "mcp-client",
    *,
    source_path: str,
    tables: str = "",
    **kw: Any,
) -> dict:
    """Preview what a merge would do without executing it.

    Parameters
    ----------
    source_path:
        Absolute path to the source brain.db to read from.
    tables:
        Optional comma-separated list of table names to preview
        (defaults to all standard tables).
    """
    if not source_path or not source_path.strip():
        return {"ok": False, "error": "source_path must not be empty"}

    table_list = [t.strip() for t in tables.split(",") if t.strip()] or None

    try:
        report = _merge_mod.status(
            source_path=source_path.strip(),
            target_path=str(DB_PATH),
        )
        if table_list:
            # Re-run with table filter for the preview
            report = _merge_mod.merge(
                source_path=source_path.strip(),
                target_path=str(DB_PATH),
                dry_run=True,
                tables=table_list,
            )
        return {"ok": True, **report}
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"merge preview failed: {exc}"}


def tool_merge_execute(
    agent_id: str = "mcp-client",
    *,
    source_path: str,
    dry_run: bool = True,
    tables: str = "",
    **kw: Any,
) -> dict:
    """Execute a merge of source_path into the active brain.db.

    Defaults to dry_run=True for safety. Set dry_run=False to commit changes.

    Parameters
    ----------
    source_path:
        Absolute path to the source brain.db to merge from.
    dry_run:
        If True (default), compute what would happen but make no changes.
    tables:
        Optional comma-separated list of table names to merge
        (defaults to all standard tables).
    """
    if not source_path or not source_path.strip():
        return {"ok": False, "error": "source_path must not be empty"}

    table_list = [t.strip() for t in tables.split(",") if t.strip()] or None

    try:
        report = _merge_mod.merge(
            source_path=source_path.strip(),
            target_path=str(DB_PATH),
            dry_run=dry_run,
            tables=table_list,
        )
        return {"ok": True, **report}
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"merge failed: {exc}"}


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="merge_status",
        description=(
            "Preview what a brainctl merge would do without making changes. "
            "Returns a report of rows that would be copied and conflicts detected."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID for attribution (default: mcp-client)",
                },
                "source_path": {
                    "type": "string",
                    "description": "Absolute path to the source brain.db to preview merging from",
                },
                "tables": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated list of table names to preview. "
                        "Defaults to all standard tables."
                    ),
                },
            },
            "required": ["source_path"],
        },
    ),
    Tool(
        name="merge_execute",
        description=(
            "Merge a source brain.db into the active brain.db. "
            "Defaults to dry_run=True for safety — set dry_run=false to commit changes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID for attribution (default: mcp-client)",
                },
                "source_path": {
                    "type": "string",
                    "description": "Absolute path to the source brain.db to merge from",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true (default), preview only — do not write changes",
                    "default": True,
                },
                "tables": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated list of table names to merge. "
                        "Defaults to all standard tables."
                    ),
                },
            },
            "required": ["source_path"],
        },
    ),
]

DISPATCH: dict = {
    "merge_status": tool_merge_status,
    "merge_execute": tool_merge_execute,
}
