"""brainctl MCP tools — entorhinal cortex conceptual grid.

48 grid cells across 3 scales (fine=16, medium=16, coarse=16). Each
memory activates a small subset of cells based on a deterministic hash
of its content. Provides pattern-indexed lookup that scales sub-linearly
with the memory count.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH = get_db_path()
_SCALES = (1, 2, 3)
_CELLS_PER_SCALE = 16


def _db(): return open_db(str(DB_PATH))
def _rows(r: Iterable[sqlite3.Row]): return [dict(x) for x in r]


def _activate_cells(content: str) -> list[tuple[int, int, float]]:
    """Return [(scale, cell_index, activation)] for the content.

    Deterministic hash → 1 cell active per scale. Multi-scale activation
    means similar concepts share cells at coarse scales but differ at fine.
    """
    out = []
    base = hashlib.sha256(content.encode("utf-8")).digest()
    for scale in _SCALES:
        # Use a different slice of the hash for each scale
        idx = base[scale - 1] % _CELLS_PER_SCALE
        out.append((scale, idx, 1.0))
    return out


def tool_entorhinal_activate(
    memory_id: int, content: str, **kw: Any,
) -> dict[str, Any]:
    """Compute and persist grid-cell activations for a memory's content.
    Idempotent on (memory_id, cell_id)."""
    if not content:
        return {"ok": False, "error": "content is required"}
    activations = _activate_cells(content)
    db = _db()
    try:
        recorded = []
        for scale, cell_idx, act in activations:
            cell_row = db.execute(
                "SELECT id FROM entorhinal_grid_cells WHERE scale=? AND cell_index=?",
                (scale, cell_idx),
            ).fetchone()
            if not cell_row:
                continue
            cell_id = int(cell_row[0])
            db.execute(
                "INSERT INTO entorhinal_memory_activations (memory_id, cell_id, activation) "
                "VALUES (?, ?, ?) ON CONFLICT(memory_id, cell_id) DO UPDATE SET "
                "activation=excluded.activation, "
                "recorded_at=strftime('%Y-%m-%dT%H:%M:%S','now')",
                (int(memory_id), cell_id, float(act)),
            )
            recorded.append({"scale": scale, "cell_index": cell_idx, "cell_id": cell_id})
        db.commit()
        return {"ok": True, "memory_id": int(memory_id),
                "cells_activated": recorded}
    finally:
        db.close()


def tool_entorhinal_lookup(
    content: str, top_n: int = 10, **kw: Any,
) -> dict[str, Any]:
    """Find memories that activate the same grid cells as the query content.
    Returns candidates sorted by number of overlapping cells across scales
    (a memory matching all 3 scales is a strong match; 1 scale is weak)."""
    if not content:
        return {"ok": False, "error": "content is required"}
    activations = _activate_cells(content)
    db = _db()
    try:
        cell_ids = []
        for scale, cell_idx, _act in activations:
            row = db.execute(
                "SELECT id FROM entorhinal_grid_cells WHERE scale=? AND cell_index=?",
                (scale, cell_idx),
            ).fetchone()
            if row:
                cell_ids.append(int(row[0]))
        if not cell_ids:
            return {"ok": True, "matches": []}
        placeholders = ",".join(["?"] * len(cell_ids))
        rows = db.execute(
            f"SELECT memory_id, COUNT(*) AS overlap, AVG(activation) AS mean_act "
            f"FROM entorhinal_memory_activations WHERE cell_id IN ({placeholders}) "
            f"GROUP BY memory_id ORDER BY overlap DESC, mean_act DESC LIMIT ?",  # nosec B608
            cell_ids + [max(1, min(int(top_n), 100))],
        ).fetchall()
        return {"ok": True, "query_cells": [
            {"scale": s, "cell_index": ci} for s, ci, _ in activations
        ], "matches": _rows(rows)}
    finally:
        db.close()


def tool_entorhinal_status(**kw: Any) -> dict[str, Any]:
    db = _db()
    try:
        cells = db.execute(
            "SELECT scale, COUNT(*) AS n FROM entorhinal_grid_cells GROUP BY scale ORDER BY scale"
        ).fetchall()
        activations = db.execute(
            "SELECT COUNT(*) FROM entorhinal_memory_activations"
        ).fetchone()[0]
        top_cells = db.execute(
            "SELECT egc.scale, egc.cell_index, COUNT(ema.memory_id) AS n_memories "
            "FROM entorhinal_grid_cells egc "
            "LEFT JOIN entorhinal_memory_activations ema ON ema.cell_id=egc.id "
            "GROUP BY egc.id ORDER BY n_memories DESC LIMIT 10"
        ).fetchall()
        return {"ok": True,
                "cells_by_scale": _rows(cells),
                "activations_total": activations,
                "top_cells_by_memory_count": _rows(top_cells)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(name="entorhinal_activate",
         description="Compute + persist grid-cell activations for a memory's content "
                     "across 3 scales (fine, medium, coarse).",
         inputSchema={"type": "object", "properties": {
             "memory_id": {"type": "integer"},
             "content": {"type": "string"},
         }, "required": ["memory_id", "content"]}),
    Tool(name="entorhinal_lookup",
         description="Find memories whose grid-cell activations overlap with the query "
                     "content's. Sub-linear pattern lookup.",
         inputSchema={"type": "object", "properties": {
             "content": {"type": "string"},
             "top_n": {"type": "integer", "default": 10},
         }, "required": ["content"]}),
    Tool(name="entorhinal_status",
         description="Inspect grid-cell occupancy + total activations + most-active cells.",
         inputSchema={"type": "object", "properties": {}}),
]
_EG_TOOLS = {"entorhinal_activate": tool_entorhinal_activate,
             "entorhinal_lookup": tool_entorhinal_lookup,
             "entorhinal_status": tool_entorhinal_status}
DISPATCH = {n: (lambda _f=f, **kw: _f(**kw)) for n, f in _EG_TOOLS.items()}


def register_tools(): return TOOLS, DISPATCH
