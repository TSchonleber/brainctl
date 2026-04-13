"""brainctl MCP tools — neuromodulation."""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

from agentmemory.lib.mcp_helpers import now_iso, open_db

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


_now = now_iso


# ---------------------------------------------------------------------------
# Neuro presets and aliases
# ---------------------------------------------------------------------------

_NEURO_PRESETS: dict[str, dict] = {
    "normal": {
        "arousal_level": 0.3,
        "retrieval_breadth_multiplier": 1.0,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 240,
        "focus_level": 0.3,
        "similarity_threshold_delta": 0.0,
        "exploitation_bias": 0.0,
        "temporal_lambda": 0.030,
        "context_window_depth": 50,
        "confidence_decay_rate": 0.020,
    },
    "incident": {
        "arousal_level": 0.9,
        "retrieval_breadth_multiplier": 1.6,
        "consolidation_immediacy": "immediate",
        "consolidation_interval_mins": 30,
        "focus_level": 0.1,
        "similarity_threshold_delta": -0.10,
        "exploitation_bias": 0.0,
        "temporal_lambda": 0.100,
        "context_window_depth": 75,
        "confidence_decay_rate": 0.005,
    },
    "sprint": {
        "arousal_level": 0.5,
        "retrieval_breadth_multiplier": 1.2,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 120,
        "focus_level": 0.5,
        "similarity_threshold_delta": 0.0,
        "exploitation_bias": 0.2,
        "temporal_lambda": 0.060,
        "context_window_depth": 30,
        "confidence_decay_rate": 0.015,
    },
    "strategic_planning": {
        "arousal_level": 0.2,
        "retrieval_breadth_multiplier": 0.9,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 480,
        "focus_level": 0.4,
        "similarity_threshold_delta": 0.05,
        "exploitation_bias": 0.1,
        "temporal_lambda": 0.005,
        "context_window_depth": 200,
        "confidence_decay_rate": 0.010,
    },
    "focused_work": {
        "arousal_level": 0.6,
        "retrieval_breadth_multiplier": 0.8,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 120,
        "focus_level": 0.8,
        "similarity_threshold_delta": 0.08,
        "exploitation_bias": 0.4,
        "temporal_lambda": 0.080,
        "context_window_depth": 25,
        "confidence_decay_rate": 0.015,
    },
}

_MODE_ALIASES: dict[str, str] = {
    "normal": "normal",
    "urgent": "incident",
    "incident": "incident",
    "sprint": "sprint",
    "strategic": "strategic_planning",
    "strategic_planning": "strategic_planning",
    "focused": "focused_work",
    "focused_work": "focused_work",
}


# ---------------------------------------------------------------------------
# Private helpers (ported from _impl.py)
# ---------------------------------------------------------------------------

def _neuro_get_state(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT * FROM neuromodulation_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def _neuro_is_expired(state: dict) -> bool:
    if not state.get("expires_at"):
        return False
    try:
        exp = datetime.fromisoformat(state["expires_at"])
        now = datetime.utcnow() if exp.tzinfo is None else datetime.now(timezone.utc)
        return now > exp
    except Exception:
        return False


def _neuro_detect(db: sqlite3.Connection) -> tuple[str, str]:
    """Auto-detect org_state from recent events. Returns (org_state, reason)."""
    if db.execute(
        "SELECT id FROM epochs WHERE (name LIKE '%incident%' OR name LIKE '%outage%' OR name LIKE '%emergency%') "
        "AND started_at <= strftime('%Y-%m-%dT%H:%M:%S','now') "
        "AND (ended_at IS NULL OR ended_at >= strftime('%Y-%m-%dT%H:%M:%S','now')) LIMIT 1"
    ).fetchone():
        return "incident", "active incident epoch"

    err = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('error','warning') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-2 hours'))"
    ).fetchone()[0]
    if err >= 5:
        return "incident", f"{err} error/warning events in last 2h"

    total6h = db.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-6 hours'))"
    ).fetchone()[0]
    plan6h = db.execute(
        "SELECT COUNT(*) FROM events WHERE "
        "(summary LIKE '%planning%' OR summary LIKE '%roadmap%' OR summary LIKE '%strategy%' "
        "OR event_type='decision') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-6 hours'))"
    ).fetchone()[0]
    if total6h > 0 and plan6h / total6h >= 0.5:
        return "strategic_planning", f"{plan6h}/{total6h} recent events are planning-tagged"

    if db.execute(
        "SELECT id FROM epochs WHERE name LIKE '%sprint%' "
        "AND started_at <= strftime('%Y-%m-%dT%H:%M:%S','now') "
        "AND (ended_at IS NULL OR ended_at >= strftime('%Y-%m-%dT%H:%M:%S','now')) LIMIT 1"
    ).fetchone():
        return "sprint", "active sprint epoch"

    trate = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='task_update' "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-2 hours'))"
    ).fetchone()[0]
    if trate > 16:
        return "sprint", f"high task activity: {trate} task events in last 2h"

    if total6h >= 3:
        row = db.execute(
            "SELECT project, COUNT(*) as cnt FROM events "
            "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-2 hours')) "
            "AND project IS NOT NULL GROUP BY project ORDER BY cnt DESC LIMIT 1"
        ).fetchone()
        if row and total6h > 0 and row[1] / total6h >= 0.80:
            return "focused_work", f"80%+ events from project: {row[0]}"

    return "normal", "no trigger conditions met"


