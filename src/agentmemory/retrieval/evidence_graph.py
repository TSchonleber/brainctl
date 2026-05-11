"""Evidence expansion helpers for procedure retrieval."""

from __future__ import annotations

import sqlite3
from typing import Any


def expand_procedure_evidence(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    max_sources_per_candidate: int = 4,
) -> dict[int, dict[str, Any]]:
    """Attach 1-hop provenance and support evidence to top procedure candidates."""

    if not candidates:
        return {}

    out: dict[int, dict[str, Any]] = {}
    for cand in candidates:
        proc_id = int(cand["id"])
        sources = [
            dict(row)
            for row in conn.execute(
                """
                SELECT memory_id, event_id, decision_id, entity_id, source_role, created_at
                  FROM procedure_sources
                 WHERE procedure_id = ?
                 ORDER BY id
                 LIMIT ?
                """,
                (proc_id, max_sources_per_candidate),
            ).fetchall()
        ]
        edges = [
            dict(row)
            for row in conn.execute(
                """
                SELECT target_table, target_id, relation_type, weight
                  FROM knowledge_edges
                 WHERE source_table = 'procedures' AND source_id = ?
                 ORDER BY weight DESC, id DESC
                 LIMIT ?
                """,
                (proc_id, max_sources_per_candidate),
            ).fetchall()
        ]
        support_bonus = min((len(sources) * 0.14) + (sum(float(edge.get("weight") or 0.0) for edge in edges) * 0.08), 0.8)
        out[proc_id] = {
            "sources": sources,
            "edges": edges,
            "support_bonus": round(support_bonus, 4),
        }
    return out
