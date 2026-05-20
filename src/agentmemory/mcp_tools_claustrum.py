"""brainctl MCP tools — claustrum (cross-modal binding).

Phase 1 per research-avenues memo Avenue 3. Detects when multiple
retrieval modalities converge on the same memory — a cross-modal
agreement signal that wasn't previously a first-class concept.

Phase 1 = manual binding events + queries. Phase 2 auto-detects from
cmd_search output. Phase 3 boosts memory confidence by binding strength.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_ENFORCEMENT_MODES = {"shadow", "enforce", "disabled"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("claustrum_state", "claustrum_modality_catalog", "claustrum_binding_events"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return f"claustrum schema missing: {t}. Run `brainctl migrate` (079)."
    return None


def tool_claustrum_status(**_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM claustrum_state WHERE id = 1").fetchone()
        catalog = _rows(conn.execute("SELECT * FROM claustrum_modality_catalog ORDER BY name").fetchall())
        last_5 = _rows(conn.execute(
            "SELECT * FROM claustrum_binding_events ORDER BY id DESC LIMIT 5"
        ).fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(binding_strength), 0.0) AS mean_strength,
                   COALESCE(MAX(modality_count), 0) AS peak_modalities
              FROM claustrum_binding_events
             WHERE bound_at >= datetime('now', '-24 hours')
            """
        ).fetchone()
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "modality_catalog": catalog,
        "last_5_bindings": last_5,
        "aggregate_24h": dict(agg) if agg else {},
    }


def tool_claustrum_record_binding(
    memory_id: int, modalities: list[str] | str,
    agent_id: str | None = None, query_hash: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Record a cross-modal binding event. modalities can be a list of
    modality names or a comma-separated string. Computes
    binding_strength as the mean of modality weights × normalized count."""
    if isinstance(modalities, str):
        modality_list = [m.strip() for m in modalities.split(",") if m.strip()]
    else:
        modality_list = [str(m).strip() for m in modalities if str(m).strip()]
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM claustrum_state WHERE id = 1").fetchone()
        min_n = int(state["min_modalities_for_binding"]) if state else 2
        if len(modality_list) < min_n:
            return {"error": f"modality count {len(modality_list)} < min_modalities_for_binding={min_n}"}
        catalog = {r["name"]: float(r["weight"]) for r in conn.execute(
            "SELECT name, weight FROM claustrum_modality_catalog"
        ).fetchall()}
        unknown = [m for m in modality_list if m not in catalog]
        if unknown:
            return {"error": f"unknown modalities: {unknown}. Register via claustrum_register_modality."}
        # binding_strength = mean(weights) × min(1.0, count / 4)
        mean_w = sum(catalog[m] for m in modality_list) / len(modality_list)
        count_factor = min(1.0, len(modality_list) / 4.0)
        binding_strength = max(0.0, min(1.0, mean_w * count_factor))
        modalities_csv = ",".join(sorted(set(modality_list)))
        cur = conn.execute(
            """
            INSERT INTO claustrum_binding_events
              (memory_id, agent_id, query_hash, modalities, modality_count, binding_strength, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(memory_id), agent_id, query_hash, modalities_csv,
             len(set(modality_list)), float(binding_strength), notes),
        )
        binding_id = cur.lastrowid
        conn.execute(
            "UPDATE claustrum_state SET total_bindings = total_bindings + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = 1"
        )
        conn.commit()
    return {
        "ok": True, "binding_id": binding_id,
        "memory_id": int(memory_id),
        "modalities": modalities_csv,
        "binding_strength": binding_strength,
    }


