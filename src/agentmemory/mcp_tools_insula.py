"""brainctl MCP tools — insula / interoception.

Self-state monitoring. Posterior insula = raw counters (already in
brainctl's stats/telemetry/access_log); anterior insula = the
aggregated felt-state vector that other subsystems subscribe to.

Phase 1: 4 tools — sample, state read, subscribe, check triggers.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH = get_db_path()
_VALID_COMPARATORS = {"gt", "lt", "abs_gt"}
_EMA_ALPHA = 0.05
_KNOWN_SIGNALS = {
    "write_pressure", "retrieval_strain", "consolidation_debt",
    "embedding_health", "attention_load", "certainty",
}


def _db(): return open_db(str(DB_PATH))
def _rows(r: Iterable[sqlite3.Row]): return [dict(x) for x in r]


def _label_for(state: dict[str, float]) -> str:
    if state.get("write_pressure", 0) > 0.8 or state.get("retrieval_strain", 0) > 0.8:
        return "overloaded"
    if state.get("consolidation_debt", 0) > 0.7:
        return "fatigued"
    if state.get("certainty", 0.5) < 0.3:
        return "uncertain"
    if state.get("attention_load", 0) > 0.6:
        return "strained"
    return "calm"


def _sample_signals(conn: sqlite3.Connection) -> dict[str, float]:
    """Pull current values from brainctl's existing telemetry sources.
    Each is normalized to [0, 1] before storage."""
    out: dict[str, float] = {}

    # write_pressure: recent memory writes vs typical
    try:
        row = conn.execute(
            "SELECT CAST(COUNT(*) AS REAL) / 100.0 FROM memories "
            "WHERE created_at > datetime('now','-1 hour')"
        ).fetchone()
        out["write_pressure"] = min(1.0, float(row[0] or 0))
    except sqlite3.OperationalError:
        out["write_pressure"] = 0.0

    # retrieval_strain: cross-encoder skip rate (rough proxy via access_log
    # action=search count over recent window)
    try:
        row = conn.execute(
            "SELECT CAST(COUNT(*) AS REAL) / 200.0 FROM access_log "
            "WHERE action = 'search' AND created_at > datetime('now','-1 hour')"
        ).fetchone()
        out["retrieval_strain"] = min(1.0, float(row[0] or 0))
    except sqlite3.OperationalError:
        out["retrieval_strain"] = 0.0

    # consolidation_debt: low-priority memories that haven't been recalled
    try:
        row = conn.execute(
            "SELECT CAST(COUNT(*) AS REAL) / (SELECT MAX(1, COUNT(*)) FROM memories) "
            "FROM memories WHERE replay_priority < 1.0 AND "
            "(last_recalled_at IS NULL OR last_recalled_at < datetime('now','-24 hours'))"
        ).fetchone()
        out["consolidation_debt"] = min(1.0, float(row[0] or 0))
    except sqlite3.OperationalError:
        out["consolidation_debt"] = 0.0

    # embedding_health: rough proxy — fraction of memories with embeddings present
    try:
        row = conn.execute(
            "SELECT CAST(SUM(CASE WHEN write_tier='full' THEN 1 ELSE 0 END) AS REAL) / "
            "MAX(1, COUNT(*)) FROM memories"
        ).fetchone()
        out["embedding_health"] = float(row[0] or 1.0)
    except sqlite3.OperationalError:
        out["embedding_health"] = 1.0

    # attention_load: workspace_broadcasts in last hour vs typical
    try:
        row = conn.execute(
            "SELECT CAST(COUNT(*) AS REAL) / 50.0 FROM workspace_broadcasts "
            "WHERE broadcast_at > datetime('now','-1 hour')"
        ).fetchone()
        out["attention_load"] = min(1.0, float(row[0] or 0))
    except sqlite3.OperationalError:
        out["attention_load"] = 0.0

    # certainty: mean confidence on recent memories
    try:
        row = conn.execute(
            "SELECT AVG(confidence) FROM memories "
            "WHERE created_at > datetime('now','-24 hours')"
        ).fetchone()
        out["certainty"] = float(row[0] or 0.5)
    except sqlite3.OperationalError:
        out["certainty"] = 0.5

    return out


def tool_insula_sample(**kw: Any) -> dict[str, Any]:
    """Collect-all-signals pass. Writes insula_signals rows + updates the
    insula_state singleton + recomputes EMAs/deviations."""
    db = _db()
    try:
        signals = _sample_signals(db)
        # Update EMA + deviation per signal
        for name, val in signals.items():
            row = db.execute(
                "SELECT baseline_ema FROM insula_signals WHERE signal_name = ? "
                "ORDER BY sampled_at DESC LIMIT 1", (name,),
            ).fetchone()
            old_ema = float(row[0]) if row and row[0] is not None else val
            new_ema = (1 - _EMA_ALPHA) * old_ema + _EMA_ALPHA * val
            deviation = val - new_ema
            db.execute(
                "INSERT INTO insula_signals (signal_name, raw_value, normalized_value, "
                "baseline_ema, deviation, source) VALUES (?, ?, ?, ?, ?, 'insula_sample')",
                (name, val, val, new_ema, deviation),
            )
        # Update aggregate state singleton
        label = _label_for(signals)
        urgency = max(
            signals.get("write_pressure", 0),
            signals.get("retrieval_strain", 0),
            signals.get("attention_load", 0),
            1.0 - signals.get("certainty", 0.5),
        )
        db.execute(
            "UPDATE insula_state SET write_pressure=?, retrieval_strain=?, "
            "consolidation_debt=?, embedding_health=?, attention_load=?, "
            "certainty=?, felt_state_label=?, urgency_score=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=1",
            (signals["write_pressure"], signals["retrieval_strain"],
             signals["consolidation_debt"], signals["embedding_health"],
             signals["attention_load"], signals["certainty"],
             label, urgency),
        )
        db.commit()
        return {"ok": True, "signals": signals, "felt_state_label": label,
                "urgency_score": urgency}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_insula_state(**kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        row = db.execute("SELECT * FROM insula_state WHERE id=1").fetchone()
        # Last-N samples per signal
        signals_recent = {}
        for s in _KNOWN_SIGNALS:
            r = db.execute(
                "SELECT raw_value, normalized_value, baseline_ema, deviation, sampled_at "
                "FROM insula_signals WHERE signal_name=? ORDER BY sampled_at DESC LIMIT 5",
                (s,),
            ).fetchall()
            signals_recent[s] = _rows(r)
        return {"ok": True, "state": dict(row) if row else None,
                "recent_signals": signals_recent}
    finally:
        db.close()


def tool_insula_subscribe(
    subsystem: str, signal_name: str, threshold: float,
    comparator: str = "gt", action_hint: str | None = None,
    enabled: bool = True, **kw: Any,
) -> dict[str, Any]:
    if comparator not in _VALID_COMPARATORS:
        return {"ok": False, "error": f"comparator must be one of {sorted(_VALID_COMPARATORS)}"}
    db = _db()
    try:
        db.execute(
            "INSERT INTO insula_subscribers (subsystem, signal_name, threshold, "
            "comparator, action_hint, enabled) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(subsystem, signal_name, action_hint) DO UPDATE SET "
            "threshold=excluded.threshold, comparator=excluded.comparator, "
            "enabled=excluded.enabled",
            (subsystem, signal_name, float(threshold), comparator,
             action_hint, 1 if enabled else 0),
        )
        db.commit()
        return {"ok": True, "subsystem": subsystem, "signal_name": signal_name}
    finally:
        db.close()


def tool_insula_check_triggers(**kw: Any) -> dict[str, Any]:
    """Evaluate current state against all subscribers; return fired triggers."""
    db = _db()
    try:
        state = db.execute("SELECT * FROM insula_state WHERE id=1").fetchone()
        if not state:
            return {"ok": True, "triggered": []}
        s = dict(state)
        subs = db.execute(
            "SELECT id, subsystem, signal_name, threshold, comparator, action_hint "
            "FROM insula_subscribers WHERE enabled=1"
        ).fetchall()
        fired = []
        for sub in subs:
            sid, subsystem, sig, threshold, cmp, hint = sub
            val = s.get(sig)
            if val is None:
                continue
            fire = False
            if cmp == "gt" and val > threshold:
                fire = True
            elif cmp == "lt" and val < threshold:
                fire = True
            elif cmp == "abs_gt" and abs(val) > threshold:
                fire = True
            if fire:
                fired.append({"subscriber_id": sid, "subsystem": subsystem,
                              "signal": sig, "current_value": val,
                              "threshold": threshold, "action_hint": hint})
                db.execute(
                    "UPDATE insula_subscribers SET last_fired_at = "
                    "strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?", (sid,),
                )
        db.commit()
        return {"ok": True, "triggered": fired,
                "felt_state_label": s.get("felt_state_label"),
                "urgency_score": s.get("urgency_score")}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(name="insula_sample",
         description="Sample all interoceptive signals (write_pressure, retrieval_strain, "
                     "consolidation_debt, embedding_health, attention_load, certainty), "
                     "update EMAs/deviations, refresh insula_state singleton.",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="insula_state",
         description="O(1) read of the current felt-state vector + last-N samples per signal.",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="insula_subscribe",
         description="Register a subsystem's interest in a signal crossing a threshold. "
                     "Action_hint declares what they'd like to be told (e.g. "
                     "'request_mode_consolidate').",
         inputSchema={"type": "object", "properties": {
             "subsystem": {"type": "string"},
             "signal_name": {"type": "string"},
             "threshold": {"type": "number"},
             "comparator": {"type": "string", "enum": sorted(_VALID_COMPARATORS), "default": "gt"},
             "action_hint": {"type": "string"},
             "enabled": {"type": "boolean", "default": True},
         }, "required": ["subsystem", "signal_name", "threshold"]}),
    Tool(name="insula_check_triggers",
         description="Evaluate current state against subscribers; return fired triggers.",
         inputSchema={"type": "object", "properties": {}}),
]
_INSULA_TOOLS = {"insula_sample": tool_insula_sample, "insula_state": tool_insula_state,
                 "insula_subscribe": tool_insula_subscribe,
                 "insula_check_triggers": tool_insula_check_triggers}
DISPATCH = {n: (lambda _f=f, **kw: _f(**kw)) for n, f in _INSULA_TOOLS.items()}


def register_tools(): return TOOLS, DISPATCH
