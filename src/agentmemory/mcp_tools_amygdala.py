"""brainctl MCP tools — amygdala valence/threat tagging.

Phase 1 of the amygdala subsystem per docs/proposals/amygdala.md. The
amygdala is the rapid one-shot valence/threat tagging layer. Per McGaugh,
it does NOT store memories — it MODULATES consolidation/retrieval/broadcast
elsewhere via per-target valence tags.

Three load-bearing properties:
- One-shot updates with a saturating tanh on (arousal × valence_delta),
  capping single-event movement at ±0.5 to prevent PTSD-mode lock-in.
- Reconsolidation: query_valence opens a 1-hour labile window where the
  next update uses elevated learning rate.
- Extinction as overlay, not erasure: context-keyed inhibitory gates
  suppress the effective valence without modifying the underlying tag.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_TARGET_KINDS = {"entity", "agent", "context"}

_DEFAULT_LEARNING_RATE = 0.1
_LABILE_LEARNING_RATE = 0.4   # 4x normal during the reconsolidation window
_LABILE_WINDOW_SECONDS = 3600
_MAX_SINGLE_UPDATE = 0.5      # saturating tanh cap to prevent runaway


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
            "amygdala_valence_tags",
            "amygdala_valence_events",
            "amygdala_extinction_gates",
        )
        if not _table_exists(conn, t)
    ]
    if missing:
        return "amygdala schema missing tables: " + ", ".join(missing)
    return None


def _clamp(value: Any, lo: float = -1.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _saturating_update(current: float, raw_delta: float, lr: float) -> float:
    """Apply a saturating update bounded by _MAX_SINGLE_UPDATE.

    Uses tanh squashing so a single extreme event can move the tag at most
    ±_MAX_SINGLE_UPDATE, no matter how large `raw_delta * lr` is. Prevents
    PTSD-mode lock-in.
    """
    raw = float(lr) * float(raw_delta)
    capped = _MAX_SINGLE_UPDATE * math.tanh(raw / max(_MAX_SINGLE_UPDATE, 1e-9))
    return max(-1.0, min(1.0, current + capped))


def _is_labile(labile_until: str | None) -> bool:
    if not labile_until:
        return False
    import datetime as _dt
    try:
        # The stored timestamps come from strftime('%Y-%m-%dT%H:%M:%S', 'now')
        # which is naive UTC. Compare with a naive UTC "now" to avoid mixing
        # naive/aware datetimes.
        return (
            _dt.datetime.fromisoformat(labile_until.replace("Z", ""))
            > _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        )
    except Exception:
        return False


def tool_amygdala_status(
    target_kind: str | None = None,
    top_n: int = 10,
    **kw: Any,
) -> dict[str, Any]:
    """Snapshot of the amygdala subsystem state."""
    if target_kind and target_kind not in VALID_TARGET_KINDS:
        return {"ok": False, "error": f"target_kind must be one of {sorted(VALID_TARGET_KINDS)}"}
    try:
        top_n_int = max(1, min(int(top_n or 10), 50))
    except (TypeError, ValueError):
        return {"ok": False, "error": "top_n must be an integer"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        where = "WHERE target_kind = ?" if target_kind else ""
        params: list[Any] = [target_kind] if target_kind else []

        counts = db.execute(
            f"""
            SELECT target_kind, COUNT(*) AS n,
                   ROUND(AVG(valence), 4) AS mean_valence,
                   ROUND(AVG(arousal), 4) AS mean_arousal,
                   ROUND(MAX(ABS(valence)), 4) AS max_abs_valence
            FROM amygdala_valence_tags
            {where}
            GROUP BY target_kind
            ORDER BY target_kind
            """,  # nosec B608
            params,
        ).fetchall()

        top_tags = db.execute(
            f"""
            SELECT target_kind, target_id,
                   ROUND(valence, 4) AS valence,
                   ROUND(arousal, 4) AS arousal,
                   n_updates, last_updated, labile_until
            FROM amygdala_valence_tags
            {where}
            ORDER BY ABS(valence) DESC, n_updates DESC
            LIMIT ?
            """,  # nosec B608
            params + [top_n_int],
        ).fetchall()

        recent_events = db.execute(
            f"""
            SELECT id, target_kind, target_id,
                   ROUND(valence_delta, 4) AS valence_delta,
                   ROUND(arousal, 4) AS arousal,
                   reason, fired_at
            FROM amygdala_valence_events
            {where}
            ORDER BY fired_at DESC
            LIMIT ?
            """,  # nosec B608
            params + [top_n_int],
        ).fetchall()

        extinction_summary = db.execute(
            f"""
            SELECT target_kind, COUNT(*) AS n_gates,
                   ROUND(AVG(suppression_level), 4) AS mean_suppression
            FROM amygdala_extinction_gates
            {where}
            GROUP BY target_kind
            """,  # nosec B608
            params,
        ).fetchall()

        return {
            "ok": True,
            "target_kind_filter": target_kind,
            "tag_counts": _rows_to_list(counts),
            "top_tags": _rows_to_list(top_tags),
            "recent_events": _rows_to_list(recent_events),
            "extinction_summary": _rows_to_list(extinction_summary),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_amygdala_tag(
    target_kind: str,
    target_id: str,
    valence: float,
    arousal: float = 0.5,
    reason: str | None = None,
    source_memory_id: int | None = None,
    source_event_id: int | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """One-shot or incremental valence tag on a target.

    Applies a saturating update so a single event can move the tag at most
    ±0.5. If the tag is currently in its reconsolidation labile window,
    uses an elevated learning rate (4x normal). Logs a valence event row.
    """
    if target_kind not in VALID_TARGET_KINDS:
        return {"ok": False, "error": f"target_kind must be one of {sorted(VALID_TARGET_KINDS)}"}
    if not target_id or not isinstance(target_id, str):
        return {"ok": False, "error": "target_id is required"}
    try:
        valence_delta = _clamp(valence, -1.0, 1.0, default=0.0)
        arousal_val = _clamp(arousal, 0.0, 1.0, default=0.5)
    except Exception:
        return {"ok": False, "error": "valence and arousal must be numeric"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}

        existing = db.execute(
            "SELECT valence, arousal, n_updates, labile_until "
            "FROM amygdala_valence_tags "
            "WHERE target_kind = ? AND target_id = ?",
            (target_kind, target_id),
        ).fetchone()

        old_valence = float(existing[0]) if existing else 0.0
        old_arousal = float(existing[1]) if existing else 0.0
        labile = _is_labile(existing[3]) if existing else False
        lr = _LABILE_LEARNING_RATE if labile else _DEFAULT_LEARNING_RATE

        # Effective delta is arousal-weighted (high arousal = more learning)
        effective_delta = valence_delta * arousal_val
        new_valence = _saturating_update(old_valence, effective_delta, lr)
        # Arousal EMA
        alpha = 0.3
        new_arousal = (1 - alpha) * old_arousal + alpha * arousal_val

        db.execute(
            """
            INSERT INTO amygdala_valence_tags
              (target_kind, target_id, valence, arousal, n_updates, labile_until)
            VALUES (?, ?, ?, ?, 1, NULL)
            ON CONFLICT(target_kind, target_id) DO UPDATE SET
              valence = ?,
              arousal = ?,
              n_updates = n_updates + 1,
              last_updated = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              labile_until = NULL
            """,
            (target_kind, target_id, new_valence, new_arousal,
             new_valence, new_arousal),
        )
        db.execute(
            """
            INSERT INTO amygdala_valence_events (
                target_kind, target_id, valence_delta, arousal,
                source_memory_id, source_event_id, reason, learning_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (target_kind, target_id, valence_delta, arousal_val,
             source_memory_id, source_event_id, reason, lr),
        )
        db.commit()

        return {
            "ok": True,
            "target_kind": target_kind,
            "target_id": target_id,
            "old_valence": old_valence,
            "new_valence": new_valence,
            "arousal": new_arousal,
            "learning_rate": lr,
            "was_labile": labile,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_amygdala_query_valence(
    target_kind: str,
    target_id: str,
    context_hash: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Return the effective valence for a target.

    Effective valence = raw_valence × (1 − max(extinction gates matching
    context)). Marks the tag labile for the next 1h (reconsolidation
    window) — subsequent amygdala_tag calls within that window use the
    elevated learning rate.
    """
    if target_kind not in VALID_TARGET_KINDS:
        return {"ok": False, "error": f"target_kind must be one of {sorted(VALID_TARGET_KINDS)}"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        row = db.execute(
            "SELECT valence, arousal, n_updates, last_updated "
            "FROM amygdala_valence_tags WHERE target_kind = ? AND target_id = ?",
            (target_kind, target_id),
        ).fetchone()
        if not row:
            return {
                "ok": True, "target_kind": target_kind, "target_id": target_id,
                "raw_valence": 0.0, "effective_valence": 0.0,
                "arousal": 0.0, "n_updates": 0, "extinction_gates": [],
            }
        raw_valence = float(row[0])
        arousal = float(row[1])
        n_updates = int(row[2])

        # Find matching extinction gates
        if context_hash:
            gates = db.execute(
                """
                SELECT id, context_hash, suppression_level, n_safe_exposures
                FROM amygdala_extinction_gates
                WHERE target_kind = ? AND target_id = ? AND context_hash = ?
                """,
                (target_kind, target_id, context_hash),
            ).fetchall()
        else:
            gates = db.execute(
                """
                SELECT id, context_hash, suppression_level, n_safe_exposures
                FROM amygdala_extinction_gates
                WHERE target_kind = ? AND target_id = ?
                ORDER BY suppression_level DESC
                LIMIT 10
                """,
                (target_kind, target_id),
            ).fetchall()

        max_suppression = max(
            (float(g[2]) for g in gates), default=0.0,
        ) if gates else 0.0
        effective_valence = raw_valence * (1.0 - max_suppression)

        # Open the reconsolidation labile window for the next 1h.
        db.execute(
            """
            UPDATE amygdala_valence_tags
            SET labile_until = strftime('%Y-%m-%dT%H:%M:%S', 'now', '+' || ? || ' seconds')
            WHERE target_kind = ? AND target_id = ?
            """,
            (_LABILE_WINDOW_SECONDS, target_kind, target_id),
        )
        db.commit()

        return {
            "ok": True,
            "target_kind": target_kind,
            "target_id": target_id,
            "raw_valence": raw_valence,
            "effective_valence": effective_valence,
            "max_suppression": max_suppression,
            "arousal": arousal,
            "n_updates": n_updates,
            "extinction_gates": [
                {"id": g[0], "context_hash": g[1],
                 "suppression_level": float(g[2]),
                 "n_safe_exposures": int(g[3])}
                for g in gates
            ],
            "labile_for_seconds": _LABILE_WINDOW_SECONDS,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_amygdala_extinguish(
    target_kind: str,
    target_id: str,
    context_hash: str,
    suppression_level: float = 0.5,
    increment_exposure: bool = True,
    **kw: Any,
) -> dict[str, Any]:
    """Install or strengthen a context-keyed extinction gate.

    ITC-analog: does NOT erase the underlying tag, only installs an
    inhibitory overlay keyed to a specific context. If a gate already
    exists for this (target, context), `suppression_level` is set to
    `max(existing, new)` and `n_safe_exposures` increments.
    """
    if target_kind not in VALID_TARGET_KINDS:
        return {"ok": False, "error": f"target_kind must be one of {sorted(VALID_TARGET_KINDS)}"}
    if not context_hash or not isinstance(context_hash, str):
        return {"ok": False, "error": "context_hash is required"}
    sup = _clamp(suppression_level, 0.0, 1.0, default=0.5)
    if sup <= 0.0:
        return {"ok": False, "error": "suppression_level must be > 0"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}

        existing = db.execute(
            "SELECT id, suppression_level, n_safe_exposures "
            "FROM amygdala_extinction_gates "
            "WHERE target_kind = ? AND target_id = ? AND context_hash = ?",
            (target_kind, target_id, context_hash),
        ).fetchone()

        if existing:
            new_sup = max(float(existing[1]), sup)
            n_new = int(existing[2]) + (1 if increment_exposure else 0)
            db.execute(
                """
                UPDATE amygdala_extinction_gates
                SET suppression_level = ?, n_safe_exposures = ?
                WHERE id = ?
                """,
                (new_sup, n_new, int(existing[0])),
            )
            gate_id = int(existing[0])
        else:
            cur = db.execute(
                """
                INSERT INTO amygdala_extinction_gates (
                    target_kind, target_id, context_hash,
                    suppression_level, n_safe_exposures
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (target_kind, target_id, context_hash, sup),
            )
            gate_id = cur.lastrowid
            new_sup = sup
            n_new = 1

        db.commit()
        return {
            "ok": True,
            "gate_id": gate_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "context_hash": context_hash,
            "suppression_level": new_sup,
            "n_safe_exposures": n_new,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(
        name="amygdala_status",
        description=(
            "Inspect the amygdala subsystem: tag counts + means by target_kind, "
            "top-|valence| tags, recent valence events, extinction gate summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {"type": "string", "enum": sorted(VALID_TARGET_KINDS)},
                "top_n": {"type": "integer", "default": 10},
            },
        },
    ),
    Tool(
        name="amygdala_tag",
        description=(
            "One-shot or incremental valence tag on an entity/agent/context. "
            "Saturating update (tanh on arousal × valence_delta × lr) caps single-"
            "event movement at ±0.5 to prevent PTSD-mode lock-in. Uses elevated "
            "learning rate when the tag is currently in its reconsolidation "
            "labile window (opened by a recent amygdala_query_valence call)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {"type": "string", "enum": sorted(VALID_TARGET_KINDS)},
                "target_id": {"type": "string"},
                "valence": {"type": "number", "description": "−1.0 (aversive) to +1.0 (appetitive)"},
                "arousal": {"type": "number", "default": 0.5, "description": "0 (low) to 1 (high)"},
                "reason": {"type": "string"},
                "source_memory_id": {"type": "integer"},
                "source_event_id": {"type": "integer"},
            },
            "required": ["target_kind", "target_id", "valence"],
        },
    ),
    Tool(
        name="amygdala_query_valence",
        description=(
            "Return effective valence = raw_valence × (1 − max(extinction "
            "gates matching context)). Opens a 1-hour reconsolidation labile "
            "window: subsequent amygdala_tag calls within that window use an "
            "elevated learning rate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {"type": "string", "enum": sorted(VALID_TARGET_KINDS)},
                "target_id": {"type": "string"},
                "context_hash": {"type": "string"},
            },
            "required": ["target_kind", "target_id"],
        },
    ),
    Tool(
        name="amygdala_extinguish",
        description=(
            "Install or strengthen a context-keyed extinction gate. ITC-analog: "
            "does NOT erase the underlying tag, only installs an inhibitory "
            "overlay keyed to a specific context. Mirrors biology — fear "
            "extinction is competing inhibition, not unlearning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_kind": {"type": "string", "enum": sorted(VALID_TARGET_KINDS)},
                "target_id": {"type": "string"},
                "context_hash": {"type": "string"},
                "suppression_level": {"type": "number", "default": 0.5},
                "increment_exposure": {"type": "boolean", "default": True},
            },
            "required": ["target_kind", "target_id", "context_hash"],
        },
    ),
]

_AMYGDALA_TOOLS = {
    "amygdala_status": tool_amygdala_status,
    "amygdala_tag": tool_amygdala_tag,
    "amygdala_query_valence": tool_amygdala_query_valence,
    "amygdala_extinguish": tool_amygdala_extinguish,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _AMYGDALA_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
