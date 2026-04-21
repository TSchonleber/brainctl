"""Regression locks for the 2.5.0 audit-follow-up fixes.

Locks the I25 / I29 / I31 behavioral fixes so later refactors can't
silently revert them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# --- I25: decision_lookup intent actually adds "decisions" to tables -----

def test_decision_lookup_guard_source_is_correct():
    """Lock the exact fix at _impl.py:6207-ish — the guard must check
    ``"decisions" not in tables`` (not ``not in results`` — that was
    always False, because ``results`` is pre-initialized with a
    "decisions" key), AND the set union must include "decisions" itself.

    A source-level check is the load-bearing lock here: the alternative
    (reproducing the regression end-to-end through cmd_search) requires
    stubbing out the intent router, which itself has two branches
    (_classify_intent vs _builtin_classify_intent) and an optional bin/
    dependency. A single-line read locks the fix with zero coupling.
    """
    impl_path = SRC / "agentmemory" / "_impl.py"
    src = impl_path.read_text()
    assert "_intent_result.intent == \"decision_lookup\"" in src, (
        "decision_lookup intent handling removed — retrieval for that "
        "intent will regress"
    )
    # The correct guard checks `tables`, not `results`.
    assert '"decisions" not in tables' in src, (
        "decision_lookup guard must check `\"decisions\" not in tables` — "
        "pre-2.5.0 it checked `results` which was always initialized with "
        "that key, making the guard a no-op"
    )
    # The body must actually add "decisions" to the tables set union.
    # Scan the ~15 lines following the guard — the body assignment sits
    # right after the closing `):`.
    after_guard = src.split('"decisions" not in tables', 1)[1]
    body_window = "\n".join(after_guard.splitlines()[:15])
    assert '"decisions"' in body_window, (
        "decision_lookup body must include \"decisions\" in the table set "
        "union — otherwise the guard fires but the decisions table still "
        "isn't searched (audit I25 second bug)"
    )


# --- I29: config loader surfaces permission/OS errors via logging -------

def test_config_load_falls_back_on_permission_error(tmp_path, monkeypatch, caplog):
    from agentmemory import config

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[search]\nlimit = 42\n")
    cfg_file.chmod(0)  # owner-only permissions removed — read will OSError

    monkeypatch.setenv("BRAINCTL_CONFIG", str(cfg_file))
    with caplog.at_level("WARNING", logger="agentmemory.config"):
        try:
            loaded = config.load()
        finally:
            cfg_file.chmod(0o644)  # restore for cleanup
    # Must fall back to defaults (not crash) and must have logged a warning.
    assert isinstance(loaded, dict)
    assert any("cannot read config" in rec.message for rec in caplog.records), (
        "config.load should log a warning when the config file can't be read"
    )


def test_config_load_falls_back_on_malformed_toml(tmp_path, monkeypatch, caplog):
    from agentmemory import config

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("this is = not valid = toml = at all")
    monkeypatch.setenv("BRAINCTL_CONFIG", str(cfg_file))
    with caplog.at_level("WARNING", logger="agentmemory.config"):
        loaded = config.load()
    assert isinstance(loaded, dict)
    assert any("not valid TOML" in rec.message for rec in caplog.records)


# --- I31: federated search reports both total and returned counts --------

def test_federated_memory_search_reports_both_counts(tmp_path, monkeypatch):
    from agentmemory import federation
    from agentmemory.brain import Brain

    db = tmp_path / "brain.db"
    b = Brain(db_path=str(db))
    # Use a distinctive word that FTS5 tokenizes identically across rows.
    for i in range(5):
        b.remember(f"widgetron fixture row {i}", category="lesson")

    monkeypatch.setenv("BRAIN_DB", str(db))
    monkeypatch.delenv("BRAIN_DB_FEDERATION", raising=False)

    r = federation.federated_memory_search("widgetron", limit=3)
    assert r["ok"] is True, r
    assert "returned_count" in r, (
        "federated_memory_search must expose `returned_count` so callers "
        "can branch on returned vs total without inferring from len()"
    )
    assert r["returned_count"] == len(r["results"]), (
        "returned_count must equal len(results) by construction"
    )
    assert r["returned_count"] <= 3, "returned count must respect the limit"
    assert r["total_results"] >= r["returned_count"], (
        "total_results must never undercount the returned slice"
    )
