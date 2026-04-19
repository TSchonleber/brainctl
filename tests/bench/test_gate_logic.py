"""Unit tests for ``tests.bench.gate``.

These tests mock baseline + current metric dicts and assert correct
pass/fail on every edge case the PH5 plan gate cares about:

  * exact match on headline metrics (delta == 0 passes)
  * just-under-tolerance passes, just-over fails
  * per-slice failures (headline green, slice red -> gate fails)
  * per-slice small-bucket skip (count < min_count -> informational)
  * latency regression (ratio > multiplier -> fails)
  * missing baseline p95 -> informational only (first-run friendly)
  * hit_at_10 gating is active at the overall layer
  * missing baseline metric key -> informational only (fresh keys
    added to the gate set before the baseline refresh don't red-flag CI)
"""

from __future__ import annotations

import pytest

from tests.bench import gate as _gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _overall(
    hit_at_1=0.90, hit_at_5=0.97, hit_at_10=0.99, mrr=0.93,
    ndcg_at_5=0.89, recall_at_5=0.92, n_questions=100,
):
    return {
        "hit_at_1": hit_at_1,
        "hit_at_5": hit_at_5,
        "hit_at_10": hit_at_10,
        "mrr": mrr,
        "ndcg_at_5": ndcg_at_5,
        "recall_at_5": recall_at_5,
        "n_questions": n_questions,
    }


def _by_category(multi_session=None, single_user=None, small_slice=None):
    cats = {}
    if multi_session is not None:
        cats["multi-session"] = {"count": 140, **multi_session}
    if single_user is not None:
        cats["single-session-user"] = {"count": 70, **single_user}
    if small_slice is not None:
        # 10 questions < default min_count=30 -> should NOT gate
        cats["tiny-axis"] = {"count": 10, **small_slice}
    return cats


_DEFAULT_BUDGET = {
    "overall": {
        "tolerances": {
            "hit_at_1":    0.002,
            "hit_at_5":    0.002,
            "hit_at_10":   0.002,
            "mrr":         0.002,
            "ndcg_at_5":   0.002,
            "recall_at_5": 0.002,
        },
    },
    "slices": {
        "tolerances": {
            "hit_at_1":  0.010,
            "mrr":       0.010,
            "ndcg_at_5": 0.010,
        },
        "min_count": 30,
    },
    "latency": {
        "p95_multiplier": 1.15,
        "min_baseline_ms": 50.0,
    },
}


# ---------------------------------------------------------------------------
# Overall gate
# ---------------------------------------------------------------------------

def test_exact_match_passes():
    """Zero-delta should always pass — same baseline as current."""
    base = {"overall": _overall(), "by_category": {}}
    cur = {"overall": _overall(), "by_category": {}}
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert rep.ok, rep.as_dict()
    assert all(c.passed for c in rep.overall_checks)


def test_just_under_tolerance_passes():
    """delta = -0.0019 with tolerance 0.002 -> passes by 0.0001."""
    base = {"overall": _overall(hit_at_1=0.90), "by_category": {}}
    cur = {"overall": _overall(hit_at_1=0.8981), "by_category": {}}
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert rep.ok, [c.as_dict() for c in rep.overall_checks]


def test_just_over_tolerance_fails():
    """delta = -0.003 with tolerance 0.002 -> fails."""
    base = {"overall": _overall(hit_at_1=0.90), "by_category": {}}
    cur = {"overall": _overall(hit_at_1=0.897), "by_category": {}}
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert not rep.ok
    failing = [c for c in rep.overall_checks if not c.passed]
    assert len(failing) == 1
    assert failing[0].metric == "hit_at_1"


def test_hit_at_10_is_gated():
    """hit_at_10 must be surfaced explicitly; a hit_at_10 regression fails."""
    base = {"overall": _overall(hit_at_10=0.99), "by_category": {}}
    cur = {"overall": _overall(hit_at_10=0.985), "by_category": {}}   # -0.5pp
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert not rep.ok
    names = [c.metric for c in rep.overall_checks if not c.passed]
    assert "hit_at_10" in names


def test_improvement_always_passes():
    """A positive delta should never trip the regression gate."""
    base = {"overall": _overall(hit_at_1=0.80), "by_category": {}}
    cur = {"overall": _overall(hit_at_1=0.85), "by_category": {}}   # +5pp
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert rep.ok


# ---------------------------------------------------------------------------
# Slice gate
# ---------------------------------------------------------------------------

