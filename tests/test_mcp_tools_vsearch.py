"""Focused tests for the vsearch MCP tool — happy-path with mocked Ollama/vec."""
from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_meb as meb_mod
from agentmemory.brain import Brain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _packed_floats(n: int = 768) -> bytes:
    return struct.pack(f"{n}f", *([0.1] * n))


def _make_vec_conn_mock(memories: list[dict] | None = None) -> MagicMock:
    """Return a MagicMock mimicking a sqlite-vec-enabled connection.

    Handles two query patterns that tool_vsearch issues:
      1. SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ...
      2. SELECT id, content, ... FROM memories WHERE id IN (...)
    """
    if memories is None:
        memories = [
            {"id": 1, "content": "Use pytest", "category": "convention",
             "scope": None, "confidence": 1.0, "created_at": "2026-01-01T00:00:00Z",
             "recalled_count": 0, "temporal_class": None, "last_recalled_at": None},
        ]

    vec_rows = [{"rowid": m["id"], "distance": 0.1 * (i + 1)} for i, m in enumerate(memories)]
    dist_map = {r["rowid"]: r["distance"] for r in vec_rows}

    def _execute(sql, params=None):
        result = MagicMock()
        if "vec_memories" in sql:
            result.fetchall.return_value = vec_rows
        elif "memories_fts" in sql:
            # FTS5 re-rank phase
            fts_rows = [{"rowid": m["id"], "rank": -1.0} for m in memories]
            result.fetchall.return_value = fts_rows
        elif "FROM memories WHERE" in sql:
            result.fetchall.return_value = memories
        else:
            result.fetchall.return_value = []
        return result

    conn = MagicMock(spec=sqlite3.Connection)
    conn.row_factory = sqlite3.Row
    conn.execute.side_effect = _execute
    conn.close = MagicMock()
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Fresh Brain DB, point the MEB module at it."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
    return brain


@pytest.fixture
def brain_with_memories(tmp_path, monkeypatch):
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
    brain.remember("Use pytest for all tests", category="convention")
    brain.remember("Python 3.12 required", category="environment")
    return brain


# ---------------------------------------------------------------------------
# vsearch happy-path tests (mocked Ollama + mocked vec connection)
# ---------------------------------------------------------------------------


class TestVsearchHappyPath:
    def test_vsearch_returns_ok_true_with_mocked_vec(self, monkeypatch):
        """vsearch returns ok=True when both embedding and vec conn are mocked."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="pytest convention", tables="memories")

        assert result["ok"] is True

    def test_vsearch_returns_memories_list(self, monkeypatch):
        """vsearch result contains a 'memories' key with a list."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="convention", tables="memories")

        assert result["ok"] is True
        assert "memories" in result
        assert isinstance(result["memories"], list)

    def test_vsearch_result_count_matches_vec_rows(self, monkeypatch):
        """Number of memory results matches what the vec query returns."""
        embedding = _packed_floats(768)
        memories = [
            {"id": 1, "content": "m1", "category": "a", "scope": None, "confidence": 1.0,
             "created_at": "2026-01-01T00:00:00Z", "recalled_count": 0,
             "temporal_class": None, "last_recalled_at": None},
            {"id": 2, "content": "m2", "category": "b", "scope": None, "confidence": 0.9,
             "created_at": "2026-01-01T00:00:00Z", "recalled_count": 0,
             "temporal_class": None, "last_recalled_at": None},
        ]
        vec_conn = _make_vec_conn_mock(memories=memories)

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="something", tables="memories", vec_only=True)

        assert result["ok"] is True
        assert len(result["memories"]) == 2

    def test_vsearch_results_have_id_and_content(self, monkeypatch):
        """Each result dict must contain 'id' and 'content' fields."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="test", tables="memories", vec_only=True)

        assert result["ok"] is True
        for r in result["memories"]:
            assert "id" in r
            assert "content" in r

    def test_vsearch_results_have_distance_and_score(self, monkeypatch):
        """Results must contain 'distance' and 'score' for downstream scoring."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="test", tables="memories", vec_only=True)

        assert result["ok"] is True
        for r in result["memories"]:
            assert "distance" in r
            assert "score" in r

    def test_vsearch_score_is_inverse_of_distance(self, monkeypatch):
        """score should be approximately 1 - distance for vec-only mode."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="test", tables="memories", vec_only=True)

        assert result["ok"] is True
        for r in result["memories"]:
            assert abs(r["score"] - (1.0 - r["distance"])) < 0.01

    def test_vsearch_respects_limit(self, monkeypatch):
        """limit parameter caps the result count."""
        embedding = _packed_floats(768)
        # Provide 5 mock memories
        memories = [
            {"id": i, "content": f"mem{i}", "category": "x", "scope": None,
             "confidence": 1.0, "created_at": "2026-01-01T00:00:00Z",
             "recalled_count": 0, "temporal_class": None, "last_recalled_at": None}
            for i in range(1, 6)
        ]
        vec_conn = _make_vec_conn_mock(memories=memories)

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(
                    query="test", tables="memories", limit=2, vec_only=True
                )

        assert result["ok"] is True
        assert len(result["memories"]) <= 2

    def test_vsearch_mode_field_present_in_result(self, monkeypatch):
        """Result must include a 'mode' field describing the search strategy."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="test", tables="memories")

        assert result["ok"] is True
        assert "mode" in result

    def test_vsearch_query_echoed_in_result(self, monkeypatch):
        """Result must echo back the query string."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="my search query", tables="memories")

        assert result["ok"] is True
        assert result.get("query") == "my search query"

    def test_vsearch_fails_gracefully_when_embed_fails(self, monkeypatch):
        """If embedding fails (None), vsearch returns ok=False with an error message."""
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=None):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.tool_vsearch(query="test", tables="memories")

        assert result["ok"] is False
        assert "error" in result

    def test_vsearch_empty_query_still_errors(self, monkeypatch):
        """Empty query returns error regardless of vec availability."""
        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        result = meb_mod.tool_vsearch(query="")
        assert result["ok"] is False
        assert "error" in result

    def test_vsearch_unavailable_when_dylib_none(self, monkeypatch):
        """VEC_DYLIB=None → ok=False, even with valid query."""
        monkeypatch.setattr(meb_mod, "VEC_DYLIB", None)
        result = meb_mod.tool_vsearch(query="hello world")
        assert result["ok"] is False
        assert "sqlite-vec" in result["error"] or "not available" in result["error"]

    def test_vsearch_dispatch_happy_path(self, monkeypatch):
        """DISPATCH['vsearch'] routes correctly and returns ok=True on success."""
        embedding = _packed_floats(768)
        vec_conn = _make_vec_conn_mock()

        monkeypatch.setattr(meb_mod, "VEC_DYLIB", "/fake/vec0.so")
        with patch.object(meb_mod, "_embed_query_safe", return_value=embedding):
            with patch.object(meb_mod, "_get_vec_db", return_value=vec_conn):
                result = meb_mod.DISPATCH["vsearch"](
                    {"query": "pytest", "tables": "memories"}
                )

        assert result["ok"] is True
