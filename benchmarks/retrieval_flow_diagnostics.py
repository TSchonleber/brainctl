from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _binary_dcg(labels: list[float]) -> float:
    return sum(float(rel) / math.log2(index + 2) for index, rel in enumerate(labels))


def _ideal_dcg(gold_count: int, k: int) -> float:
    return _binary_dcg([1.0] * min(max(gold_count, 0), k))


def _rank_map(retrieved_ids: list[str]) -> dict[str, int]:
    return {item: index + 1 for index, item in enumerate(retrieved_ids)}


def _query_operator(question_type: str, question: str = "") -> str:
    text = f"{question_type} {question}".lower()
    if "temporal" in text or any(term in text for term in ("before", "after", "latest", "current", "recent")):
        return "temporal"
    if "update" in text or any(term in text for term in ("currently", "previously", "changed", "new ")):
        return "update_resolution"
    if "multi" in text or any(term in text for term in ("how many", "all ", "both ", "total")):
        return "set_coverage"
    if any(term in text for term in ("compare", "which", "most", "least")):
        return "comparison"
    return "single_fact"


def _step(name: str, ok: bool, detail: str) -> dict[str, str]:
    return {"step": name, "status": "pass" if ok else "fail", "detail": detail}


def classify_longmemeval_row(row: dict[str, Any], *, k: int = 5) -> dict[str, Any]:
    gold = _as_str_list(row.get("answer_session_ids"))
    retrieved = _as_str_list(row.get("top_session_ids") or row.get("retrieved_ids"))
    gold_set = set(gold)
    top_k = retrieved[:k]
    top_10 = retrieved[:10]
    ranks = _rank_map(retrieved)
    gold_ranks = {item: ranks[item] for item in gold if item in ranks}
    found_top_k = [item for item in top_k if item in gold_set]
    found_top_10 = [item for item in top_10 if item in gold_set]
    missing_top_k = [item for item in gold if item not in set(top_k)]
    missing_top_10 = [item for item in gold if item not in set(top_10)]
    labels_at_k = [1.0 if item in gold_set else 0.0 for item in top_k]
    dcg_at_k = float(row.get(f"dcg_at_{k}") or _binary_dcg(labels_at_k))
    idcg_at_k = float(row.get(f"idcg_at_{k}") or _ideal_dcg(len(gold), k))
    dcg_gap = max(idcg_at_k - dcg_at_k, 0.0)
    first_gold_rank = min(gold_ranks.values()) if gold_ranks else None
    top1_is_gold = bool(top_k and top_k[0] in gold_set)
    ideal_top_k_count = min(len(gold), k)

    has_retrieved = bool(retrieved)
    has_gold_top_10 = bool(found_top_10)
    has_gold_top_k = bool(found_top_k)
    has_full_top_k_coverage = len(found_top_k) >= ideal_top_k_count
    has_clean_top_k_order = top1_is_gold or not has_gold_top_k
    has_no_dcg_loss = dcg_gap <= 1e-9

    if not has_retrieved:
        first_failure = "candidate_generation_empty"
    elif not has_gold_top_10:
        first_failure = "candidate_generation_miss"
    elif not has_gold_top_k:
        first_failure = "top_k_admission_miss"
    elif not has_clean_top_k_order:
        first_failure = "top_k_ordering_loss"
    elif not has_full_top_k_coverage:
        first_failure = "set_coverage_loss"
    elif not has_no_dcg_loss:
        first_failure = "topheavy_dcg_loss"
    else:
        first_failure = "success"

    steps = [
        _step("query_shape", True, _query_operator(str(row.get("question_type", "")), str(row.get("question", "")))),
        _step("candidate_generation", has_retrieved, f"retrieved={len(retrieved)}"),
        _step("gold_recall_at_10", has_gold_top_10, f"found={len(found_top_10)} missing={len(missing_top_10)}"),
        _step("top_k_admission", has_gold_top_k, f"k={k} found={len(found_top_k)} first_gold_rank={first_gold_rank}"),
        _step("top_k_ordering", has_clean_top_k_order, f"top1={top_k[0] if top_k else None}"),
        _step("set_coverage", has_full_top_k_coverage, f"found={len(found_top_k)} ideal={ideal_top_k_count}"),
        _step("dcg_realization", has_no_dcg_loss, f"dcg_gap={round(dcg_gap, 4)}"),
    ]

    return {
        "benchmark": "longmemeval",
        "question_id": str(row.get("question_id", "")),
        "question_type": str(row.get("question_type", "")),
        "query_operator": _query_operator(str(row.get("question_type", "")), str(row.get("question", ""))),
        "first_failure": first_failure,
        "steps": steps,
        "gold_ids": gold,
        "retrieved_ids": retrieved,
        "top_k_ids": top_k,
        "gold_ranks": gold_ranks,
        "missing_top_k": missing_top_k,
        "missing_top_10": missing_top_10,
        "dcg_gap_at_5": round(max(float(row.get("idcg_at_5") or _ideal_dcg(len(gold), 5)) - float(row.get("dcg_at_5") or _binary_dcg([1.0 if item in gold_set else 0.0 for item in retrieved[:5]])), 0.0), 4),
        "dcg_gap_at_10": round(max(float(row.get("idcg_at_10") or _ideal_dcg(len(gold), 10)) - float(row.get("dcg_at_10") or _binary_dcg([1.0 if item in gold_set else 0.0 for item in retrieved[:10]])), 0.0), 4),
        "ndcg_at_5": row.get("ndcg_at_5"),
        "ndcg_at_10": row.get("ndcg_at_10"),
        "question": row.get("question", ""),
    }


