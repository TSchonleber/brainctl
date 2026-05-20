"""brainctl MCP tools — workspace bandwidth limit.

Phase 1: bookkeeping for top-K-per-epoch limit on workspace_broadcasts.
Closes the second half of the May 15 audit's workspace partial entry
(thalamus mode-broadcast closed the first half — org_state coupling).

Phase 1 is inspection + state mgmt. Phase 2 wires the limit into
workspace_ingest. Phase 3 lets the limit be context-modulated.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_ENFORCEMENT_MODES = {"shadow", "enforce", "disabled"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        t for t in ("workspace_bandwidth_state", "workspace_bandwidth_epochs")
        if not _table_exists(conn, t)
    ]
    if missing:
        return ("workspace bandwidth schema missing: " + ", ".join(missing)
                + ". Run `brainctl migrate` (migration 072).")
    return None


def _rotate_if_due(conn: sqlite3.Connection) -> dict[str, Any]:
    """Close out the current epoch if `epoch_duration_seconds` has elapsed.

    Inserts a row into workspace_bandwidth_epochs, resets the live
    counters, returns the rotated-epoch metadata (or empty dict if no
    rotation happened).
    """
    state = conn.execute("SELECT * FROM workspace_bandwidth_state WHERE id = 1").fetchone()
    if not state:
        return {}
    rot = conn.execute(
        """
        SELECT (julianday('now') * 86400 - julianday(?) * 86400) AS elapsed_s
        """,
        (state["epoch_started_at"],),
    ).fetchone()
    elapsed = float(rot["elapsed_s"]) if rot and rot["elapsed_s"] is not None else 0.0
    duration = int(state["epoch_duration_seconds"])
    if elapsed < duration:
        return {}
    admitted = int(state["epoch_count"])
    bandwidth = int(state["bandwidth_limit"])
    saturation = admitted / bandwidth if bandwidth else 0.0
    conn.execute(
        """
        INSERT INTO workspace_bandwidth_epochs
          (epoch_started_at, duration_seconds, admitted_count, rejected_count,
           bandwidth_limit, enforcement_mode, saturation)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (state["epoch_started_at"], duration, admitted, 0,
         bandwidth, state["enforcement_mode"], saturation),
    )
    conn.execute(
        """
        UPDATE workspace_bandwidth_state SET
          epoch_started_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
          epoch_count = 0,
          updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
         WHERE id = 1
        """
    )
    conn.commit()
    return {"rotated": True, "admitted": admitted, "limit": bandwidth, "saturation": saturation}


