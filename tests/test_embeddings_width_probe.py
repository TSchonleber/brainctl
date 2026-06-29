"""Tests for ``agentmemory.embeddings.sample_db_embedding_widths`` (issue #160).

The function used to be imported by ``cmd_vec_reindex`` but was never
defined, so ``brainctl vec reindex`` crashed with an ``ImportError`` for
every user. These tests pin the contract the reindex guard relies on.
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

import agentmemory.embeddings as emb


def _packed(n_dims: int, fill: float = 0.1) -> bytes:
    return struct.pack(f"{n_dims}f", *([fill] * n_dims))


def _plain_vec_table(tmp_path, widths) -> sqlite3.Connection:
    """A plain ``vec_memories(embedding BLOB)`` populated with float32 blobs
    of the given widths.

    A real vec0 virtual table validates vector length on insert, so it can't
    hold the mixed-width rows we need to simulate interrupted-reindex
    corruption — a plain table lets us store arbitrary widths while keeping
    the exact ``SELECT embedding FROM vec_memories`` shape the probe reads.
    """
    conn = sqlite3.connect(str(tmp_path / "w.db"))
    conn.execute(
        "CREATE TABLE vec_memories (rowid INTEGER PRIMARY KEY, embedding BLOB)"
    )
    for i, w in enumerate(widths, start=1):
        conn.execute(
            "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
            (i, _packed(w)),
        )
    conn.commit()
    return conn


def test_symbol_is_importable():
    # Regression: cmd_vec_reindex imports this name; it must exist.
    assert hasattr(emb, "sample_db_embedding_widths")


def test_missing_table_reports_not_ok(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    out = emb.sample_db_embedding_widths(conn)
    # The reindex guard only fires on ok AND sample_count>0 AND not consistent,
    # so a missing table must read as "nothing to check" → proceed.
    assert out["ok"] is False
    assert out["sample_count"] == 0
    assert out["consistent"] is True


def test_empty_table_is_consistent(tmp_path):
    conn = _plain_vec_table(tmp_path, [])
    out = emb.sample_db_embedding_widths(conn)
    assert out["ok"] is True
    assert out["sample_count"] == 0
    assert out["observed_dims"] == []
    assert out["consistent"] is True


def test_uniform_width_matches_declared(tmp_path, monkeypatch):
    conn = _plain_vec_table(tmp_path, [768, 768, 768])
    monkeypatch.setattr(emb, "get_db_embedding_dim", lambda c: 768)
    out = emb.sample_db_embedding_widths(conn)
    assert out["ok"] is True
    assert out["observed_dims"] == [768]
    assert out["declared_dim"] == 768
    assert out["consistent"] is True


def test_uniform_width_mismatches_declared(tmp_path, monkeypatch):
    conn = _plain_vec_table(tmp_path, [768, 768])
    monkeypatch.setattr(emb, "get_db_embedding_dim", lambda c: 1024)
    out = emb.sample_db_embedding_widths(conn)
    assert out["consistent"] is False
    assert out["observed_dims"] == [768]
    assert out["declared_dim"] == 1024


def test_mixed_widths_flagged_inconsistent(tmp_path, monkeypatch):
    conn = _plain_vec_table(tmp_path, [768, 1024, 768])
    monkeypatch.setattr(emb, "get_db_embedding_dim", lambda c: 768)
    out = emb.sample_db_embedding_widths(conn)
    assert out["consistent"] is False
    assert out["observed_dims"] == [768, 1024]
    assert out["sample_count"] == 3


def test_unknown_declared_dim_checks_only_internal_consistency(tmp_path, monkeypatch):
    conn = _plain_vec_table(tmp_path, [512, 512])
    monkeypatch.setattr(emb, "get_db_embedding_dim", lambda c: None)
    out = emb.sample_db_embedding_widths(conn)
    assert out["consistent"] is True
    assert out["observed_dims"] == [512]


def test_sample_size_limits_rows(tmp_path, monkeypatch):
    conn = _plain_vec_table(tmp_path, [768] * 20)
    monkeypatch.setattr(emb, "get_db_embedding_dim", lambda c: 768)
    out = emb.sample_db_embedding_widths(conn, sample_size=5)
    assert out["sample_count"] == 5


def test_real_vec0_table_round_trip(tmp_path):
    """Integration: against an actual sqlite-vec ``vec0`` table the probe must
    read the embedding column as a float32 blob and report the right width."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(str(tmp_path / "v.db"))
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError) as exc:
        pytest.skip(f"sqlite-vec not loadable: {exc}")

    conn.execute("CREATE VIRTUAL TABLE vec_memories USING vec0(embedding float[768])")
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
            (i, _packed(768)),
        )
    conn.commit()

    out = emb.sample_db_embedding_widths(conn)
    assert out["ok"] is True
    assert out["observed_dims"] == [768]
    assert out["declared_dim"] == 768
    assert out["consistent"] is True
