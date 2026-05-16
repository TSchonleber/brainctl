"""brainctl MCP tools — anterior cingulate cortex (ACC).

In-flight conflict / error monitor. Watches LIVE operations for:
  - co-activation conflict (Botvinick): peers writing to same scope within window
  - prediction surprise (Brown/PRO): observed conflict vs learned rate
  - EVC (Shenhav): expected value of firing a control signal

Phase 1 is audit-only: provides `acc_evaluate` (synchronous in-flight scoring)
+ `acc_status` (inspection) + `acc_predict` (learned rate read) +
`acc_resolve` (outcome-driven update). Phase 2 wires `acc_evaluate` into
memory_add / belief_set / entity_observe / workspace_broadcast.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH = get_db_path()
VALID_OPS = {"memory_add", "belief_set", "entity_observe", "supersede", "workspace_broadcast"}
_INFLIGHT_TTL_SECONDS = 5
_HOLD_COST = 0.15


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def tool_acc_evaluate(
    op_kind: str,
    op_scope: str,
    agent_id: str | None = None,
    op_hash: str | None = None,
    intent_payload: dict[str, Any] | None = None,
    fire_hold: bool = False,
    **kw: Any,
) -> dict[str, Any]:
    """Score in-flight conflict / surprise / EVC. Returns the decision but
    doesn't necessarily act — caller decides whether to honor `fire_hold`.
    """
    if op_kind not in VALID_OPS:
        return {"ok": False, "error": f"op_kind must be one of {sorted(VALID_OPS)}"}
    if not op_scope:
        return {"ok": False, "error": "op_scope is required"}

    db = _db()
    try:
        # Register in-flight + check peers in the co-activation window.
        cur = db.execute(
            """
            INSERT INTO acc_inflight
              (expires_at, agent_id, op_kind, op_scope, op_hash, intent_payload)
            VALUES (strftime('%Y-%m-%dT%H:%M:%S','now','+' || ? || ' seconds'),
                    ?, ?, ?, ?, ?)
            """,
            (_INFLIGHT_TTL_SECONDS, agent_id, op_kind, op_scope, op_hash,
             json.dumps(intent_payload) if intent_payload else None),
        )
        inflight_id = cur.lastrowid
        peers = db.execute(
            """
            SELECT COUNT(*) FROM acc_inflight
            WHERE op_scope = ? AND id != ?
              AND expires_at > strftime('%Y-%m-%dT%H:%M:%S','now')
            """,
            (op_scope, inflight_id),
        ).fetchone()[0]
        conflict_score = min(1.0, float(peers) * 0.5)

        # Surprise: |actual - p_conflict| (learned)
        pred = db.execute(
            "SELECT p_conflict, volatility FROM acc_predictions WHERE op_kind = ? AND op_scope = ?",
            (op_kind, op_scope),
        ).fetchone()
        p_conf = float(pred[0]) if pred else 0.5
        volatility = float(pred[1]) if pred else 0.5
        surprise = abs(conflict_score - p_conf)
        evc = conflict_score * (0.5 + 0.5 * volatility) - _HOLD_COST

        action = "log"
        if evc > 0.5:
            action = "hold_fired" if fire_hold else "warn"

        # Update learned rate (RVPM single-step)
        was_conflict = 1 if conflict_score > 0.3 else 0
        if pred:
            n_trials = db.execute(
                "SELECT n_trials FROM acc_predictions WHERE op_kind=? AND op_scope=?",
                (op_kind, op_scope),
            ).fetchone()[0]
            new_n = int(n_trials) + 1
            new_conflicts = int(was_conflict) + (
                db.execute(
                    "SELECT n_conflicts FROM acc_predictions WHERE op_kind=? AND op_scope=?",
                    (op_kind, op_scope),
                ).fetchone()[0]
            )
            new_p = (new_conflicts + 0.5) / (new_n + 1.0)
            new_vol = 0.9 * volatility + 0.1 * abs(was_conflict - p_conf)
            db.execute(
                "UPDATE acc_predictions SET n_trials=?, n_conflicts=?, p_conflict=?, "
                "volatility=?, updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE op_kind=? AND op_scope=?",
                (new_n, new_conflicts, new_p, new_vol, op_kind, op_scope),
            )
        else:
            db.execute(
                "INSERT INTO acc_predictions (op_kind, op_scope, n_trials, n_conflicts, p_conflict, volatility) "
                "VALUES (?, ?, 1, ?, ?, 0.5)",
                (op_kind, op_scope, was_conflict, (was_conflict + 0.5) / 2.0),
            )

        fired_hold_id = None
        if action == "hold_fired":
            try:
                from agentmemory.bg_shadow import fire_hold as _fire_hold
                h = _fire_hold(loop="acc", reason="conflict", trigger_score_gap=evc)
                if h:
                    fired_hold_id = h.get("id")
            except Exception:
                pass

        db.execute(
            """
            INSERT INTO acc_events
              (agent_id, op_kind, op_scope, conflict_score, surprise_score,
               evc_score, action, fired_hold_id, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, op_kind, op_scope, conflict_score, surprise, evc,
             action, fired_hold_id,
             json.dumps({"peers": peers, "p_conflict": p_conf, "volatility": volatility})),
        )
        db.commit()
        return {
            "ok": True, "op_kind": op_kind, "op_scope": op_scope,
            "conflict_score": round(conflict_score, 4),
            "surprise_score": round(surprise, 4),
            "evc_score": round(evc, 4),
            "action": action, "fired_hold_id": fired_hold_id,
            "peers": peers,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_acc_status(
    days: int = 1, top_n: int = 10, **kw: Any,
) -> dict[str, Any]:
    db = _db()
    try:
        days_int = max(1, int(days))
        top_n_int = max(1, min(int(top_n), 50))
        by_action = db.execute(
            f"SELECT action, COUNT(*) AS n FROM acc_events "
            f"WHERE occurred_at >= datetime('now','-{days_int} days') "
            f"GROUP BY action",  # nosec B608
        ).fetchall()
        top_scopes = db.execute(
            f"SELECT op_scope, COUNT(*) AS n, ROUND(AVG(conflict_score),4) AS mean_conflict "
            f"FROM acc_events WHERE occurred_at >= datetime('now','-{days_int} days') "
            f"GROUP BY op_scope ORDER BY n DESC LIMIT ?",  # nosec B608
            (top_n_int,),
        ).fetchall()
        recent = db.execute(
            f"SELECT * FROM acc_events ORDER BY occurred_at DESC LIMIT ?",  # nosec B608
            (top_n_int,),
        ).fetchall()
        inflight = db.execute(
            "SELECT COUNT(*) FROM acc_inflight WHERE expires_at > strftime('%Y-%m-%dT%H:%M:%S','now')"
        ).fetchone()[0]
        return {
            "ok": True, "window_days": days_int,
            "active_inflight": inflight,
            "by_action": _rows(by_action),
            "top_scopes": _rows(top_scopes),
            "recent_events": _rows(recent),
        }
    finally:
        db.close()


def tool_acc_predict(op_kind: str, op_scope: str, **kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        row = db.execute(
            "SELECT n_trials, n_conflicts, p_conflict, volatility, updated_at "
            "FROM acc_predictions WHERE op_kind=? AND op_scope=?",
            (op_kind, op_scope),
        ).fetchone()
        if not row:
            return {"ok": True, "op_kind": op_kind, "op_scope": op_scope, "exists": False, "p_conflict": 0.5}
        return {"ok": True, "op_kind": op_kind, "op_scope": op_scope, "exists": True,
                "n_trials": row[0], "n_conflicts": row[1],
                "p_conflict": float(row[2]), "volatility": float(row[3]),
                "updated_at": row[4]}
    finally:
        db.close()


def tool_acc_resolve(event_id: int, outcome: str, **kw: Any) -> dict[str, Any]:
    """Mark an acc_events row with realized outcome (win/loss/nochange).
    Updates the predictions table — the RVPM learning loop."""
    if outcome not in {"win", "loss", "nochange"}:
        return {"ok": False, "error": "outcome must be win|loss|nochange"}
    db = _db()
    try:
        ev = db.execute(
            "SELECT op_kind, op_scope FROM acc_events WHERE id=?",
            (int(event_id),),
        ).fetchone()
        if not ev:
            return {"ok": False, "error": f"event {event_id} not found"}
        # Loss = the predicted conflict was right (or we had a hold that prevented harm).
        # Win = the operation was fine despite/without conflict signal.
        was_conflict = 1 if outcome == "loss" else 0
        db.execute(
            "UPDATE acc_predictions SET n_trials = n_trials + 1, "
            "n_conflicts = n_conflicts + ?, "
            "p_conflict = (CAST(n_conflicts + ? AS REAL) + 0.5) / (CAST(n_trials AS REAL) + 1.5) "
            "WHERE op_kind=? AND op_scope=?",
            (was_conflict, was_conflict, ev[0], ev[1]),
        )
        db.execute(
            "UPDATE acc_events SET detail = COALESCE(detail,'{}') WHERE id=?",
            (int(event_id),),
        )
        db.commit()
        return {"ok": True, "event_id": int(event_id), "outcome": outcome}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(name="acc_evaluate",
         description="In-flight conflict + surprise + EVC scoring for a write op. "
                     "Registers in acc_inflight (5s TTL), detects peer ops in scope, "
                     "computes scores, logs to acc_events, optionally fires a BG hold.",
         inputSchema={"type": "object", "properties": {
             "op_kind": {"type": "string", "enum": sorted(VALID_OPS)},
             "op_scope": {"type": "string"},
             "agent_id": {"type": "string"},
             "op_hash": {"type": "string"},
             "intent_payload": {"type": "object"},
             "fire_hold": {"type": "boolean", "default": False},
         }, "required": ["op_kind", "op_scope"]}),
    Tool(name="acc_status",
         description="Snapshot of recent ACC events, active in-flight ops, top-conflict scopes.",
         inputSchema={"type": "object", "properties": {
             "days": {"type": "integer", "default": 1},
             "top_n": {"type": "integer", "default": 10},
         }}),
    Tool(name="acc_predict",
         description="Read learned conflict rate + volatility for an (op_kind, op_scope) pair.",
         inputSchema={"type": "object", "properties": {
             "op_kind": {"type": "string"}, "op_scope": {"type": "string"},
         }, "required": ["op_kind", "op_scope"]}),
    Tool(name="acc_resolve",
         description="Mark an ACC event with realized outcome (win/loss/nochange). Updates predictions.",
         inputSchema={"type": "object", "properties": {
             "event_id": {"type": "integer"},
             "outcome": {"type": "string", "enum": ["win", "loss", "nochange"]},
         }, "required": ["event_id", "outcome"]}),
]
_ACC_TOOLS = {"acc_evaluate": tool_acc_evaluate, "acc_status": tool_acc_status,
              "acc_predict": tool_acc_predict, "acc_resolve": tool_acc_resolve}
DISPATCH = {n: (lambda _f=f, **kw: _f(**kw)) for n, f in _ACC_TOOLS.items()}


def register_tools(): return TOOLS, DISPATCH
