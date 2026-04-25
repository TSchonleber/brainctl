from __future__ import annotations

from benchmarks.retrieval_flow_optimizer import (
    detect_flow_operators,
    optimize_ranked_documents,
    source_family,
    source_session,
)


def _optimize(query, docs, retrieved_rows=None, top_k=5):
    rowid_to_doc_id = {index: doc_id for index, (doc_id, _text) in enumerate(docs, start=1)}
    rowid_to_text = {index: text for index, (_doc_id, text) in enumerate(docs, start=1)}
    return optimize_ranked_documents(
        query,
        retrieved_rows or [],
        rowid_to_doc_id,
        rowid_to_text,
        top_k=top_k,
    )


def test_detects_operators_that_change_retrieval_behavior():
    role = detect_flow_operators("What is the location of my father's workplace?")
    assert role.role_fact
    assert role.single_fact
    assert not role.needs_breadth

    temporal = detect_flow_operators("What changed after the March session and what is current now?")
    assert temporal.temporal
    assert temporal.update_resolution
    assert temporal.multi_session

    comparison = detect_flow_operators("Compare Alice and Bob across both sessions.")
    assert comparison.comparison
    assert comparison.set_coverage
    assert comparison.needs_breadth


def test_source_metadata_parsing_is_generic():
    assert source_family("source_alpha_4") == "source_alpha"
    assert source_family("simple_roles_9|sid=48|g=48|s=2|t=7") == "simple_roles"
    assert source_session("simple_roles_9|sid=48|g=48|s=2|t=7") == "48"
    assert source_session("conversation-session-12", "") == "12"


def test_simple_role_fact_uses_field_fallback_when_initial_retrieval_misses():
    docs = [
        ("noise|sid=1", "My friend enjoys hiking on weekends."),
        ("simple_roles_9|sid=48|g=48|s=2|t=7", "My dad works in Miami, FL."),
        ("other|sid=2", "My coworker likes board games."),
    ]
    retrieved_rows = [{"id": 1, "final_score": 10.0}]

    ranked, trace = _optimize(
        "What is the location of my father's workplace?",
        docs,
        retrieved_rows,
        top_k=3,
    )

    assert ranked[0] == "simple_roles_9|sid=48|g=48|s=2|t=7"
    assert "role_fact" in trace["operators"]
    assert trace["candidate_counts"]["field"] >= 1


def test_empty_candidate_generation_falls_back_to_lexical_candidates():
    docs = [
        ("doc_1|sid=1", "The archive key is nebula-42."),
        ("doc_2|sid=2", "The menu included soup."),
    ]

    ranked, trace = _optimize("What archive key was mentioned?", docs, [], top_k=2)

    assert ranked[0] == "doc_1|sid=1"
    assert trace["fallback_used"] is True
    assert trace["candidate_counts"]["lexical"] >= 1


def test_set_coverage_ranking_prefers_breadth_over_duplicate_sessions():
    docs = [
        ("trip_1|sid=1", "Alice visited Rome during the trip."),
        ("trip_2|sid=1", "Alice talked more about Rome during the trip."),
        ("trip_3|sid=2", "Bob visited Paris during the trip."),
    ]
    retrieved_rows = [
        {"id": 1, "final_score": 10.0},
        {"id": 2, "final_score": 9.8},
        {"id": 3, "final_score": 2.0},
    ]

    ranked, trace = _optimize(
        "Which places did Alice and Bob visit across the trip?",
        docs,
        retrieved_rows,
        top_k=2,
    )

    assert "trip_3|sid=2" in ranked
    assert len({source_session(doc_id) for doc_id in ranked}) == 2
    assert "set_coverage" in trace["operators"] or "comparison" in trace["operators"]


def test_update_resolution_promotes_newer_current_evidence():
    docs = [
        ("profile_1|sid=1", "In an earlier session, Alice lived in Boston."),
        ("profile_2|sid=4", "Alice updated her current city to Denver."),
    ]
    retrieved_rows = [
        {"id": 1, "final_score": 10.0},
        {"id": 2, "final_score": 8.5},
    ]

    ranked, trace = _optimize("Where does Alice currently live now?", docs, retrieved_rows, top_k=2)

    assert ranked[0] == "profile_2|sid=4"
    assert "update_resolution" in trace["operators"]
    assert trace["selected"][0]["features"]["temporal_recency_bonus"] > 0


