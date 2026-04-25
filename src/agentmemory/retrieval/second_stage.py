"""Shared second-stage reranking across retrieval buckets."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

from agentmemory.retrieval.feature_builder import (
    FEATURE_VERSION_V1,
    build_features,
    vectorize_features,
)
from agentmemory.retrieval.judge import JudgeConfig, judge_candidates
from agentmemory.retrieval.mlp_reranker import DEFAULT_MODEL_PATH, TinyMLPModel

_BUCKET_TYPE_MAP = {
    "procedures": "procedure",
    "memories": "memory",
    "events": "event",
    "context": "context",
    "entities": "entity",
    "decisions": "decision",
}
_SESSION_RE = re.compile(
    r"(?:^|[|_\s-])(?:sid|session|s)[=_ :#-]*(\d+)|\bsession[_ :#-]*(\d+)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.:-]+\b")
_SOURCE_NUM_SUFFIX_RE = re.compile(r"^(.+?)[_-](\d+)$")


def _resolve_benchmark_ranking_mode(args: Any) -> str:
    mode = str(
        getattr(args, "benchmark_ranking_mode", None)
        or os.environ.get("BRAINCTL_BENCHMARK_RANKING_MODE", "raw")
        or "raw"
    ).strip().lower()
    return mode if mode in {"full", "raw"} else "raw"


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class SecondStageCandidate:
    bucket: str
    original_index: int
    row: dict[str, Any]


@dataclass(slots=True)
class SecondStageConfig:
    enabled: bool = True
    top_n: int = 10
    heuristic_weight: float = 0.62
    mlp_weight: float = 0.28
    judge_weight: float = 0.10
    model_path: str | None = None
    model_enabled: bool = True
    ranking_mode: str = "live"
    judge: JudgeConfig = field(default_factory=JudgeConfig)

    @classmethod
    def from_args(cls, args: Any) -> "SecondStageConfig":
        benchmark = bool(getattr(args, "benchmark", False))
        ranking_mode = _resolve_benchmark_ranking_mode(args) if benchmark else "live"
        requested = bool(getattr(args, "second_stage", False)) or _env_flag("BRAINCTL_SECOND_STAGE")
        judge_enabled = bool(getattr(args, "judge_rerank", None))
        judge_provider = str(getattr(args, "judge_rerank", "ollama") or "ollama")
        judge_model = str(getattr(args, "judge_model", "llama3.2:3b") or "llama3.2:3b")
        top_n = getattr(args, "second_stage_top_n", None)
        if top_n is None:
            try:
                top_n = int(os.environ.get("BRAINCTL_SECOND_STAGE_TOP_N", "10"))
            except (TypeError, ValueError):
                top_n = 10
        return cls(
            enabled=requested and not bool(getattr(args, "no_second_stage", False)) and not (benchmark and ranking_mode == "raw"),
            top_n=max(int(top_n or 10), 1),
            model_enabled=not bool(getattr(args, "no_second_stage_model", False)),
            model_path=getattr(args, "second_stage_model_path", None),
            ranking_mode=ranking_mode,
            judge=JudgeConfig(
                enabled=judge_enabled,
                provider=judge_provider,
                model=judge_model,
                top_k=max(min(int(getattr(args, "judge_top_k", 5) or 5), 5), 1),
            ),
        )


def _heuristic_score(plan: Any, features: dict[str, float]) -> float:
    intent = str(getattr(plan, "normalized_intent", "factual") or "factual")
    score = (
        features["base_score"] * 0.24
        + features["informative_overlap"] * 0.23
        + features["tfidf_cosine"] * 0.20
        + features["query_overlap"] * 0.07
        + features["intent_bucket_fit"] * 0.08
        + features["entity_overlap"] * 0.06
        + features["alias_overlap"] * 0.04
        + features["exact_phrase"] * 0.05
        + features["support_evidence_score"] * 0.03
    )
    long_context_reliable = (
        features.get("long_context_applicable", 0.0) > 0.0
        and features.get("long_context_focused_program", 0.0) > 0.0
        and features.get("long_context_confidence", 0.0) >= 0.62
        and features.get("long_context_uncertainty", 0.0) <= 0.38
    )
    if long_context_reliable:
        score += (
            features.get("long_context_score", 0.0) * 0.09
            + features.get("long_context_confidence", 0.0) * 0.03
            + features.get("long_context_agreement", 0.0) * 0.02
            + features.get("long_context_coverage", 0.0) * 0.03
            + features.get("long_context_precision", 0.0) * 0.02
        )
    if features["query_temporal"] > 0:
        score += (
            features["candidate_temporal"] * 0.04
            + features["temporal_anchor_overlap"] * 0.08
            + features["session_gap_score"] * 0.06
        )
        if long_context_reliable:
            score += features.get("long_context_score", 0.0) * 0.05
    if features.get("query_needs_ordering", 0.0) > 0.0:
        score += features["temporal_anchor_overlap"] * 0.05 + features["session_gap_score"] * 0.05
    if features.get("query_needs_update_resolution", 0.0) > 0.0:
        score += features["status_active"] * 0.04
    if features.get("query_needs_role_fact", 0.0) > 0.0:
        score += (
            features.get("role_overlap", 0.0) * 0.11
            + features.get("attribute_overlap", 0.0) * 0.10
            + features.get("role_value_pattern", 0.0) * 0.08
            + features.get("exact_phrase", 0.0) * 0.03
        )
    if features.get("query_needs_synthetic_key_value", 0.0) > 0.0:
        score += features["source_keyword"] * 0.04 + features.get("attribute_overlap", 0.0) * 0.05
    if intent in {"temporal", "decision"}:
        score += features["bucket_events"] * 0.04 + features["bucket_decisions"] * 0.03
    if intent in {"procedural", "troubleshooting"}:
        score += features["bucket_procedures"] * 0.06 + features["procedural_candidate"] * 0.04
        if long_context_reliable:
            score += features.get("long_context_confidence", 0.0) * 0.04
    if intent == "factual":
        score += features["bucket_memories"] * 0.05 + features["bucket_entities"] * 0.04
        score -= features["bucket_procedures"] * 0.04
        if long_context_reliable:
            score += features.get("long_context_precision", 0.0) * 0.04
    if features["source_graph"] > 0:
        score -= 0.08
    if features["status_stale"] > 0:
        score -= 0.12
    if features["status_needs_review"] > 0:
        score -= 0.08
    return max(min(score, 1.0), 0.0)


def _candidate_text(candidate: dict[str, Any]) -> str:
    for key in ("content", "summary", "title", "goal", "description", "name", "search_text"):
        value = candidate.get(key)
        if value:
            return str(value)
    return ""


def _candidate_source_family(candidate: dict[str, Any]) -> str:
    raw = (
        candidate.get("doc_id")
        or candidate.get("source_doc_id")
        or candidate.get("source_key")
        or candidate.get("external_id")
        or ""
    )
    head = str(raw).split("|", 1)[0]
    match = _SOURCE_NUM_SUFFIX_RE.match(head)
    return match.group(1) if match else head


def _candidate_cluster_keys(plan: Any, candidate: dict[str, Any]) -> set[str]:
    text = _candidate_text(candidate)
    keys: set[str] = set()
    family = _candidate_source_family(candidate)
    if family:
        keys.add(f"family:{family}")
    for match in _SESSION_RE.finditer(text):
        keys.add(f"session:{match.group(1) or match.group(2)}")
    if getattr(plan, "requires_temporal_reasoning", False) or getattr(plan, "needs_ordering", False):
        for match in _DATE_RE.finditer(text):
            keys.add(f"date:{match.group(0).lower()}")
    target_entities = {
        str(value).lower()
        for value in (getattr(plan, "target_entities", None) or [])
        if value
    }
    if target_entities:
        lowered = text.lower()
        for entity in target_entities:
            if entity and entity in lowered:
                keys.add(f"entity:{entity}")
    observed_entities = {
        match.group(0).lower()
        for match in _ENTITY_RE.finditer(text)
        if len(match.group(0)) > 2
    }
    for entity in sorted(observed_entities)[:3]:
        keys.add(f"obs:{entity}")
    if not keys:
        ident = candidate.get("id")
        keys.add(f"row:{candidate.get('bucket')}:{ident}")
    return keys


def _slate_score(
    *,
    plan: Any,
    candidate: dict[str, Any],
    features: dict[str, float],
    composite_score: float,
    rank_index: int,
    selected_keys: set[str],
) -> tuple[float, dict[str, float]]:
    rank_discount = 1.0 / math.log2(rank_index + 2)
    cluster_keys = _candidate_cluster_keys(plan, candidate)
    new_keys = cluster_keys - selected_keys
    coverage_bonus = 0.0
    redundancy_penalty = 0.0
    update_penalty = 0.0
    temporal_penalty = 0.0
    localization_bonus = 0.0

    if getattr(plan, "needs_set_coverage", False):
        coverage_bonus += min(0.20, 0.05 * len(new_keys))
        if not new_keys and selected_keys:
            redundancy_penalty += 0.11
    elif selected_keys and not new_keys:
        redundancy_penalty += 0.03

    if getattr(plan, "needs_update_resolution", False):
        if features.get("status_stale", 0.0) > 0.0:
            update_penalty += 0.08
        if features.get("status_needs_review", 0.0) > 0.0:
            update_penalty += 0.05
        if features.get("status_active", 0.0) > 0.0:
            coverage_bonus += 0.02

    if getattr(plan, "requires_temporal_reasoning", False) or getattr(plan, "needs_ordering", False):
        if features.get("candidate_temporal", 0.0) <= 0.0 and features.get("temporal_anchor_overlap", 0.0) <= 0.0:
            temporal_penalty += 0.05
        else:
            coverage_bonus += features.get("temporal_anchor_overlap", 0.0) * 0.03

    if getattr(plan, "needs_role_fact", False):
        coverage_bonus += features.get("role_overlap", 0.0) * 0.04
        coverage_bonus += features.get("attribute_overlap", 0.0) * 0.04
        coverage_bonus += features.get("role_value_pattern", 0.0) * 0.03

    if features.get("long_context_focused_program", 0.0) > 0.0:
        localization_bonus += (
            features.get("long_context_precision", 0.0) * 0.018
            + features.get("long_context_coverage", 0.0) * 0.014
        )

    slate_adjustment = (coverage_bonus + localization_bonus - redundancy_penalty - update_penalty - temporal_penalty) * rank_discount
    return (
        composite_score + slate_adjustment,
        {
            "coverage_bonus": round(coverage_bonus, 6),
            "localization_bonus": round(localization_bonus, 6),
            "redundancy_penalty": round(redundancy_penalty, 6),
            "update_penalty": round(update_penalty, 6),
            "temporal_penalty": round(temporal_penalty, 6),
            "rank_discount": round(rank_discount, 6),
            "new_key_count": float(len(new_keys)),
        },
    )


def _rerank_slate(
    *,
    plan: Any,
    head: list[dict[str, Any]],
    feature_rows: list[dict[str, float]],
    heuristic_scores: list[float],
    mlp_scores: list[float],
    judge_scores: list[float],
    cfg: SecondStageConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_weight = max(0.0, 1.0 - cfg.heuristic_weight - cfg.mlp_weight - cfg.judge_weight)
    pool: list[dict[str, Any]] = []
    debug_candidates: list[dict[str, Any]] = []
    for candidate, features, heuristic_score, mlp_score, judge_score in zip(
        head,
        feature_rows,
        heuristic_scores,
        mlp_scores,
        judge_scores,
    ):
        pre_score = float(candidate.get("final_score") or candidate.get("retrieval_score") or 0.0)
        composite_score = (
            pre_score * base_weight
            + heuristic_score * cfg.heuristic_weight
            + float(mlp_score) * cfg.mlp_weight
            + float(judge_score) * cfg.judge_weight
        )
        candidate["pre_second_stage_score"] = round(pre_score, 8)
        candidate["second_stage_heuristic"] = round(heuristic_score, 6)
        candidate["second_stage_mlp"] = round(float(mlp_score), 6)
        candidate["second_stage_judge"] = round(float(judge_score), 6)
        candidate["second_stage_features"] = {
            key: features.get(key)
            for key in (
                "informative_overlap",
                "tfidf_cosine",
                "entity_overlap",
                "temporal_anchor_overlap",
                "intent_bucket_fit",
                "session_gap_score",
                "query_needs_counting",
                "query_needs_comparison",
                "query_needs_ordering",
                "query_needs_update_resolution",
                "query_needs_set_coverage",
                "query_needs_role_fact",
                "query_needs_synthetic_key_value",
                "role_overlap",
                "attribute_overlap",
                "role_value_pattern",
                "long_context_score",
                "long_context_confidence",
                "long_context_agreement",
                "long_context_uncertainty",
                "long_context_focused_program",
            )
        }
        long_context_debug = candidate.pop("_long_context_debug", None) or {}
        if long_context_debug.get("applicable"):
            candidate["second_stage_features"]["long_context_program"] = long_context_debug.get("program")
            candidate["second_stage_features"]["long_context_excerpt"] = long_context_debug.get("top_chunk_excerpt")
        pool.append(
            {
                "candidate": candidate,
                "features": features,
                "composite_score": round(composite_score, 8),
                "cluster_keys": _candidate_cluster_keys(plan, candidate),
            }
        )

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    rank_index = 0
    while pool:
        best_idx = 0
        best_score = None
        best_terms: dict[str, float] | None = None
        for idx, item in enumerate(pool):
            slate_score, terms = _slate_score(
                plan=plan,
                candidate=item["candidate"],
                features=item["features"],
                composite_score=float(item["composite_score"]),
                rank_index=rank_index,
                selected_keys=selected_keys,
            )
            if best_score is None or slate_score > best_score:
                best_idx = idx
                best_score = slate_score
                best_terms = terms
        item = pool.pop(best_idx)
        candidate = item["candidate"]
        terms = best_terms or {}
        candidate["second_stage_slate_score"] = round(float(best_score or 0.0), 6)
        candidate["second_stage_slate_terms"] = terms
        selected.append(candidate)
        selected_keys.update(item["cluster_keys"])
        rank_index += 1

    debug_candidates = []
    for index, candidate in enumerate(selected, start=1):
        epsilon = max(len(selected) - index, 0) * 1e-6
        candidate["final_score"] = round(float(candidate.get("second_stage_slate_score") or 0.0) + epsilon, 8)
        debug_candidates.append(
            {
                "bucket": candidate.get("bucket"),
                "id": candidate.get("id"),
                "pre_score": round(float(candidate.get("pre_second_stage_score") or 0.0), 6),
                "heuristic": round(float(candidate.get("second_stage_heuristic") or 0.0), 6),
                "mlp": round(float(candidate.get("second_stage_mlp") or 0.0), 6),
                "judge": round(float(candidate.get("second_stage_judge") or 0.0), 6),
                "composite": round(float(candidate.get("second_stage_slate_score") or 0.0), 6),
                "selection_rank": index,
                "slate_terms": candidate.get("second_stage_slate_terms") or {},
                "features": candidate.get("second_stage_features") or {},
            }
        )
    return selected, debug_candidates


def rerank_top_candidates(
    query: str,
    plan: Any,
    candidates: list[dict[str, Any]],
    config: SecondStageConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rerank a flat candidate list using heuristic + tiny MLP + optional judge."""

    cfg = config or SecondStageConfig()
    if not cfg.enabled or not candidates:
        return candidates, {"enabled": False}

    head = [dict(candidate) for candidate in candidates[: cfg.top_n]]
    tail = [dict(candidate) for candidate in candidates[cfg.top_n :]]
    hard_query = any(
        bool(getattr(plan, attr, False))
        for attr in (
            "requires_temporal_reasoning",
            "requires_multi_hop",
            "needs_counting",
            "needs_comparison",
            "needs_ordering",
            "needs_update_resolution",
            "needs_set_coverage",
            "needs_role_fact",
            "needs_synthetic_key_value",
        )
    )
    raw_head_scores = [
        float(candidate.get("final_score") or candidate.get("retrieval_score") or 0.0)
        for candidate in head[:2]
    ]
    top_margin = abs(raw_head_scores[0] - raw_head_scores[1]) if len(raw_head_scores) >= 2 else 1.0
    if not hard_query and top_margin >= 0.08:
        passthrough = [dict(candidate) for candidate in candidates]
        for candidate in passthrough[: cfg.top_n]:
            pre_score = float(candidate.get("final_score") or candidate.get("retrieval_score") or 0.0)
            candidate.setdefault("pre_second_stage_score", round(pre_score, 8))
        return passthrough, {
            "enabled": True,
            "top_n": cfg.top_n,
            "ranking_mode": cfg.ranking_mode,
            "model_enabled": cfg.model_enabled,
            "model_path": str(cfg.model_path or DEFAULT_MODEL_PATH),
            "model_loaded": False,
            "judge_enabled": cfg.judge.enabled,
            "strategy": "passthrough_easy_query",
            "top_margin": round(top_margin, 6),
            "candidates": [],
        }
    for idx, candidate in enumerate(head):
        candidate["_stage_position"] = idx
        candidate.setdefault("bucket", candidate.get("type") or "memories")

    feature_rows: list[dict[str, float]] = []
    leader_score = head[0].get("final_score") if head else None
    for idx, candidate in enumerate(head):
        prev_score = head[idx - 1].get("final_score") if idx > 0 else None
        next_score = head[idx + 1].get("final_score") if idx + 1 < len(head) else None
        features = build_features(
            query,
            plan,
            candidate,
            neighbors={"prev_score": prev_score, "next_score": next_score, "leader_score": leader_score},
        )
        feature_rows.append(features)

    heuristic_scores = [_heuristic_score(plan, features) for features in feature_rows]

    model = TinyMLPModel.try_load(cfg.model_path or DEFAULT_MODEL_PATH) if cfg.model_enabled else None
    if model is not None:
        feature_matrix = [vectorize_features(features, feature_version=FEATURE_VERSION_V1) for features in feature_rows]
        mlp_scores = model.score(feature_matrix)
    else:
        mlp_scores = [0.0] * len(head)

    judge_scores = judge_candidates(query, head, cfg.judge)
    if judge_scores and len(judge_scores) < len(head):
        judge_scores = list(judge_scores) + [0.0] * (len(head) - len(judge_scores))
    elif not judge_scores:
        judge_scores = [0.0] * len(head)

    head, debug_candidates = _rerank_slate(
        plan=plan,
        head=head,
        feature_rows=feature_rows,
        heuristic_scores=heuristic_scores,
        mlp_scores=mlp_scores,
        judge_scores=judge_scores,
        cfg=cfg,
    )
    reranked = head + tail
    debug = {
        "enabled": True,
        "top_n": cfg.top_n,
        "ranking_mode": cfg.ranking_mode,
        "model_enabled": cfg.model_enabled,
        "model_path": str(cfg.model_path or DEFAULT_MODEL_PATH),
        "model_loaded": model is not None,
        "judge_enabled": cfg.judge.enabled,
        "base_weight": round(max(0.0, 1.0 - cfg.heuristic_weight - cfg.mlp_weight - cfg.judge_weight), 4),
        "mlp_weight": round(cfg.mlp_weight, 4),
        "judge_weight": round(cfg.judge_weight, 4),
        "strategy": "listwise_greedy_slate",
        "candidates": debug_candidates,
    }
    return reranked, debug


