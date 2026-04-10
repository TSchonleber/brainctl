"""Tests for src/agentmemory/vec.py — embedding and vector-search helpers."""
from __future__ import annotations

import json
import sqlite3
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.vec as vec_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ollama_response(n_dims: int = 768):
    """Return a mock urlopen context-manager that yields n_dims floats."""
    floats = [0.1] * n_dims
    body = json.dumps({"embeddings": [floats]}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _packed_floats(n_dims: int = 768) -> bytes:
    return struct.pack(f"{n_dims}f", *([0.1] * n_dims))


# ---------------------------------------------------------------------------
# embed_text
# ---------------------------------------------------------------------------


class TestEmbedText:
    def test_embed_text_returns_bytes_on_success(self):
        """Mock urlopen → verify bytes returned with correct length (768*4)."""
        with patch("urllib.request.urlopen", return_value=_fake_ollama_response(768)):
            result = vec_mod.embed_text("hello world")
        assert isinstance(result, bytes)
        assert len(result) == 768 * 4

    def test_embed_text_returns_none_on_network_error(self):
        """URLError → embed_text must return None, not raise."""
        import urllib.error
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = vec_mod.embed_text("hello")
        assert result is None

    def test_embed_text_returns_none_on_bad_response(self):
        """Response JSON missing 'embeddings' key → returns None."""
        bad_body = json.dumps({"error": "model not found"}).encode()
        resp = MagicMock()
        resp.read.return_value = bad_body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = vec_mod.embed_text("hello")
        assert result is None

    def test_embed_text_returns_none_on_empty_embeddings(self):
        """Empty embeddings list → returns None."""
        bad_body = json.dumps({"embeddings": [[]]}).encode()
        resp = MagicMock()
        resp.read.return_value = bad_body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = vec_mod.embed_text("hello")
        assert result is None

    def test_embed_dimensions_configurable(self, monkeypatch):
        """BRAINCTL_EMBED_DIMENSIONS env var changes the dimensionality returned by
        _embed_dimensions() and used in init_vec_tables/index_memory."""
        monkeypatch.setenv("BRAINCTL_EMBED_DIMENSIONS", "384")
        # _embed_dimensions reads the env var each call — verify it respects 384
        dims = vec_mod._embed_dimensions()
        assert dims == 384

        # Verify embed_text still packs whatever Ollama returns (384 floats here)
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_ollama_response(384),
        ):
            result = vec_mod.embed_text("hello")
        assert isinstance(result, bytes)
        assert len(result) == 384 * 4


# ---------------------------------------------------------------------------
# init_vec_tables
# ---------------------------------------------------------------------------


class TestInitVecTables:
    def test_init_vec_tables_noop_when_vec_unavailable(self):
        """When _find_vec_dylib returns None, init_vec_tables returns False."""
        with patch.object(vec_mod, "_find_vec_dylib", return_value=None):
            conn = MagicMock(spec=sqlite3.Connection)
            result = vec_mod.init_vec_tables(conn)
        assert result is False
        conn.execute.assert_not_called()

    def test_init_vec_tables_creates_table_when_vec_available(self):
        """When dylib is found and load_extension succeeds, CREATE TABLE is called."""
        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            conn = MagicMock(spec=sqlite3.Connection)
            result = vec_mod.init_vec_tables(conn)
        assert result is True
        # Verify enable_load_extension was toggled on/off
        conn.enable_load_extension.assert_any_call(True)
        conn.enable_load_extension.assert_any_call(False)
        # Verify load_extension was called with the fake path
        conn.load_extension.assert_called_once_with("/fake/vec0.so")
        # Verify CREATE VIRTUAL TABLE was executed
        create_calls = [
            str(c) for c in conn.execute.call_args_list
            if "CREATE VIRTUAL TABLE" in str(c)
        ]
        assert create_calls, "CREATE VIRTUAL TABLE IF NOT EXISTS should have been called"

    def test_init_vec_tables_returns_false_on_load_extension_error(self):
        """If load_extension raises, init_vec_tables returns False (not raises)."""
        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            conn = MagicMock(spec=sqlite3.Connection)
            conn.load_extension.side_effect = sqlite3.OperationalError("cannot open shared object")
            result = vec_mod.init_vec_tables(conn)
        assert result is False


# ---------------------------------------------------------------------------
# index_memory
# ---------------------------------------------------------------------------


