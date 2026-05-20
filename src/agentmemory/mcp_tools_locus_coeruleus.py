"""brainctl MCP tools — locus coeruleus inspection and trigger catalog.

Phase 1 of the LC subsystem per docs/proposals/locus_coeruleus.md. The
locus coeruleus is the norepinephrine-readiness broadcaster that records
surprise-triggered activations. Phase 1 is additive: schema + read/CRUD
tools only. It does not update bg_modulators.lc_ne; that wiring belongs to
Phase 2 shadow mode.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db, rows_to_list
from agentmemory.paths import get_db_path

logger = logging.getLogger(__name__)

DB_PATH: Path = get_db_path()

VALID_SOURCE_TABLES = {"cerebellum_predictions", "bg_td_events", "memory_events", "other"}
VALID_LC_STATE_MODES = {"phasic_ready", "tonic_high", "tonic_mid", "tonic_low"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        table
        for table in ("lc_triggers", "lc_firings", "lc_state")
        if not _table_exists(conn, table)
    ]
    if missing:
        return "locus coeruleus schema missing tables: " + ", ".join(missing)
    return None


def _ensure_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO lc_state (id, mode, ne_reservoir)
        VALUES (1, 'tonic_mid', 0.5)
        """
    )


def _to_float(value: Any, field: str) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, f"{field} must be numeric"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _current_bg_lc_ne(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not _table_exists(conn, "bg_modulators"):
        return None
    row = conn.execute(
        "SELECT id, lc_ne, updated_at, set_by FROM bg_modulators WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def tool_lc_status(agent_id: str | None = None, **kw: Any) -> dict[str, Any]:
    """Return LC state and last-24h firing summary.

    Phase 1 also reads bg_modulators.lc_ne when present, but never writes it.
    """
    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        _ensure_state(db)
        db.commit()

        state_row = db.execute("SELECT * FROM lc_state WHERE id = 1").fetchone()

        where = ["f.fired_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')"]
        params: list[Any] = []
        if agent_id:
            where.append("f.agent_id = ?")
            params.append(agent_id)
        where_sql = "WHERE " + " AND ".join(where)

        summary = db.execute(
            f"""
            SELECT COUNT(*) AS count,
                   ROUND(COALESCE(AVG(f.surprise_magnitude), 0.0), 4) AS mean_surprise_magnitude,
                   ROUND(COALESCE(AVG(f.ne_delta_applied), 0.0), 4) AS mean_ne_delta_applied,
                   SUM(CASE WHEN f.mode = 'tonic_shift' THEN 1 ELSE 0 END) AS mode_transitions
            FROM lc_firings f
            {where_sql}
            """,  # nosec B608
            params,
        ).fetchone()

        recent = db.execute(
            f"""
            SELECT f.id, f.fired_at, f.agent_id, f.trigger_id, t.name AS trigger_name,
                   f.trigger_source_event_id, f.surprise_magnitude,
                   f.ne_delta_applied, f.mode, f.context_hash, f.notes
            FROM lc_firings f
            LEFT JOIN lc_triggers t ON t.id = f.trigger_id
            {where_sql}
            ORDER BY f.fired_at DESC, f.id DESC
            LIMIT 10
            """,  # nosec B608
            params,
        ).fetchall()

        return {
            "ok": True,
            "agent_filter": agent_id,
            "state": dict(state_row) if state_row else None,
            "bg_modulators_lc_ne": _current_bg_lc_ne(db),
            "recent_24h": dict(summary) if summary else {
                "count": 0,
                "mean_surprise_magnitude": 0.0,
                "mean_ne_delta_applied": 0.0,
                "mode_transitions": 0,
            },
            "recent_firings": rows_to_list(recent),
        }
    except Exception as exc:
        logger.exception("lc_status failed")
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_lc_fire(
    trigger_name: str,
    surprise_magnitude: float,
    agent_id: str | None = None,
    source_event_id: int | None = None,
    notes: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Manually log a phasic LC activation by trigger name.

    Phase 1 updates LC's own activation log and reservoir only. It does not
    write bg_modulators.lc_ne.
    """
    if not trigger_name or not isinstance(trigger_name, str):
        return {"ok": False, "error": "trigger_name is required"}
    magnitude, magnitude_error = _to_float(surprise_magnitude, "surprise_magnitude")
    if magnitude_error:
        return {"ok": False, "error": magnitude_error}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        _ensure_state(db)

        trigger = db.execute(
            "SELECT * FROM lc_triggers WHERE name = ?",
            (trigger_name,),
        ).fetchone()
        if trigger is None:
            return {"ok": False, "error": f"unknown LC trigger: {trigger_name}"}

        ne_delta = float(trigger["default_ne_delta"] or 0.0)
        cursor = db.execute(
            """
            INSERT INTO lc_firings
                (agent_id, trigger_id, trigger_source_event_id,
                 surprise_magnitude, ne_delta_applied, mode, notes)
            VALUES (?, ?, ?, ?, ?, 'phasic', ?)
            """,
            (agent_id, trigger["id"], source_event_id, magnitude, ne_delta, notes),
        )
        db.execute(
            """
            UPDATE lc_state
            SET ne_reservoir = MIN(1.0, MAX(0.0, ne_reservoir + ?)),
                last_phasic_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            WHERE id = 1
            """,
            (ne_delta,),
        )
        db.commit()
        state = db.execute("SELECT * FROM lc_state WHERE id = 1").fetchone()
        return {
            "ok": True,
            "firing_id": cursor.lastrowid,
            "trigger_id": trigger["id"],
            "trigger_name": trigger["name"],
            "ne_delta_applied": ne_delta,
            "mode": "phasic",
            "lc_state_mode": state["mode"] if state else None,
        }
    except Exception as exc:
        logger.exception("lc_fire failed")
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_lc_register_trigger(
    name: str,
    source_table: str,
    threshold_field: str | None = None,
    threshold_value: float | None = None,
    default_ne_delta: float = 0.0,
    description: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Idempotent UPSERT into lc_triggers keyed by trigger name."""
    if not name or not isinstance(name, str):
        return {"ok": False, "error": "name is required"}
    if source_table not in VALID_SOURCE_TABLES:
        return {"ok": False, "error": f"source_table must be one of {sorted(VALID_SOURCE_TABLES)}"}
    threshold_float, threshold_error = _to_float(threshold_value, "threshold_value")
    if threshold_error:
        return {"ok": False, "error": threshold_error}
    delta_float, delta_error = _to_float(default_ne_delta, "default_ne_delta")
    if delta_error:
        return {"ok": False, "error": delta_error}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        db.execute(
            """
            INSERT INTO lc_triggers
                (name, source_table, threshold_field, threshold_value,
                 default_ne_delta, description)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                source_table = excluded.source_table,
                threshold_field = excluded.threshold_field,
                threshold_value = excluded.threshold_value,
                default_ne_delta = excluded.default_ne_delta,
                description = excluded.description
            """,
            (
                name,
                source_table,
                threshold_field,
                threshold_float,
                delta_float,
                description,
            ),
        )
        db.commit()
        row = db.execute("SELECT * FROM lc_triggers WHERE name = ?", (name,)).fetchone()
        return {"ok": True, "trigger": dict(row) if row else None}
    except Exception as exc:
        logger.exception("lc_register_trigger failed")
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_lc_signal_history(
    limit: int = 20,
    since: str | None = None,
    agent_id: str | None = None,
    trigger_id: int | None = None,
    **kw: Any,
) -> list[dict[str, Any]]:
    """Return recent LC firing history with optional filters."""
    try:
        limit_int = max(1, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        limit_int = 20

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return [{"ok": False, "error": schema_error}]
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("f.fired_at >= ?")
            params.append(since)
        if agent_id:
            clauses.append("f.agent_id = ?")
            params.append(agent_id)
        if trigger_id is not None:
            clauses.append("f.trigger_id = ?")
            params.append(int(trigger_id))
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            f"""
            SELECT f.id, f.fired_at, f.agent_id, f.trigger_id, t.name AS trigger_name,
                   f.trigger_source_event_id, f.surprise_magnitude,
                   f.ne_delta_applied, f.mode, f.context_hash, f.notes
            FROM lc_firings f
            LEFT JOIN lc_triggers t ON t.id = f.trigger_id
            {where_sql}
            ORDER BY f.fired_at DESC, f.id DESC
            LIMIT ?
            """,  # nosec B608
            params + [limit_int],
        ).fetchall()
        return rows_to_list(rows)
    except Exception as exc:
        logger.exception("lc_signal_history failed")
        return [{"ok": False, "error": str(exc)}]
    finally:
        db.close()


def tool_lc_set_mode(mode: str, reason: str | None = None, **kw: Any) -> dict[str, Any]:
    """Update the single-row LC state mode after validation."""
    if mode not in VALID_LC_STATE_MODES:
        return {"ok": False, "error": f"mode must be one of {sorted(VALID_LC_STATE_MODES)}"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        _ensure_state(db)

        if mode.startswith("tonic_"):
            db.execute(
                """
                UPDATE lc_state
                SET mode = ?,
                    last_tonic_shift_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                WHERE id = 1
                """,
                (mode,),
            )
        else:
            db.execute(
                """
                UPDATE lc_state
                SET mode = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                WHERE id = 1
                """,
                (mode,),
            )

        db.execute(
            """
            INSERT INTO lc_firings
                (surprise_magnitude, ne_delta_applied, mode, notes)
            VALUES (0.0, 0.0, 'tonic_shift', ?)
            """,
            (reason,),
        )
        db.commit()
        row = db.execute("SELECT * FROM lc_state WHERE id = 1").fetchone()
        return {"ok": True, "state": dict(row) if row else None, "reason": reason}
    except Exception as exc:
        logger.exception("lc_set_mode failed")
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(
        name="lc_status",
        description=(
            "Inspect the locus coeruleus subsystem: current LC state, current "
            "bg_modulators.lc_ne value if present, and last-24h firing summary "
            "(count, mean surprise magnitude, mean NE delta, tonic-shift count)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Optional agent filter for firing summary"},
            },
        },
    ),
    Tool(
        name="lc_fire",
        description=(
            "Manually log a phasic LC firing by trigger name. Inserts lc_firings "
            "and updates lc_state only. Phase 1 does not write bg_modulators.lc_ne."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "trigger_name": {"type": "string", "description": "Name in lc_triggers"},
                "surprise_magnitude": {"type": "number"},
                "agent_id": {"type": "string"},
                "source_event_id": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["trigger_name", "surprise_magnitude"],
        },
    ),
    Tool(
        name="lc_register_trigger",
        description=(
            "Register or update an LC trigger. Idempotent UPSERT by name. "
            "source_table must be cerebellum_predictions, bg_td_events, "
            "memory_events, or other."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "source_table": {"type": "string", "enum": sorted(VALID_SOURCE_TABLES)},
                "threshold_field": {"type": "string"},
                "threshold_value": {"type": "number"},
                "default_ne_delta": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["name", "source_table", "default_ne_delta"],
        },
    ),
    Tool(
        name="lc_signal_history",
        description=(
            "Return LC firing history with optional filters: since timestamp, "
            "agent_id, trigger_id, and limit. Sorted newest first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "agent_id": {"type": "string"},
                "trigger_id": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="lc_set_mode",
        description=(
            "Set the single-row LC mode. Valid modes: phasic_ready, tonic_high, "
            "tonic_mid, tonic_low. Records a tonic_shift audit row."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": sorted(VALID_LC_STATE_MODES)},
                "reason": {"type": "string"},
            },
            "required": ["mode"],
        },
    ),
]

_LC_TOOLS = {
    "lc_status": tool_lc_status,
    "lc_fire": tool_lc_fire,
    "lc_register_trigger": tool_lc_register_trigger,
    "lc_signal_history": tool_lc_signal_history,
    "lc_set_mode": tool_lc_set_mode,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _LC_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    """Return tool descriptors and dispatch map for mcp_server integration."""
    return TOOLS, DISPATCH
