"""Basal ganglia Phase 2 + 3 hookpoints.

Phase 2 (shadow-only logging):
- `consult_for_dispatch(action_key, agent_id, arguments)` — called before
  every MCP tool dispatch. Looks up the bg_action; if registered, reads the
  striatal weights for the current context and emits a shadow decision row.
  Never raises; silent when no bg_action is registered for the tool name
  (early-exit, no DB write).

- `broadcast_td_error(task_id, agent_id, outcome, utility=None)` — called
  from outcome_annotate. Converts outcome string to utility scalar, computes
  δ = utility + γ·V(s') − V(s), inserts a bg_td_events row.

Phase 3 (closing the actor-critic loop — the BG now LEARNS from outcomes):
- `consult_for_dispatch` additionally **deposits an eligibility trace** in
  bg_eligibility_traces tagged with (action_id, context_hash). Strength
  starts at 1.0 and decays each time it's consumed.
- `broadcast_td_error` additionally **consumes active eligibility traces**
  and updates bg_striatal_weights via the opponent Go/NoGo three-factor
  learning rule:
    if δ > 0: w_go  += lr · trace · δ      (D1 LTP)
              w_nogo -= lr · trace · δ/2    (D2 LTD, weaker)
    if δ < 0: w_nogo += lr · trace · |δ|   (D2 LTP)
              w_go  -= lr · trace · |δ|/2  (D1 LTD, weaker)
  Weights clamped to [0, 1]. After consumption, trace strength is decayed
  by its decay_constant (default 0.95); traces with strength < 0.05 or
  older than 1 hour are pruned by `sweep_eligibility_traces`.

Dispatch enforcement remains shadow-only — weights move from real outcomes,
but the gate doesn't act on them yet. That flip is Phase 4.
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


_DEFAULT_LEARNING_RATE = 0.1
_DEFAULT_TRACE_DECAY = 0.95
_TRACE_PRUNE_STRENGTH = 0.05
_TRACE_TTL_SECONDS = 3600  # 1 hour

# Hyperdirect-pathway hold thresholds.
_HOLD_SURPRISE_DELTA = 0.8
_HOLD_CONFLICT_WINDOW_SECONDS = 5

# Perf cache: (db_path, action_key) → (timestamp, (action_id, loop) | None).
# None means "definitely not registered as of timestamp." 60s TTL.
_ACTION_CACHE: dict[tuple[str, str], tuple[float, tuple[int, str] | None]] = {}
_ACTION_TTL_SECONDS = 60.0


def _resolve_action_cached(action_key: str, db_path_str: str) -> tuple[int, str] | None:
    """Cache-first lookup. Returns (action_id, loop) or None. Opens a DB
    connection only on cache miss / TTL expiry.
    """
    import time as _time
    lookup_key = action_key if action_key.startswith("tool:") else f"tool:{action_key}"
    cache_key = (db_path_str, lookup_key)
    now = _time.monotonic()
    entry = _ACTION_CACHE.get(cache_key)
    if entry is not None and (now - entry[0]) < _ACTION_TTL_SECONDS:
        return entry[1]

    conn = _connect(db_path_str)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT id, loop FROM bg_actions WHERE action_key = ? LIMIT 1",
                (lookup_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            _ACTION_CACHE[cache_key] = (now, None)
            return None
        value = (int(row[0]), str(row[1])) if row else None
        _ACTION_CACHE[cache_key] = (now, value)
        return value
    finally:
        try:
            conn.close()
        except Exception:
            pass


def clear_caches() -> None:
    """Test helper — clear module-level perf caches."""
    _ACTION_CACHE.clear()


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection | None:
    try:
        path = db_path or str(get_db_path())
        c = sqlite3.connect(path, timeout=2.0)
        # Shadow audit writes — NORMAL sync. See cerebellum_shadow for rationale.
        try:
            c.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        return c
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

    Performance-optimized path: cached action lookup avoids a DB open on
    unregistered tools; single transaction across shadow_decisions insert,
    eligibility_trace insert, and (optional) hold check.
    """
    if not action_key or not isinstance(action_key, str):
        return None
    if action_key.startswith("bg_") or action_key in {"stats", "health"}:
        return None

    db_path_str = db_path or str(get_db_path())
    cached = _resolve_action_cached(action_key, db_path_str)
    if cached is None:
        return None
    action_id, loop = cached
    action_lookup_key = action_key if action_key.startswith("tool:") else f"tool:{action_key}"

    conn = _connect(db_path_str)
    if conn is None:
        return None
    try:
        # Single transaction wrapping everything below.
        try:
            conn.execute("BEGIN")
        except sqlite3.OperationalError:
            return None

        # Fetch weight sums + recent surprise + recent conflict in one shot.
        try:
            sums = conn.execute(
                "SELECT COALESCE(SUM(w_go), 0.0), COALESCE(SUM(w_nogo), 0.0) "
                "FROM bg_striatal_weights WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            sum_go = float(sums[0]) if sums else 0.0
            sum_nogo = float(sums[1]) if sums else 0.0
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None

        net = sum_go - sum_nogo
        if net > 0.5:
            decision = "approve"
            reason = f"net Go signal {net:.2f} > 0.5"
        elif net < -0.5:
            decision = "block"
            reason = f"net NoGo signal {net:.2f} < -0.5"
        else:
            decision = "approve"
            reason = f"neutral signal {net:.2f}; default approve"

        ctx_hash = _context_hash(agent_id, arguments)
        args_hash = _arguments_hash(arguments)

        try:
            conn.execute(
                "INSERT INTO bg_shadow_decisions "
                "(agent_id, action_key, loop, decision, reason, net_signal, "
                " w_go, w_nogo, context_hash, arguments_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, action_lookup_key, loop, decision, reason,
                 net, sum_go, sum_nogo, ctx_hash, args_hash),
            )
        except sqlite3.OperationalError as exc:
            logger.debug("bg_shadow: shadow_decisions table missing: %s", exc)
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None

        # Eligibility trace (same transaction).
        try:
            conn.execute(
                "INSERT INTO bg_eligibility_traces "
                "(action_id, context_hash, trace_strength, decay_constant, expires_at) "
                "VALUES (?, ?, 1.0, ?, "
                "        strftime('%Y-%m-%dT%H:%M:%S', 'now', '+' || ? || ' seconds'))",
                (action_id, ctx_hash, _DEFAULT_TRACE_DECAY, _TRACE_TTL_SECONDS),
            )
        except sqlite3.OperationalError as exc:
            logger.debug("bg_shadow: bg_eligibility_traces missing: %s", exc)

        # Hyperdirect hold detection (same connection, same transaction).
        fired_hold = _maybe_fire_hold(
            conn, action_loop=str(loop),
            action_key=action_lookup_key, agent_id=agent_id,
        )
        active_holds = _check_active_holds(conn)

        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError:
            pass

        result: dict[str, Any] = {
            "action_key": action_lookup_key,
            "loop": loop,
            "decision": decision,
            "reason": reason,
            "net_signal": net,
            "context_hash": ctx_hash,
            "active_holds": active_holds,
        }
        if fired_hold is not None:
            result["fired_hold"] = fired_hold
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _apply_three_factor_update(
    conn: sqlite3.Connection,
    *,
    action_id: int,
    context_hash: str,
    trace_strength: float,
    delta: float,
    learning_rate: float = _DEFAULT_LEARNING_RATE,
) -> tuple[float, float]:
    """Apply the opponent Go/NoGo three-factor learning rule for one trace.

    Returns the (new_w_go, new_w_nogo) values after the update.
    """
    # Ensure the (action, context) row exists with default weights
    conn.execute(
        """
        INSERT OR IGNORE INTO bg_striatal_weights (action_id, context_hash)
        VALUES (?, ?)
        """,
        (action_id, context_hash),
    )
    row = conn.execute(
        "SELECT w_go, w_nogo FROM bg_striatal_weights WHERE action_id = ? AND context_hash = ?",
        (action_id, context_hash),
    ).fetchone()
    w_go = float(row[0])
    w_nogo = float(row[1])

    abs_delta = abs(delta)
    if delta > 0:
        d_go = learning_rate * trace_strength * delta
        d_nogo = -learning_rate * trace_strength * delta * 0.5
    else:
        d_go = -learning_rate * trace_strength * abs_delta * 0.5
        d_nogo = learning_rate * trace_strength * abs_delta

    new_w_go = max(0.0, min(1.0, w_go + d_go))
    new_w_nogo = max(0.0, min(1.0, w_nogo + d_nogo))

    conn.execute(
        """
        UPDATE bg_striatal_weights
        SET w_go = ?, w_nogo = ?, n_updates = n_updates + 1,
            last_updated = strftime('%Y-%m-%dT%H:%M:%S', 'now')
        WHERE action_id = ? AND context_hash = ?
        """,
        (new_w_go, new_w_nogo, action_id, context_hash),
    )
    return (new_w_go, new_w_nogo)


