"""Phase 0 correctness tests.

These tests pin down the five fixes bundled in the Phase 0 PR:

  1. ``Brain.remember`` now routes through the W(m) gate
     (``agentmemory._gates.evaluate_write``), with ``bypass_gate`` and
     ``strict`` kwargs for migrations and fail-fast callers.
  2. Reconsolidation lability windows block *writes* by foreign agents,
     not just recall boosting.
  3. ``Brain.check_triggers`` does word-boundary matching so "deploy"
     no longer fires on "redeploy".
  4. ``Brain._safe_fts`` preserves phrases and prefix operators and
     doesn't blow up on punctuation-only queries.
  5. FTS5 ``OperationalError`` handlers log the underlying failure
     instead of silently falling through to LIKE.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentmemory import _gates
from agentmemory.brain import Brain, GateRejected, _safe_fts


# ---------------------------------------------------------------------------
# 1. W(m) gate — bypass / strict / rejection
# ---------------------------------------------------------------------------


def test_remember_returns_int_on_accept(brain):
    mid = brain.remember(
        "The mitochondria is the powerhouse of this particular cell",
        category="lesson",
    )
    assert isinstance(mid, int)
    assert mid > 0


def _force_low_surprise(monkeypatch):
    """Make the word-overlap surprise scorer always return near-duplicate.

    Without Ollama the surprise calc uses a non-deterministic LIKE probe
    over a hash-ordered set, so we can't rely on real duplicate detection
    in tests. Patching the helper directly keeps the rejection path
    deterministic while still exercising :func:`_gates.evaluate_write`.
    """
    monkeypatch.setattr(
        _gates, "_word_overlap_surprise", lambda db, content: (0.05, "test_forced")
    )


def test_remember_bypass_gate_always_inserts(brain, monkeypatch):
    """bypass_gate=True must insert unconditionally, even when the gate
    would normally reject.
    """
    _force_low_surprise(monkeypatch)

    text = "any content would be rejected under forced-low surprise"
    dup = brain.remember(text, category="lesson")
    assert dup is None, "gate should reject under forced-low surprise"

    # bypass_gate=True must ignore the rejection.
    forced = brain.remember(text, category="lesson", bypass_gate=True)
    assert forced is not None


def test_remember_strict_raises_on_reject(brain, monkeypatch):
    _force_low_surprise(monkeypatch)
    with pytest.raises(GateRejected) as excinfo:
        brain.remember(
            "strict rejected content", category="lesson", strict=True
        )
    assert excinfo.value.decision is not None
    assert excinfo.value.decision.accepted is False
    assert excinfo.value.decision.reason


def test_remember_rejection_emits_write_rejected_event(brain, monkeypatch):
    _force_low_surprise(monkeypatch)
    before = _count_rejected_events(brain)
    assert brain.remember("event-forced rejection", category="lesson") is None
    after = _count_rejected_events(brain)
    assert after == before + 1


def _count_rejected_events(brain: Brain) -> int:
    conn = sqlite3.connect(str(brain.db_path))
    try:
        return conn.execute(
            "SELECT count(*) FROM events WHERE event_type='write_rejected'"
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# W(m) gate parity — Brain.remember vs mcp_server.tool_memory_add
# ---------------------------------------------------------------------------


def test_gate_parity_brain_vs_mcp_reject(tmp_path, monkeypatch):
    """Both write paths must reject the same forced-duplicate input.

    We patch the surprise helper in both modules so the pre-worthiness
    floor fires deterministically and there's no dependency on Ollama.
    """
    pytest.importorskip("mcp")  # mcp_server imports mcp.server
    from agentmemory import mcp_server

    db_file = tmp_path / "parity.db"
    brain = Brain(db_path=str(db_file), agent_id="parity-agent")

    # Force both surprise scorers to the same low value.
    monkeypatch.setattr(
        _gates, "_word_overlap_surprise", lambda db, content: (0.05, "test_forced")
    )
    monkeypatch.setattr(
        mcp_server,
        "_surprise_score_mcp",
        lambda db, content, blob=None: (0.05, "test_forced"),
    )
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)
    monkeypatch.setattr(mcp_server, "get_db", lambda: _open_db(db_file))
    # Don't let MCP phone Ollama.
    monkeypatch.setattr(mcp_server, "_embed_safe", lambda text: None)

    text = "Parity seed content about octopi navigating reef systems"

    brain_dup = brain.remember(text, category="lesson")
    assert brain_dup is None, "Brain path should reject"

    mcp_result = mcp_server.tool_memory_add(
        agent_id="parity-agent",
        content=text,
        category="lesson",
        scope="global",
        confidence=1.0,
    )
    assert mcp_result.get("ok") is False, f"MCP path should reject, got {mcp_result}"
    assert mcp_result.get("rejected") is True


def test_gate_parity_both_accept_novel(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from agentmemory import mcp_server

    db_file = tmp_path / "parity_accept.db"
    brain = Brain(db_path=str(db_file), agent_id="parity-agent")

    # Force high surprise so both paths accept deterministically.
    monkeypatch.setattr(
        _gates, "_word_overlap_surprise", lambda db, content: (1.0, "test_forced")
    )
    monkeypatch.setattr(
        mcp_server,
        "_surprise_score_mcp",
        lambda db, content, blob=None: (1.0, "test_forced"),
    )
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)
    monkeypatch.setattr(mcp_server, "get_db", lambda: _open_db(db_file))
    monkeypatch.setattr(mcp_server, "_embed_safe", lambda text: None)

    brain_id = brain.remember(
        "A completely novel observation about tidepool biodiversity",
        category="lesson",
    )
    assert brain_id is not None

    mcp_result = mcp_server.tool_memory_add(
        agent_id="parity-agent",
        content="Another completely different fact about volcanic rock textures",
        category="lesson",
        scope="global",
        confidence=1.0,
    )
    assert mcp_result.get("ok") is True, f"MCP path should accept, got {mcp_result}"


def _open_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# 2. Reconsolidation lability window enforcement on writes
# ---------------------------------------------------------------------------


def test_lability_window_blocks_foreign_agent_write(tmp_path):
    """Open the lability window on a memory for agent A and verify that
    a write from agent B targeting that same memory is rejected, while a
    subsequent write by agent A itself is allowed.
    """
    db_file = tmp_path / "lability.db"
    brain_a = Brain(db_path=str(db_file), agent_id="agent-A")
    brain_b = Brain(db_path=str(db_file), agent_id="agent-B")

    seed = brain_a.remember(
        "Target memory about migratory bird routes over the Bering Strait",
        category="project",
    )
    assert seed is not None

    # Manually open a lability window owned by agent-A.
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "UPDATE memories SET labile_until = ?, labile_agent_id = ? WHERE id = ?",
            (expires, "agent-A", seed),
        )
        conn.commit()
    finally:
        conn.close()

    # Agent B tries to supersede the labile memory — must be rejected.
    b_result = brain_b.remember(
        "Conflicting update about migratory bird routes forced by agent B",
        category="project",
        supersedes_id=seed,
    )
    assert b_result is None, "cross-agent write into open lability window must reject"

    # Agent A can still update within its own window.
    a_result = brain_a.remember(
        "Reconsolidated detail about Bering Strait bird routes from the owner",
        category="project",
        supersedes_id=seed,
    )
    assert a_result is not None


def test_lability_window_closed_allows_any_agent(tmp_path):
    db_file = tmp_path / "lability_closed.db"
    brain_a = Brain(db_path=str(db_file), agent_id="agent-A")
    brain_b = Brain(db_path=str(db_file), agent_id="agent-B")

    seed = brain_a.remember(
        "Stale target memory about deep-sea volcanic vent microbiology",
        category="project",
    )
    assert seed is not None

    # Open a window that already expired in the past.
    expired = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "UPDATE memories SET labile_until = ?, labile_agent_id = ? WHERE id = ?",
            (expired, "agent-A", seed),
        )
        conn.commit()
    finally:
        conn.close()

    # With an expired window, agent B's write should go through the
    # regular gate instead of being hard-blocked on lability.
    b_result = brain_b.remember(
        "Fresh B-authored observation about hydrothermal vent bacteria density",
        category="project",
        supersedes_id=seed,
    )
    assert b_result is not None


# ---------------------------------------------------------------------------
# 3. Trigger word-boundary matching
# ---------------------------------------------------------------------------


def test_check_triggers_word_boundary(brain):
    brain.trigger("deploy failure", "deploy,rollback", "check rollback procedure")

    # False positive today: "deploy" matches "redeploy". After the fix it
    # must NOT match.
    matches = brain.check_triggers("we need to redeploy the frontend")
    assert matches == [], f"unexpected match: {matches}"

    # Positive control: a real word-boundary hit should still fire.
    matches = brain.check_triggers("the deploy failed last night")
    assert len(matches) == 1
    assert "deploy" in matches[0]["matched_keywords"]


# ---------------------------------------------------------------------------
# 4. _safe_fts preserves phrases, prefixes, and drops junk
# ---------------------------------------------------------------------------


def _make_fts_table() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='porter')"
    )
    conn.executemany(
        "INSERT INTO docs(body) VALUES (?)",
        [
            ("See the release notes for details",),
            ("The release notes cover new features",),
            ("api gateway configuration is documented",),
            ("apiary management is separate",),
            ("quick brown fox",),
        ],
    )
    conn.commit()
    return conn


def test_safe_fts_phrase_query_matches_phrase():
    conn = _make_fts_table()
    fts_q = _safe_fts('"release notes"')
    assert fts_q == '"release notes"'
    rows = conn.execute("SELECT body FROM docs WHERE docs MATCH ?", (fts_q,)).fetchall()
    assert len(rows) == 2
    assert all("release notes" in r[0] for r in rows)


def test_safe_fts_prefix_query_matches_prefix():
    conn = _make_fts_table()
    fts_q = _safe_fts("api*")
    assert fts_q == "api*"
    rows = conn.execute("SELECT body FROM docs WHERE docs MATCH ?", (fts_q,)).fetchall()
    bodies = {r[0] for r in rows}
    assert any("api gateway" in b for b in bodies)
    assert any("apiary" in b for b in bodies)


def test_safe_fts_degenerate_input_returns_empty():
    assert _safe_fts("- - -") == ""
    assert _safe_fts("()()") == ""
    assert _safe_fts("") == ""
    assert _safe_fts("   ") == ""


def test_safe_fts_mixed_tokens_are_or_joined():
    # Default bag-of-words: OR of cleaned tokens.
    assert _safe_fts("hello world") == "hello OR world"


def test_safe_fts_unbalanced_quote_is_dropped():
    # An unbalanced quote must not crash FTS5.
    conn = _make_fts_table()
    fts_q = _safe_fts('release"')
    assert '"' not in fts_q
    # Should parse cleanly.
    conn.execute("SELECT body FROM docs WHERE docs MATCH ?", (fts_q,)).fetchall()


# ---------------------------------------------------------------------------
# 5. OperationalError handlers log instead of silently swallowing
# ---------------------------------------------------------------------------


def test_fts5_missing_falls_back_with_warning(tmp_path, caplog):
    db_file = tmp_path / "no_fts.db"
    brain = Brain(db_path=str(db_file), agent_id="noftsagent")
    brain.remember("Observation about arctic tern migration patterns", category="project")

    # Drop the FTS5 table so search() triggers OperationalError.
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level(logging.WARNING, logger="agentmemory.brain"):
        results = brain.search("arctic tern")

    assert len(results) >= 1
    assert any("FTS5 search failed" in rec.message for rec in caplog.records)