def tool_workspace_bandwidth_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        rotated = _rotate_if_due(conn)
        state = conn.execute("SELECT * FROM workspace_bandwidth_state WHERE id = 1").fetchone()
        last_epochs = _rows(conn.execute(
            "SELECT * FROM workspace_bandwidth_epochs ORDER BY id DESC LIMIT 5"
        ).fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(AVG(saturation), 0.0) AS mean_sat,
                   COALESCE(MAX(saturation), 0.0) AS peak_sat,
                   SUM(admitted_count) AS sum_admitted,
                   SUM(rejected_count) AS sum_rejected
              FROM workspace_bandwidth_epochs
             WHERE epoch_ended_at >= datetime('now', '-1 hour')
            """
        ).fetchone()
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_epochs": last_epochs,
        "last_hour_aggregate": dict(agg) if agg else {},
        "rotation": rotated,
    }


def tool_workspace_bandwidth_set(
    bandwidth_limit: int | None = None,
    epoch_duration_seconds: int | None = None,
    enforcement_mode: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Update workspace bandwidth state. Each arg is optional — pass
    only what you want to change. Returns the new state."""
    if bandwidth_limit is not None and bandwidth_limit <= 0:
        return {"error": "bandwidth_limit must be > 0"}
    if epoch_duration_seconds is not None and epoch_duration_seconds <= 0:
        return {"error": "epoch_duration_seconds must be > 0"}
    if enforcement_mode is not None and enforcement_mode not in VALID_ENFORCEMENT_MODES:
        return {"error": f"invalid enforcement_mode {enforcement_mode!r}; "
                f"expected one of {sorted(VALID_ENFORCEMENT_MODES)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        updates = []
        params: list[Any] = []
        if bandwidth_limit is not None:
            updates.append("bandwidth_limit = ?"); params.append(int(bandwidth_limit))
        if epoch_duration_seconds is not None:
            updates.append("epoch_duration_seconds = ?"); params.append(int(epoch_duration_seconds))
        if enforcement_mode is not None:
            updates.append("enforcement_mode = ?"); params.append(enforcement_mode)
        if not updates:
            return {"error": "no arguments passed; nothing to update"}
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        conn.execute(
            f"UPDATE workspace_bandwidth_state SET {', '.join(updates)} WHERE id = 1",
            tuple(params),
        )
        conn.commit()
        state = conn.execute("SELECT * FROM workspace_bandwidth_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None}


def tool_workspace_bandwidth_admit(**_kw: Any) -> dict[str, Any]:
    """Record one workspace broadcast admit. Returns the post-increment
    epoch_count and the would-have-been-rejected flag (`saturated=True`
    when epoch_count exceeds bandwidth_limit; in `shadow` mode the admit
    still goes through; in `enforce` mode the caller should refuse).
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        _rotate_if_due(conn)
        state = conn.execute("SELECT * FROM workspace_bandwidth_state WHERE id = 1").fetchone()
        if not state:
            return {"error": "workspace_bandwidth_state seed row missing"}
        new_count = int(state["epoch_count"]) + 1
        saturated = new_count > int(state["bandwidth_limit"])
        if saturated and state["enforcement_mode"] == "enforce":
            conn.execute(
                """
                UPDATE workspace_bandwidth_state SET
                  total_rejects = total_rejects + 1,
                  updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                 WHERE id = 1
                """
            )
            conn.commit()
            return {
                "ok": True, "admitted": False, "rejected": True,
                "saturated": True, "epoch_count": int(state["epoch_count"]),
                "bandwidth_limit": int(state["bandwidth_limit"]),
                "enforcement_mode": state["enforcement_mode"],
            }
        conn.execute(
            """
            UPDATE workspace_bandwidth_state SET
              epoch_count = ?,
              total_admits = total_admits + 1,
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (new_count,),
        )
        conn.commit()
    return {
        "ok": True, "admitted": True, "rejected": False,
        "saturated": saturated,
        "epoch_count": new_count,
        "bandwidth_limit": int(state["bandwidth_limit"]),
        "enforcement_mode": state["enforcement_mode"],
    }


def tool_workspace_bandwidth_epochs_history(
    limit: int = 50, since: str | None = None,
    min_saturation: float | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses, params = [], []
        if since:
            clauses.append("epoch_ended_at >= ?"); params.append(since)
        if min_saturation is not None:
            clauses.append("saturation >= ?"); params.append(float(min_saturation))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM workspace_bandwidth_epochs {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {"ok": True, "epochs": _rows(rows)}


TOOLS: list[Tool] = [
    Tool(
        name="workspace_bandwidth_status",
        description=(
            "Workspace bandwidth Phase 1 inspection. Current state (epoch + counts + "
            "enforcement_mode) + last 5 completed epochs + 1h aggregate. Side-effect: "
            "rotates the epoch if the current one has expired."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="workspace_bandwidth_set",
        description=(
            "Update workspace bandwidth state. enforcement_mode ∈ {shadow, enforce, "
            "disabled}. Each arg is optional; pass only what you want to change."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "bandwidth_limit": {"type": "integer"},
                "epoch_duration_seconds": {"type": "integer"},
                "enforcement_mode": {"type": "string", "enum": sorted(VALID_ENFORCEMENT_MODES)},
            },
        },
    ),
    Tool(
        name="workspace_bandwidth_admit",
        description=(
            "Record one workspace broadcast admit. Returns admitted/rejected status. "
            "In `shadow` mode: always admits, returns saturated=True if over limit. "
            "In `enforce` mode: admits up to bandwidth_limit per epoch, then rejects. "
            "In `disabled` mode: behaves like shadow (logs only)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="workspace_bandwidth_epochs_history",
        description="Paginated completed-epoch history. Filters: since, min_saturation. limit clamped to [1, 500].",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
                "since": {"type": "string"},
                "min_saturation": {"type": "number"},
            },
        },
    ),
]


_WB_TOOLS = {
    "workspace_bandwidth_status": tool_workspace_bandwidth_status,
    "workspace_bandwidth_set": tool_workspace_bandwidth_set,
    "workspace_bandwidth_admit": tool_workspace_bandwidth_admit,
    "workspace_bandwidth_epochs_history": tool_workspace_bandwidth_epochs_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _WB_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