def test_slice_regression_fails_even_if_overall_passes():
    """Overall OK, multi-session slice regresses -> gate fails."""
    base = {
        "overall": _overall(),
        "by_category": _by_category(
            multi_session={"hit_at_1": 0.90, "mrr": 0.94, "ndcg_at_5": 0.88},
            single_user={"hit_at_1": 0.90, "mrr": 0.93, "ndcg_at_5": 0.95},
        ),
    }
    cur = {
        "overall": _overall(),  # unchanged -> overall passes
        "by_category": _by_category(
            multi_session={"hit_at_1": 0.87, "mrr": 0.94, "ndcg_at_5": 0.88},  # -3pp hit_at_1
            single_user={"hit_at_1": 0.90, "mrr": 0.93, "ndcg_at_5": 0.95},
        ),
    }
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET,
                         slice_key_label="question_type")
    assert not rep.ok
    ms = next(s for s in rep.slice_checks if s.slice_name == "multi-session")
    assert ms.gated
    failing = [c for c in ms.checks if not c.passed]
    assert len(failing) == 1 and failing[0].metric == "hit_at_1"


def test_small_slice_is_informational_only():
    """count < min_count -> slice reports but does NOT fail the gate."""
    base = {
        "overall": _overall(),
        "by_category": _by_category(
            small_slice={"hit_at_1": 0.90, "mrr": 0.94, "ndcg_at_5": 0.88},
        ),
    }
    cur = {
        "overall": _overall(),
        "by_category": _by_category(
            small_slice={"hit_at_1": 0.60, "mrr": 0.70, "ndcg_at_5": 0.55},  # huge regression
        ),
    }
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert rep.ok                       # gate passes because slice was too small to gate
    tiny = next(s for s in rep.slice_checks if s.slice_name == "tiny-axis")
    assert not tiny.gated
    assert "min_count" in tiny.skip_reason
    for c in tiny.checks:
        assert c.informational


def test_slice_under_tolerance_passes():
    """Per-slice tolerance is 1.0pp by default — 0.8pp regression should pass."""
    base = {
        "overall": _overall(),
        "by_category": _by_category(
            multi_session={"hit_at_1": 0.90, "mrr": 0.94, "ndcg_at_5": 0.88},
        ),
    }
    cur = {
        "overall": _overall(),
        "by_category": _by_category(
            multi_session={"hit_at_1": 0.892, "mrr": 0.94, "ndcg_at_5": 0.88},  # -0.8pp
        ),
    }
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    assert rep.ok


# ---------------------------------------------------------------------------
# Latency gate
# ---------------------------------------------------------------------------

def test_latency_under_multiplier_passes():
    base = {"overall": _overall(), "by_category": {}}
    cur = {"overall": _overall(), "by_category": {},
           "per_query_ms": [100.0] * 100}  # p95 == 100ms
    # Baseline 100ms * 1.15 = 115ms ceiling; we're at 100ms. passes.
    rep = _gate.evaluate(
        "longmemeval", cur, base, budget=_DEFAULT_BUDGET,
        current_latencies_ms=cur["per_query_ms"],
        baseline_p95_ms=100.0,
    )
    assert rep.latency_check is not None
    assert rep.latency_check.passed
    assert rep.ok


def test_latency_regression_fails():
    base = {"overall": _overall(), "by_category": {}}
    # Mix of samples, p95 winds up ~160ms — over 100 * 1.15 = 115ms.
    samples = [80.0] * 90 + [160.0] * 10
    rep = _gate.evaluate(
        "longmemeval", {"overall": _overall(), "by_category": {}},
        base, budget=_DEFAULT_BUDGET,
        current_latencies_ms=samples,
        baseline_p95_ms=100.0,
    )
    assert rep.latency_check is not None
    assert not rep.latency_check.passed
    assert rep.latency_check.ratio > 1.15
    assert not rep.ok


def test_latency_missing_baseline_is_informational():
    """First-run friendly: no baseline_p95_ms -> informational, does not fail."""
    base = {"overall": _overall(), "by_category": {}}
    cur = {"overall": _overall(), "by_category": {}}
    rep = _gate.evaluate(
        "longmemeval", cur, base, budget=_DEFAULT_BUDGET,
        current_latencies_ms=[100.0] * 10,
        baseline_p95_ms=None,
    )
    assert rep.latency_check is not None
    assert rep.latency_check.informational
    assert rep.ok
    # One warning for the missing latency baseline.
    assert any("baseline_p95_ms" in w for w in rep.warnings)


def test_latency_below_min_baseline_is_informational():
    """A 5ms baseline tripping a 15% ratio is noise, not signal."""
    base = {"overall": _overall(), "by_category": {}}
    cur = {"overall": _overall(), "by_category": {}}
    rep = _gate.evaluate(
        "longmemeval", cur, base, budget=_DEFAULT_BUDGET,
        current_latencies_ms=[8.0] * 100,   # p95 = 8ms, 60% over a 5ms baseline
        baseline_p95_ms=5.0,
    )
    assert rep.latency_check is not None
    assert rep.latency_check.informational
    assert rep.ok


