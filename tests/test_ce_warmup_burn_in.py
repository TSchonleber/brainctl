"""Regression lock for the CE warmup burn-in fix — audit I23.

Before 2.5.0, the first cross-encoder rerank call (which includes model
loading, typically 15-40s for bge-reranker-v2-m3) appended its latency
straight into `_CE_LATENCY_SAMPLES_MS`. That one cold-start sample
poisoned the rolling p95 for the full 64-call deque rotation — CE was
silently skipped for the next ~minute of queries under the strict
latency fallback, and the only visible signal was a lack of
`cross_encoder_applied` entries in `_debug_skips`.

This test exercises the burn-in counter directly, without pulling in a
real cross-encoder model — we simulate the effect of a warmup sample vs
a normal sample on the rolling window.
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_warmup_counter_starts_at_zero_and_excludes_first_sample():
    """The first call should increment _CE_WARMUP_SEEN without
    appending to the rolling window; subsequent calls should append
    normally. The logic we're locking lives at `_impl.py` around the
    `is_warmup = _CE_WARMUP_SEEN[0] < _CE_WARMUP_SAMPLES` check.
    """
    from agentmemory import _impl

    # Reset module state so tests are hermetic.
    _impl._CE_WARMUP_SEEN[0] = 0
    _impl._CE_LATENCY_SAMPLES_MS.clear()

    # Simulate 1 warmup + 5 normal calls with the same logic the hot path uses.
    samples_ms = [40000.0, 120.0, 90.0, 150.0, 200.0, 180.0]
    for ce_ms in samples_ms:
        is_warmup = _impl._CE_WARMUP_SEEN[0] < _impl._CE_WARMUP_SAMPLES
        if is_warmup:
            _impl._CE_WARMUP_SEEN[0] += 1
        else:
            _impl._CE_LATENCY_SAMPLES_MS.append(ce_ms)

    # The 40s warmup sample was NOT appended — the rolling window only
    # contains the 5 post-warmup samples.
    assert _impl._CE_WARMUP_SEEN[0] == 1
    assert list(_impl._CE_LATENCY_SAMPLES_MS) == samples_ms[1:]

    # p95 on the realistic sample set is well under the 350ms budget.
    assert _impl._p95_ms(_impl._CE_LATENCY_SAMPLES_MS) < 350.0


def test_warmup_disabled_by_env_var_zero(monkeypatch):
    """Setting BRAINCTL_CE_WARMUP_SAMPLES=0 must record every sample —
    the knob exists so operators can opt out if they want the old
    behavior (e.g. for deterministic benchmarks where they guarantee
    the model is already loaded)."""
    monkeypatch.setenv("BRAINCTL_CE_WARMUP_SAMPLES", "0")

    import importlib
    from agentmemory import _impl
    importlib.reload(_impl)

    _impl._CE_WARMUP_SEEN[0] = 0
    _impl._CE_LATENCY_SAMPLES_MS.clear()

    # With warmup=0, the first sample IS recorded.
    is_warmup = _impl._CE_WARMUP_SEEN[0] < _impl._CE_WARMUP_SAMPLES
    assert is_warmup is False
