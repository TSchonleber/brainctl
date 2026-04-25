"""Regression gate for the retrieval eval harness under tests/bench/.

Loads the committed baseline at tests/bench/baselines/search_quality.json,
reruns the full query set against the `cmd` pipeline, and asserts that each
headline metric stays within the 2% tolerance ported over from the
search-quality benchmark that inspired this work. Fails loudly when a
regression is introduced so CI stops changes that silently degrade recall.
"""
from __future__ import annotations

import random

import pytest

from tests.bench import eval as bench_eval


@pytest.fixture(scope="module", autouse=True)
def _seed_thompson_rng():
    """Seed Python's stdlib RNG before the bench fixture runs.

    ``cmd_search``'s Thompson-sampling step calls ``random.betavariate``
    via ``_apply_recency_and_trim``; without a seed the bench's p_at_1
    metric drifts ~0.40-0.45 across runs and the regression gate flakes
    ~50% of the time. The CLI entry at ``tests/bench/run.py`` already
    seeds at module top with ``random.seed(42)``; mirror that here so
    pytest invocations are equally deterministic.
    """
    random.seed(42)


@pytest.fixture(scope="module")
def bench_result():
    return bench_eval.run(pipeline="cmd")


def test_baseline_exists():
    """The committed baseline must exist — it's the regression contract."""
    baseline = bench_eval.load_baseline()
    assert baseline is not None, (
        "tests/bench/baselines/search_quality.json is missing. "
        "Re-run `python3 -m tests.bench.run --update-baseline` to recreate it."
    )
    assert "overall" in baseline
    for metric in bench_eval.GATED_METRICS:
        assert metric in baseline["overall"], f"baseline missing gated metric {metric}"


def test_no_regression(bench_result):
    """Current metrics must not drop more than REGRESSION_TOLERANCE below baseline."""
    baseline = bench_eval.load_baseline()
    assert baseline is not None
    diff = bench_eval.compare_to_baseline(bench_result, baseline)
    if not diff["ok"]:
        # Build a human-readable failure message so CI logs point at the
        # specific metric(s) that regressed.
        msg_lines = [
            "Retrieval benchmark regressed beyond tolerance "
            f"({diff['tolerance'] * 100:.0f}%):"
        ]
        for row in diff["failing"]:
            msg_lines.append(
                f"  {row['metric']}: current={row['current']:.4f} "
                f"baseline={row['baseline']:.4f}"
            )
        msg_lines.append(
            "Either fix the regression or, if intentional, refresh the "
            "baseline with `python3 -m tests.bench.run --update-baseline`."
        )
        pytest.fail("\n".join(msg_lines))


def test_deltas_within_bounds(bench_result):
    """Also assert that *improvements* don't silently drift the baseline
    upward by more than 15% without an explicit refresh — that usually
    means the fixture changed or the metric code shifted semantics."""
    baseline = bench_eval.load_baseline()
    diff = bench_eval.compare_to_baseline(bench_result, baseline)
    for metric, row in diff["deltas"].items():
        assert row["delta"] <= 0.15, (
            f"{metric} improved by {row['delta']:.4f} vs baseline "
            f"{row['baseline']:.4f} — refresh the baseline or investigate why."
        )


def test_metric_primitives():
    """Sanity-check the pure metric functions so broken math can't hide
    behind a lucky baseline match."""
    # Perfect ranking
    assert bench_eval.p_at_k(["a", "b"], {"a": 3, "b": 2}, 2) == 1.0
    assert bench_eval.mrr(["a", "b"], {"a": 3}) == 1.0
    # Miss
    assert bench_eval.mrr(["a", "b"], {"c": 3}) == 0.0
    # nDCG: ideal ordering gets 1.0
    rel = {"a": 3, "b": 2, "c": 1}
    assert bench_eval.ndcg_at_k(["a", "b", "c"], rel, 3) == pytest.approx(1.0)
    # Reverse ordering is imperfect
    assert bench_eval.ndcg_at_k(["c", "b", "a"], rel, 3) < 1.0
    # Empty relevance is vacuously perfect
    assert bench_eval.ndcg_at_k(["a"], {}, 3) == 1.0
    # Sparse relevance caps attainable P@k below 1.0
    assert bench_eval.p_at_k_ceiling({"a": 3}, 5) == 0.2
    assert bench_eval.p_at_k_ceiling({"a": 3, "b": 2, "c": 1}, 5) == 0.6
    assert bench_eval.p_at_k_ceiling({}, 5) == 0.0


def test_p_at_5_diagnostics_expose_fixture_ceiling():
    result = bench_eval.run(pipeline="cmd")
    overall = result["overall"]

    assert overall["answerable_queries"] == 20
    assert overall["empty_relevance_queries"] == 2
    assert overall["p_at_5_ceiling"] == pytest.approx(0.4273)
    assert overall["p_at_5_answerable_ceiling"] == pytest.approx(0.47)
    assert overall["p_at_5_answerable"] == pytest.approx(0.42)
    assert overall["p_at_5_ratio_to_ceiling"] == pytest.approx(0.8935, abs=1e-4)
    assert overall["p_at_5_macro_ratio_to_ceiling"] == pytest.approx(0.9167, abs=1e-4)
    assert overall["p_at_5_answerable_ratio_to_ceiling"] == pytest.approx(0.8936, abs=1e-4)
    assert overall["p_at_5_answerable_macro_ratio_to_ceiling"] == pytest.approx(0.9167, abs=1e-4)
