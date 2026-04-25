"""Debug payload builders for retrieval executive output."""

from __future__ import annotations

from typing import Any


def build_debug_payload(
    *,
    query_plan: dict[str, Any],
    procedure_debug: dict[str, Any] | None,
    answerability: dict[str, Any] | None,
    second_stage: dict[str, Any] | None = None,
    top_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query_plan": query_plan,
    }
    if procedure_debug:
        payload["procedures"] = procedure_debug
    if second_stage:
        payload["second_stage"] = second_stage
    if answerability:
        payload["answerability"] = answerability
    if top_candidates is not None:
        payload["top_candidates"] = [
            {
                "type": cand.get("type"),
                "id": cand.get("id"),
                "final_score": cand.get("final_score"),
                "pre_second_stage_score": cand.get("pre_second_stage_score"),
                "second_stage_heuristic": cand.get("second_stage_heuristic"),
                "second_stage_mlp": cand.get("second_stage_mlp"),
                "second_stage_judge": cand.get("second_stage_judge"),
                "second_stage_slate_score": cand.get("second_stage_slate_score"),
                "second_stage_slate_terms": cand.get("second_stage_slate_terms"),
                "why_retrieved": cand.get("why_retrieved"),
                "feature_summary": cand.get("second_stage_features"),
                "text": cand.get("content") or cand.get("summary") or cand.get("title") or cand.get("goal") or cand.get("name"),
            }
            for cand in top_candidates[:5]
        ]
    return payload
