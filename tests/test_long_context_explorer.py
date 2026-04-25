from __future__ import annotations

from pathlib import Path

from agentmemory.retrieval.feature_builder import build_features
from agentmemory.retrieval.long_context import analyze_long_context
from agentmemory.retrieval.query_planner import plan_query
from agentmemory.retrieval.second_stage import SecondStageConfig, rerank_top_candidates

from tests.test_second_stage_reranker import _temp_model


def test_long_context_probe_finds_session_anchor(monkeypatch):
    monkeypatch.setenv("BRAINCTL_LONG_CONTEXT_PROBES", "1")
    plan = plan_query("When did Caroline go to the LGBTQ support group?", requested_tables=["memories"])
    candidate = {
        "id": 1,
        "bucket": "memories",
        "type": "memory",
        "final_score": 0.72,
        "retrieval_score": 0.72,
        "source": "keyword",
    }
    text = "\n".join(
        [
            "Session ID: session_1",
            "Session Date: 2025-01-12",
            "Conversation:",
            'Alice: We talked about cooking classes and weekend plans.',
            'Bob: Nothing else noteworthy happened this week.',
            'Caroline: I went to the LGBTQ support group after work and felt better.',
            'Alice: We also mentioned a grocery list and cleaning supplies.',
        ]
    )

    result = analyze_long_context(
        "When did Caroline go to the LGBTQ support group?",
        plan,
        candidate,
        text=text,
    )

    assert result["applicable"] is True
    assert result["score"] > 0.55
    assert result["confidence"] > 0.45
    assert result["uncertainty"] < 0.7
    assert "LGBTQ support group" in result["top_chunk_excerpt"]


def test_second_stage_uses_long_context_probe_to_promote_focused_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRAINCTL_LONG_CONTEXT_PROBES", "1")
    plan = plan_query("When did Caroline go to the LGBTQ support group?", requested_tables=["memories"])
    model_path = _temp_model(tmp_path / "tiny.json")
    diffuse = {
        "id": 1,
        "bucket": "memories",
        "type": "memory",
        "content": "\n".join(
            [
                "Session ID: session_7",
                "Session Date: 2025-01-20",
                "Conversation:",
                'Alice: Caroline mentioned some errands after work.',
                'Bob: She later mentioned a support group but I do not remember when.',
                'Alice: Then we switched topics to a restaurant review and sprint planning.',
                'Bob: We also talked about a support group again in passing.',
                'Alice: Nothing pinned the exact date.',
            ]
        ),
        "final_score": 0.789,
        "retrieval_score": 0.789,
        "source": "both",
        "confidence": 0.9,
    }
    focused = {
        "id": 2,
        "bucket": "memories",
        "type": "memory",
        "content": "\n".join(
            [
                "Session ID: session_1",
                "Session Date: 2025-01-12",
                "Conversation:",
                "Alice: We opened with a grocery list and a reminder about dry cleaning.",
                "Bob: Then we talked about a dentist appointment and an office lunch.",
                'Caroline: I went to the LGBTQ support group after work on January 12.',
                'Alice: We noted it in the session log for follow-up.',
                "Bob: After that we switched to weekend errands and recipe planning.",
                "Alice: We ended with notes about commute timing and a restaurant reservation.",
            ]
        ),
        "final_score": 0.776,
        "retrieval_score": 0.776,
        "source": "keyword",
        "confidence": 0.9,
    }

    reranked, debug = rerank_top_candidates(
        "When did Caroline go to the LGBTQ support group?",
        plan,
        [diffuse, focused],
        config=SecondStageConfig(top_n=2, model_path=str(model_path)),
    )

    assert reranked[0]["id"] == 2
    assert reranked[0]["second_stage_features"]["long_context_score"] > reranked[1]["second_stage_features"]["long_context_score"]
    assert debug["enabled"] is True


def test_query_planner_flags_temporal_aggregation_and_inference():
    temporal_multi = plan_query("How much have I made from selling eggs this month?", requested_tables=["memories"])
    assert temporal_multi.requires_temporal_reasoning is True
    assert temporal_multi.requires_multi_hop is True
    assert temporal_multi.normalized_intent == "temporal"

    inference = plan_query("What personality traits might Melanie say Caroline has?", requested_tables=["memories"])
    assert inference.requires_multi_hop is True


def test_long_context_probe_requires_close_temporal_candidates(monkeypatch):
    monkeypatch.setenv("BRAINCTL_LONG_CONTEXT_PROBES", "1")
    plan = plan_query("When did Caroline go to the LGBTQ support group?", requested_tables=["memories"])
    candidate = {
        "id": 7,
        "bucket": "memories",
        "type": "memory",
        "content": "\n".join(
            [
                "Session ID: session_9",
                "Session Date: 2025-01-19",
                "Conversation:",
                "Alice: We discussed errands and a support group in passing.",
                "Caroline: I went to the LGBTQ support group after work on January 12.",
                "Bob: We wrote it down in the follow-up notes.",
                "Alice: Then we switched to grocery planning and restaurants.",
                "Bob: We revisited the support group briefly before closing the session.",
            ]
        ),
        "final_score": 0.91,
        "retrieval_score": 0.91,
        "source": "keyword",
    }

    far_apart = build_features(
        "When did Caroline go to the LGBTQ support group?",
        plan,
        dict(candidate),
        neighbors={"prev_score": None, "next_score": 0.76, "leader_score": 0.91},
    )
    assert far_apart["long_context_applicable"] == 0.0

    close_scores = build_features(
        "When did Caroline go to the LGBTQ support group?",
        plan,
        dict(candidate),
        neighbors={"prev_score": None, "next_score": 0.889, "leader_score": 0.91},
    )
    assert close_scores["long_context_applicable"] == 1.0
    assert close_scores["long_context_focused_program"] == 1.0


def test_long_context_probe_ignores_whole_document_only_matches(monkeypatch):
    monkeypatch.setenv("BRAINCTL_LONG_CONTEXT_PROBES", "1")
    plan = plan_query("When did Caroline go to the LGBTQ support group?", requested_tables=["memories"])
    candidate = {
        "id": 11,
        "bucket": "memories",
        "type": "memory",
        "final_score": 0.72,
        "retrieval_score": 0.72,
        "source": "keyword",
    }
    text = (
        "Session ID: session_1 Session Date: 2025-01-12 Conversation "
        + "Caroline went to the LGBTQ support group after work on January 12 and we kept discussing it in the same paragraph without line breaks or sentence boundaries " * 24
    )

    result = analyze_long_context(
        "When did Caroline go to the LGBTQ support group?",
        plan,
        candidate,
        text=text,
    )

    assert result["applicable"] is False
    assert result["reason"] == "no_focused_program"
