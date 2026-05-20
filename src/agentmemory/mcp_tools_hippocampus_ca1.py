"""brainctl MCP tools — hippocampus CA1 + Subiculum.

Phase 1 per docs/proposals/hippocampus_ca1_subiculum.md. Completes
the trisynaptic loop after migration 059 shipped DG + CA3.

CA1 = match/mismatch detector (compare entorhinal input vs CA3 output).
Subiculum = hippocampal output bridge to cortex.

Phase 1 is inspection + manual writes; Phase 2 auto-wires into the
hippocampus_dg/ca3 pipeline.
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

VALID_CLASSIFICATIONS = {"match", "mismatch", "partial", "ambiguous"}
VALID_TARGET_CHANNELS = {"cortex_general", "workspace_broadcast", "thalamus_relay", "other"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        t for t in ("hippocampus_ca1_comparisons", "hippocampus_ca1_state",
                    "hippocampus_subiculum_outputs")
        if not _table_exists(conn, t)
    ]
    if missing:
        return ("CA1/subiculum schema missing: " + ", ".join(missing)
                + ". Run `brainctl migrate` (migration 071).")
    return None


def _hash_similarity(h1: str | None, h2: str | None) -> float:
    """Naive bit-string similarity over two hex hashes of equal length.

    Phase 1 stand-in for proper embedding cosine. Returns 0.0 if either
    hash is None or lengths differ.
    """
    if not h1 or not h2 or len(h1) != len(h2):
        return 0.0
    matches = sum(1 for a, b in zip(h1, h2) if a == b)
    return matches / len(h1)


def tool_ca1_compare(
    memory_id: int | None = None,
    ec_input_hash: str | None = None,
    ca3_output_hash: str | None = None,
    classification: str | None = None,
    agent_id: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Record one CA1 comparison.

    If classification is not passed explicitly, it's derived from the
    computed match_score: ≥0.85 = match, ≤0.15 = mismatch, 0.40-0.60 =
    ambiguous, else partial.
    """
    match_score = _hash_similarity(ec_input_hash, ca3_output_hash)
    novelty_score = 1.0 - match_score
    if classification is None:
        if match_score >= 0.85:
            classification = "match"
        elif match_score <= 0.15:
            classification = "mismatch"
        elif 0.40 <= match_score <= 0.60:
            classification = "ambiguous"
        else:
            classification = "partial"
    if classification not in VALID_CLASSIFICATIONS:
        return {"error": f"invalid classification {classification!r}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        cur = conn.execute(
            """
            INSERT INTO hippocampus_ca1_comparisons
              (agent_id, memory_id, ec_input_hash, ca3_output_hash,
               match_score, novelty_score, classification, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, memory_id, ec_input_hash, ca3_output_hash,
             match_score, novelty_score, classification, notes),
        )
        comparison_id = cur.lastrowid
        state = conn.execute("SELECT * FROM hippocampus_ca1_state WHERE id = 1").fetchone()
        old_match = float(state["recent_match_rate"]) if state else 0.5
        old_nov = float(state["recent_novelty_rate"]) if state else 0.5
        new_match = 0.9 * old_match + 0.1 * match_score
        new_nov = 0.9 * old_nov + 0.1 * novelty_score
        conn.execute(
            """
            UPDATE hippocampus_ca1_state SET
              recent_match_rate = ?, recent_novelty_rate = ?,
              total_comparisons = total_comparisons + 1,
              last_comparison_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
              updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
             WHERE id = 1
            """,
            (new_match, new_nov),
        )
        conn.commit()
    return {
        "ok": True, "comparison_id": comparison_id,
        "match_score": match_score, "novelty_score": novelty_score,
        "classification": classification,
        "new_recent_match_rate": new_match,
        "new_recent_novelty_rate": new_nov,
    }


def tool_ca1_status(agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        state = conn.execute("SELECT * FROM hippocampus_ca1_state WHERE id = 1").fetchone()
        last_5 = _rows(conn.execute(
            """
            SELECT id, compared_at, agent_id, memory_id, classification,
                   match_score, novelty_score
              FROM hippocampus_ca1_comparisons
              WHERE (? IS NULL OR agent_id = ?)
             ORDER BY id DESC LIMIT 5
            """, (agent_id, agent_id)
        ).fetchall())
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(match_score), 0.0) AS mean_match,
                   COALESCE(AVG(novelty_score), 0.0) AS mean_novelty,
                   SUM(CASE WHEN classification='match' THEN 1 ELSE 0 END) AS n_match,
                   SUM(CASE WHEN classification='mismatch' THEN 1 ELSE 0 END) AS n_mismatch,
                   SUM(CASE WHEN classification='partial' THEN 1 ELSE 0 END) AS n_partial,
                   SUM(CASE WHEN classification='ambiguous' THEN 1 ELSE 0 END) AS n_ambiguous
              FROM hippocampus_ca1_comparisons
             WHERE compared_at >= datetime('now', '-24 hours')
               AND (? IS NULL OR agent_id = ?)
            """, (agent_id, agent_id)
        ).fetchone()
    return {
        "ok": True,
        "state": dict(state) if state else None,
        "last_5_comparisons": last_5,
        "aggregate_24h": dict(agg) if agg else {},
    }