# ---------------------------------------------------------------------------
# Mixed cases
# ---------------------------------------------------------------------------

def test_multi_axis_failure_gate_is_red():
    """Overall AND latency both regress — gate fails, report flags both."""
    base = {"overall": _overall(hit_at_1=0.90), "by_category": {}}
    cur = {"overall": _overall(hit_at_1=0.88), "by_category": {}}  # -2pp
    rep = _gate.evaluate(
        "longmemeval", cur, base, budget=_DEFAULT_BUDGET,
        current_latencies_ms=[160.0] * 100,
        baseline_p95_ms=100.0,
    )
    assert not rep.ok
    failing_metrics = [c.metric for c in rep.overall_checks if not c.passed]
    assert "hit_at_1" in failing_metrics
    assert rep.latency_check is not None
    assert not rep.latency_check.passed


def test_missing_baseline_metric_key_is_informational():
    """Gate adds hit_at_10 but baseline was frozen before we added it ->
    informational, doesn't red-flag CI."""
    base_overall = _overall()
    base_overall.pop("hit_at_10")               # simulate older baseline
    base = {"overall": base_overall, "by_category": {}}
    cur = {"overall": _overall(), "by_category": {}}
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET)
    h10 = next(c for c in rep.overall_checks if c.metric == "hit_at_10")
    assert h10.informational
    assert h10.passed
    assert rep.ok


def test_serialisation_is_json_safe():
    """as_dict() output must be json-serialisable for --report-json."""
    import json

    base = {"overall": _overall(), "by_category": _by_category(
        multi_session={"hit_at_1": 0.90, "mrr": 0.94, "ndcg_at_5": 0.88},
    )}
    cur = {"overall": _overall(), "by_category": _by_category(
        multi_session={"hit_at_1": 0.89, "mrr": 0.93, "ndcg_at_5": 0.87},
    )}
    rep = _gate.evaluate("longmemeval", cur, base, budget=_DEFAULT_BUDGET,
                         current_latencies_ms=[100.0, 110.0, 95.0],
                         baseline_p95_ms=100.0)
    # Should round-trip cleanly.
    blob = json.dumps(rep.as_dict())
    back = json.loads(blob)
    assert back["bench"] == "longmemeval"
    assert isinstance(back["overall"], list)
    assert isinstance(back["slices"], list)


# ---------------------------------------------------------------------------
# Budget loading
# ---------------------------------------------------------------------------

def test_load_budget_returns_defaults_for_unknown_bench(tmp_path, monkeypatch):
    """Missing YAML -> hardcoded defaults."""
    # Point BUDGETS_DIR at an empty tmp dir so no YAML is found.
    monkeypatch.setattr(_gate, "BUDGETS_DIR", tmp_path)
    b = _gate.load_budget("nonexistent-bench")
    assert b["overall"]["tolerances"]["hit_at_10"] == 0.002
    assert b["latency"]["p95_multiplier"] == 1.15


def test_load_budget_merges_yaml_overrides(tmp_path, monkeypatch):
    """Sparse YAML overrides only the specified fields; others fall back."""
    yaml_mod = _gate._try_import_yaml()
    if yaml_mod is None:
        pytest.skip("PyYAML not installed — budget override test needs it")
    monkeypatch.setattr(_gate, "BUDGETS_DIR", tmp_path)
    (tmp_path / "demo.yaml").write_text(
        "overall:\n"
        "  tolerances:\n"
        "    hit_at_1: 0.005\n"
        "latency:\n"
        "  p95_multiplier: 1.25\n"
    )
    b = _gate.load_budget("demo")
    # Override applied.
    assert b["overall"]["tolerances"]["hit_at_1"] == 0.005
    assert b["latency"]["p95_multiplier"] == 1.25
    # Unmentioned metric retained from defaults.
    assert b["overall"]["tolerances"]["hit_at_10"] == 0.002
    # Unmentioned slice section retained from defaults.
    assert b["slices"]["min_count"] == 30


def test_quantile_p95_matches_latency_module_convention():
    """p95 on 100 samples matches linear-interp expectation."""
    samples = list(range(1, 101))   # 1..100
    p95 = _gate.quantile(samples, 0.95)
    # Linear interp: pos = 0.95 * 99 = 94.05 -> between samples[94]=95 and samples[95]=96
    assert 95.0 <= p95 <= 96.0
