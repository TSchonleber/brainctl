"""brainctl MCP tools — nucleus basalis inspection and CRUD.

Phase 1 of the NB subsystem per docs/proposals/nucleus_basalis.md.
Inspection + idempotent writes only; no behavior change to existing
brainctl tools yet. Phase 2 (separate PR) wires the shadow consult.

NB pairs with the locus coeruleus (LC) subsystem — they are the dual
gain/attention control axes:
  - LC fires on surprise (broadly) → broadcasts NE
  - NB fires on attention shifts (target-locked) → broadcasts ACh
Both feed bg_modulators; LC writes lc_ne, NB writes the acetylcholine
column added by migration 068.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_CHANNEL_KINDS = {
    "thalamic_sector",
    "agent_scope",
    "intent_class",
    "entity_type",
    "other",
}
VALID_NB_MODES = {"phasic_locked", "tonic_high", "tonic_mid", "tonic_low"}
VALID_FIRING_MODES = {"phasic", "tonic_shift"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows_to_list(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


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
        for table in ("nb_attention_targets", "nb_firings", "nb_state")
        if not _table_exists(conn, table)
    ]
    if missing:
        return (
            "NB schema missing: tables "
            + ", ".join(missing)
            + " not found. Run `brainctl migrate` (migration 068) and retry."
        )
    return None


def _context_hash(parts: list[str]) -> str:
    joined = "|".join(p or "" for p in parts)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=12).hexdigest()


# --------------------------------------------------------------------- tools


def tool_nb_status(agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    """Return the current NB state row + last-24h firing summary.

    Phase 1 inspection tool. No side effects.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute(
            "SELECT * FROM nb_state WHERE id = 1"
        ).fetchone()
        recent = conn.execute(
            """
            SELECT
              COUNT(*) AS n,
              COALESCE(AVG(attention_magnitude), 0.0) AS mean_attention,
              COALESCE(AVG(ach_delta_applied), 0.0)  AS mean_ach_delta,
              SUM(CASE WHEN mode = 'phasic' THEN 1 ELSE 0 END)       AS phasic_count,
              SUM(CASE WHEN mode = 'tonic_shift' THEN 1 ELSE 0 END)  AS tonic_shift_count
            FROM nb_firings
            WHERE fired_at >= datetime('now', '-24 hours')
              AND (? IS NULL OR agent_id = ?)
            """,
            (agent_id, agent_id),
        ).fetchone()
        last_firings = _rows_to_list(
            conn.execute(
                """
                SELECT f.id, f.fired_at, f.agent_id, f.attention_magnitude,
                       f.ach_delta_applied, f.mode, t.name AS target_name,
                       t.channel_kind
                FROM nb_firings f
                JOIN nb_attention_targets t ON t.id = f.target_id
                WHERE (? IS NULL OR f.agent_id = ?)
                ORDER BY f.id DESC
                LIMIT 5
                """,
                (agent_id, agent_id),
            ).fetchall()
        )
        target_count = conn.execute(
            "SELECT COUNT(*) FROM nb_attention_targets"
        ).fetchone()[0]

    return {
        "ok": True,
        "state": dict(state) if state else None,
        "recent_24h": dict(recent) if recent else {},
        "last_firings": last_firings,
        "registered_targets": target_count,
    }