def tool_subiculum_output(
    target_channel: str,
    memory_id: int | None = None,
    ca1_comparison_id: int | None = None,
    output_strength: float = 0.5,
    agent_id: str | None = None,
    notes: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if target_channel not in VALID_TARGET_CHANNELS:
        return {"error": f"invalid target_channel {target_channel!r}; expected one of {sorted(VALID_TARGET_CHANNELS)}"}
    if not 0.0 <= output_strength <= 1.0:
        return {"error": "output_strength must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        cur = conn.execute(
            """
            INSERT INTO hippocampus_subiculum_outputs
              (agent_id, memory_id, ca1_comparison_id, target_channel, output_strength, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent_id, memory_id, ca1_comparison_id, target_channel,
             float(output_strength), notes),
        )
        conn.commit()
        return {"ok": True, "output_id": cur.lastrowid, "target_channel": target_channel}


def tool_ca1_subiculum_history(
    limit: int = 20, since: str | None = None,
    agent_id: str | None = None, classification: str | None = None,
    target_channel: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    """Paginated combined history of CA1 comparisons + Subiculum outputs.

    Filters apply per-bucket: classification → comparisons; target_channel
    → outputs. Returns two lists.
    """
    limit = max(1, min(int(limit), 200))
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        cmp_clauses, cmp_params = [], []
        out_clauses, out_params = [], []
        if since:
            cmp_clauses.append("compared_at >= ?"); cmp_params.append(since)
            out_clauses.append("output_at >= ?"); out_params.append(since)
        if agent_id:
            cmp_clauses.append("agent_id = ?"); cmp_params.append(agent_id)
            out_clauses.append("agent_id = ?"); out_params.append(agent_id)
        if classification:
            cmp_clauses.append("classification = ?"); cmp_params.append(classification)
        if target_channel:
            out_clauses.append("target_channel = ?"); out_params.append(target_channel)
        cmp_where = "WHERE " + " AND ".join(cmp_clauses) if cmp_clauses else ""
        out_where = "WHERE " + " AND ".join(out_clauses) if out_clauses else ""
        comparisons = _rows(conn.execute(
            f"SELECT * FROM hippocampus_ca1_comparisons {cmp_where} ORDER BY id DESC LIMIT ?",
            (*cmp_params, limit),
        ).fetchall())
        outputs = _rows(conn.execute(
            f"SELECT * FROM hippocampus_subiculum_outputs {out_where} ORDER BY id DESC LIMIT ?",
            (*out_params, limit),
        ).fetchall())
    return {"ok": True, "comparisons": comparisons, "outputs": outputs}


TOOLS: list[Tool] = [
    Tool(
        name="ca1_compare",
        description=(
            "Record one CA1 match/mismatch comparison (entorhinal input vs CA3 output). "
            "match_score auto-computed from hash similarity. classification auto-derived "
            "if not passed (≥0.85=match, ≤0.15=mismatch, [0.4,0.6]=ambiguous, else partial)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "ec_input_hash": {"type": "string"},
                "ca3_output_hash": {"type": "string"},
                "classification": {"type": "string", "enum": sorted(VALID_CLASSIFICATIONS)},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    ),
    Tool(
        name="ca1_status",
        description="CA1 Phase 1 inspection. State (EWMA match/novelty rates) + last 5 comparisons + 24h aggregate.",
        inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}},
    ),
    Tool(
        name="subiculum_output",
        description=(
            "Record one Subiculum output event. target_channel ∈ {cortex_general, "
            "workspace_broadcast, thalamus_relay, other}. output_strength in [0,1]. "
            "Optional ca1_comparison_id links the output back to the comparison that "
            "drove it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_channel": {"type": "string", "enum": sorted(VALID_TARGET_CHANNELS)},
                "memory_id": {"type": "integer"},
                "ca1_comparison_id": {"type": "integer"},
                "output_strength": {"type": "number", "default": 0.5},
                "agent_id": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["target_channel"],
        },
    ),
    Tool(
        name="ca1_subiculum_history",
        description=(
            "Combined paginated history of CA1 comparisons + Subiculum outputs. "
            "Filters: since, agent_id, classification (cmp-side), target_channel "
            "(out-side). limit clamped to [1, 200]."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "since": {"type": "string"},
                "agent_id": {"type": "string"},
                "classification": {"type": "string", "enum": sorted(VALID_CLASSIFICATIONS)},
                "target_channel": {"type": "string", "enum": sorted(VALID_TARGET_CHANNELS)},
            },
        },
    ),
]


_CA1_TOOLS = {
    "ca1_compare": tool_ca1_compare,
    "ca1_status": tool_ca1_status,
    "subiculum_output": tool_subiculum_output,
    "ca1_subiculum_history": tool_ca1_subiculum_history,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _CA1_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
