"""Regression tests for ``sample_db_embedding_widths`` (issue #160).

``brainctl vec reindex`` imported ``sample_db_embedding_widths`` from
``agentmemory.embeddings`` and called it, but the function was never defined —
so the command crashed with ``ImportError: cannot import name
'sample_db_embedding_widths'`` before doing any work. These tests pin the
symbol's existence, its import wiring, and the width-probe contract the
reindex guard depends on.

The probe only reads ``SELECT embedding FROM vec_memories``, so a plain table
whose declared type carries the ``float[N]`` dim (matched by the same regex
``get_db_embedding_dim`` uses) is a faithful, extension-free fixture.
"""
from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.embeddings import sample_db_embedding_widths


def _packed(n: int) -> bytes:
    return struct.pack(f"{n}f", *([0.1] * n))


def _conn_with_vec(dim: int, widths: list[int]) -> sqlite3.Connection:
    """A DB whose ``vec_memories`` DDL declares ``float[dim]`` and whose rows
    carry embeddings of the given ``widths`` (in float count)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE vec_memories (rowid INTEGER PRIMARY KEY, embedding float[{dim}])")
    conn.executemany(
        "INSERT INTO vec_memories (embedding) VALUES (?)",
        [(_packed(w),) for w in widths],
    )
    conn.commit()
    return conn


def test_symbol_is_importable():
    """The bare ImportError regression: the reindex import must resolve."""
    from agentmemory.embeddings import sample_db_embedding_widths as fn  # noqa: F401

    assert callable(fn)


def test_missing_table_reports_not_ok_but_consistent():
    """No ``vec_memories`` -> nothing to probe, guard must proceed (consistent)."""
    conn = sqlite3.connect(":memory:")
    out = sample_db_embedding_widths(conn, sample_size=8)
    assert out["ok"] is False
    assert out["sample_count"] == 0
    assert out["consistent"] is True
    assert out["observed_dims"] == []


def test_empty_table_is_consistent():
    conn = _conn_with_vec(768, widths=[])
    out = sample_db_embedding_widths(conn)
    assert out["ok"] is True
    assert out["sample_count"] == 0
    assert out["consistent"] is True


def test_uniform_width_matching_declared_dim_is_consistent():
    conn = _conn_with_vec(768, widths=[768, 768, 768])
    out = sample_db_embedding_widths(conn)
    assert out["ok"] is True
    assert out["sample_count"] == 3
    assert out["declared_dim"] == 768
    assert out["observed_dims"] == [768]
    assert out["consistent"] is True


def test_mixed_widths_are_inconsistent():
    conn = _conn_with_vec(768, widths=[768, 768, 384])
    out = sample_db_embedding_widths(conn)
    assert out["consistent"] is False
    assert out["observed_dims"] == [384, 768]
    assert "mixed" in out["message"].lower()


def test_uniform_width_disagreeing_with_declared_dim_is_inconsistent():
    conn = _conn_with_vec(768, widths=[384, 384])
    out = sample_db_embedding_widths(conn)
    assert out["consistent"] is False
    assert out["declared_dim"] == 768
    assert out["observed_dims"] == [384]


def test_sample_size_caps_rows_read():
    conn = _conn_with_vec(768, widths=[768] * 50)
    out = sample_db_embedding_widths(conn, sample_size=8)
    assert out["sample_count"] == 8
