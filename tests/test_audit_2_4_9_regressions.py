"""Regression locks for the 2.4.9 audit follow-up fixes.

These tests don't exercise behavior that any other test covers — they
exist so a later "simplify" pass can't silently revert the fixes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# --- I9: cmd_doctor reflects health in exit code + ok flag ---------------

def test_cmd_doctor_exits_nonzero_on_issues(tmp_path, monkeypatch, capsys):
    import argparse
    import json as _json
    from agentmemory import _impl

    # Fresh virgin DB guaranteed to surface at least one issue
    # (schema_versions empty but init_schema has late-migration columns).
    db = tmp_path / "brain.db"
    from agentmemory.brain import Brain

    import os
    os.environ["BRAINCTL_SILENT_MIGRATIONS"] = "1"
    Brain(str(db))
    os.environ.pop("BRAINCTL_SILENT_MIGRATIONS", None)

    monkeypatch.setattr(_impl, "DB_PATH", db)
    monkeypatch.setattr(
        _impl, "get_db", lambda *a, **k: sqlite3.connect(str(db))
    )

    args = argparse.Namespace(json=True)
    with pytest.raises(SystemExit) as exc:
        _impl.cmd_doctor(args)
    assert exc.value.code == 1, "doctor must exit 1 when issues present"

    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["healthy"] is False
    assert len(payload["issues"]) >= 1


# --- I16: federation LIKE escaping ---------------------------------------

def test_federation_escape_like_prevents_wildcard_injection():
    from agentmemory.federation import _escape_like

    assert _escape_like("api_key") == "api\\_key"
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a\\b") == "a\\\\b"

    # Behavioral: escaped + ESCAPE '\' must not treat `_` as wildcard.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (c TEXT)")
    conn.executemany(
        "INSERT INTO t(c) VALUES (?)",
        [("api_key",), ("apikey",), ("api-key",), ("100%",), ("100",)],
    )

    unescaped = [r[0] for r in conn.execute(
        "SELECT c FROM t WHERE c LIKE ?", ("%api_key%",)
    ).fetchall()]
    escaped = [r[0] for r in conn.execute(
        "SELECT c FROM t WHERE c LIKE ? ESCAPE '\\'",
        (f"%{_escape_like('api_key')}%",),
    ).fetchall()]
    # Pre-fix behavior: `_` is a wildcard, matches `api-key` too.
    assert "api-key" in unescaped
    # Post-fix: literal `_`, matches only the real underscore string.
    assert escaped == ["api_key"]


# --- I17: merge guards against source == target --------------------------

def test_merge_rejects_self_merge(tmp_path):
    from agentmemory import merge
    from agentmemory.brain import Brain

    db = tmp_path / "self.db"
    Brain(db_path=str(db))

    # Direct equality.
    with pytest.raises(ValueError, match="merge a database with itself"):
        merge.merge(source_path=str(db), target_path=str(db))

    # Path-equivalent (resolved form).
    with pytest.raises(ValueError, match="merge a database with itself"):
        merge.merge(
            source_path=str(db),
            target_path=str(db.parent / "." / "self.db"),
        )
