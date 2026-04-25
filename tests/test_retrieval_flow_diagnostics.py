from __future__ import annotations

from benchmarks.analyze_benchmark_failures import _metric
from benchmarks.retrieval_flow_diagnostics import (
    analyze_retrieval_flow,
    classify_locomo_row,
    classify_longmemeval_row,
    classify_membench_row,
)


def test_longmemeval_classifies_top_k_ordering_loss():
    row = {
        "question_id": "q1",
        "question_type": "multi-session",
        "answer_session_ids": ["gold_a", "gold_b"],
        "top_session_ids": ["noise", "gold_a", "gold_b", "other"],
    }

    flow = classify_longmemeval_row(row)

    assert flow["first_failure"] == "top_k_ordering_loss"
    assert flow["gold_ranks"] == {"gold_a": 2, "gold_b": 3}
    assert any(step["step"] == "top_k_ordering" and step["status"] == "fail" for step in flow["steps"])


def test_longmemeval_classifies_candidate_generation_miss():
    row = {
        "question_id": "q2",
        "question_type": "single-session-user",
        "answer_session_ids": ["gold"],
        "top_session_ids": ["noise_1", "noise_2"],
    }

    flow = classify_longmemeval_row(row)

    assert flow["first_failure"] == "candidate_generation_miss"
    assert flow["missing_top_10"] == ["gold"]


def test_locomo_classifies_set_coverage_loss():
    row = {
        "sample_id": "s1",
        "category_name": "Temporal-inference",
        "question": "What happened after the appointment?",
        "evidence_ids": ["session_1", "session_3"],
        "retrieved_ids": ["session_1", "session_2"],
        "recall": 0.5,
    }

    flow = classify_locomo_row(row)

    assert flow["first_failure"] == "set_coverage_loss"
    assert flow["missing_top_k"] == ["session_3"]
    assert flow["query_operator"] == "temporal"


def test_membench_classifies_hit_and_miss():
    hit = classify_membench_row(
        {
            "tid": 1,
            "target_ids": ["119"],
            "retrieved_ids": ["119", "120"],
            "hit_at_k": True,
        }
    )
    miss = classify_membench_row(
        {
            "tid": 2,
            "target_ids": ["119"],
            "retrieved_ids": ["120", "121"],
            "hit_at_k": False,
        }
    )

    assert hit["first_failure"] == "success"
    assert miss["first_failure"] == "candidate_generation_miss"


def test_analyze_retrieval_flow_summarizes_failures():
    payload = analyze_retrieval_flow(
        longmemeval_rows=[
            {
                "question_id": "q1",
                "question_type": "multi-session",
                "answer_session_ids": ["gold"],
                "top_session_ids": ["noise", "gold"],
            }
        ],
        locomo_rows=[],
        membench_rows=[],
    )

    assert payload["longmemeval"]["failed"] == 1
    assert payload["longmemeval"]["by_first_failure"]["top_k_ordering_loss"] == 1


def test_analyzer_metric_preserves_zero_values():
    assert _metric({"recall": 0.0}, "recall", 1.0) == 0.0
    assert _metric({}, "recall", 1.0) == 1.0