def classify_locomo_row(row: dict[str, Any], *, k: int = 10) -> dict[str, Any]:
    gold = _as_str_list(row.get("evidence_ids"))
    retrieved = _as_str_list(row.get("retrieved_ids"))
    gold_set = set(gold)
    top_k = retrieved[:k]
    ranks = _rank_map(retrieved)
    gold_ranks = {item: ranks[item] for item in gold if item in ranks}
    found_top_k = [item for item in top_k if item in gold_set]
    missing_top_k = [item for item in gold if item not in set(top_k)]
    recall = float(row.get("recall", 1.0) or 0.0)
    category = str(row.get("category_name") or row.get("category") or "")

    has_retrieved = bool(retrieved)
    has_gold_top_k = bool(found_top_k) or not gold
    has_full_top_k_coverage = recall >= 1.0

    if not has_retrieved:
        first_failure = "candidate_generation_empty"
    elif gold and not has_gold_top_k:
        first_failure = "candidate_generation_miss"
    elif not has_full_top_k_coverage:
        first_failure = "set_coverage_loss"
    else:
        first_failure = "success"

    steps = [
        _step("query_shape", True, _query_operator(category, str(row.get("question", "")))),
        _step("candidate_generation", has_retrieved, f"retrieved={len(retrieved)}"),
        _step("gold_recall_at_k", has_gold_top_k, f"k={k} found={len(found_top_k)} missing={len(missing_top_k)}"),
        _step("set_coverage", has_full_top_k_coverage, f"recall={round(recall, 4)}"),
    ]
    return {
        "benchmark": "locomo",
        "question_id": str(row.get("sample_id", "")),
        "question_type": category,
        "query_operator": _query_operator(category, str(row.get("question", ""))),
        "first_failure": first_failure,
        "steps": steps,
        "gold_ids": gold,
        "retrieved_ids": retrieved,
        "top_k_ids": top_k,
        "gold_ranks": gold_ranks,
        "missing_top_k": missing_top_k,
        "recall": round(recall, 4),
        "dcg_gap_at_5": float(row.get("dcg_gap_at_5") or 0.0),
        "dcg_gap_at_10": float(row.get("dcg_gap_at_10") or 0.0),
        "question": row.get("question", ""),
    }


def classify_membench_row(row: dict[str, Any], *, k: int = 5) -> dict[str, Any]:
    gold = _as_str_list(row.get("target_ids"))
    retrieved = _as_str_list(row.get("retrieved_ids"))
    top_k = retrieved[:k]
    gold_set = set(gold)
    hit = bool(row.get("hit_at_k"))
    found = [item for item in top_k if item in gold_set]
    first_failure = "success" if hit else "candidate_generation_miss"
    return {
        "benchmark": "membench",
        "question_id": str(row.get("tid", "")),
        "question_type": str(row.get("category") or row.get("topic") or ""),
        "query_operator": "single_fact",
        "first_failure": first_failure,
        "steps": [
            _step("query_shape", True, "single_fact"),
            _step("candidate_generation", bool(retrieved), f"retrieved={len(retrieved)}"),
            _step("top_k_admission", hit, f"k={k} found={len(found)}"),
        ],
        "gold_ids": gold,
        "retrieved_ids": retrieved,
        "top_k_ids": top_k,
        "gold_ranks": {item: _rank_map(retrieved)[item] for item in gold if item in set(retrieved)},
        "missing_top_k": [item for item in gold if item not in set(top_k)],
        "question": row.get("question", ""),
    }