def tool_claustrum_register_modality(
    name: str, description: str | None = None, weight: float = 1.0,
    **_kw: Any,
) -> dict[str, Any]:
    if not 0.0 <= weight <= 1.0:
        return {"error": "weight must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            """
            INSERT INTO claustrum_modality_catalog (name, description, weight)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              weight = excluded.weight,
              description = COALESCE(excluded.description, claustrum_modality_catalog.description)
            """,
            (name, description, float(weight)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM claustrum_modality_catalog WHERE name = ?", (name,)
        ).fetchone()
    return {"ok": True, "modality": dict(row) if row else None}


def tool_claustrum_memory_bindings(memory_id: int, limit: int = 10, **_kw: Any) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        rows = conn.execute(
            """
            SELECT * FROM claustrum_binding_events
             WHERE memory_id = ? ORDER BY id DESC LIMIT ?
            """,
            (int(memory_id), limit),
        ).fetchall()
    return {"ok": True, "memory_id": int(memory_id), "bindings": _rows(rows)}


def tool_claustrum_set(
    binding_window_seconds: int | None = None,
    min_modalities_for_binding: int | None = None,
    enforcement_mode: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if binding_window_seconds is not None and binding_window_seconds <= 0:
        return {"error": "binding_window_seconds must be > 0"}
    if min_modalities_for_binding is not None and min_modalities_for_binding < 2:
        return {"error": "min_modalities_for_binding must be ≥ 2"}
    if enforcement_mode is not None and enforcement_mode not in VALID_ENFORCEMENT_MODES:
        return {"error": f"invalid enforcement_mode"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        updates, params = [], []
        if binding_window_seconds is not None:
            updates.append("binding_window_seconds = ?"); params.append(int(binding_window_seconds))
        if min_modalities_for_binding is not None:
            updates.append("min_modalities_for_binding = ?"); params.append(int(min_modalities_for_binding))
        if enforcement_mode is not None:
            updates.append("enforcement_mode = ?"); params.append(enforcement_mode)
        if not updates:
            return {"error": "no fields to update"}
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        conn.execute(f"UPDATE claustrum_state SET {', '.join(updates)} WHERE id = 1", tuple(params))
        conn.commit()
        state = conn.execute("SELECT * FROM claustrum_state WHERE id = 1").fetchone()
    return {"ok": True, "state": dict(state) if state else None}


TOOLS: list[Tool] = [
    Tool(
        name="claustrum_status",
        description="Claustrum state + modality catalog + last 5 bindings + 24h aggregate.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="claustrum_record_binding",
        description=(
            "Record a cross-modal binding event when ≥min_modalities retrieval modalities "
            "converged on the same memory. modalities can be a list or CSV. binding_strength "
            "auto-computed from mean modality weights × min(1.0, count/4)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "modalities": {"oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"},
                ]},
                "agent_id": {"type": "string"},
                "query_hash": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["memory_id", "modalities"],
        },
    ),
    Tool(
        name="claustrum_register_modality",
        description="Idempotent UPSERT on modality_catalog. weight in [0, 1]. Used when a new retrieval modality is added to brainctl.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "weight": {"type": "number", "default": 1.0},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="claustrum_memory_bindings",
        description="All binding events for a specific memory_id, newest first. limit clamped to [1, 100].",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="claustrum_set",
        description="Update claustrum state knobs. binding_window_seconds > 0; min_modalities_for_binding ≥ 2; enforcement_mode ∈ {shadow, enforce, disabled}.",
        inputSchema={
            "type": "object",
            "properties": {
                "binding_window_seconds": {"type": "integer"},
                "min_modalities_for_binding": {"type": "integer"},
                "enforcement_mode": {"type": "string", "enum": sorted(VALID_ENFORCEMENT_MODES)},
            },
        },
    ),
]


_CLAUSTRUM_TOOLS = {
    "claustrum_status": tool_claustrum_status,
    "claustrum_record_binding": tool_claustrum_record_binding,
    "claustrum_register_modality": tool_claustrum_register_modality,
    "claustrum_memory_bindings": tool_claustrum_memory_bindings,
    "claustrum_set": tool_claustrum_set,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _CLAUSTRUM_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