class TestIndexMemory:
    def _make_conn_stub(self, tmp_path) -> sqlite3.Connection:
        """Create a real in-memory-ish SQLite connection for testing."""
        db_file = tmp_path / "stub.db"
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        return conn

    def test_index_memory_calls_embed(self, tmp_path):
        """embed_text is called with the supplied content string."""
        conn = self._make_conn_stub(tmp_path)
        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=None) as mock_embed:
                vec_mod.index_memory(conn, 1, "some memory content")
        mock_embed.assert_called_once_with("some memory content")
        conn.close()

    def test_index_memory_returns_false_when_vec_unavailable(self, tmp_path):
        """When dylib not found, index_memory returns False immediately."""
        conn = self._make_conn_stub(tmp_path)
        with patch.object(vec_mod, "_find_vec_dylib", return_value=None):
            result = vec_mod.index_memory(conn, 1, "content")
        assert result is False
        conn.close()

    def test_index_memory_returns_false_on_embed_failure(self, tmp_path):
        """embed_text returns None → index_memory returns False without raising."""
        conn = self._make_conn_stub(tmp_path)
        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=None):
                result = vec_mod.index_memory(conn, 42, "failed embed")
        assert result is False
        conn.close()

    def test_index_memory_inserts_into_vec_memories(self, tmp_path):
        """Happy path: embedding produced and INSERT OR REPLACE called on vec conn."""
        embedding = _packed_floats(768)
        conn = self._make_conn_stub(tmp_path)

        # We mock sqlite3.connect so we intercept the second connection that
        # index_memory opens with sqlite-vec loaded.
        vec_conn_mock = MagicMock()
        vec_conn_mock.execute.return_value = MagicMock()
        vec_conn_mock.__enter__ = lambda s: s
        vec_conn_mock.__exit__ = MagicMock(return_value=False)

        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=embedding):
                with patch("sqlite3.connect", return_value=vec_conn_mock):
                    result = vec_mod.index_memory(conn, 7, "test content")

        assert result is True
        # The INSERT OR REPLACE call must include memory_id=7 and the embedding bytes
        insert_calls = [
            c for c in vec_conn_mock.execute.call_args_list
            if "INSERT OR REPLACE" in str(c)
        ]
        assert insert_calls, "INSERT OR REPLACE should have been called on vec_conn"
        args = insert_calls[0][0]  # positional args of first matching call
        assert args[1][0] == 7  # memory_id
        assert args[1][1] == embedding
        conn.close()

    def test_index_memory_returns_true_on_success(self, tmp_path):
        """index_memory returns True when embedding and insert both succeed."""
        embedding = _packed_floats(768)
        conn = self._make_conn_stub(tmp_path)
        vec_conn_mock = MagicMock()
        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=embedding):
                with patch("sqlite3.connect", return_value=vec_conn_mock):
                    result = vec_mod.index_memory(conn, 99, "any content")
        assert result is True
        conn.close()


# ---------------------------------------------------------------------------
# vec_search
# ---------------------------------------------------------------------------


def _make_conn_mock(db_path: str) -> MagicMock:
    """Return a MagicMock for a sqlite3.Connection that returns a PRAGMA db path."""
    pragma_result = MagicMock()
    pragma_result.fetchone.return_value = (0, "main", db_path)
    conn = MagicMock(spec=sqlite3.Connection)
    conn.execute.return_value = pragma_result
    return conn


def _make_vec_conn_mock_for_search(
    vec_rows: list[dict],
    src_rows: list[dict],
) -> MagicMock:
    """MagicMock connection that dispatches by SQL keyword."""
    def _execute(sql, params=None):
        result = MagicMock()
        if "vec_memories" in sql:
            result.fetchall.return_value = vec_rows
        elif "FROM memories" in sql:
            result.fetchall.return_value = src_rows
        else:
            result.fetchall.return_value = []
        return result

    conn = MagicMock(spec=sqlite3.Connection)
    conn.execute.side_effect = _execute
    conn.close = MagicMock()
    return conn