def _neuro_apply_preset(
    db: sqlite3.Connection,
    org_state: str,
    method: str,
    agent_id: str,
    notes: str,
    expires_at: str | None = None,
) -> None:
    p = _NEURO_PRESETS[org_state]
    db.execute(
        """UPDATE neuromodulation_state SET
        org_state=?,arousal_level=?,retrieval_breadth_multiplier=?,
        consolidation_immediacy=?,consolidation_interval_mins=?,
        focus_level=?,similarity_threshold_delta=?,exploitation_bias=?,
        temporal_lambda=?,context_window_depth=?,confidence_decay_rate=?,
        detection_method=?,detected_at=strftime('%Y-%m-%dT%H:%M:%S','now'),
        expires_at=?,triggered_by=?,notes=? WHERE id=1""",
        (
            org_state, p["arousal_level"], p["retrieval_breadth_multiplier"],
            p["consolidation_immediacy"], p["consolidation_interval_mins"],
            p["focus_level"], p["similarity_threshold_delta"], p["exploitation_bias"],
            p["temporal_lambda"], p["context_window_depth"], p["confidence_decay_rate"],
            method, expires_at, agent_id, notes,
        ),
    )


def _compute_neurotransmitter_levels(db: sqlite3.Connection) -> dict:
    """Compute dopamine, norepinephrine, acetylcholine, serotonin levels from org activity."""
    now_sql = _now()

    # Dopamine (reward signal): positive vs negative event ratio in last 24h
    positive_24h = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('result','decision','memory_promoted') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours'))"
    ).fetchone()[0]
    negative_24h = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('error','warning','stale_context') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours'))"
    ).fetchone()[0]
    total_24h = positive_24h + negative_24h
    if total_24h == 0:
        dopamine = 0.4
    else:
        dopamine = min(1.0, max(0.0, positive_24h / total_24h))

    nm_row = db.execute(
        "SELECT dopamine_signal FROM neuromodulation_state WHERE id=1"
    ).fetchone()
    if nm_row and nm_row["dopamine_signal"]:
        dopamine = min(1.0, max(0.0, dopamine + nm_row["dopamine_signal"] * 0.3))

    # Norepinephrine (arousal/urgency): error events in last 2h
    error_2h = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN ('error','warning') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-2 hours'))"
    ).fetchone()[0]
    incident_active = db.execute(
        "SELECT 1 FROM epochs WHERE (name LIKE '%incident%' OR name LIKE '%outage%' OR name LIKE '%emergency%') "
        "AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?) LIMIT 1",
        (now_sql, now_sql),
    ).fetchone()
    norepinephrine_raw = min(1.0, error_2h / 5.0)
    if incident_active:
        norepinephrine_raw = max(0.8, norepinephrine_raw)
    norepinephrine = round(norepinephrine_raw, 3)

    # Acetylcholine (attention/novelty): new unique scopes written recently
    new_memories_1h = db.execute(
        "SELECT COUNT(DISTINCT scope) FROM memories WHERE retired_at IS NULL "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-1 hour'))"
    ).fetchone()[0]
    active_scopes = db.execute(
        "SELECT COUNT(DISTINCT scope) FROM memories WHERE retired_at IS NULL"
    ).fetchone()[0] or 1
    acetylcholine = round(min(1.0, new_memories_1h / max(1, active_scopes * 0.2)), 3)
    distinct_agents_1h = db.execute(
        "SELECT COUNT(DISTINCT agent_id) FROM events "
        "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-1 hour')) "
        "AND agent_id IS NOT NULL"
    ).fetchone()[0]
    if distinct_agents_1h > 3:
        acetylcholine = min(1.0, acetylcholine + 0.2)

    # Serotonin (patience/horizon): derived from temporal_lambda
    nm_lambda = db.execute(
        "SELECT temporal_lambda FROM neuromodulation_state WHERE id=1"
    ).fetchone()
    lam = nm_lambda["temporal_lambda"] if nm_lambda else 0.030
    serotonin = round(1.0 - min(1.0, max(0.0, (lam - 0.005) / 0.095)), 3)

    return {
        "dopamine_level": round(dopamine, 3),
        "norepinephrine_level": norepinephrine,
        "acetylcholine_level": acetylcholine,
        "serotonin_level": serotonin,
    }


