from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.retrieval.query_planner import plan_query
from agentmemory.retrieval.second_stage import SecondStageConfig, rerank_top_candidates


def _rank(candidate_id: str, rows: list[dict]) -> int:
    for idx, row in enumerate(rows, start=1):
        if row["id"] == candidate_id:
            return idx
    return 999


def _run_slice(query: str, candidates: list[dict], gold_id: str) -> tuple[int, int, list[dict]]:
    raw_ranked = sorted(candidates, key=lambda row: row["final_score"], reverse=True)
    plan = plan_query(query, requested_tables=["memories"])
    reranked, _debug = rerank_top_candidates(
        query,
        plan,
        raw_ranked,
        config=SecondStageConfig(enabled=True, top_n=len(raw_ranked), model_enabled=False),
    )
    return _rank(gold_id, raw_ranked), _rank(gold_id, reranked), reranked


def test_heldout_non_benchmark_queries_do_not_regress_under_full_rerank():
    """Small hand-labeled slice outside LongMemEval/LoCoMo/MemBench fixtures."""

    cases = [
        {
            "query": "Who owns the Solana signer key rotation checklist?",
            "gold": "signer-owner",
            "candidates": [
                {
                    "id": "signer-distractor",
                    "bucket": "memories",
                    "content": (
                        "The Solana signer key rotation checklist was discussed during "
                        "platform review; checklist risk remains open."
                    ),
                    "final_score": 0.91,
                    "retrieval_score": 0.91,
                    "source": "semantic",
                },
                {
                    "id": "signer-owner",
                    "bucket": "memories",
                    "content": "Nia owns the Solana signer key rotation checklist for signed export releases.",
                    "final_score": 0.77,
                    "retrieval_score": 0.77,
                    "source": "keyword",
                },
            ],
        },
        {
            "query": "Which runtime verifies signed export bundles offline?",
            "gold": "offline-verifier",
            "candidates": [
                {
                    "id": "online-pin",
                    "bucket": "memories",
                    "content": "Signed export bundles can optionally pin a SHA-256 hash on Solana.",
                    "final_score": 0.88,
                    "retrieval_score": 0.88,
                    "source": "semantic",
                },
                {
                    "id": "offline-verifier",
                    "bucket": "memories",
                    "content": "The Python verifier checks signed export bundles offline before any on-chain pin.",
                    "final_score": 0.82,
                    "retrieval_score": 0.82,
                    "source": "keyword",
                },
            ],
        },
        {
            "query": "What happened after the invoice webhook outage?",
            "gold": "webhook-after",
            "candidates": [
                {
                    "id": "webhook-before",
                    "bucket": "memories",
                    "content": "Session ID: session_4\nInvoice webhook retries began before the queue worker restart.",
                    "final_score": 0.89,
                    "retrieval_score": 0.89,
                    "source": "semantic",
                },
                {
                    "id": "webhook-after",
                    "bucket": "memories",
                    "content": "Session ID: session_5\nAfter the invoice webhook outage, Nia restarted the queue worker.",
                    "final_score": 0.84,
                    "retrieval_score": 0.84,
                    "source": "keyword",
                },
            ],
        },
    ]

    raw_hits = 0
    full_hits = 0
    for case in cases:
        raw_rank, full_rank, _rows = _run_slice(case["query"], case["candidates"], case["gold"])
        raw_hits += int(raw_rank == 1)
        full_hits += int(full_rank == 1)
        assert full_rank <= raw_rank

    assert full_hits >= raw_hits
    assert full_hits == len(cases)


def test_exact_field_ablation_promotes_generic_role_fact_not_only_synthetic_ids():
    """The field-aware value pattern should help normal role/owner prose too."""

    query = "What is Arlo's role in group alpha?"
    candidates = [
        {
            "id": "role-distractor",
            "bucket": "memories",
            "content": (
                "Arlo joined group alpha. The team discussed the role taxonomy "
                "and group alpha backlog, but no assignment was decided."
            ),
            "final_score": 0.93,
            "retrieval_score": 0.93,
            "source": "semantic",
        },
        {
            "id": "role-answer",
            "bucket": "memories",
            "content": "Member profile: Arlo is the quartermaster for group alpha and owns supply handoff.",
            "final_score": 0.72,
            "retrieval_score": 0.72,
            "source": "keyword",
        },
    ]

    raw_rank, full_rank, reranked = _run_slice(query, candidates, "role-answer")

    assert raw_rank == 2
    assert full_rank == 1
    assert reranked[0]["second_stage_features"]["role_value_pattern"] == 1.0
