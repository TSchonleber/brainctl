"""Regression tests for timezone-aware/naive datetime handling (issue #168).

``dream-cycle`` / ``dream-daemon`` crashed in the NREM Hebbian pass because
``hippocampus.run_hebbian_pass`` builds a naive ``now = datetime.now()`` and
diffs it (via ``days_since``) against ``created_at`` values that ``memory_add``
writes as *aware* UTC (``_utc_now_iso`` -> trailing ``Z``). ``parse_ts`` turned
the ``Z`` suffix into an aware datetime, so ``now - dt`` raised
``TypeError: can't subtract offset-naive and offset-aware datetimes``.

The fix normalizes every parsed timestamp to aware UTC and coerces a naive
``now`` to aware UTC inside ``days_since`` so the subtraction is always
aware - aware, regardless of which writer produced the stored value.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.hippocampus import days_since, parse_ts


def test_parse_ts_always_returns_aware():
    """Both aware (``Z``) and naive stored strings parse to aware UTC."""
    aware = parse_ts("2026-04-20T23:16:04Z")
    naive_assumed_utc = parse_ts("2026-04-20T23:16:04")
    assert aware.tzinfo is not None
    assert naive_assumed_utc.tzinfo is not None
    # A naive string is interpreted as UTC, so the two are the same instant.
    assert aware == naive_assumed_utc


def test_days_since_naive_now_vs_aware_timestamp_does_not_crash():
    """The exact #168 crash: naive ``now`` (datetime.now()) vs aware UTC
    ``created_at`` (memory_add). Must return a value, not raise TypeError."""
    naive_now = datetime(2026, 4, 21, 0, 0, 0)  # what datetime.now() yields
    aware_created_at = "2026-04-20T23:16:04Z"  # what _utc_now_iso() writes
    result = days_since(naive_now, aware_created_at)
    assert result >= 0.0


def test_days_since_aware_now_vs_naive_timestamp_does_not_crash():
    """The mirror case: aware ``now`` vs a naive stored ``last_recalled_at``
    (apply_recall_boost wrote naive local time)."""
    aware_now = datetime.now(timezone.utc)
    naive_recalled_at = "2026-01-01T00:00:00"
    assert days_since(aware_now, naive_recalled_at) >= 0.0


def test_days_since_computes_expected_age():
    """Age math stays correct across the aware/naive boundary."""
    now = datetime(2026, 1, 11, 0, 0, 0)  # naive
    older = days_since(now, "2026-01-01T00:00:00Z")  # ~10 days
    newer = days_since(now, "2026-01-10T00:00:00Z")  # ~1 day
    assert round(older) == 10
    assert round(newer) == 1
    assert older > newer


def test_days_since_none_timestamp_is_zero():
    assert days_since(datetime.now(timezone.utc), None) == 0.0
