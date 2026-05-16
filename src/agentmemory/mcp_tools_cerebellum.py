"""brainctl MCP tools — cerebellum forward-model inspection and learning loop.

Phase 1 of the cerebellum subsystem per docs/proposals/cerebellum.md.
Read + minimal idempotent writes. The full predict-observe loop is exposed
here so callers can use it directly; Phase 2 will wire it into the dispatch
shadow consult automatically.

Five cortical partners (mirror BG's five loops): motor, oculomotor, dlpfc,
lofc, acc. Three prediction kinds: success_probability, expected_latency_ms,
expected_outcome_class.

Each (partner, prediction_kind) is a module. The "model" is a sparse linear
readout over hashed context features (Marr-Albus granule-cell expansion).
Learning rule is supervised LTD: weight -= lr × eligibility × δ_forward.
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

VALID_PARTNERS = {
    "motor_partner", "oculomotor_partner", "dlpfc_partner",
    "lofc_partner", "acc_partner",
}
VALID_PREDICTION_KINDS = {
    "success_probability", "expected_latency_ms", "expected_outcome_class",
}

_DEFAULT_LEARNING_RATE = 0.05
_DEFAULT_TRACE_DECAY = 0.95
_TRACE_TTL_SECONDS = 3600
_BOUNDARY_THRESHOLD = 0.5
_CONFIDENCE_EMA_ALPHA = 0.1


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows_to_list(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        t for t in (
            "cerebellum_modules", "cerebellum_weights", "cerebellum_predictions",
            "cerebellum_traces", "cerebellum_boundaries",
        )
        if not _table_exists(conn, t)
    ]
    if missing:
        return "cerebellum schema missing tables: " + ", ".join(missing)
    return None


def _context_hash(context: Any) -> str:
    """Stable, short context hash. Accepts dict or string."""
    if isinstance(context, dict):
        body = "|".join(f"{k}={context.get(k)}" for k in sorted(context.keys()))
    else:
        body = str(context)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def tool_cerebellum_status(
    partner: str | None = None,
    top_n: int = 10,
    **kw: Any,
) -> dict[str, Any]:
    """Snapshot of forward-model state.

    Returns per-module stats (n_predictions, mean_abs_error), top weights by
    |weight|, recent prediction log, pending un-observed predictions, recent
    boundary-marker events.
    """
    if partner and partner not in VALID_PARTNERS:
        return {"ok": False, "error": f"partner must be one of {sorted(VALID_PARTNERS)}"}
    try:
        top_n_int = max(1, min(int(top_n or 10), 50))
    except (TypeError, ValueError):
        return {"ok": False, "error": "top_n must be an integer"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        where_partner = "WHERE partner = ?" if partner else ""
        partner_params: list[Any] = [partner] if partner else []
        modules = db.execute(
            f"""
            SELECT id, partner, prediction_kind, description,
                   n_predictions, mean_abs_error, created_at
            FROM cerebellum_modules
            {where_partner}
            ORDER BY partner, prediction_kind
            """,  # nosec B608
            partner_params,
        ).fetchall()

        top_weights = db.execute(
            f"""
            SELECT m.partner, m.prediction_kind,
                   w.context_hash, ROUND(w.weight, 4) AS weight,
                   ROUND(w.confidence, 4) AS confidence, w.n_updates
            FROM cerebellum_weights w
            JOIN cerebellum_modules m ON m.id = w.module_id
            {where_partner.replace('partner', 'm.partner') if where_partner else ''}
            ORDER BY ABS(w.weight) DESC, w.n_updates DESC
            LIMIT ?
            """,  # nosec B608
            partner_params + [top_n_int],
        ).fetchall()

        recent = db.execute(
            f"""
            SELECT p.id, m.partner, m.prediction_kind,
                   ROUND(p.predicted_value, 4) AS predicted,
                   p.observed_value, ROUND(p.delta_forward, 4) AS delta_forward,
                   ROUND(p.confidence, 4) AS confidence, p.fired_at, p.observed_at
            FROM cerebellum_predictions p
            JOIN cerebellum_modules m ON m.id = p.module_id
            {where_partner.replace('partner', 'm.partner') if where_partner else ''}
            ORDER BY p.fired_at DESC
            LIMIT ?
            """,  # nosec B608
            partner_params + [top_n_int],
        ).fetchall()

        pending = db.execute(
            f"""
            SELECT COUNT(*) FROM cerebellum_predictions p
            JOIN cerebellum_modules m ON m.id = p.module_id
            {where_partner.replace('partner', 'm.partner') if where_partner else 'WHERE 1=1'}
            AND p.observed_at IS NULL
            """,  # nosec B608
            partner_params,
        ).fetchone()[0]

        boundaries = db.execute(
            f"""
            SELECT id, partner, ROUND(delta_forward, 4) AS delta_forward,
                   ROUND(salience, 4) AS salience, fired_at, consumed_at
            FROM cerebellum_boundaries
            {('WHERE partner = ?' if partner else '')}
            ORDER BY fired_at DESC
            LIMIT ?
            """,  # nosec B608
            partner_params + [top_n_int],
        ).fetchall()

        trace_count = db.execute(
            "SELECT COUNT(*) FROM cerebellum_traces "
            "WHERE expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now')"
        ).fetchone()[0]

        return {
            "ok": True,
            "partner_filter": partner,
            "modules": _rows_to_list(modules),
            "top_weights": _rows_to_list(top_weights),
            "recent_predictions": _rows_to_list(recent),
            "pending_observations": pending,
            "active_traces": trace_count,
            "recent_boundaries": _rows_to_list(boundaries),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_cerebellum_module_register(
    partner: str,
    prediction_kind: str,
    description: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Idempotent UPSERT into cerebellum_modules."""
    if partner not in VALID_PARTNERS:
        return {"ok": False, "error": f"partner must be one of {sorted(VALID_PARTNERS)}"}
    if prediction_kind not in VALID_PREDICTION_KINDS:
        return {"ok": False, "error": f"prediction_kind must be one of {sorted(VALID_PREDICTION_KINDS)}"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        db.execute(
            """
            INSERT INTO cerebellum_modules (partner, prediction_kind, description)
            VALUES (?, ?, ?)
            ON CONFLICT(partner, prediction_kind) DO UPDATE SET
                description = COALESCE(excluded.description, cerebellum_modules.description)
            """,
            (partner, prediction_kind, description),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM cerebellum_modules WHERE partner = ? AND prediction_kind = ?",
            (partner, prediction_kind),
        ).fetchone()
        return {"ok": True, "module": dict(row) if row else None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_cerebellum_predict(
    partner: str,
    prediction_kind: str,
    context: Any,
    agent_id: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Compute a forward prediction. Returns {predicted_value, confidence,
    prediction_id}. Logs to cerebellum_predictions + deposits an eligibility
    trace. Never blocks the caller — returns confidence=0 if the module is
    not yet registered.
    """
    if partner not in VALID_PARTNERS:
        return {"ok": False, "error": f"partner must be one of {sorted(VALID_PARTNERS)}"}
    if prediction_kind not in VALID_PREDICTION_KINDS:
        return {"ok": False, "error": f"prediction_kind must be one of {sorted(VALID_PREDICTION_KINDS)}"}
    if context is None:
        return {"ok": False, "error": "context is required"}

    ctx_hash = _context_hash(context)
    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        module_row = db.execute(
            "SELECT id FROM cerebellum_modules WHERE partner = ? AND prediction_kind = ?",
            (partner, prediction_kind),
        ).fetchone()
        if not module_row:
            # Auto-register on first predict (idempotent)
            db.execute(
                "INSERT INTO cerebellum_modules (partner, prediction_kind, description) VALUES (?, ?, ?)",
                (partner, prediction_kind, "auto-registered on first predict"),
            )
            db.commit()
            module_row = db.execute(
                "SELECT id FROM cerebellum_modules WHERE partner = ? AND prediction_kind = ?",
                (partner, prediction_kind),
            ).fetchone()
        module_id = int(module_row[0])

        weight_row = db.execute(
            "SELECT weight, confidence, n_updates FROM cerebellum_weights WHERE module_id = ? AND context_hash = ?",
            (module_id, ctx_hash),
        ).fetchone()
        if weight_row:
            predicted = float(weight_row[0])
            confidence = float(weight_row[1])
        else:
            predicted = 0.0
            confidence = 0.0

        cursor = db.execute(
            """
            INSERT INTO cerebellum_predictions (
                module_id, context_hash, predicted_value, confidence
            )
            VALUES (?, ?, ?, ?)
            """,
            (module_id, ctx_hash, predicted, confidence),
        )
        prediction_id = cursor.lastrowid

        db.execute(
            """
            INSERT INTO cerebellum_traces (
                module_id, context_hash, prediction_id, trace_strength,
                decay_constant, expires_at
            )
            VALUES (?, ?, ?, 1.0, ?,
                    strftime('%Y-%m-%dT%H:%M:%S', 'now', '+' || ? || ' seconds'))
            """,
            (module_id, ctx_hash, prediction_id, _DEFAULT_TRACE_DECAY, _TRACE_TTL_SECONDS),
        )
        db.commit()

        return {
            "ok": True,
            "prediction_id": prediction_id,
            "partner": partner,
            "prediction_kind": prediction_kind,
            "context_hash": ctx_hash,
            "predicted_value": predicted,
            "confidence": confidence,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_cerebellum_observe(
    prediction_id: int,
    observed_value: float,
    **kw: Any,
) -> dict[str, Any]:
    """Close the forward-model loop. Computes δ_forward = observed -
    predicted, updates the weight via supervised LTD (weight -= lr × trace ×
    δ_forward), decays the eligibility trace, fires a boundary marker if
    |δ_forward| exceeds threshold, and broadcasts onto the BG TD-error bus
    as a forward-error supplement.
    """
    try:
        pid = int(prediction_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "prediction_id must be an integer"}
    try:
        observed = float(observed_value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "observed_value must be numeric"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        pred = db.execute(
            """
            SELECT p.id, p.module_id, p.context_hash, p.predicted_value,
                   p.observed_at, m.partner, m.prediction_kind
            FROM cerebellum_predictions p
            JOIN cerebellum_modules m ON m.id = p.module_id
            WHERE p.id = ?
            """,
            (pid,),
        ).fetchone()
        if not pred:
            return {"ok": False, "error": f"prediction {pid} not found"}
        if pred[4] is not None:
            return {"ok": False, "error": f"prediction {pid} already observed"}
        module_id = int(pred[1])
        ctx_hash = str(pred[2])
        predicted = float(pred[3])
        partner = str(pred[5])

        delta_forward = observed - predicted

        # Find the eligibility trace and supervised update
        trace = db.execute(
            """
            SELECT id, trace_strength, decay_constant FROM cerebellum_traces
            WHERE prediction_id = ? LIMIT 1
            """,
            (pid,),
        ).fetchone()
        trace_strength = float(trace[1]) if trace else 1.0
        decay = float(trace[2]) if trace else _DEFAULT_TRACE_DECAY

        # Ensure the weight row exists
        db.execute(
            "INSERT OR IGNORE INTO cerebellum_weights (module_id, context_hash) VALUES (?, ?)",
            (module_id, ctx_hash),
        )
        w_row = db.execute(
            "SELECT weight, confidence FROM cerebellum_weights WHERE module_id = ? AND context_hash = ?",
            (module_id, ctx_hash),
        ).fetchone()
        old_weight = float(w_row[0])
        old_conf = float(w_row[1])

        # Marr-Albus LTD: weight moves OPPOSITE to delta (over-prediction → shrink).
        # In additive form: new_weight = old_weight + lr × trace × δ
        # so the weight tracks the target value via PE-driven gradient descent.
        new_weight = old_weight + _DEFAULT_LEARNING_RATE * trace_strength * delta_forward
        # Confidence is 1 - EMA(|δ_forward|), bounded to [0, 1]
        new_conf = _clamp(
            (1 - _CONFIDENCE_EMA_ALPHA) * old_conf + _CONFIDENCE_EMA_ALPHA * (1.0 - min(1.0, abs(delta_forward))),
            default=0.0,
        )

        db.execute(
            """
            UPDATE cerebellum_weights
            SET weight = ?, confidence = ?, n_updates = n_updates + 1,
                last_updated = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            WHERE module_id = ? AND context_hash = ?
            """,
            (new_weight, new_conf, module_id, ctx_hash),
        )

        # Close the prediction row
        db.execute(
            """
            UPDATE cerebellum_predictions
            SET observed_value = ?, observed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                delta_forward = ?
            WHERE id = ?
            """,
            (observed, delta_forward, pid),
        )

        # Decay the trace
        if trace:
            db.execute(
                "UPDATE cerebellum_traces SET trace_strength = trace_strength * ? WHERE id = ?",
                (decay, int(trace[0])),
            )

        # Update module rollup stats
        db.execute(
            """
            UPDATE cerebellum_modules
            SET n_predictions = n_predictions + 1,
                mean_abs_error = ((mean_abs_error * n_predictions) + ?) / (n_predictions + 1)
            WHERE id = ?
            """,
            (abs(delta_forward), module_id),
        )

        # Boundary marker (complex-spike analog) on large |delta_forward|
        boundary_id: int | None = None
        if abs(delta_forward) >= _BOUNDARY_THRESHOLD:
            cur = db.execute(
                """
                INSERT INTO cerebellum_boundaries (
                    partner, delta_forward, context_hash, prediction_id, salience
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (partner, delta_forward, ctx_hash, pid, min(1.0, abs(delta_forward))),
            )
            boundary_id = cur.lastrowid

        db.commit()

        # Broadcast onto BG TD-error bus as δ_forward supplement. Never raises.
        td_event = None
        try:
            from agentmemory.bg_shadow import broadcast_td_error
            td_event = broadcast_td_error(
                task_id=f"cerebellum:{pid}",
                agent_id=None,
                utility=delta_forward,
                source="cerebellum_observe",
            )
        except Exception:
            pass

        return {
            "ok": True,
            "prediction_id": pid,
            "partner": partner,
            "predicted_value": predicted,
            "observed_value": observed,
            "delta_forward": delta_forward,
            "weight_before": old_weight,
            "weight_after": new_weight,
            "confidence": new_conf,
            "boundary_id": boundary_id,
            "bg_td_event": td_event,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(
        name="cerebellum_status",
        description=(
            "Inspect the cerebellum subsystem: per-module stats (n_predictions, "
            "mean_abs_error), top learned weights, recent predictions, pending "
            "observations, recent boundary-marker events. Optional partner filter."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partner": {"type": "string", "enum": sorted(VALID_PARTNERS)},
                "top_n": {"type": "integer", "default": 10},
            },
        },
    ),
    Tool(
        name="cerebellum_module_register",
        description=(
            "Idempotent UPSERT of a (partner, prediction_kind) forward-model module. "
            "Auto-fires from cerebellum_predict if not pre-registered."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partner": {"type": "string", "enum": sorted(VALID_PARTNERS)},
                "prediction_kind": {"type": "string", "enum": sorted(VALID_PREDICTION_KINDS)},
                "description": {"type": "string"},
            },
            "required": ["partner", "prediction_kind"],
        },
    ),
    Tool(
        name="cerebellum_predict",
        description=(
            "Issue a forward prediction for a context. Returns predicted_value, "
            "confidence, and prediction_id. Deposits an eligibility trace so a "
            "later cerebellum_observe(prediction_id, observed) can close the loop. "
            "Never blocks: returns confidence=0 if the model has no learned weights "
            "for this context yet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partner": {"type": "string", "enum": sorted(VALID_PARTNERS)},
                "prediction_kind": {"type": "string", "enum": sorted(VALID_PREDICTION_KINDS)},
                "context": {
                    "description": "Dict or string describing the context. Stable hash → expansion key.",
                },
                "agent_id": {"type": "string"},
            },
            "required": ["partner", "prediction_kind", "context"],
        },
    ),
    Tool(
        name="cerebellum_observe",
        description=(
            "Close the forward-model loop. Computes δ_forward = observed − "
            "predicted, applies supervised LTD update to the weight, decays the "
            "eligibility trace, fires a boundary marker if |δ_forward| ≥ 0.5, "
            "and broadcasts the δ_forward onto the BG TD-error bus."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prediction_id": {"type": "integer"},
                "observed_value": {"type": "number"},
            },
            "required": ["prediction_id", "observed_value"],
        },
    ),
]

_CEREBELLUM_TOOLS = {
    "cerebellum_status": tool_cerebellum_status,
    "cerebellum_module_register": tool_cerebellum_module_register,
    "cerebellum_predict": tool_cerebellum_predict,
    "cerebellum_observe": tool_cerebellum_observe,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _CEREBELLUM_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
