"""Regression test for the MCP surprise-score parity fix (2.4.9, audit I7).

The 2026-04-18 audit found that `mcp_server._surprise_score_mcp` was a
stale copy of `_impl._surprise_score` that missed the 2.2.3
neutral-fallback fix: it still returned `(1.0, "fts5_no_matches")` when
FTS5 found no neighbors, inflating W(m) novelty on the dominant MCP
write path. The fix deletes the duplicate and imports the canonical
scorer. This test locks the parity so the two paths can't drift again.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory import mcp_server
from agentmemory._impl import _surprise_score as impl_surprise_score
from agentmemory.brain import Brain


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "brain.db"
    Brain(db_path=str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_mcp_server_imports_canonical_surprise_score():
    # The module no longer defines a local copy.
    assert not hasattr(mcp_server, "_surprise_score_mcp"), (
        "mcp_server should no longer define _surprise_score_mcp; the MCP "
        "write path must route through _impl._surprise_score to stay in "
        "sync with the 2.2.3 neutral-fallback fix."
    )
    # The name it does expose points at _impl's implementation.
    assert mcp_server._surprise_score is impl_surprise_score, (
        "mcp_server._surprise_score must be the canonical _impl._surprise_score, "
        "not a local shadow."
    )


def test_no_fts_matches_returns_neutral_not_novel(fresh_db):
    # The original bug: pre-2.4.9, this content with no FTS neighbors
    # returned (1.0, "fts5_no_matches"); canonical is (0.5,
    # "fts5_no_matches_neutral") per the 2.2.3 fix.
    surprise, method = mcp_server._surprise_score(
        fresh_db, "completely novel content nobody else wrote about"
    )
    assert surprise == 0.5
    assert method == "fts5_no_matches_neutral"


def test_empty_content_returns_neutral(fresh_db):
    surprise, method = mcp_server._surprise_score(fresh_db, "")
    assert surprise == 0.5
    assert method == "empty_neutral"
