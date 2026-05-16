"""Cerebellum Phase 2 hookpoints — auto-wire predict/observe at dispatch.

Performance pass (perf-shadow-consults branch):
- Module-level cached registered-action lookup (60s TTL) means unregistered
  tools fast-skip without opening a sqlite connection at all.
- Module-id cache avoids the SELECT-after-INSERT-OR-IGNORE round-trip per
  prediction kind.
- All three predictions are written in a single transaction (one commit
  instead of three).
- observe_dispatch writes all three weight updates + prediction closes +
  trace decays + module rollups + (optional) boundary markers + the BG
  TD-error broadcast in a single transaction (one commit instead of
  three independent tool_cerebellum_observe calls — each of which used to
  open its own connection).
"""
from __future__ import annotations

import hashlib
import logging
import math
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

_LOOP_TO_PARTNER = {
    "motor": "motor_partner",
    "oculomotor": "oculomotor_partner",
    "dlpfc": "dlpfc_partner",
    "lofc": "lofc_partner",
    "acc": "acc_partner",
}

_SKIP_PREFIXES = ("cerebellum_", "bg_", "thalamus_", "amygdala_")
_SKIP_EXACT = {"stats", "health", "telemetry", "lint", "validate"}

# Same learning constants as the MCP tool (mcp_tools_cerebellum.py) so the
# batched observe path here produces identical weights to the per-call path.
_LEARNING_RATE = 0.05
_TRACE_DECAY = 0.95
_BOUNDARY_THRESHOLD = 0.5
_CONF_EMA_ALPHA = 0.1
_TRACE_TTL_SECONDS = 3600

# Cache keyed by (db_path, action_key) → (timestamp, partner_or_None).
# When partner is None, "definitely not registered as of timestamp".
_REG_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_REG_TTL_SECONDS = 60.0

# module_id cache keyed by (db_path, partner, prediction_kind) → int
_MODULE_ID_CACHE: dict[tuple[str, str, str], int] = {}


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection | None:
    try:
        path = db_path or str(get_db_path())
        c = sqlite3.connect(path, timeout=2.0, isolation_level=None)
        # Shadow audit writes don't need full fsync durability — a crash
        # losing the last few audit rows is acceptable. NORMAL cuts commit
        # cost by ~5x on WAL DBs.
        try:
            c.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        return c
    except Exception as exc:  # pragma: no cover
        logger.debug("cerebellum_shadow: cannot open db: %s", exc)
        return None


def _should_skip(action_key: str) -> bool:
    if not action_key:
        return True
    if action_key in _SKIP_EXACT:
        return True
    return any(action_key.startswith(p) for p in _SKIP_PREFIXES)