def rerank_bucketed_results(
    query: str,
    plan: Any,
    buckets: dict[str, list[dict[str, Any]]],
    config: SecondStageConfig | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Apply second-stage reranking to the combined head across all buckets."""

    cfg = config or SecondStageConfig()
    if not cfg.enabled:
        return buckets, {"enabled": False}

    ordered: list[SecondStageCandidate] = []
    for bucket_name in ("procedures", "memories", "events", "context", "entities", "decisions"):
        rows = buckets.get(bucket_name) or []
        for idx, row in enumerate(rows):
            candidate = dict(row)
            candidate["bucket"] = bucket_name
            candidate["type"] = str(candidate.get("type") or _BUCKET_TYPE_MAP.get(bucket_name, bucket_name))
            ordered.append(SecondStageCandidate(bucket_name, idx, candidate))
    ordered.sort(key=lambda item: item.row.get("final_score", 0.0), reverse=True)

    reranked_rows, debug = rerank_top_candidates(
        query,
        plan,
        [item.row for item in ordered],
        config=cfg,
    )
    scored: dict[tuple[str, Any], dict[str, Any]] = {}
    for row in reranked_rows:
        scored[(str(row.get("bucket") or "memories"), row.get("id"))] = row

    updated: dict[str, list[dict[str, Any]]] = {name: [] for name in buckets}
    for bucket_name, rows in buckets.items():
        updated_rows: list[dict[str, Any]] = []
        for row in rows or []:
            updated_rows.append(scored.get((bucket_name, row.get("id")), row))
        updated_rows.sort(key=lambda item: item.get("final_score", 0.0), reverse=True)
        updated[bucket_name] = updated_rows
    return updated, debug
