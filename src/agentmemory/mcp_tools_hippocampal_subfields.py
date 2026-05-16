"""brainctl MCP tools — hippocampal subfields (DG / CA3 / CA1).

Phase 1 augmentation of the existing hippocampus.py module. Two audit
streams expose what was previously implicit:

  hippocampus_dg_separate     — pattern-separation decision at write time
  hippocampus_ca3_complete    — pattern-completion event at retrieval time
  hippocampus_subfields_status — inspection
  hippocampus_dg_check        — read-only: would this candidate be
                                 separated, merged, or pass through?

The real DG pattern-separation and CA3 pattern-completion are not wired
into the W(m) and retrieval hot paths yet — that's Phase 2. Phase 1
gives the audit substrate + callable tools so any caller (or future
hookpoint) can record and inspect subfield decisions.
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

VALID_DECISIONS = {"separate", "merge", "deduplicate", "passthrough"}
_DG_SEPARATE_THRESHOLD = 0.85    # cosine SIMILARITY above which we'd merge
_DG_DEDUPE_THRESHOLD = 0.97       # at-or-above is essentially duplicate


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
            "hippocampus_pattern_separations",
            "hippocampus_completion_traces",
        )
        if not _table_exists(conn, t)
    ]
    if missing:
        return "hippocampal subfields schema missing: " + ", ".join(missing)
    return None


def _decision_for_distance(cosine_distance: float) -> tuple[str, str | None]:
    """Map cosine distance → DG decision + separation tag (when applicable).

    cosine_distance is in [0, 2]; we treat ≤(1 - threshold) as similar.
    """
    similarity = 1.0 - float(cosine_distance)
    if similarity >= _DG_DEDUPE_THRESHOLD:
        return ("deduplicate", None)
    if similarity >= _DG_SEPARATE_THRESHOLD:
        # Close enough to interfere — apply a separation tag so future
        # retrieval can distinguish.
        return ("separate", hashlib.sha256(f"dg:{cosine_distance:.4f}".encode()).hexdigest()[:12])
    return ("passthrough", None)


def tool_hippocampus_dg_separate(
    memory_id: int | None = None,
    nearest_neighbor_id: int | None = None,
    cosine_distance: float | None = None,
    decision: str | None = None,
    separation_tag: str | None = None,
    scope: str | None = None,
    agent_id: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Record a DG pattern-separation decision. Caller (typically the
    W(m) write gate) supplies the cosine_distance to the nearest neighbor;
    decision can be auto-computed if not provided.
    """
    if cosine_distance is None:
        return {"ok": False, "error": "cosine_distance is required"}
    try:
        cd = float(cosine_distance)
    except (TypeError, ValueError):
        return {"ok": False, "error": "cosine_distance must be numeric"}

    if decision is None:
        decision, auto_tag = _decision_for_distance(cd)
        if separation_tag is None:
            separation_tag = auto_tag
    if decision not in VALID_DECISIONS:
        return {"ok": False, "error": f"decision must be one of {sorted(VALID_DECISIONS)}"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        cur = db.execute(
            """
            INSERT INTO hippocampus_pattern_separations
              (memory_id, nearest_neighbor_id, cosine_distance, decision,
               separation_tag, scope, agent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, nearest_neighbor_id, cd, decision,
             separation_tag, scope, agent_id),
        )
        db.commit()
        return {
            "ok": True,
            "separation_id": cur.lastrowid,
            "decision": decision,
            "separation_tag": separation_tag,
            "cosine_distance": cd,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_hippocampus_dg_check(
    cosine_distance: float,
    **kw: Any,
) -> dict[str, Any]:
    """Read-only DG decision preview. Useful for the W(m) gate to consult
    before actually writing.
    """
    try:
        cd = float(cosine_distance)
    except (TypeError, ValueError):
        return {"ok": False, "error": "cosine_distance must be numeric"}
    decision, tag = _decision_for_distance(cd)
    return {
        "ok": True,
        "cosine_distance": cd,
        "similarity": 1.0 - cd,
        "decision": decision,
        "separation_tag": tag,
        "separate_threshold_sim": _DG_SEPARATE_THRESHOLD,
        "dedupe_threshold_sim": _DG_DEDUPE_THRESHOLD,
    }


def tool_hippocampus_ca3_complete(
    query_hash: str,
    completed_to_memory_id: int,
    distance: float,
    rank: int = 1,
    agent_id: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Record a CA3 pattern-completion event. Caller is typically the
    retrieval path (memory_search / push) after returning a top-k match.
    """
    if not query_hash:
        return {"ok": False, "error": "query_hash is required"}
    try:
        dist = float(distance)
        rnk = max(1, int(rank))
    except (TypeError, ValueError):
        return {"ok": False, "error": "distance must be numeric, rank an integer"}
    try:
        mid = int(completed_to_memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "completed_to_memory_id must be an integer"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        cur = db.execute(
            """
            INSERT INTO hippocampus_completion_traces
              (query_hash, completed_to_memory_id, distance, rank, agent_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (query_hash, mid, dist, rnk, agent_id),
        )
        db.commit()
        return {"ok": True, "trace_id": cur.lastrowid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_hippocampus_subfields_status(
    days: int = 7,
    top_n: int = 10,
    **kw: Any,
) -> dict[str, Any]:
    """Snapshot of recent DG separations + CA3 completions."""
    try:
        days_int = max(1, int(days))
        top_n_int = max(1, min(int(top_n), 50))
    except (TypeError, ValueError):
        return {"ok": False, "error": "days and top_n must be integers"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        by_decision = db.execute(
            f"""
            SELECT decision, COUNT(*) AS n,
                   ROUND(AVG(cosine_distance), 4) AS mean_distance
            FROM hippocampus_pattern_separations
            WHERE decided_at >= datetime('now', '-{days_int} days')
            GROUP BY decision
            ORDER BY n DESC
            """,  # nosec B608
        ).fetchall()
        recent_separations = db.execute(
            f"""
            SELECT id, memory_id, nearest_neighbor_id, cosine_distance,
                   decision, separation_tag, scope, agent_id, decided_at
            FROM hippocampus_pattern_separations
            WHERE decided_at >= datetime('now', '-{days_int} days')
            ORDER BY decided_at DESC
            LIMIT ?
            """,  # nosec B608
            (top_n_int,),
        ).fetchall()
        recent_completions = db.execute(
            f"""
            SELECT id, query_hash, completed_to_memory_id,
                   ROUND(distance, 4) AS distance, rank, agent_id, completed_at
            FROM hippocampus_completion_traces
            WHERE completed_at >= datetime('now', '-{days_int} days')
            ORDER BY completed_at DESC
            LIMIT ?
            """,  # nosec B608
            (top_n_int,),
        ).fetchall()
        sep_total = db.execute(
            "SELECT COUNT(*) FROM hippocampus_pattern_separations"
        ).fetchone()[0]
        comp_total = db.execute(
            "SELECT COUNT(*) FROM hippocampus_completion_traces"
        ).fetchone()[0]
        return {
            "ok": True,
            "window_days": days_int,
            "separations_total": sep_total,
            "completions_total": comp_total,
            "by_decision": _rows_to_list(by_decision),
            "recent_separations": _rows_to_list(recent_separations),
            "recent_completions": _rows_to_list(recent_completions),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(
        name="hippocampus_dg_separate",
        description=(
            "Record a DG pattern-separation decision. The W(m) write gate or "
            "any caller can supply the cosine distance to the nearest "
            "existing memory; the decision (separate / merge / deduplicate "
            "/ passthrough) is auto-computed from thresholds if not given. "
            "Audit-only in Phase 1."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "nearest_neighbor_id": {"type": "integer"},
                "cosine_distance": {"type": "number"},
                "decision": {"type": "string", "enum": sorted(VALID_DECISIONS)},
                "separation_tag": {"type": "string"},
                "scope": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            "required": ["cosine_distance"],
        },
    ),
    Tool(
        name="hippocampus_dg_check",
        description=(
            "Read-only DG decision preview from a cosine distance. Returns "
            "what `hippocampus_dg_separate` would decide without writing a row."
        ),
        inputSchema={
            "type": "object",
            "properties": {"cosine_distance": {"type": "number"}},
            "required": ["cosine_distance"],
        },
    ),
    Tool(
        name="hippocampus_ca3_complete",
        description=(
            "Record a CA3 pattern-completion event: a retrieval that "
            "completed from a query cue to a stored memory beyond the "
            "similarity threshold. Audit-only in Phase 1."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query_hash": {"type": "string"},
                "completed_to_memory_id": {"type": "integer"},
                "distance": {"type": "number"},
                "rank": {"type": "integer", "default": 1},
                "agent_id": {"type": "string"},
            },
            "required": ["query_hash", "completed_to_memory_id", "distance"],
        },
    ),
    Tool(
        name="hippocampus_subfields_status",
        description=(
            "Snapshot of recent DG separations + CA3 completions: counts "
            "by decision, mean distances, recent rows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7},
                "top_n": {"type": "integer", "default": 10},
            },
        },
    ),
]

_HIPPO_TOOLS = {
    "hippocampus_dg_separate": tool_hippocampus_dg_separate,
    "hippocampus_dg_check": tool_hippocampus_dg_check,
    "hippocampus_ca3_complete": tool_hippocampus_ca3_complete,
    "hippocampus_subfields_status": tool_hippocampus_subfields_status,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _HIPPO_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
