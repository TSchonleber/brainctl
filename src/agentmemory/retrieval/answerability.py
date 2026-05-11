"""Grounded answerability gate."""

from __future__ import annotations

import re
from typing import Any

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "has", "have", "how", "i", "in", "is", "it", "its", "of",
    "on", "or", "that", "the", "to", "was", "we", "what", "when", "where",
    "which", "who", "why", "will", "with", "you", "did",
}
_LOW_SIGNAL_TOKENS = {
    "summary", "history", "timeline", "recent", "today", "yesterday", "tomorrow",
    "game", "issue", "problem", "thing", "stuff", "update",
}


def _normalize_token(token: str) -> str:
    tok = re.sub(r"[^a-z0-9]+", "", (token or "").lower())
    if len(tok) <= 2 or tok in _STOPWORDS:
        return ""
    if tok.endswith("ies") and len(tok) > 4:
        tok = tok[:-3] + "y"
    elif tok.endswith("ing") and len(tok) > 5:
        tok = tok[:-3]
    elif tok.endswith("ed") and len(tok) > 4:
        tok = tok[:-2]
    elif tok.endswith("es") and len(tok) > 4:
        tok = tok[:-2]
    elif tok.endswith("s") and len(tok) > 3:
        tok = tok[:-1]
    return tok


def _token_set(text: str) -> set[str]:
    return {
        norm
        for part in re.split(r"[^A-Za-z0-9]+", text or "")
        if (norm := _normalize_token(part))
    }


def _informative_tokens(text: str) -> set[str]:
    return {token for token in _token_set(text) if token not in _LOW_SIGNAL_TOKENS}


def assess_answerability(
    query: str,
    plan,
    buckets: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Estimate whether the current retrieval set is grounded enough to answer."""

    flat: list[dict[str, Any]] = []
    for rows in buckets.values():
        flat.extend(rows or [])
    flat.sort(key=lambda item: item.get("final_score", 0.0), reverse=True)

    if not flat:
        return {
            "score": 0.0,
            "abstain": True,
            "reason": "no_candidates",
            "top_margin": 0.0,
        }

    top = flat[0]
    second = flat[1] if len(flat) > 1 else None
    top_score = float(top.get("final_score") or 0.0)
    second_score = float(second.get("final_score") or 0.0) if second else 0.0
    margin = top_score - second_score

    query_tokens = _token_set(query)
    informative_query_tokens = _informative_tokens(query)
    top_text = " ".join(
        str(top.get(key) or "")
        for key in ("content", "summary", "title", "goal", "description", "search_text", "name", "compiled_truth", "observations", "aliases")
    )
    top_text_tokens = _token_set(top_text)
    top_informative_tokens = _informative_tokens(top_text)
    supporting_text = " ".join(
        " ".join(
            str(row.get(key) or "")
            for key in ("content", "summary", "title", "goal", "description", "search_text", "name", "compiled_truth", "observations", "aliases")
        )
        for row in flat[:3]
    )
    supporting_tokens = _token_set(supporting_text)
    supporting_informative_tokens = _informative_tokens(supporting_text)
    coverage = 0.0
    if query_tokens:
        coverage = len(query_tokens & supporting_tokens) / len(query_tokens)
    informative_coverage = 0.0
    if informative_query_tokens:
        informative_coverage = len(informative_query_tokens & supporting_informative_tokens) / len(informative_query_tokens)
    anchor_overlap = len(query_tokens & top_text_tokens)
    informative_anchor_overlap = len(informative_query_tokens & top_informative_tokens)
    evidence_diversity = len({
        row.get("type") or bucket_name.rstrip("s")
        for bucket_name, rows in buckets.items()
        for row in (rows or [])[:2]
    })
    direct_support = len(top.get("supporting_evidence") or [])
    stale_penalty = 0.25 if top.get("status") in {"stale", "needs_review", "superseded", "retired"} else 0.0
    strong_candidate_count = 0
    for row in flat[:5]:
        row_text = " ".join(
            str(row.get(key) or "")
            for key in ("content", "summary", "title", "goal", "description", "search_text", "name", "compiled_truth", "observations", "aliases")
        )
        row_tokens = _token_set(row_text)
        row_informative = _informative_tokens(row_text)
        row_coverage = len(query_tokens & row_tokens) / max(len(query_tokens), 1) if query_tokens else 0.0
        row_informative_coverage = (
            len(informative_query_tokens & row_informative) / max(len(informative_query_tokens), 1)
            if informative_query_tokens else row_coverage
        )
        if row_coverage >= 0.3 or row_informative_coverage >= 0.3:
            strong_candidate_count += 1

    score = (
        (top_score * 0.45)
        + (margin * 0.35)
        + (coverage * 0.45)
        + (informative_coverage * 0.35)
        + min(direct_support / 3.0, 1.0) * 0.15
        + min(evidence_diversity / 3.0, 1.0) * 0.1
        - stale_penalty
    )
    abstain = False
    reason = "grounded"
    grounded_consensus = strong_candidate_count >= 1 and top_score >= 0.85 and informative_anchor_overlap >= 1
    if informative_coverage < 0.34 and informative_anchor_overlap == 0 and direct_support == 0:
        abstain = True
        reason = "weak_informative_coverage"
    if informative_query_tokens and informative_anchor_overlap <= 1 and informative_coverage < 0.4 and top_score < 0.75:
        abstain = True
        reason = "weak_topical_anchor"
    if margin < 0.08 and informative_coverage < 0.5 and informative_anchor_overlap < 2 and plan.abstain_allowed:
        if strong_candidate_count < 2 and not grounded_consensus:
            abstain = True
            reason = "diffuse_candidates"
    if plan.abstain_allowed and score < 0.5 and informative_coverage < 0.5:
        if strong_candidate_count < 2 and not grounded_consensus:
            abstain = True
            reason = "low_answerability_score"
    if "summary" in (query or "").lower() and informative_anchor_overlap < 2 and informative_coverage < 0.45:
        abstain = True
        reason = "ungrounded_summary_request"

    return {
        "score": round(score, 4),
        "abstain": abstain,
        "reason": reason,
        "top_margin": round(margin, 4),
        "coverage": round(coverage, 4),
        "informative_coverage": round(informative_coverage, 4),
        "anchor_overlap": anchor_overlap,
        "informative_anchor_overlap": informative_anchor_overlap,
        "evidence_diversity": evidence_diversity,
        "direct_support": direct_support,
        "strong_candidate_count": strong_candidate_count,
    }