def test_family_expansion_admits_sibling_evidence_for_multi_part_queries():
    docs = [
        ("source_alpha_1|sid=1", "The deployment needs a smoke test first."),
        ("source_alpha_2|sid=2", "The deployment also needs rollback notes."),
        ("noise_beta_1|sid=3", "The cafeteria changed its menu."),
    ]
    retrieved_rows = [
        {"id": 1, "final_score": 10.0},
        {"id": 3, "final_score": 8.0},
    ]

    ranked, trace = _optimize(
        "List all deployment requirements across sessions.",
        docs,
        retrieved_rows,
        top_k=2,
    )

    assert "source_alpha_2|sid=2" in ranked
    selected_channels = {
        channel
        for selected in trace["selected"]
        for channel in selected["channels"]
    }
    assert "family" in selected_channels


def test_whole_session_family_admission_promotes_compact_sibling_evidence():
    docs = [
        ("distractor_alpha", "Session ID: distractor_alpha\nSession Date: 2023/01/01\nConversation: User: I asked about museum tickets."),
        ("noise_beta", "Session ID: noise_beta\nSession Date: 2023/01/02\nConversation: User: I discussed a museum blog post."),
        ("answer_trip_1", "Session ID: answer_trip_1\nSession Date: 2023/01/03\nConversation: User: I visited the science museum."),
        ("noise_gamma", "Session ID: noise_gamma\nSession Date: 2023/01/04\nConversation: User: I asked about travel planning."),
        ("answer_trip_2", "Session ID: answer_trip_2\nSession Date: 2023/01/05\nConversation: User: I visited the art museum."),
        ("answer_trip_3", "Session ID: answer_trip_3\nSession Date: 2023/01/06\nConversation: User: I visited the history museum."),
    ]
    retrieved_rows = [
        {"id": 1, "final_score": 10.0},
        {"id": 2, "final_score": 9.0},
        {"id": 3, "final_score": 8.0},
        {"id": 4, "final_score": 7.0},
        {"id": 5, "final_score": 6.0},
        {"id": 6, "final_score": 5.0},
    ]

    ranked, trace = _optimize(
        "What is the order of the museums I visited from earliest to latest?",
        docs,
        retrieved_rows,
        top_k=5,
    )

    assert ranked[:4] == ["distractor_alpha", "answer_trip_1", "answer_trip_2", "answer_trip_3"]
    assert trace["strategy"] == "whole_session_family_admission"


def test_session_id_corpus_preserves_first_stage_without_compact_families():
    docs = [
        ("session_1", "Alex said, \"I visited the museum on Monday.\""),
        ("session_2", "Alex said, \"I visited the garden on Tuesday.\""),
        ("session_3", "Alex said, \"I visited the library on Wednesday.\""),
    ]
    retrieved_rows = [
        {"id": 1, "final_score": 10.0},
        {"id": 2, "final_score": 9.0},
        {"id": 3, "final_score": 8.0},
    ]

    ranked, trace = _optimize(
        "What is the order of places Alex visited from earliest to latest?",
        docs,
        retrieved_rows,
        top_k=3,
    )

    assert ranked == ["session_1", "session_2", "session_3"]
    assert trace["strategy"] == "preserve_first_stage_order"


def test_role_fact_uses_same_session_coreference_without_gold_ids():
    docs = [
        ("roles_1|sid=10|g=10|s=1|t=0", "I want to tell you about my sister, Sierra."),
        ("roles_1|sid=11|g=11|s=1|t=1", "She is a Senior Research Scientist."),
        ("roles_1|sid=20|g=20|s=2|t=0", "My coworker is a Construction Supervisor."),
    ]
    retrieved_rows = [
        {"id": 3, "final_score": 10.0},
        {"id": 1, "final_score": 8.0},
        {"id": 2, "final_score": 2.0},
    ]

    ranked, trace = _optimize("What is the position of my sister?", docs, retrieved_rows, top_k=2)

    assert ranked[0] == "roles_1|sid=11|g=11|s=1|t=1"
    assert trace["selected"][0]["features"]["role_coref_group_bonus"] > 0
