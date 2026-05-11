"""Deterministic late reranking for procedure candidates."""

from __future__ import annotations

from typing import Any


def rerank_procedure_candidates(
    candidates: list[dict[str, Any]],
    evidence: dict[int, dict[str, Any]],
    *,
    benchmark_mode: bool = False,
) -> list[dict[str, Any]]:
    reranked: list[dict[str, Any]] = []
    for cand in candidates:
        proc_id = int(cand["id"])
        ev = evidence.get(proc_id) or {}
        bonus = float(ev.get("support_bonus") or 0.0)
        base = float(cand.get("final_score") or 0.0)
        status = cand.get("status") or "active"
        status_multiplier = {
            "active": 1.0,
            "candidate": 0.9,
            "needs_review": 0.72,
            "stale": 0.64,
            "superseded": 0.3,
            "retired": 0.1,
        }.get(status, 1.0)
        if benchmark_mode:
            score = base * status_multiplier
        else:
            score = (base + bonus) * status_multiplier
        updated = dict(cand)
        updated["supporting_evidence"] = ev.get("sources") or []
        updated["evidence_edges"] = ev.get("edges") or []
        updated["evidence_bonus"] = round(bonus, 4)
        updated["final_score"] = round(score, 6)
        updated["why_retrieved"] = updated.get("why_retrieved") or (
            "strong procedural evidence cluster" if bonus >= 0.3 else "direct procedural match"
        )
        reranked.append(updated)
    reranked.sort(key=lambda item: item.get("final_score", 0.0), reverse=True)
    return reranked
