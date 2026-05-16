"""Basal ganglia Phase 2 hookpoints.

Two functions:
- `consult_for_dispatch(action_key, agent_id, arguments)` — called before
  every MCP tool dispatch. Looks up the bg_action; if registered, reads the
  striatal weights for the current context and emits a shadow decision row.
  Never raises; silent when no bg_action is registered for the tool name
  (early-exit, no DB write). Decision policy in Phase 2:
    * net_signal (w_go − w_nogo) > 0.5 → "approve"
    * net_signal < -0.5 → "block" (shadow only)
    * |net_signal| < 0.5 → "approve" (default conservative)
  Real enforcement is Phase 3.

- `broadcast_td_error(task_id, agent_id, outcome, utility=None)` — called
  from outcome_annotate. Converts outcome string ("success"/"failure"/...)
  to utility scalar, computes δ (without V(s) learning yet, δ ≈ utility),
  inserts a bg_td_events row. Eligibility trace updates are deferred to
  Phase 3.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any, Optional

from agentmemory.paths import get_db_path

logger = logging.getLogger(__name__)

# Utility mapping per outcome label (post-RPE conventions: rewards positive,
# punishments negative, neutral zero).
_OUTCOME_UTILITY = {
    "success": 1.0,
    "successful": 1.0,
    "completed": 1.0,
    "partial": 0.3,
    "neutral": 0.0,
    "unknown": 0.0,
    "blocked": -0.3,
    "failure": -1.0,
    "failed": -1.0,
    "error": -1.0,
    "hallucination": -1.0,
    "tool_misuse": -0.7,
    "context_loss": -0.7,
}


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection | None:
    try:
        path = db_path or str(get_db_path())
        return sqlite3.connect(path, timeout=2.0)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("bg_shadow: cannot open db: %s", exc)
        return None


def _context_hash(agent_id: str | None, arguments: dict[str, Any] | None) -> str:
    args = arguments or {}
    keys = sorted(
        k for k in args.keys()
        if k in {"project", "scope", "category", "loop", "agent_id"}
    )
    parts = [str(agent_id or "")]
    for k in keys:
        parts.append(f"{k}={args.get(k)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _arguments_hash(arguments: dict[str, Any] | None) -> str:
    try:
        body = json.dumps(arguments or {}, sort_keys=True, default=str)
    except Exception:
        body = str(arguments)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def consult_for_dispatch(
    *,
    action_key: str,
    agent_id: str | None,
    arguments: dict[str, Any] | None = None,
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Pre-dispatch shadow consult. Returns the decision dict if a bg_action
    was registered for this tool name; otherwise returns None (no-op, no DB
    write). Never raises.
    """
    if not action_key or not isinstance(action_key, str):
        return None
    # Guard: skip our own observability tools so we don't recurse / spam.
    if action_key.startswith("bg_") or action_key in {"stats", "health"}:
        return None

    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        # Look up bg_action by action_key — match the tool: prefix convention.
        action_lookup_key = action_key if action_key.startswith("tool:") else f"tool:{action_key}"
        try:
            row = conn.execute(
                """
                SELECT a.id, a.loop,
                       COALESCE(SUM(w.w_go), 0.0) AS sum_go,
                       COALESCE(SUM(w.w_nogo), 0.0) AS sum_nogo
                FROM bg_actions a
                LEFT JOIN bg_striatal_weights w ON w.action_id = a.id
                WHERE a.action_key = ?
                GROUP BY a.id
                LIMIT 1
                """,
                (action_lookup_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Migration 054 not applied yet
            return None

        if not row:
            return None  # not registered; no-op

        _action_id, loop, sum_go, sum_nogo = row
        net = float(sum_go) - float(sum_nogo)
        if net > 0.5:
            decision = "approve"
            reason = f"net Go signal {net:.2f} > 0.5"
        elif net < -0.5:
            decision = "block"
            reason = f"net NoGo signal {net:.2f} < -0.5"
        else:
            decision = "approve"
            reason = f"neutral signal {net:.2f}; default approve"

        try:
            conn.execute(
                """
                INSERT INTO bg_shadow_decisions (
                    agent_id, action_key, loop, decision, reason,
                    net_signal, w_go, w_nogo, context_hash, arguments_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    action_lookup_key,
                    loop,
                    decision,
                    reason,
                    net,
                    float(sum_go),
                    float(sum_nogo),
                    _context_hash(agent_id, arguments),
                    _arguments_hash(arguments),
                ),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            logger.debug("bg_shadow: shadow_decisions table missing: %s", exc)
            return None

        return {
            "action_key": action_lookup_key,
            "loop": loop,
            "decision": decision,
            "reason": reason,
            "net_signal": net,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def broadcast_td_error(
    *,
    task_id: str | None,
    agent_id: str | None,
    outcome: str | None = None,
    utility: float | None = None,
    v_current: float = 0.0,
    v_next: float = 0.0,
    gamma: float = 0.95,
    source: str = "outcome_annotate",
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Compute δ and broadcast onto the bg_td_events bus.

    If `utility` is None, it is derived from `outcome` via _OUTCOME_UTILITY.
    Returns the inserted row's id + delta on success, None on no-op/failure.
    Never raises.
    """
    if utility is None:
        if outcome is None:
            return None
        utility = _OUTCOME_UTILITY.get(str(outcome).lower().strip(), 0.0)
    try:
        utility_f = float(utility)
        gamma_f = float(gamma)
        v_c = float(v_current)
        v_n = float(v_next)
    except (TypeError, ValueError):
        return None

    delta = utility_f + gamma_f * v_n - v_c

    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        try:
            cursor = conn.execute(
                """
                INSERT INTO bg_td_events (
                    task_id, agent_id, utility, v_current, v_next, gamma,
                    delta, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, agent_id, utility_f, v_c, v_n, gamma_f, delta, source),
            )
            conn.commit()
            return {"event_id": cursor.lastrowid, "delta": delta, "utility": utility_f}
        except sqlite3.OperationalError as exc:
            logger.debug("bg_shadow: bg_td_events table missing: %s", exc)
            return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
