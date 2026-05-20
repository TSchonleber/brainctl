"""Tests for sigmoid_gate — issue #116 Phase 1-C.

Covers:
  - SigmoidParams validation
  - sigmoid() endpoint behavior + midpoint crossing
  - normalize_rank() edge cases
  - weight_for_rank() ordering
  - weights_for_results() shape
  - annotate_with_weights() mutation + idempotence
"""
from __future__ import annotations

import pytest

from agentmemory.sigmoid_gate import (
    DEFAULT_MIDPOINT,
    DEFAULT_PARAMS,
    DEFAULT_SLOPE,
    SigmoidParams,
    annotate_with_weights,
    normalize_rank,
    sigmoid,
    weight_for_rank,
    weights_for_results,
)


def test_sigmoid_crosses_05_at_midpoint():
    # At x == midpoint, sigmoid output is exactly 0.5 regardless of slope.
    assert sigmoid(0.5, slope=1.0, midpoint=0.5) == pytest.approx(0.5)
    assert sigmoid(0.7, slope=12.0, midpoint=0.7) == pytest.approx(0.5)


def test_sigmoid_approaches_endpoints():
    # Far above midpoint → 1; far below → 0.
    assert sigmoid(100.0, slope=1.0, midpoint=0.0) > 0.999
    assert sigmoid(-100.0, slope=1.0, midpoint=0.0) < 0.001


def test_sigmoid_handles_extreme_overflow():
    # The internal exp() must not raise on extreme inputs.
    assert sigmoid(1e9, slope=1e6, midpoint=0.0) == pytest.approx(1.0)
    assert sigmoid(-1e9, slope=1e6, midpoint=0.0) == pytest.approx(0.0)


def test_sigmoid_monotone_increasing():
    # Strictly monotone in x for fixed positive slope.
    samples = [sigmoid(x / 10.0) for x in range(0, 11)]
    for a, b in zip(samples, samples[1:]):
        assert a < b


def test_sigmoid_params_validates_midpoint():
    with pytest.raises(ValueError):
        SigmoidParams(slope=1.0, midpoint=0.0)
    with pytest.raises(ValueError):
        SigmoidParams(slope=1.0, midpoint=1.0)
    with pytest.raises(ValueError):
        SigmoidParams(slope=1.0, midpoint=-0.1)


def test_sigmoid_params_validates_slope():
    with pytest.raises(ValueError):
        SigmoidParams(slope=0.0, midpoint=0.5)
    with pytest.raises(ValueError):
        SigmoidParams(slope=-1.0, midpoint=0.5)


def test_normalize_rank_endpoints_and_edges():
    # rank=1 (best) maps to 1.0; rank=total maps to 0.0
    assert normalize_rank(1, 10) == 1.0
    assert normalize_rank(10, 10) == 0.0
    # Linear interior — rank 5 of 9 should be exactly 0.5
    assert normalize_rank(5, 9) == pytest.approx(0.5)
    # Singletons
    assert normalize_rank(1, 1) == 1.0
    # Degenerate / clamping
    assert normalize_rank(5, 0) == 0.5
    assert normalize_rank(-3, 10) == 1.0
    assert normalize_rank(99, 10) == 0.0


def test_weight_for_rank_orders_with_position():
    # In a 10-item set, rank 1 must outweigh rank 10 (strictly).
    w_top = weight_for_rank(1, 10)
    w_bot = weight_for_rank(10, 10)
    assert w_top > w_bot
    # And the middle item lands near 0.5 with default midpoint=0.5.
    w_mid = weight_for_rank(5, 9)
    assert w_mid == pytest.approx(0.5, abs=0.01)


def test_weights_for_results_shape_and_monotone():
    weights = weights_for_results(10)
    assert len(weights) == 10
    # Strictly decreasing along rank order (best-first).
    for a, b in zip(weights, weights[1:]):
        assert a > b
    # All in (0, 1).
    for w in weights:
        assert 0.0 < w < 1.0


def test_weights_for_results_empty():
    assert weights_for_results(0) == []
    assert weights_for_results(-3) == []


def test_annotate_with_weights_mutates_in_place_and_preserves_order():
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    out = annotate_with_weights(items)
    assert out is not items  # returns a list copy
    assert [d["id"] for d in out] == [1, 2, 3]
    # Weights present, monotone-decreasing
    weights = [d["_sigmoid_rank_weight"] for d in out]
    assert all(0.0 < w < 1.0 for w in weights)
    for a, b in zip(weights, weights[1:]):
        assert a > b


def test_annotate_does_not_overwrite_existing_weight():
    items = [{"id": 1, "_sigmoid_rank_weight": 0.42}, {"id": 2}]
    out = annotate_with_weights(items)
    assert out[0]["_sigmoid_rank_weight"] == 0.42  # untouched
    assert "_sigmoid_rank_weight" in out[1]         # new


def test_annotate_skips_non_dict_items():
    items = [{"id": 1}, "not-a-dict", {"id": 3}]
    out = annotate_with_weights(items)
    assert "_sigmoid_rank_weight" in out[0]
    assert out[1] == "not-a-dict"
    assert "_sigmoid_rank_weight" in out[2]


def test_default_params_have_sane_values():
    assert DEFAULT_PARAMS.slope == DEFAULT_SLOPE
    assert DEFAULT_PARAMS.midpoint == DEFAULT_MIDPOINT
    # And the defaults must satisfy the constructor's validation.
    SigmoidParams(slope=DEFAULT_SLOPE, midpoint=DEFAULT_MIDPOINT)
