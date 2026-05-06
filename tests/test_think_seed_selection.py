"""Issue #97-5: ``think``'s seed selection used to pass the raw query to
FTS5 MATCH, which uses implicit-AND across bare tokens. Multi-word
natural-language queries returned no seeds even when ``memory_search``
resolved the same query (memory_search OR-rewrites tokens via
``_build_fts_match_expression``).

This test confirms ``think_from_query`` now uses the same OR-rewrite
helpers and stops returning empty seeds for queries that memory_search
handles correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
from agentmemory.dream import think_from_query


@pytest.fixture
def brain_with_memories(tmp_path):
    db = tmp_path / "brain.db"
    brain = Brain(db_path=str(db), agent_id="think-agent")
    # Seed memories where each contains some — but not all — of the
    # multi-word query tokens. Implicit-AND would return zero seeds; the
    # OR-rewrite returns the matching row.
    brain.remember(
        "Kelly village water pumps run on solar arrays",
        category="project",
    )
    brain.remember(
        "Howler is a labrador, weighs 65 pounds",
        category="preference",
    )
    brain.remember(
        "Infrastructure for Kelly's outpost was rebuilt in March",
        category="project",
    )
    return brain, db


def test_think_finds_seeds_for_multiword_query(brain_with_memories):
    """Multi-word natural-language query: at least one seed should match
    via the OR-rewrite, even though no single memory contains every
    token."""
    _, db_path = brain_with_memories
    import sqlite3

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    out = think_from_query(
        db, "Kelly village infrastructure", seed_limit=5, hops=1, top_k=10
    )
    db.close()

    assert out["ok"] is True
    assert out["seeds"], (
        "Regression: implicit-AND query produced empty seeds. The "
        f"helper should have OR-rewritten the tokens. note={out.get('note')}"
    )


def test_think_still_empty_for_no_match(brain_with_memories):
    """Sanity: nonsense token still produces empty seeds + the standard
    note, no spurious LIKE-fallback hit."""
    _, db_path = brain_with_memories
    import sqlite3

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    out = think_from_query(
        db, "zzqqxxnonsense", seed_limit=5, hops=1, top_k=10
    )
    db.close()

    assert out["ok"] is True
    assert out["seeds"] == []
    assert "no seed memories matched" in (out.get("note") or "")