def _log_neuro_event(
    db: sqlite3.Connection,
    levels: dict,
    org_state: str,
    source: str,
    agent_id: str | None = None,
    notes: str | None = None,
) -> None:
    """Log neurotransmitter levels to neuro_events history table."""
    try:
        db.execute(
            "INSERT INTO neuro_events (org_state, dopamine_level, norepinephrine_level, "
            "acetylcholine_level, serotonin_level, source, agent_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                org_state,
                levels["dopamine_level"],
                levels["norepinephrine_level"],
                levels["acetylcholine_level"],
                levels["serotonin_level"],
                source,
                agent_id,
                notes,
            ),
        )
    except Exception:
        pass  # neuro_events table may not exist on older brain.db versions


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_neuro_status(agent_id: str = "mcp-client", **kw) -> dict:
    """Return current neuromodulation state, auto-reverting expired manual overrides."""
    try:
        db = _db()
        state = _neuro_get_state(db)
        if not state:
            return {"ok": False, "error": "neuromodulation_state table not found"}

        reverted = False
        revert_reason = ""
        if state.get("detection_method") == "manual" and _neuro_is_expired(state):
            new_state, reason = _neuro_detect(db)
            _neuro_apply_preset(db, new_state, "auto", "auto", f"auto-reverted: {reason}")
            db.commit()
            state = _neuro_get_state(db)
            reverted = True
            revert_reason = reason

        result: dict[str, Any] = {"ok": True, **state}
        if reverted:
            result["auto_reverted"] = True
            result["revert_reason"] = revert_reason
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_neuro_set(
    agent_id: str = "mcp-client",
    mode: str = "normal",
    notes: str | None = None,
    expires: str | None = None,
    **kw,
) -> dict:
    """Set neuromodulation mode manually."""
    try:
        org_state = _MODE_ALIASES.get(mode.lower())
        if org_state is None:
            return {
                "ok": False,
                "error": f"Unknown mode '{mode}'. Valid: {', '.join(sorted(_MODE_ALIASES))}",
            }
        db = _db()
        current = _neuro_get_state(db)
        from_state = current.get("org_state", "normal")
        resolved_notes = notes or f"manual override to {org_state}"
        _neuro_apply_preset(db, org_state, "manual", agent_id, resolved_notes, expires)
        if from_state != org_state:
            db.execute(
                "INSERT INTO neuromodulation_transitions (from_state,to_state,reason,triggered_by) "
                "VALUES (?,?,?,?)",
                (from_state, org_state, resolved_notes, agent_id),
            )
        db.commit()
        result: dict[str, Any] = {
            "ok": True,
            "org_state": org_state,
            "from_state": from_state,
        }
        if expires:
            result["expires_at"] = expires
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_neuro_detect(
    agent_id: str = "mcp-client",
    force: bool = False,
    **kw,
) -> dict:
    """Auto-detect org state from recent activity and apply it."""
    try:
        db = _db()
        current = _neuro_get_state(db)
        from_state = current.get("org_state", "normal")
        if (
            current.get("detection_method") == "manual"
            and not _neuro_is_expired(current)
            and not force
        ):
            return {
                "ok": True,
                "skipped": True,
                "reason": f"Manual override active ({from_state}) — use force=true to override.",
                "org_state": from_state,
            }
        org_state, reason = _neuro_detect(db)
        _neuro_apply_preset(db, org_state, "auto", agent_id, reason)
        if from_state != org_state:
            db.execute(
                "INSERT INTO neuromodulation_transitions (from_state,to_state,reason,triggered_by) "
                "VALUES (?,?,?,?)",
                (from_state, org_state, reason, agent_id),
            )
        db.commit()
        return {
            "ok": True,
            "org_state": org_state,
            "reason": reason,
            "from_state": from_state,
            "transitioned": from_state != org_state,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_neuro_history(
    agent_id: str = "mcp-client",
    limit: int = 20,
    **kw,
) -> dict:
    """Return recent neuromodulation state transitions."""
    try:
        db = _db()
        rows = db.execute(
            "SELECT from_state,to_state,reason,triggered_by,transitioned_at "
            "FROM neuromodulation_transitions ORDER BY transitioned_at DESC LIMIT ?",
            (limit or 20,),
        ).fetchall()
        return {"ok": True, "transitions": [dict(r) for r in rows], "count": len(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_neurostate(
    agent_id: str = "mcp-client",
    detect: bool = False,
    **kw,
) -> dict:
    """Compute and return current neurotransmitter levels derived from org activity."""
    try:
        db = _db()
        state = _neuro_get_state(db)
        if not state:
            return {"ok": False, "error": "neuromodulation_state table not found. Run brain.db migrations first."}

        if detect or state.get("detection_method") == "auto":
            new_org_state, reason = _neuro_detect(db)
            if new_org_state != state.get("org_state"):
                from_state = state.get("org_state", "normal")
                _neuro_apply_preset(db, new_org_state, "auto", agent_id, reason)
                db.execute(
                    "INSERT INTO neuromodulation_transitions (from_state,to_state,reason,triggered_by) "
                    "VALUES (?,?,?,?)",
                    (from_state, new_org_state, reason, agent_id),
                )
                db.commit()
                state = _neuro_get_state(db)

        org_state = state.get("org_state", "normal")
        levels = _compute_neurotransmitter_levels(db)

        _log_neuro_event(db, levels, org_state, "auto_detect", agent_id)
        db.commit()

        return {
            "ok": True,
            "org_state": org_state,
            **levels,
            "neuromod_params": dict(state),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_neuro_signal(
    agent_id: str = "mcp-client",
    dopamine: float = 0.0,
    scope: str | None = None,
    since: str | None = None,
    **kw,
) -> dict:
    """Inject a dopamine signal — boost or penalize memory confidence."""
    try:
        dopamine = float(dopamine)
        if not (-1.0 <= dopamine <= 1.0):
            return {"ok": False, "error": "--dopamine must be between -1.0 and +1.0"}

        db = _db()
        magnitude = abs(dopamine)
        now_sql = _now()

        where_parts = ["retired_at IS NULL"]
        params: list = []
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        if since:
            where_parts.append("last_recalled_at >= ?")
            params.append(since)
        where = " AND ".join(where_parts)

        if dopamine > 0:
            db.execute(
                f"UPDATE memories SET confidence = MIN(1.0, confidence + ?) WHERE {where}",
                [round(0.1 * magnitude, 4)] + params,
            )
            direction = "boost"
        else:
            db.execute(
                f"UPDATE memories SET confidence = MAX(0.1, confidence - ?), "
                f"tags = json_insert(COALESCE(tags, '[]'), '$[#]', 'needs_review') WHERE {where}",
                [round(0.08 * magnitude, 4)] + params,
            )
            direction = "penalize"

        affected = db.execute("SELECT changes()").fetchone()[0]

        current_signal = db.execute(
            "SELECT dopamine_signal FROM neuromodulation_state WHERE id=1"
        ).fetchone()
        cur = float(current_signal["dopamine_signal"]) if current_signal else 0.0
        new_signal = max(-1.0, min(1.0, cur + dopamine * 0.5))
        db.execute(
            "UPDATE neuromodulation_state SET dopamine_signal=?, dopamine_last_fired_at=? WHERE id=1",
            (round(new_signal, 4), now_sql),
        )

        state = _neuro_get_state(db)
        levels = _compute_neurotransmitter_levels(db)
        _log_neuro_event(
            db, levels, state.get("org_state", "normal"), "signal_inject",
            agent_id, f"dopamine={dopamine:+.2f} scope={scope} since={since}",
        )
        db.commit()

        return {
            "ok": True,
            "signal": dopamine,
            "direction": direction,
            "affected_memories": affected,
            "new_dopamine_signal": new_signal,
            "scope": scope,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_weights(
    agent_id: str = "mcp-client",
    query: str | None = None,
    **kw,
) -> dict:
    """Show current adaptive retrieval weights and the store stats that drove them."""
    try:
        import salience_routing as _sal  # type: ignore[import]
    except ImportError:
        return {"ok": False, "error": "salience_routing module not available"}
    try:
        db = _db()
        nm = _neuro_get_state(db)
        weights = _sal.compute_adaptive_weights(db, query=query, neuro=nm or {})
        core = {k: v for k, v in weights.items() if not k.startswith("_")}
        diag = {k: v for k, v in weights.items() if k.startswith("_")}
        return {
            "ok": True,
            "weights": core,
            "diagnostics": diag,
            "query": query,
            "note": "weights sum to 1.0; diagnostics explain how they were derived",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="neuro_status",
        description=(
            "Return current neuromodulation state (org_state, arousal, lambda, etc.). "
            "Automatically reverts expired manual overrides back to auto-detected state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
            },
        },
    ),
    Tool(
        name="neuro_set",
        description=(
            "Manually set the neuromodulation mode. Valid modes: normal, urgent/incident, sprint, "
            "strategic/strategic_planning, focused/focused_work. "
            "Applies the corresponding preset parameters and logs a transition."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
                "mode": {
                    "type": "string",
                    "enum": sorted(_MODE_ALIASES.keys()),
                    "description": "Neuromodulation mode to set",
                },
                "notes": {"type": "string", "description": "Optional notes for this override"},
                "expires": {
                    "type": "string",
                    "description": "Optional ISO datetime when manual override expires",
                },
            },
            "required": ["mode"],
        },
    ),
    Tool(
        name="neuro_detect",
        description=(
            "Auto-detect org state from recent events (incidents, sprints, planning activity) "
            "and apply corresponding neuromodulation preset. Skips if a valid manual override is active "
            "unless force=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
                "force": {
                    "type": "boolean",
                    "description": "Override active manual setting",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="neuro_history",
        description="Return recent neuromodulation state transitions in reverse chronological order.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
                "limit": {
                    "type": "integer",
                    "description": "Max transitions to return (default: 20)",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="neurostate",
        description=(
            "Compute and return current neurotransmitter levels (dopamine, norepinephrine, "
            "acetylcholine, serotonin) derived from org activity. Logs the snapshot to neuro_events. "
            "Pass detect=true to also auto-update the org_state first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
                "detect": {
                    "type": "boolean",
                    "description": "Re-run auto-detection before computing levels",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="neuro_signal",
        description=(
            "Inject a dopamine signal to boost (+) or penalize (-) memory confidence. "
            "Positive values boost confidence by 10% × magnitude; negative values penalize by 8% × magnitude "
            "and tag affected memories with 'needs_review'. Also updates the dopamine_signal reservoir in "
            "neuromodulation_state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
                "dopamine": {
                    "type": "number",
                    "description": "Signal strength in [-1.0, +1.0]. Positive = reward, negative = penalty.",
                },
                "scope": {
                    "type": "string",
                    "description": "Limit to memories in this scope (e.g. 'project:foo'). Omit for all.",
                },
                "since": {
                    "type": "string",
                    "description": "Limit to memories last_recalled_at >= this ISO datetime.",
                },
            },
            "required": ["dopamine"],
        },
    ),
    Tool(
        name="weights",
        description=(
            "Show current adaptive retrieval weights and diagnostics computed by salience_routing. "
            "Weights sum to 1.0. Pass a query to get query-specific weight adjustments. "
            "Returns an error if the salience_routing module is not installed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID", "default": "mcp-client"},
                "query": {
                    "type": "string",
                    "description": "Optional query string for query-specific weight computation",
                },
            },
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch map
# ---------------------------------------------------------------------------

DISPATCH: dict = {
    "neuro_status": tool_neuro_status,
    "neuro_set": tool_neuro_set,
    "neuro_detect": tool_neuro_detect,
    "neuro_history": tool_neuro_history,
    "neurostate": tool_neurostate,
    "neuro_signal": tool_neuro_signal,
    "weights": tool_weights,
}
