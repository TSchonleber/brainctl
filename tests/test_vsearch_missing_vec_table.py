"""Regression test for vsearch resilience to missing vec tables (issue #161).

``brainctl vsearch`` defaults to searching ``memories,events,context``, but
``vec_events`` / ``vec_context`` are never created anywhere — only
``vec_memories`` is. The inner ``_vsearch_table`` ran the ``MATCH`` query with
no guard, so a single missing table raised ``OperationalError`` and the CLI's
catch-all aborted the whole command, discarding results from ``vec_memories``
which *does* exist.

The fix catches ``OperationalError`` for a missing vec table and returns ``[]``
for that source, so vsearch still returns results from the tables that exist.
"""
from __future__ import annotations

import json
import sqlite3
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory._impl as impl


_MEMORIES = [
    {
        "id": 1, "content": "deploy key rotation runbook", "category": "ops",
        "scope": None, "confidence": 1.0, "created_at": "2026-01-01T00:00:00Z",
        "recalled_count": 0, "temporal_class": None, "last_recalled_at": None,
    },
]


def _mock_vec_conn() -> MagicMock:
    """Mock connection: vec_memories works; vec_events/vec_context are absent
    (raise OperationalError, exactly like a real missing table)."""
    vec_rows = [{"rowid": m["id"], "distance": 0.1} for m in _MEMORIES]

    def _execute(sql, params=None):
        result = MagicMock()
        if "vec_events" in sql or "vec_context" in sql:
            raise sqlite3.OperationalError("no such table: vec_events")
        if "vec_memories" in sql:
            result.fetchall.return_value = vec_rows
        elif "memories_fts" in sql:
            result.fetchall.return_value = [{"rowid": m["id"], "rank": -1.0} for m in _MEMORIES]
        elif "FROM memories WHERE" in sql:
            result.fetchall.return_value = _MEMORIES
        elif "MAX(recalled_count)" in sql:
            result.fetchone.return_value = [0]
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = None
        return result

    conn = MagicMock(spec=sqlite3.Connection)
    conn.row_factory = sqlite3.Row
    conn.execute.side_effect = _execute
    conn.commit = MagicMock()
    conn.close = MagicMock()
    return conn


def test_vsearch_skips_missing_vec_tables(monkeypatch, capsys):
    monkeypatch.setattr(impl, "_get_db_with_vec", lambda: _mock_vec_conn())
    monkeypatch.setattr(impl, "_embed_query", lambda text: struct.pack("768f", *([0.1] * 768)))
    monkeypatch.setattr(impl, "log_access", lambda *a, **k: None)

    args = SimpleNamespace(
        query="deploy key rotation", limit=10, alpha=0.5, tables=None,
        vec_only=False, graph_boost=False, agent="tester",
    )

    # Pre-fix this raised OperationalError out of cmd_vsearch; must not now.
    impl.cmd_vsearch(args)

    out = json.loads(capsys.readouterr().out)
    # Results from the table that exists are preserved...
    assert len(out["memories"]) == 1
    assert out["memories"][0]["id"] == 1
    # ...and the missing tables degrade to empty, not a crash.
    assert out["events"] == []
    assert out["context"] == []
