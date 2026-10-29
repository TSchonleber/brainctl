"""Tests for the entity_lookup keyword rule in agentmemory/lib/intent_classifier.py
(2.2.3 Item 3 — audit memory 1675).

Covers Rule 9b: keyword-driven entity_lookup that mirrors the inline
_builtin_classify_intent fallback in src/agentmemory/_impl.py. Without this
rule, queries like "Who is Alice?" reached the external classifier and fell
through to factual_lookup, missing the entity_lookup rerank profile.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the module file directly. Other tests may have already
# imported a different copy via `agentmemory._impl` (which does `from
# intent_classifier import ...` — that resolves through cwd-relative sys.path
# and can pick up an installed-package or main-repo copy ahead of the
# worktree's). Force-load from this worktree's bin/ via spec_from_file_location
# so we always test the version in the current branch, not whatever happens
# to be cached in sys.modules.
_BIN_IC = (
    Path(__file__).resolve().parent.parent / "src" / "agentmemory" / "lib" / "intent_classifier.py"
)
_spec = importlib.util.spec_from_file_location(
    "intent_classifier_worktree_2_2_3", str(_BIN_IC)
)
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)


# ---------------------------------------------------------------------------
# Positive cases — the new rule must classify these as entity_lookup
# ---------------------------------------------------------------------------


class TestEntityLookupKeywordRule:
    def test_who_is_question(self):
        """The headline bug case from the audit."""
        result = ic.classify_intent("Who is Alice?")
        assert result.intent == "entity_lookup"
        assert result.matched_rule == "entity_kw:who"
        assert result.confidence == pytest.approx(0.80)

    def test_person_keyword(self):
        result = ic.classify_intent("Find the person responsible for migration 048")
        assert result.intent == "entity_lookup"
        assert result.matched_rule == "entity_kw:person"

    def test_team_keyword(self):
        # Avoid 'consolidation' / other research_concept tokens that would
        # claim the query at Rule 7 before reaching Rule 9b.
        result = ic.classify_intent("Which team owns the staging environment?")
        assert result.intent == "entity_lookup"
        assert result.matched_rule == "entity_kw:team"

    def test_tables_match_route_preset(self):
        result = ic.classify_intent("Who is Alice?")
        # Same table set as _TABLE_ROUTES["entity_lookup"] — order is the
        # preset's order, not the spec's literal list.
        assert set(result.tables) == {"memories", "events", "context"}
        assert result.tables == ic._TABLE_ROUTES["entity_lookup"]

    def test_format_hint_uses_entity_card(self):
        result = ic.classify_intent("Who is Alice?")
        assert result.format_hint == ic._FORMAT_HINTS["entity_lookup"]


# ---------------------------------------------------------------------------
# Ordering / non-regression — earlier rules must still claim their queries
# ---------------------------------------------------------------------------


class TestRuleOrderingPreserved:
    def test_known_agent_name_still_wins_over_keyword(self):
        """Rule 9 (agent_names) must fire before Rule 9b (entity keywords).
        Catches the 'someone moved the rule above agent_names' regression.
        Use a query without trailing punctuation so the bare 'hermes' token
        actually appears in the set-of-words intersection."""
        result = ic.classify_intent("hermes is the agent of interest")
        assert result.intent == "entity_lookup"
        assert result.matched_rule == "agent_name:hermes"

    def test_troubleshooting_keyword_in_query_with_agent_word(self):
        """Rule 2 (troubleshooting) earlier than Rule 9b — 'fail' wins
        over 'agent' even though both are keyword-matchable."""
        result = ic.classify_intent("agent is failing on staging")
        assert result.intent == "troubleshooting"

    def test_task_status_assigned_keyword_wins(self):
        """Rule 3 (task_status) earlier than Rule 9b — 'assigned' is a
        task_status keyword in the external taxonomy. Intentional divergence
        from the builtin (which routes 'assigned' to entity_lookup)."""
        result = ic.classify_intent("who assigned this ticket?")
        assert result.intent == "task_status"

    def test_proper_noun_alone_unchanged(self):
        """Rule 9c (proper_noun_alone) still works after Rule 9b insertion.
        Uses a query whose lowercased form contains none of the entity
        keywords, so Rule 9b passes through and the regex check fires."""
        result = ic.classify_intent("Memory Intelligence Division")
        assert result.intent == "entity_lookup"
        assert result.matched_rule == "proper_noun_alone"


# ---------------------------------------------------------------------------
# Fallback safety — queries with none of the new keywords still fall through
# ---------------------------------------------------------------------------


class TestFallbackUnaffected:
    def test_factual_lookup_still_default(self):
        result = ic.classify_intent("postgres connection string")
        assert result.intent == "factual_lookup"

    def test_who_substring_does_not_match(self):
        """'who' must be followed by a space — substring 'whole' should NOT
        match the entity_kw rule. The keyword is 'who ' with trailing space."""
        result = ic.classify_intent("explain whole pipeline architecture")
        assert result.intent != "entity_lookup" or result.matched_rule != "entity_kw:who"
