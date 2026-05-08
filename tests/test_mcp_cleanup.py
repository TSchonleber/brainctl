"""Tests for ``bin/brainctl-mcp-cleanup``.

The HIGH-priority bug we're regressing against: macOS BSD ``ps`` does
not support ``etimes`` (Linux-only seconds-counter flag). The original
helper requested ``etimes`` and silently returned an empty list when ps
exited with an error — making the cleanup script blind to live MCP
processes on the very platform we ship on.

These tests pin:

* The portable ``etime`` parser handles all three BSD/POSIX formats.
* The classifier produces correct flags (ORPHAN / HOLDS_DB / STALE /
  LIVE) given representative process states.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Module loader — bin/brainctl-mcp-cleanup is an executable Python file
# without a .py extension, so we import it via importlib spec_from_loader.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cleanup_mod():
    from agentmemory import mcp_cleanup
    return mcp_cleanup


# ---------------------------------------------------------------------------
# _parse_etime — the bug at the heart of the Codex review finding.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # MM:SS — under 1 hour
    ("00:00", 0),
    ("00:30", 30),
    ("01:30", 90),
    ("59:59", 59 * 60 + 59),
    # HH:MM:SS — under 1 day
    ("01:00:00", 3600),
    ("12:34:56", 12 * 3600 + 34 * 60 + 56),
    ("23:59:59", 23 * 3600 + 59 * 60 + 59),
    # DD-HH:MM:SS — 1 day or more (this was the hardest format to
    # eyeball in the original ps output)
    ("01-00:00:00", 86400),
    ("03-00:49:18", 3 * 86400 + 49 * 60 + 18),
    ("10-12:34:56", 10 * 86400 + 12 * 3600 + 34 * 60 + 56),
])
def test_parse_etime_valid_formats(cleanup_mod, raw, expected):
    assert cleanup_mod._parse_etime(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "garbage",
    "abc:def",
    "12-bad:ok:00",
    "1:2:3:4",        # too many fields
    "x:y",
])
def test_parse_etime_unparseable_returns_none(cleanup_mod, raw):
    """Unparseable input must NOT raise. The cleanup helper falls back
    to age=0 so a single garbled row doesn't poison the whole report."""
    assert cleanup_mod._parse_etime(raw) is None


def test_parse_etime_handles_leading_whitespace(cleanup_mod):
    """ps right-pads the etime column; the parser must handle it."""
    assert cleanup_mod._parse_etime("       29:27") == 29 * 60 + 27


# ---------------------------------------------------------------------------
# _classify — flag assignment.
# ---------------------------------------------------------------------------


def test_classify_marks_orphan_when_ppid_is_init(cleanup_mod, monkeypatch):
    """ppid == 1 means launchd/init reparented us — true orphan."""
    monkeypatch.setattr(cleanup_mod, "_holders_of_brain_db", lambda: set())
    monkeypatch.setattr(cleanup_mod, "_proc_name", lambda pid: "launchd")
    out = cleanup_mod._classify(
        [{"pid": 100, "ppid": 1, "age_sec": 60, "user": "u", "command": "x"}],
        age_hours=24,
    )
    assert "ORPHAN" in out[0]["flags"]


def test_classify_marks_holds_db_when_lsof_says_so(cleanup_mod, monkeypatch):
    monkeypatch.setattr(cleanup_mod, "_holders_of_brain_db", lambda: {200})
    monkeypatch.setattr(cleanup_mod, "_proc_name", lambda pid: "Codex")
    out = cleanup_mod._classify(
        [{"pid": 200, "ppid": 28272, "age_sec": 60, "user": "u", "command": "x"}],
        age_hours=24,
    )
    assert "HOLDS_DB" in out[0]["flags"]


def test_classify_marks_stale_past_age_threshold(cleanup_mod, monkeypatch):
    monkeypatch.setattr(cleanup_mod, "_holders_of_brain_db", lambda: set())
    monkeypatch.setattr(cleanup_mod, "_proc_name", lambda pid: "Codex")
    # 25h > 24h threshold
    out = cleanup_mod._classify(
        [{"pid": 300, "ppid": 28272, "age_sec": 25 * 3600,
          "user": "u", "command": "x"}],
        age_hours=24,
    )
    assert "STALE" in out[0]["flags"]


def test_classify_marks_live_when_no_other_flags(cleanup_mod, monkeypatch):
    """A young process with a live parent and no DB lock should be
    flagged LIVE so an operator knows NOT to kill it blindly."""
    monkeypatch.setattr(cleanup_mod, "_holders_of_brain_db", lambda: set())
    monkeypatch.setattr(cleanup_mod, "_proc_name", lambda pid: "Codex")
    out = cleanup_mod._classify(
        [{"pid": 400, "ppid": 28272, "age_sec": 60, "user": "u", "command": "x"}],
        age_hours=24,
    )
    assert out[0]["flags"] == ["LIVE"]


def test_classify_can_combine_flags(cleanup_mod, monkeypatch):
    """STALE + HOLDS_DB on the same row is the worst case — both flags
    must surface so the operator can choose the most aggressive
    (and risky) cleanup path."""
    monkeypatch.setattr(cleanup_mod, "_holders_of_brain_db", lambda: {500})
    monkeypatch.setattr(cleanup_mod, "_proc_name", lambda pid: "Codex")
    out = cleanup_mod._classify(
        [{"pid": 500, "ppid": 28272, "age_sec": 48 * 3600,
          "user": "u", "command": "x"}],
        age_hours=24,
    )
    flags = out[0]["flags"]
    assert "STALE" in flags
    assert "HOLDS_DB" in flags
    assert "LIVE" not in flags  # LIVE means "no other flags"