def summarize_flow(classifications: list[dict[str, Any]], *, top_n: int = 20) -> dict[str, Any]:
    first_failures = Counter(item["first_failure"] for item in classifications)
    failed_steps: Counter[str] = Counter()
    by_operator = Counter(item.get("query_operator", "") for item in classifications if item["first_failure"] != "success")
    by_type = Counter(item.get("question_type", "") for item in classifications if item["first_failure"] != "success")
    dcg_gap_by_failure: dict[str, float] = defaultdict(float)

    for item in classifications:
        for step in item.get("steps", []):
            if step.get("status") == "fail":
                failed_steps[step.get("step", "")] += 1
        dcg_gap_by_failure[item["first_failure"]] += float(item.get("dcg_gap_at_5") or 0.0)

    examples = sorted(
        [item for item in classifications if item["first_failure"] != "success"],
        key=lambda item: (float(item.get("dcg_gap_at_5") or 0.0), len(item.get("missing_top_k") or [])),
        reverse=True,
    )[:top_n]

    return {
        "total": len(classifications),
        "success": first_failures.get("success", 0),
        "failed": len(classifications) - first_failures.get("success", 0),
        "by_first_failure": dict(first_failures.most_common()),
        "by_failed_step": dict(failed_steps.most_common()),
        "by_query_operator": dict(by_operator.most_common()),
        "by_question_type": dict(by_type.most_common()),
        "dcg_gap_at_5_by_first_failure": {
            key: round(value, 4)
            for key, value in sorted(dcg_gap_by_failure.items(), key=lambda pair: pair[1], reverse=True)
            if value
        },
        "top_examples": [
            {
                "question_id": item.get("question_id"),
                "question_type": item.get("question_type"),
                "query_operator": item.get("query_operator"),
                "first_failure": item.get("first_failure"),
                "dcg_gap_at_5": item.get("dcg_gap_at_5"),
                "ndcg_at_5": item.get("ndcg_at_5"),
                "recall": item.get("recall"),
                "gold_ids": item.get("gold_ids"),
                "top_k_ids": item.get("top_k_ids"),
                "missing_top_k": item.get("missing_top_k"),
                "question": item.get("question", ""),
            }
            for item in examples
        ],
    }


def analyze_retrieval_flow(
    *,
    longmemeval_rows: list[dict[str, Any]] | None = None,
    locomo_rows: list[dict[str, Any]] | None = None,
    membench_rows: list[dict[str, Any]] | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    long_items = [classify_longmemeval_row(row) for row in (longmemeval_rows or [])]
    locomo_items = [classify_locomo_row(row) for row in (locomo_rows or [])]
    membench_items = [classify_membench_row(row) for row in (membench_rows or [])]
    return {
        "longmemeval": summarize_flow(long_items, top_n=top_n),
        "locomo": summarize_flow(locomo_items, top_n=top_n),
        "membench": summarize_flow(membench_items, top_n=top_n),
    }


def render_markdown_report(payload: dict[str, Any]) -> str:
    lines = ["# Retrieval Flow Failure Report", ""]
    for benchmark in ("longmemeval", "locomo", "membench"):
        section = payload.get(benchmark) or {}
        lines.extend(
            [
                f"## {benchmark}",
                "",
                f"- total: {section.get('total', 0)}",
                f"- success: {section.get('success', 0)}",
                f"- failed: {section.get('failed', 0)}",
                f"- first failures: {section.get('by_first_failure', {})}",
                f"- failed steps: {section.get('by_failed_step', {})}",
                f"- query operators: {section.get('by_query_operator', {})}",
                "",
            ]
        )
        examples = section.get("top_examples") or []
        if examples:
            lines.append("| first_failure | type | gap@5 | id | missing | top_k |")
            lines.append("|---|---:|---:|---|---|---|")
            for item in examples[:10]:
                lines.append(
                    "| {first_failure} | {question_type} | {dcg_gap_at_5} | {question_id} | {missing} | {top} |".format(
                        first_failure=item.get("first_failure"),
                        question_type=item.get("question_type"),
                        dcg_gap_at_5=item.get("dcg_gap_at_5"),
                        question_id=item.get("question_id"),
                        missing=", ".join(_as_str_list(item.get("missing_top_k")))[:80],
                        top=", ".join(_as_str_list(item.get("top_k_ids")))[:100],
                    )
                )
            lines.append("")
    return "\n".join(lines)
