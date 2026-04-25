"""Candidate generation for procedure-aware retrieval."""

from __future__ import annotations

import sqlite3
from typing import Any

from agentmemory import procedural
from .query_planner import QueryPlan


def generate_procedure_candidates(
    conn: sqlite3.Connection,
    query: str,
    plan: QueryPlan,
    *,
    limit: int = 10,
    scope: str | None = None,
) -> dict[str, Any]:
    """Search procedures and attach minimal diagnostics."""

    if "procedures" not in plan.candidate_tables:
        return {"candidates": [], "debug": {"skipped": "procedures_not_in_plan"}}

    search = procedural.search_procedures(
        conn,
        query,
        limit=max(limit * 3, 12),
        scope=scope,
        debug=True,
    )
    candidates = search.get("procedures", [])
    for cand in candidates:
        cand.setdefault("type", "procedure")
        cand.setdefault("source", "procedure_fts")
    return {
        "candidates": candidates,
        "debug": {
            "query": query,
            "count": len(candidates),
            **(search.get("debug") or {}),
        },
    }