def _resolve_partner_cached(action_key: str, db_path_str: str) -> str | None:
    """Cache-first lookup. Opens a connection only on cache miss / TTL
    expiry. None means "this action is not in the bg_actions catalog."
    """
    lookup_key = action_key if action_key.startswith("tool:") else f"tool:{action_key}"
    cache_key = (db_path_str, lookup_key)
    now = time.monotonic()
    entry = _REG_CACHE.get(cache_key)
    if entry is not None and (now - entry[0]) < _REG_TTL_SECONDS:
        return entry[1]

    # Cache miss or stale — query the DB
    conn = _connect(db_path_str)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT loop FROM bg_actions WHERE action_key = ? LIMIT 1",
                (lookup_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            _REG_CACHE[cache_key] = (now, None)
            return None
        partner = _LOOP_TO_PARTNER.get(str(row[0])) if row else None
        _REG_CACHE[cache_key] = (now, partner)
        return partner
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_module_ids(conn: sqlite3.Connection, db_path_str: str, partner: str) -> dict[str, int]:
    """Return {prediction_kind: module_id} for all three kinds, hitting the
    cache first and only touching the DB on miss.
    """
    missing: list[str] = []
    out: dict[str, int] = {}
    for kind in _PREDICTION_KINDS:
        cached = _MODULE_ID_CACHE.get((db_path_str, partner, kind))
        if cached is not None:
            out[kind] = cached
        else:
            missing.append(kind)
    if not missing:
        return out

    # One ensure + one read for the missing kinds. Use executemany for the
    # ensure (idempotent INSERT OR IGNORE) and a single IN-list query for
    # the read.
    conn.executemany(
        "INSERT OR IGNORE INTO cerebellum_modules (partner, prediction_kind, description) "
        "VALUES (?, ?, 'auto-registered via dispatch shadow consult')",
        [(partner, k) for k in missing],
    )
    placeholders = ",".join(["?"] * len(missing))
    rows = conn.execute(
        f"SELECT prediction_kind, id FROM cerebellum_modules "
        f"WHERE partner = ? AND prediction_kind IN ({placeholders})",  # nosec B608
        [partner, *missing],
    ).fetchall()
    for kind, mid in rows:
        mid_int = int(mid)
        _MODULE_ID_CACHE[(db_path_str, partner, kind)] = mid_int
        out[kind] = mid_int
    return out


def _context_hash(agent_id: str | None, arguments: dict[str, Any] | None) -> str:
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
    """Pre-dispatch forward predictions. Single transaction across all 3
    modules. Returns a dict with the prediction handles for observe_dispatch.
    """
    if _should_skip(action_key):
        return None
    db_path_str = db_path or str(get_db_path())
    partner = _resolve_partner_cached(action_key, db_path_str)
    if partner is None:
        return None

    conn = _connect(db_path_str)
    if conn is None:
        return None
    try:
        try:
            module_ids = _ensure_module_ids(conn, db_path_str, partner)
        except sqlite3.OperationalError:
            return None
        if not module_ids:
            return None

        ctx_hash = _context_hash(agent_id, arguments)

        # Batch read existing weights for (module, context) — one query
        mids = list(module_ids.values())
        weight_rows = conn.execute(
            f"SELECT module_id, weight, confidence FROM cerebellum_weights "
            f"WHERE context_hash = ? AND module_id IN ({','.join(['?']*len(mids))})",  # nosec B608
            [ctx_hash, *mids],
        ).fetchall()
        weights_by_mid: dict[int, tuple[float, float]] = {
            int(r[0]): (float(r[1]), float(r[2])) for r in weight_rows
        }

        # Single transaction for all writes
        conn.execute("BEGIN")
        predictions: dict[str, dict[str, Any]] = {}
        for kind, mid in module_ids.items():
            predicted, confidence = weights_by_mid.get(mid, (0.0, 0.0))
            cur = conn.execute(
                "INSERT INTO cerebellum_predictions "
                "(module_id, context_hash, predicted_value, confidence) "
                "VALUES (?, ?, ?, ?)",
                (mid, ctx_hash, predicted, confidence),
            )
            pid = cur.lastrowid
            predictions[kind] = {
                "prediction_id": pid,
                "predicted_value": predicted,
                "confidence": confidence,
                "module_id": mid,
            }

        # Deposit eligibility traces — single executemany
        conn.executemany(
            "INSERT INTO cerebellum_traces "
            "(module_id, context_hash, prediction_id, trace_strength, "
            " decay_constant, expires_at) "
            "VALUES (?, ?, ?, 1.0, 0.95, "
            "        strftime('%Y-%m-%dT%H:%M:%S', 'now', '+' || ? || ' seconds'))",
            [
                (p["module_id"], ctx_hash, p["prediction_id"], _TRACE_TTL_SECONDS)
                for p in predictions.values()
            ],
        )
        conn.execute("COMMIT")

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


def _resolve_sentinel_memory_id(conn: sqlite3.Connection) -> int | None:
    """Find the cerebellum-system sentinel memory created by migration 057.
    Cached in module-level state."""
    try:
        row = conn.execute(
            "SELECT id FROM memories "
            "WHERE agent_id = 'cerebellum-system' AND scope = 'system' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None
    except sqlite3.OperationalError:
        return None


_SENTINEL_MEMORY_CACHE: dict[str, int | None] = {}


def observe_dispatch(
    consult_result: dict[str, Any] | None,
    *,
    error: BaseException | str | None = None,
    db_path: Optional[str] = None,
) -> dict[str, Any] | None:
    """Close all three predictions in a single transaction.

    Replicates the supervised LTD update + trace decay + module stat update
    + (optional) boundary marker + workspace broadcast + BG TD broadcast
    that tool_cerebellum_observe does — but for all three modules at once,
    in one transaction, on a single connection.
    """
    if not consult_result:
        return None
    predictions = consult_result.get("predictions") or {}
    if not predictions:
        return None
    started_ns = int(consult_result.get("started_at_ns") or 0)
    elapsed_ms = (time.monotonic_ns() - started_ns) / 1e6 if started_ns else 0.0
    success = 1.0 if error is None else 0.0
    partner = str(consult_result.get("partner") or "")
    ctx_hash = str(consult_result.get("context_hash") or "")

    observed_values = {
        "success_probability": success,
        "expected_latency_ms": float(elapsed_ms),
        "expected_outcome_class": success,
    }

    db_path_str = db_path or str(get_db_path())
    conn = _connect(db_path_str)
    if conn is None:
        return None
    try:
        try:
            conn.execute("BEGIN")
        except sqlite3.OperationalError:
            return None

        closed: dict[str, Any] = {}
        boundary_inserts: list[tuple[Any, ...]] = []

        # Pre-fetch existing weight rows so we can compute updates locally
        mids = [p["module_id"] for p in predictions.values()]
        weight_rows = conn.execute(
            f"SELECT module_id, weight, confidence FROM cerebellum_weights "
            f"WHERE context_hash = ? AND module_id IN ({','.join(['?']*len(mids))})",  # nosec B608
            [ctx_hash, *mids],
        ).fetchall()
        cur_weights: dict[int, tuple[float, float]] = {
            int(r[0]): (float(r[1]), float(r[2])) for r in weight_rows
        }

        # Ensure weight rows exist for any new (module, context)
        new_rows = [
            (mid, ctx_hash) for mid in mids if mid not in cur_weights
        ]
        if new_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO cerebellum_weights (module_id, context_hash) "
                "VALUES (?, ?)",
                new_rows,
            )
            for mid, _c in new_rows:
                cur_weights.setdefault(mid, (0.0, 0.0))

        weight_updates: list[tuple[float, float, int, str]] = []
        prediction_updates: list[tuple[float, float, int]] = []
        module_stat_updates: list[tuple[float, int]] = []

        for kind, pred in predictions.items():
            mid = int(pred["module_id"])
            pid = int(pred["prediction_id"])
            predicted = float(pred["predicted_value"])
            observed = observed_values[kind]
            delta = observed - predicted

            old_w, old_c = cur_weights[mid]
            new_w = max(-1.0, min(1.0, old_w + _LEARNING_RATE * 1.0 * delta))
            new_c = max(
                0.0,
                min(
                    1.0,
                    (1 - _CONF_EMA_ALPHA) * old_c
                    + _CONF_EMA_ALPHA * (1.0 - min(1.0, abs(delta))),
                ),
            )

            weight_updates.append((new_w, new_c, mid, ctx_hash))
            prediction_updates.append((observed, delta, pid))
            module_stat_updates.append((abs(delta), mid))

            closed[kind] = {
                "delta_forward": delta,
                "weight_after": new_w,
                "boundary_id": None,
            }

            if abs(delta) >= _BOUNDARY_THRESHOLD:
                boundary_inserts.append(
                    (partner, delta, ctx_hash, pid, min(1.0, abs(delta)))
                )

        conn.executemany(
            "UPDATE cerebellum_weights "
            "SET weight = ?, confidence = ?, n_updates = n_updates + 1, "
            "    last_updated = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
            "WHERE module_id = ? AND context_hash = ?",
            weight_updates,
        )
        conn.executemany(
            "UPDATE cerebellum_predictions "
            "SET observed_value = ?, observed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
            "    delta_forward = ? "
            "WHERE id = ?",
            prediction_updates,
        )
        # Decay all traces for this consult
        pids = [p["prediction_id"] for p in predictions.values()]
        conn.execute(
            f"UPDATE cerebellum_traces SET trace_strength = trace_strength * ? "
            f"WHERE prediction_id IN ({','.join(['?']*len(pids))})",  # nosec B608
            [_TRACE_DECAY, *pids],
        )
        conn.executemany(
            "UPDATE cerebellum_modules "
            "SET n_predictions = n_predictions + 1, "
            "    mean_abs_error = ((mean_abs_error * n_predictions) + ?) / (n_predictions + 1) "
            "WHERE id = ?",
            module_stat_updates,
        )

        # Boundary markers (optionally with workspace broadcasts)
        sentinel_id = _SENTINEL_MEMORY_CACHE.get(db_path_str)
        if sentinel_id is None:
            sentinel_id = _resolve_sentinel_memory_id(conn)
            _SENTINEL_MEMORY_CACHE[db_path_str] = sentinel_id

        if boundary_inserts:
            cur = conn.executemany(
                "INSERT INTO cerebellum_boundaries "
                "(partner, delta_forward, context_hash, prediction_id, salience) "
                "VALUES (?, ?, ?, ?, ?)",
                boundary_inserts,
            )
            # Recover boundary IDs by selecting recent rows for these
            # (prediction_id, partner) keys. Acceptable cost — boundaries
            # are rare.
            recent = conn.execute(
                f"SELECT id, prediction_id FROM cerebellum_boundaries "
                f"WHERE prediction_id IN ({','.join(['?']*len(pids))}) "
                f"ORDER BY id DESC LIMIT ?",  # nosec B608
                [*pids, len(boundary_inserts)],
            ).fetchall()
            pid_to_boundary = {int(p): int(bid) for bid, p in recent}
            for kind, pred in predictions.items():
                pid = int(pred["prediction_id"])
                if pid in pid_to_boundary:
                    closed[kind]["boundary_id"] = pid_to_boundary[pid]

            # Workspace broadcasts (one row per boundary)
            if sentinel_id is not None:
                try:
                    conn.executemany(
                        "INSERT INTO workspace_broadcasts "
                        "(memory_id, agent_id, salience, summary, target_scope, triggered_by) "
                        "VALUES (?, 'cerebellum-system', ?, ?, 'global', ?)",
                        [
                            (
                                sentinel_id,
                                min(1.0, abs(d)),
                                f"cerebellum surprise: partner={partner} "
                                f"δ_forward={d:+.4f}",
                                f"cerebellum_boundary:{pid_to_boundary[pid]}",
                            )
                            for (partner, d, _c, pid, _s) in boundary_inserts
                            if pid in pid_to_boundary
                        ],
                    )
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    pass

        conn.execute("COMMIT")

        # BG TD-error broadcast is opt-in: only fire when at least one
        # boundary marker triggered (significant prediction error). Skipping
        # the per-dispatch broadcast saves a connection open + trace consume
        # on every routine tool call.
        if boundary_inserts:
            try:
                from agentmemory.bg_shadow import broadcast_td_error
                mean_d = sum(c["delta_forward"] for c in closed.values()) / max(1, len(closed))
                broadcast_td_error(
                    task_id="cerebellum:dispatch_boundary",
                    agent_id=None,
                    utility=mean_d,
                    source="cerebellum_observe_batched",
                    db_path=db_path_str,
                )
            except Exception:
                pass

        return {
            "partner": partner,
            "closed": closed,
            "elapsed_ms": elapsed_ms,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def clear_caches() -> None:
    """Test helper — clears module-level caches. Public so test cleanup
    can avoid cross-test pollution."""
    _REG_CACHE.clear()
    _MODULE_ID_CACHE.clear()
    _SENTINEL_MEMORY_CACHE.clear()