def _consume_eligibility_traces(
    conn: sqlite3.Connection,
    *,
    delta: float,
    learning_rate: float = _DEFAULT_LEARNING_RATE,
) -> dict[str, Any]:
    """Apply δ across all active traces, update striatal weights, decay
    traces. Returns a stats summary.
    """
    try:
        traces = conn.execute(
            """
            SELECT id, action_id, context_hash, trace_strength, decay_constant
            FROM bg_eligibility_traces
            WHERE (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now'))
              AND trace_strength >= ?
            """,
            (_TRACE_PRUNE_STRENGTH,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"traces_consumed": 0, "weight_updates": 0}

    updated = 0
    for trace_id, action_id, ctx, strength, decay in traces:
        _apply_three_factor_update(
            conn,
            action_id=int(action_id),
            context_hash=str(ctx),
            trace_strength=float(strength),
            delta=delta,
            learning_rate=learning_rate,
        )
        # Decay the trace for next consumption
        conn.execute(
            "UPDATE bg_eligibility_traces SET trace_strength = trace_strength * ? WHERE id = ?",
            (float(decay), trace_id),
        )
        updated += 1
    return {"traces_consumed": len(traces), "weight_updates": updated}


def _check_active_holds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return currently-active (un-released) holds. Safe on missing table."""
    try:
        rows = conn.execute(
            """
            SELECT id, loop, reason, trigger_score_gap, ticks, fired_at
            FROM bg_holds
            WHERE released_at IS NULL
              AND fired_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-60 seconds')
            ORDER BY fired_at DESC
            LIMIT 10
            """
        ).fetchall()
        return [
            {
                "id": r[0], "loop": r[1], "reason": r[2],
                "trigger_score_gap": r[3], "ticks": r[4], "fired_at": r[5],
            }
            for r in rows
        ]
    except sqlite3.OperationalError:
        return []


def _maybe_fire_hold(
    conn: sqlite3.Connection,
    *,
    action_loop: str,
    action_key: str,
    agent_id: str | None,
) -> dict[str, Any] | None:
    """Detect conflict and surprise triggers; fire a bg_holds row if either
    condition holds. Returns the fired hold descriptor or None.

    Triggers (Phase 3 detector):
      - SURPRISE: a bg_td_events row in the last 60s with |δ| > 0.8.
      - CONFLICT: a different loop dispatched a registered action for the
        same agent within the last _HOLD_CONFLICT_WINDOW_SECONDS.
    """
    fired: dict[str, Any] | None = None
    # SURPRISE
    try:
        row = conn.execute(
            """
            SELECT MAX(ABS(delta)) FROM bg_td_events
            WHERE fired_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-60 seconds')
            """
        ).fetchone()
        max_abs_delta = float(row[0]) if row and row[0] is not None else 0.0
    except sqlite3.OperationalError:
        max_abs_delta = 0.0

    if max_abs_delta > _HOLD_SURPRISE_DELTA:
        try:
            cur = conn.execute(
                """
                INSERT INTO bg_holds (loop, reason, trigger_score_gap, ticks)
                VALUES (?, 'surprise', ?, 1)
                """,
                (action_loop, max_abs_delta),
            )
            # Outer caller controls the transaction; do NOT commit here.
            fired = {
                "id": cur.lastrowid, "loop": action_loop, "reason": "surprise",
                "trigger_score_gap": max_abs_delta, "ticks": 1,
            }
            return fired
        except sqlite3.OperationalError:
            pass

    # CONFLICT: any other registered loop hit by the same agent recently?
    try:
        row = conn.execute(
            f"""
            SELECT loop, COUNT(*) FROM bg_shadow_decisions
            WHERE agent_id = ?
              AND loop != ?
              AND decision_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-{_HOLD_CONFLICT_WINDOW_SECONDS} seconds')
            GROUP BY loop
            ORDER BY 2 DESC
            LIMIT 1
            """,  # nosec B608 — interpolated int constant
            (agent_id or "", action_loop),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None

    if row:
        try:
            cur = conn.execute(
                """
                INSERT INTO bg_holds (loop, reason, trigger_score_gap, ticks)
                VALUES (?, 'conflict', ?, 1)
                """,
                (action_loop, float(row[1])),
            )
            # Outer caller controls the transaction; do NOT commit here.
            fired = {
                "id": cur.lastrowid, "loop": action_loop, "reason": "conflict",
                "trigger_score_gap": float(row[1]), "ticks": 1,
            }
        except sqlite3.OperationalError:
            pass
    return fired


def fire_hold(
    *,
    loop: str,
    reason: str = "explicit_stop",
    trigger_score_gap: float | None = None,
    ticks: int = 1,
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Manually fire a hyperdirect hold. Used by `tool_bg_hold_trigger` and
    can be called directly by callers that detect their own conflict signal.
    """
    if reason not in {"conflict", "surprise", "explicit_stop"}:
        return None
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        try:
            cur = conn.execute(
                """
                INSERT INTO bg_holds (loop, reason, trigger_score_gap, ticks)
                VALUES (?, ?, ?, ?)
                """,
                (loop, reason, trigger_score_gap, max(1, int(ticks))),
            )
            conn.commit()
            return {
                "id": cur.lastrowid, "loop": loop, "reason": reason,
                "trigger_score_gap": trigger_score_gap, "ticks": ticks,
            }
        except sqlite3.OperationalError:
            return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def release_hold(hold_id: int, db_path: Optional[str] = None) -> dict[str, Any] | None:
    """Mark a hold as released. Idempotent on already-released holds."""
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        try:
            cur = conn.execute(
                """
                UPDATE bg_holds
                SET released_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
                WHERE id = ? AND released_at IS NULL
                """,
                (int(hold_id),),
            )
            conn.commit()
            return {"id": hold_id, "released": cur.rowcount > 0}
        except sqlite3.OperationalError:
            return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sweep_eligibility_traces(db_path: Optional[str] = None) -> dict[str, Any]:
    """Remove expired or weak traces. Safe to call periodically (cron or
    after large δ broadcasts). Never raises.
    """
    conn = _connect(db_path)
    if conn is None:
        return {"ok": False, "error": "db unavailable"}
    try:
        try:
            cur = conn.execute(
                """
                DELETE FROM bg_eligibility_traces
                WHERE trace_strength < ?
                   OR (expires_at IS NOT NULL AND expires_at < strftime('%Y-%m-%dT%H:%M:%S', 'now'))
                """,
                (_TRACE_PRUNE_STRENGTH,),
            )
            conn.commit()
            removed = cur.rowcount or 0
            remaining = conn.execute(
                "SELECT COUNT(*) FROM bg_eligibility_traces"
            ).fetchone()[0]
            return {"ok": True, "removed": removed, "remaining": remaining}
        except sqlite3.OperationalError as exc:
            return {"ok": False, "error": str(exc)}
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
            event_id = cursor.lastrowid
        except sqlite3.OperationalError as exc:
            logger.debug("bg_shadow: bg_td_events table missing: %s", exc)
            return None

        # Phase 3: consume eligibility traces. Apply opponent Go/NoGo update
        # rule across all active (action, context) traces.
        trace_stats = _consume_eligibility_traces(conn, delta=delta)
        if trace_stats["weight_updates"] > 0:
            conn.execute(
                "UPDATE bg_td_events SET consumed_count = ? WHERE id = ?",
                (trace_stats["weight_updates"], event_id),
            )
        conn.commit()
        return {
            "event_id": event_id,
            "delta": delta,
            "utility": utility_f,
            "traces_consumed": trace_stats["traces_consumed"],
            "weight_updates": trace_stats["weight_updates"],
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
