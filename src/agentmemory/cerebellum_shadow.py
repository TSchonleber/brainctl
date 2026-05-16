"""Cerebellum Phase 2 hookpoints — auto-wire predict/observe at dispatch.

This is the cerebellum's equivalent of bg_shadow.py: every MCP tool
dispatch automatically issues forward predictions BEFORE the call fires,
then measures actual latency + outcome AFTER the call returns and closes
the predict→observe loop. No agent code change required.

Two functions:
  consult_for_dispatch(action_key, agent_id, arguments) →
      {prediction_ids, started_at_ns, partner, predictions: {kind: value}}
  observe_dispatch(prediction_ids, started_at_ns, error=None) →
      closes all three predictions with the measured outcomes:
        success_probability  → 1.0 if no error else 0.0
        expected_latency_ms  → wall-time delta
        expected_outcome_class → 1.0 success / 0.0 failure (numeric for
          uniformity; full enum-prediction is Phase 3)

Partner mapping mirrors BG's loop→partner convention:
  motor → motor_partner, oculomotor → oculomotor_partner, etc.

Skip rules (early-exit, no DB write):
  - action_key in our own observability surface (cerebellum_*, bg_*, etc.)
  - bg_action lookup returns no match (tool isn't in the catalog yet)

Never raises. Silent on missing schema.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

from agentmemory.paths import get_db_path

logger = logging.getLogger(__name__)

_PREDICTION_KINDS = (
    "success_probability",
    "expected_latency_ms",
    "expected_outcome_class",
)

# Map a BG loop name (motor/oculomotor/dlpfc/lofc/acc) to a cerebellum
# partner. Same five channels, with "_partner" suffix.
_LOOP_TO_PARTNER = {
    "motor": "motor_partner",
    "oculomotor": "oculomotor_partner",
    "dlpfc": "dlpfc_partner",
    "lofc": "lofc_partner",
    "acc": "acc_partner",
}

# Skip the cerebellum entirely for these prefixes / names to avoid
# recursion and observability noise.
_SKIP_PREFIXES = ("cerebellum_", "bg_", "thalamus_")
_SKIP_EXACT = {"stats", "health", "telemetry", "lint", "validate"}


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection | None:
    try:
        path = db_path or str(get_db_path())
        return sqlite3.connect(path, timeout=2.0)
    except Exception as exc:  # pragma: no cover
        logger.debug("cerebellum_shadow: cannot open db: %s", exc)
        return None


def _should_skip(action_key: str) -> bool:
    if not action_key:
        return True
    if action_key in _SKIP_EXACT:
        return True
    return any(action_key.startswith(p) for p in _SKIP_PREFIXES)


def _resolve_partner(conn: sqlite3.Connection, action_key: str) -> str | None:
    """Look up which BG loop owns this tool, then translate to a cerebellum
    partner. Returns None if the tool isn't registered in bg_actions yet."""
    lookup_key = action_key if action_key.startswith("tool:") else f"tool:{action_key}"
    try:
        row = conn.execute(
            "SELECT loop FROM bg_actions WHERE action_key = ? LIMIT 1",
            (lookup_key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return _LOOP_TO_PARTNER.get(str(row[0]))


def _context_hash(agent_id: str | None, arguments: dict[str, Any] | None) -> str:
    import hashlib
    args = arguments or {}
    keys = sorted(
        k for k in args.keys()
        if k in {"project", "scope", "category", "agent_id", "query"}
    )
    parts = [str(agent_id or "")]
    for k in keys:
        parts.append(f"{k}={args.get(k)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def consult_for_dispatch(
    *,
    action_key: str,
    agent_id: str | None,
    arguments: dict[str, Any] | None = None,
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Pre-dispatch forward predictions. Returns a dict with prediction_ids +
    started_at_ns + partner + per-kind predicted values, or None if skipped.

    The caller MUST call observe_dispatch(...) with the returned dict after
    the dispatched tool returns, so the eligibility traces close.
    """
    if _should_skip(action_key):
        return None
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        partner = _resolve_partner(conn, action_key)
        if partner is None:
            return None  # tool not in bg_actions catalog → cerebellum skips

        ctx_hash = _context_hash(agent_id, arguments)
        predictions: dict[str, dict[str, Any]] = {}

        for kind in _PREDICTION_KINDS:
            # Ensure module exists (auto-register on first use)
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cerebellum_modules (partner, prediction_kind, description)
                    VALUES (?, ?, ?)
                    """,
                    (partner, kind, f"auto-registered via dispatch shadow consult"),
                )
            except sqlite3.OperationalError:
                return None

            # Read existing weight for this context
            module_row = conn.execute(
                "SELECT id FROM cerebellum_modules WHERE partner = ? AND prediction_kind = ?",
                (partner, kind),
            ).fetchone()
            if not module_row:
                continue
            module_id = int(module_row[0])
            weight_row = conn.execute(
                "SELECT weight, confidence FROM cerebellum_weights WHERE module_id = ? AND context_hash = ?",
                (module_id, ctx_hash),
            ).fetchone()
            predicted = float(weight_row[0]) if weight_row else 0.0
            confidence = float(weight_row[1]) if weight_row else 0.0

            # Log the prediction
            cursor = conn.execute(
                """
                INSERT INTO cerebellum_predictions (
                    module_id, context_hash, predicted_value, confidence
                )
                VALUES (?, ?, ?, ?)
                """,
                (module_id, ctx_hash, predicted, confidence),
            )
            prediction_id = cursor.lastrowid

            # Deposit eligibility trace
            conn.execute(
                """
                INSERT INTO cerebellum_traces (
                    module_id, context_hash, prediction_id, trace_strength,
                    decay_constant, expires_at
                )
                VALUES (?, ?, ?, 1.0, 0.95,
                        strftime('%Y-%m-%dT%H:%M:%S', 'now', '+3600 seconds'))
                """,
                (module_id, ctx_hash, prediction_id),
            )

            predictions[kind] = {
                "prediction_id": prediction_id,
                "predicted_value": predicted,
                "confidence": confidence,
            }

        conn.commit()
        return {
            "partner": partner,
            "context_hash": ctx_hash,
            "started_at_ns": time.monotonic_ns(),
            "predictions": predictions,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def observe_dispatch(
    consult_result: dict[str, Any] | None,
    *,
    error: BaseException | str | None = None,
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Close the predict→observe loop after a dispatched tool returns.

    Computes observed values:
      success_probability  → 1.0 if no error else 0.0
      expected_latency_ms  → (time.monotonic_ns - started_at_ns) / 1e6
      expected_outcome_class → 1.0 success / 0.0 failure
    Applies the supervised LTD update via tool_cerebellum_observe.
    """
    if not consult_result:
        return None
    predictions = consult_result.get("predictions") or {}
    if not predictions:
        return None
    started_ns = int(consult_result.get("started_at_ns") or 0)
    elapsed_ms = (time.monotonic_ns() - started_ns) / 1e6 if started_ns else 0.0
    success = 1.0 if error is None else 0.0

    observed = {
        "success_probability": success,
        "expected_latency_ms": float(elapsed_ms),
        "expected_outcome_class": success,
    }

    # Local import keeps the hot path light when cerebellum schema missing.
    try:
        from agentmemory.mcp_tools_cerebellum import tool_cerebellum_observe
    except Exception:
        return None

    closed: dict[str, Any] = {}
    for kind, pred in predictions.items():
        pid = pred.get("prediction_id")
        if pid is None or kind not in observed:
            continue
        try:
            result = tool_cerebellum_observe(prediction_id=int(pid), observed_value=observed[kind])
            if result.get("ok"):
                closed[kind] = {
                    "delta_forward": result.get("delta_forward"),
                    "weight_after": result.get("weight_after"),
                    "boundary_id": result.get("boundary_id"),
                }
        except Exception as exc:
            logger.debug("cerebellum_shadow.observe: %s", exc)
    return {"partner": consult_result.get("partner"), "closed": closed, "elapsed_ms": elapsed_ms}
