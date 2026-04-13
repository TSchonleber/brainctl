"""Tests for agentmemory.lib.mcp_helpers — the canonical MCP helper module."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from agentmemory.lib.mcp_helpers import (
    now_iso,
    open_db,
    rows_to_list,
    safe_fts,
    tool_error,
    tool_ok,
)


def test_open_db_creates_connection_with_row_factory(tmp_path):
    db_path = tmp_path / "brain.db"
    # Initialise an empty sqlite file so open_db has something to open.
    sqlite3.connect(str(db_path)).close()

    conn = open_db(str(db_path))
    try:
        assert conn.row_factory is sqlite3.Row
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk[0] == 1
    finally:
        conn.close()


def test_open_db_default_path(monkeypatch, tmp_path):
    """Passing no path falls back to agentmemory.paths.get_db_path()."""
    db_path = tmp_path / "default" / "brain.db"
    db_path.parent.mkdir(parents=True)
    sqlite3.connect(str(db_path)).close()

    monkeypatch.setenv("BRAIN_DB", str(db_path))
    conn = open_db()
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_now_iso_format():
    ts = now_iso()
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    # Must be parseable as ISO-8601 once the Z is replaced with +00:00.
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.microsecond == 0


def test_rows_to_list_empty_and_none():
    assert rows_to_list([]) == []
    assert rows_to_list(None) == []


def test_rows_to_list_with_rows(tmp_path):
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
    rows = conn.execute("SELECT * FROM t ORDER BY id").fetchall()
    out = rows_to_list(rows)
    assert out == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    conn.close()


def test_safe_fts_simple_query_runs_against_fts5(tmp_path):
    db_path = tmp_path / "fts.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        conn.execute("INSERT INTO docs(body) VALUES ('hello world foo bar')")
        q = safe_fts("hello world")
        assert q  # non-empty
        rows = conn.execute(
            "SELECT body FROM docs WHERE docs MATCH ?",
            (q,),
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_safe_fts_strips_specials():
    # Only alphanumerics/underscores should survive; tokens joined with OR.
    assert safe_fts("foo!@# bar") == "foo OR bar"
    assert safe_fts("") == ""
    assert safe_fts("!!!") == ""


def test_tool_error_shape():
    s = tool_error("boom")
    payload = json.loads(s)
    assert payload == {"ok": False, "error": "boom", "code": "error"}


def test_tool_error_custom_code():
    payload = json.loads(tool_error("nope", code="not_found"))
    assert payload == {"ok": False, "error": "nope", "code": "not_found"}


def test_tool_ok_shape():
    payload = json.loads(tool_ok({"count": 5}))
    assert payload == {"ok": True, "result": {"count": 5}}
