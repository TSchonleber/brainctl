"""Regression test for vsearch aborting on a missing vec table (issue #161).

`cmd_vsearch` defaults to searching `memories,events,context`, but
`vec_events` / `vec_context` are never created. A single missing table used
to raise `OperationalError` and abort the whole command, discarding results
from `vec_memories` (which exists and has hits). The inner `_vsearch_table`
now skips a missing vec table and returns the rest.
"""
from __future__ import annotations

import sqlite3
import struct
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory._impl as _impl


def _packed(n: int = 768) -> bytes:
    return struct.pack(f"{n}f", *([0.1] * n))


def test_vsearch_skips_missing_vec_table(cli_db, monkeypatch):
    if not getattr(_impl, "VEC_DYLIB", None):
        pytest.skip("sqlite-vec dylib not available")

    # Point _impl (and therefore _get_db_with_vec) at the fully-initialised
    # test DB built by the cli_db fixture.
    monkeypatch.setattr(_impl, "DB_PATH", cli_db)

    # Seed one memory and index its vector into a real vec_memories table.
    # Deliberately do NOT create vec_events / vec_context — that's the bug.
    try:
        conn = _impl._get_db_with_vec()
    except SystemExit:
        pytest.skip("sqlite-vec not loadable in this environment")
    cur = conn.execute(
        "INSERT INTO memories (agent_id, category, content, created_at, updated_at) "
        "VALUES ('tester', 'convention', 'always use pytest for tests', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    mem_id = cur.lastrowid
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[768])"
    )
    conn.execute(
        "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
        (mem_id, _packed(768)),
    )
    conn.commit()
    conn.close()

    # Avoid hitting Ollama — feed a fixed query vector.
    monkeypatch.setattr(_impl, "_embed_query", lambda text: _packed(768))

    captured = {}
    monkeypatch.setattr(_impl, "json_out", lambda data, compact=False: captured.update(data))

    args = types.SimpleNamespace(
        query="pytest",
        limit=10,
        alpha=0.5,
        vec_only=True,           # skip the FTS branch to keep the test focused
        tables="memories,events",  # 'events' has no vec_events table → the bug
        graph_boost=False,
        agent="tester",
    )

    # Must not raise (previously: OperationalError "no such table: vec_events").
    _impl.cmd_vsearch(args)

    # vec_memories hit is returned; the missing events table is skipped (empty).
    assert captured, "cmd_vsearch produced no output"
    assert "memories" in captured
    assert [m["id"] for m in captured["memories"]] == [mem_id]
    assert captured.get("events") == []
