"""Issue #97-7: ``memory_search(multi_pass=True)`` enrichment was too
broad — it added every >4-char word from the top-3 pass-1 results to a
combined OR query and accepted any pass-2 hit, regardless of whether
the hit shared any original-query token. Result: memories connected
only by surface-level word overlap (the user's example: a brainctl/FTS
query pulling in a Pigendom book-analysis memory) showed up as pass-2
hits.

The tightened version drops pass-2 hits that share zero original-query
tokens. This test pins the new behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.mcp_server as mcp_server


@pytest.fixture
def seeded_brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    brain = Brain(db_path=str(db), agent_id="ms-agent")
    monkeypatch.setattr(mcp_server, "DB_PATH", db)

    # On-topic memories (matching the original "brainctl startup FTS index"
    # query). They share the words "brainctl", "startup", "FTS", "index".
    brain.remember(
        "brainctl startup script rebuilds the memories_fts index on cold start.",
        category="environment",
    )
    brain.remember(
        "FTS5 index integrity-check is the cleanest signal that brainctl "
        "needs a rebuild.",
        category="lesson",
    )
    brain.remember(
        "memories_fts uses external content via content=memories, content_rowid=id.",
        category="convention",
    )

    # Bridge memory: contains both an on-topic word ("startup") and an
    # off-topic word ("Pigendom"). The bug let "Pigendom" leak into pass2.
    brain.remember(
        "Pigendom planning notes during startup operations were boring.",
        category="project",
    )

    # Pure off-topic memories — no overlap with the original query at all.
    # These should never appear in pass-2 results.
    brain.remember(
        "Pigendom is a 700-page novel by an unrelated author.",
        category="project",
    )
    brain.remember(
        "The boring novel review noted character development inconsistencies.",
        category="project",
    )
    return brain


def test_multi_pass_does_not_pull_unrelated_memories(seeded_brain):
    out = mcp_server.tool_memory_search(
        agent_id="ms-agent",
        query="brainctl FTS index",
        limit=20,
        multi_pass=True,
    )
    contents = [r.get("content", "") for r in out.get("results", [])] or out.get(
        "memories", []
    )
    # Brain-style return: try several known shapes; the function returns a list directly
    # via tool_memory_search but the helper output may differ. Prefer raw `results`.
    if isinstance(out, dict) and "results" not in out:
        # tool_memory_search returns a list under no specific key in some
        # versions — flatten to defensive form.
        contents = [r.get("content", "") for r in out.get("memories", [])]
    if not contents and isinstance(out, list):
        contents = [r.get("content", "") for r in out]

    # Defensive: if the call directly returned a list, use it
    if isinstance(out, list):
        results_list = out
    else:
        results_list = out.get("results") or out.get("memories") or []
    contents = [r.get("content", "") for r in results_list]

    assert contents, f"Expected at least pass-1 hits, got: {out!r}"

    pure_offtopic_phrases = [
        "700-page novel",
        "boring novel review",
    ]
    for phrase in pure_offtopic_phrases:
        assert not any(phrase in c for c in contents), (
            f"Regression: multi_pass leaked unrelated memory containing "
            f"{phrase!r}. results={contents}"
        )


def test_multi_pass_keeps_anchored_results(seeded_brain):
    """A memory that does share an original-query token (e.g. 'startup')
    is still allowed even if it also has off-topic words like
    'Pigendom'."""
    out = mcp_server.tool_memory_search(
        agent_id="ms-agent",
        query="brainctl startup FTS",
        limit=20,
        multi_pass=True,
    )
    if isinstance(out, list):
        results_list = out
    else:
        results_list = out.get("results") or out.get("memories") or []
    contents = [r.get("content", "") for r in results_list]

    assert any("Pigendom planning notes during startup" in c for c in contents), (
        "Bridge memory shares the 'startup' query token; it must remain "
        f"reachable. contents={contents}"
    )