def tool_nb_register_target(
    name: str,
    channel_kind: str,
    default_ach_gain: float = 0.10,
    description: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Idempotent UPSERT on nb_attention_targets keyed by `name`.

    Validates channel_kind against the CHECK constraint. Returns the
    target row whether newly inserted or already present.
    """
    if channel_kind not in VALID_CHANNEL_KINDS:
        return {
            "error": (
                f"invalid channel_kind {channel_kind!r}; "
                f"expected one of {sorted(VALID_CHANNEL_KINDS)}"
            )
        }
    if not 0.0 <= default_ach_gain <= 1.0:
        return {"error": "default_ach_gain must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        # UPSERT — if a row with this name exists, update its metadata
        # without bumping created_at; if not, insert fresh.
        conn.execute(
            """
            INSERT INTO nb_attention_targets
                (name, channel_kind, default_ach_gain, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                channel_kind     = excluded.channel_kind,
                default_ach_gain = excluded.default_ach_gain,
                description      = COALESCE(excluded.description,
                                            nb_attention_targets.description)
            """,
            (name, channel_kind, float(default_ach_gain), description),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM nb_attention_targets WHERE name = ?",
            (name,),
        ).fetchone()

    return {"ok": True, "target": dict(row) if row else None}


def tool_nb_fire(
    target_name: str,
    attention_magnitude: float,
    agent_id: str | None = None,
    source_event_id: int | None = None,
    notes: str | None = None,
    mode: str = "phasic",
    **_kw: Any,
) -> dict[str, Any]:
    """Record one NB firing (cholinergic broadcast) at a named target.

    Phase 1 behavior:
      - Inserts a nb_firings row with ach_delta_applied derived from
        target.default_ach_gain × attention_magnitude
      - Updates nb_state.last_attended_target_id + last_phasic_at /
        last_tonic_shift_at according to `mode`
      - Updates nb_attention_targets.last_attended_at
      - Does NOT update bg_modulators.acetylcholine — that's Phase 2

    Returns the inserted firing row + the computed ACh delta.
    """
    if mode not in VALID_FIRING_MODES:
        return {
            "error": (
                f"invalid mode {mode!r}; expected one of "
                f"{sorted(VALID_FIRING_MODES)}"
            )
        }
    if not 0.0 <= attention_magnitude <= 1.0:
        return {"error": "attention_magnitude must be in [0, 1]"}

    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}

        target = conn.execute(
            "SELECT id, default_ach_gain FROM nb_attention_targets WHERE name = ?",
            (target_name,),
        ).fetchone()
        if not target:
            return {
                "error": (
                    f"target {target_name!r} not registered; "
                    "call nb_register_target first"
                )
            }

        ach_delta = round(float(target["default_ach_gain"]) * float(attention_magnitude), 4)
        ctx = _context_hash([target_name, mode, agent_id or "", str(source_event_id or "")])

        cur = conn.execute(
            """
            INSERT INTO nb_firings
                (agent_id, target_id, target_source_event_id,
                 attention_magnitude, ach_delta_applied, mode,
                 context_hash, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id, target["id"], source_event_id,
                float(attention_magnitude), ach_delta, mode, ctx, notes,
            ),
        )
        firing_id = cur.lastrowid

        # Update nb_state — phasic fires touch last_phasic_at,
        # tonic_shift fires touch last_tonic_shift_at. Either way the
        # last_attended_target_id is set to this target.
        if mode == "phasic":
            conn.execute(
                """
                UPDATE nb_state
                   SET last_attended_target_id = ?,
                       last_phasic_at          = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                       updated_at              = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                 WHERE id = 1
                """,
                (target["id"],),
            )
        else:  # tonic_shift
            conn.execute(
                """
                UPDATE nb_state
                   SET last_attended_target_id = ?,
                       last_tonic_shift_at     = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                       updated_at              = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                 WHERE id = 1
                """,
                (target["id"],),
            )

        conn.execute(
            """
            UPDATE nb_attention_targets
               SET last_attended_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = ?
            """,
            (target["id"],),
        )
        conn.commit()

    return {
        "ok": True,
        "firing_id": firing_id,
        "target_name": target_name,
        "attention_magnitude": float(attention_magnitude),
        "ach_delta_applied": ach_delta,
        "mode": mode,
        "context_hash": ctx,
    }


def tool_nb_attend_sector(
    sector_name: str,
    attention_magnitude: float,
    agent_id: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Convenience wrapper for firing NB at a thalamic sector.

    Resolves the sector name to a target_id and delegates to nb_fire.
    Returns nb_fire's output, or an error if the sector isn't
    registered as a thalamic_sector channel.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        row = conn.execute(
            """
            SELECT name FROM nb_attention_targets
             WHERE name = ? AND channel_kind = 'thalamic_sector'
            """,
            (sector_name,),
        ).fetchone()
    if not row:
        return {
            "error": (
                f"sector {sector_name!r} not registered as a "
                "thalamic_sector NB target"
            )
        }
    return tool_nb_fire(
        target_name=sector_name,
        attention_magnitude=attention_magnitude,
        agent_id=agent_id,
        mode="phasic",
    )


def tool_nb_signal_history(
    limit: int = 20,
    since: str | None = None,
    agent_id: str | None = None,
    target_id: int | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Paginated NB firing history, newest first.

    Phase 1 inspection tool. Filters: `since` (ISO timestamp lower
    bound), `agent_id`, `target_id`. `limit` clamped to [1, 200].
    """
    limit = max(1, min(int(limit), 200))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        clauses = []
        params: list[Any] = []
        if since:
            clauses.append("f.fired_at >= ?")
            params.append(since)
        if agent_id:
            clauses.append("f.agent_id = ?")
            params.append(agent_id)
        if target_id is not None:
            clauses.append("f.target_id = ?")
            params.append(int(target_id))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT f.id, f.fired_at, f.agent_id, f.target_id,
                   t.name AS target_name, t.channel_kind,
                   f.attention_magnitude, f.ach_delta_applied,
                   f.mode, f.context_hash, f.notes
              FROM nb_firings f
              JOIN nb_attention_targets t ON t.id = f.target_id
              {where}
             ORDER BY f.id DESC
             LIMIT ?
        """
        rows = conn.execute(sql, (*params, limit)).fetchall()
    return {"ok": True, "history": _rows_to_list(rows)}


# --------------------------------------------------------------------- registration

TOOLS: list[Tool] = [
    Tool(
        name="nb_status",
        description=(
            "Nucleus basalis Phase 1 inspection. Returns the current "
            "nb_state row (mode + ach_reservoir + last attended target) "
            "plus a 24-hour firing summary and the last 5 firings. "
            "Filter by agent_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
        },
    ),
    Tool(
        name="nb_register_target",
        description=(
            "Idempotent UPSERT of an NB attention target (channel "
            "brainctl can direct cholinergic broadcast to). channel_kind "
            "∈ {thalamic_sector, agent_scope, intent_class, entity_type, "
            "other}. default_ach_gain in [0, 1]."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "channel_kind": {
                    "type": "string",
                    "enum": sorted(VALID_CHANNEL_KINDS),
                },
                "default_ach_gain": {"type": "number", "default": 0.10},
                "description": {"type": "string"},
            },
            "required": ["name", "channel_kind"],
        },
    ),
    Tool(
        name="nb_fire",
        description=(
            "Record one NB firing (cholinergic broadcast) at a "
            "registered target. ach_delta_applied is derived from "
            "target.default_ach_gain × attention_magnitude. mode ∈ "
            "{phasic, tonic_shift}. Phase 1: writes nb_firings + "
            "updates nb_state; does NOT update bg_modulators.acetylcholine "
            "(that's Phase 2)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_name": {"type": "string"},
                "attention_magnitude": {"type": "number"},
                "agent_id": {"type": "string"},
                "source_event_id": {"type": "integer"},
                "notes": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": sorted(VALID_FIRING_MODES),
                    "default": "phasic",
                },
            },
            "required": ["target_name", "attention_magnitude"],
        },
    ),
    Tool(
        name="nb_attend_sector",
        description=(
            "Convenience: fire NB at a thalamic sector by name. "
            "Resolves the sector to a target_id and delegates to "
            "nb_fire with mode='phasic'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sector_name": {"type": "string"},
                "attention_magnitude": {"type": "number"},
                "agent_id": {"type": "string"},
            },
            "required": ["sector_name", "attention_magnitude"],
        },
    ),
    Tool(
        name="nb_signal_history",
        description=(
            "Paginated NB firing history, newest first. Filters: since "
            "(ISO timestamp lower bound), agent_id, target_id. limit "
            "clamped to [1, 200]."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "agent_id": {"type": "string"},
                "target_id": {"type": "integer"},
            },
        },
    ),
]


_NB_TOOLS = {
    "nb_status": tool_nb_status,
    "nb_register_target": tool_nb_register_target,
    "nb_fire": tool_nb_fire,
    "nb_attend_sector": tool_nb_attend_sector,
    "nb_signal_history": tool_nb_signal_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _NB_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    """Return tool descriptors and dispatch map for mcp_server integration."""
    return TOOLS, DISPATCH