class TestVecSearch:
    def test_vec_search_returns_empty_when_vec_unavailable(self, tmp_path):
        """No dylib → vec_search returns []."""
        conn = _make_conn_mock(str(tmp_path / "stub.db"))
        with patch.object(vec_mod, "_find_vec_dylib", return_value=None):
            result = vec_mod.vec_search(conn, "query")
        assert result == []

    def test_vec_search_returns_empty_on_embed_failure(self, tmp_path):
        """embed_text returns None → vec_search returns []."""
        conn = _make_conn_mock(str(tmp_path / "stub.db"))
        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=None):
                result = vec_mod.vec_search(conn, "query")
        assert result == []

    def test_vec_search_returns_results_when_vec_available(self, tmp_path):
        """Mock vec conn returns rows → vec_search returns non-empty list."""
        embedding = _packed_floats(768)
        conn = _make_conn_mock(str(tmp_path / "stub.db"))
        vec_rows = [{"rowid": 1, "distance": 0.1}]
        src_rows = [{"id": 1, "content": "dark mode preferred", "category": "preference"}]
        vec_conn = _make_vec_conn_mock_for_search(vec_rows, src_rows)

        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=embedding):
                with patch("sqlite3.connect", return_value=vec_conn):
                    result = vec_mod.vec_search(conn, "dark mode", k=5)

        assert isinstance(result, list)
        assert len(result) >= 1

    def test_vec_search_results_have_required_fields(self, tmp_path):
        """Results must contain id, content, category, distance."""
        embedding = _packed_floats(768)
        conn = _make_conn_mock(str(tmp_path / "stub.db"))
        vec_rows = [{"rowid": 2, "distance": 0.3}]
        src_rows = [{"id": 2, "content": "use pytest", "category": "convention"}]
        vec_conn = _make_vec_conn_mock_for_search(vec_rows, src_rows)

        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=embedding):
                with patch("sqlite3.connect", return_value=vec_conn):
                    result = vec_mod.vec_search(conn, "pytest", k=5)

        assert len(result) == 1
        r = result[0]
        assert "id" in r
        assert "content" in r
        assert "category" in r
        assert "distance" in r

    def test_vec_search_respects_k(self, tmp_path):
        """k parameter is passed through to the vec0 MATCH query."""
        embedding = _packed_floats(768)
        conn = _make_conn_mock(str(tmp_path / "stub.db"))
        captured_k: list[int] = []

        def _execute(sql, params=None):
            result = MagicMock()
            if "vec_memories" in sql and params:
                captured_k.append(params[1])  # second param is k
            result.fetchall.return_value = []
            return result

        vec_conn = MagicMock(spec=sqlite3.Connection)
        vec_conn.execute.side_effect = _execute
        vec_conn.close = MagicMock()

        with patch.object(vec_mod, "_find_vec_dylib", return_value="/fake/vec0.so"):
            with patch.object(vec_mod, "embed_text", return_value=embedding):
                with patch("sqlite3.connect", return_value=vec_conn):
                    vec_mod.vec_search(conn, "query", k=3)

        assert 3 in captured_k, f"k=3 should be passed to vec query, got: {captured_k}"


# ---------------------------------------------------------------------------
# brain.remember() integration with vec indexing
# ---------------------------------------------------------------------------


class TestBrainRememberVecHook:
    def test_brain_remember_triggers_indexing(self, tmp_path, monkeypatch):
        """After brain.remember(), vec.index_memory must be called."""
        import agentmemory.brain as brain_mod

        db_file = tmp_path / "brain.db"
        from agentmemory.brain import Brain

        with patch.object(brain_mod, "_VEC_AVAILABLE", True):
            with patch.object(brain_mod, "_vec") as mock_vec_module:
                mock_vec_module.index_memory.return_value = True
                brain = Brain(db_path=str(db_file), agent_id="tester")
                mid = brain.remember("test memory content", category="test")

        assert isinstance(mid, int)
        mock_vec_module.index_memory.assert_called_once()
        call_args = mock_vec_module.index_memory.call_args[0]
        assert call_args[1] == mid
        assert call_args[2] == "test memory content"

    def test_brain_remember_succeeds_even_when_vec_fails(self, tmp_path, monkeypatch):
        """index_memory raises → remember() still returns a valid int ID."""
        import agentmemory.brain as brain_mod

        db_file = tmp_path / "brain.db"
        from agentmemory.brain import Brain

        with patch.object(brain_mod, "_VEC_AVAILABLE", True):
            with patch.object(brain_mod, "_vec") as mock_vec_module:
                mock_vec_module.index_memory.side_effect = RuntimeError("vec exploded")
                brain = Brain(db_path=str(db_file), agent_id="tester")
                mid = brain.remember("should still work")

        assert isinstance(mid, int)
        assert mid > 0

    def test_brain_remember_skips_vec_when_unavailable(self, tmp_path):
        """When _VEC_AVAILABLE is False, index_memory is never called."""
        import agentmemory.brain as brain_mod

        db_file = tmp_path / "brain.db"
        from agentmemory.brain import Brain

        with patch.object(brain_mod, "_VEC_AVAILABLE", False):
            with patch.object(brain_mod, "_vec") as mock_vec_module:
                brain = Brain(db_path=str(db_file), agent_id="tester")
                brain.remember("hello without vec")

        mock_vec_module.index_memory.assert_not_called()
