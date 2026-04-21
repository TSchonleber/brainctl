"""Latency regression gate.

Compares a fresh harness run against the committed
``tests/bench/baselines/latency.json`` and fails if any p95 metric
regressed by more than ``REGRESSION_THRESHOLD`` (50% by default).

Why 50%
-------
Wall-clock latency on a laptop has wider variance than retrieval-quality
metrics. Empirically (perf/latency-baseline measurement run, 2026-04-18):
the SAME code/harness/scale measured back-to-back showed 30-50% drift
between sweeps as the machine warmed up under sustained load. The first
sweep showed brain_search_hybrid p95 ~30ms; a fresh sweep started 5
minutes later showed ~92ms; a fresh sweep run AFTER that one showed
~137ms. None of these were regressions — they were the same code in
different thermal/page-cache states.

Tighter thresholds (10-25%) flap on this kind of cross-sweep drift.
Looser thresholds (>60%) miss the regressions we actually want to
catch (someone adds an unbatched commit inside a hot loop, doubles a
PRAGMA call, etc).

50% is the lowest threshold that stayed flake-free across three
back-to-back full sweeps on the reference darwin/M-class hardware. If
your CI laptop has wider variance, raise this — but please also
investigate WHY the variance is wider (thermal, background tasks).

Compare to: ``tests/bench/run.py`` uses 2% for retrieval QUALITY metrics
(P@k, MRR, nDCG), which are deterministic given a fixed seed and so
have basically no machine-state dependence.

Sub-millisecond ops (baseline p95 < 1.0ms) bypass the ratio test and
use a flat +1.0ms absolute budget. Below 1ms we're measuring page-cache
hits and sqlite3.connect overhead, both of which jitter ±0.5ms on macOS
without indicating any real regression.

How to run
----------
Off by default — set ``BRAINCTL_RUN_BENCH=1`` (matches LOCOMO + LongMemEval):

    BRAINCTL_RUN_BENCH=1 pytest tests/test_latency_regression.py -v

Update the baseline after a deliberate change:

    python -m tests.bench.latency --update-baseline
    git add tests/bench/baselines/latency.json
    # commit with the intended-change explanation in the message

Why not run on every PR
-----------------------
~3-5 min wall on N=10k. Same gating model as the LOCOMO + LongMemEval
benches: opt-in for perf-relevant PRs and pre-release runs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure we import the worktree's agentmemory, not the installed venv copy.
# (Same pattern as tests/conftest.py uses for the wider suite.)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


BENCH_GATE = os.environ.get("BRAINCTL_RUN_BENCH", "")
BASELINE_PATH = _REPO_ROOT / "tests" / "bench" / "baselines" / "latency.json"

# Mirror the constant in tests/bench/latency.py — kept duplicated rather than
# imported so this gate file can be read in isolation by humans grepping for
# "regression threshold". See module docstring for the empirical justification.
REGRESSION_THRESHOLD = 1.50

# Below this baseline p95, switch to absolute-delta cap instead of ratio.
# Sub-1ms ops are dominated by sqlite3.connect + page-cache jitter (±0.5ms
# is normal on macOS), so a ratio test would flag noise as regression.
SUB_MS_BASELINE_BOUNDARY = 1.0
SUB_MS_BUDGET_MS = 1.0

# Scales where the ratio gate is meaningful. N=10k results are kept in the
# baseline (and reported in `brainctl perf --full`) for diagnostic value but
# excluded from the regression assertion: at 10k, page-cache eviction
# dominates per-call cost (the FTS join pathology, escalation #1) and we've
# observed 100-200% drift between sweeps that's NOT a regression — just
# the same code in different cache states. The N=100 and N=1k scales
# exhibit much tighter variance and ARE gated.
GATED_SCALES = (100, 1_000)

# Ops excluded from the cross-platform ratio gate. Two categories fall in
# here for structurally different reasons:
#
#  (a) Subprocess-bound: wall-clock is dominated by Python interpreter
#      startup + module import + dylib load, all of which differ 50–100%
#      between macOS and ubuntu-latest on GitHub Actions. Observed:
#      cli_search_cold@100 darwin 137ms vs ubuntu 261ms — not a
#      regression, just platform-dependent Python cold-start.
#
#  (b) Ollama-dependent: the bench harness runs embeddings when
#      `_VEC_AVAILABLE` / Ollama is reachable, and floors to a no-op
#      when it isn't. The two branches are WAY apart (~0.5ms floor vs
#      ~100ms round-trip), and — critically — "Ollama unreachable"
#      fails differently by platform: darwin returns ECONNREFUSED on
#      `localhost:11434` in <1ms, linux CI sitters at a DNS / TCP
#      handshake timeout for ~100ms. So comparing a darwin baseline's
#      fast floor against a linux timeout-floor flags "regressions"
#      that are purely environmental.
#
#      The right fix for these is a platform-matched baseline, but
#      until CI grows a workflow_dispatch that captures one on
#      ubuntu-latest, the cross-platform skip is the less-wrong
#      option — we'd rather not gate than gate on noise.
#
# Library ops that don't touch embeddings (brain_search_fts, vec_*, etc.)
# stay gated cross-platform because their timing is dominated by sqlite3
# + FTS5 which behave consistently across macOS / ubuntu.
#
# To catch real regressions on the exempt ops, run the bench on a
# platform-matched machine and use `brainctl perf --full`; the CI gate
# is intentionally narrower.
#
# Name preserved (SUBPROCESS_BOUND_OPS) for backwards compatibility with
# existing contributors who grep for it. New membership sits alongside
# the original five so the diff is auditable.
SUBPROCESS_BOUND_OPS = frozenset({
    # (a) subprocess-bound — original set
    "cli_search_cold",
    "cli_search_warm",
    "cli_remember_cold",
    "cli_remember_warm",
    "cli_stats",
    # (b) Ollama-dependent — added 2026-04-21 after v2.5.0 main CI
    # surfaced 3000%+ "regressions" on these ops, all traced to the
    # darwin / linux embedding-floor divergence.
    "brain_remember_full",
    "brain_remember_construct_only",
    "brain_search_hybrid",
    "brain_entity",
    "mcp_memory_search",
})


def _bench_enabled() -> bool:
    if not BENCH_GATE:
        return False
    return BENCH_GATE in ("1", "all", "latency", "true", "yes")


pytestmark = pytest.mark.skipif(
    not _bench_enabled(),
    reason=("Latency bench is opt-in. Set BRAINCTL_RUN_BENCH=1 (or "
            "=latency) to run."),
)


@pytest.fixture(scope="module")
def baseline():
    if not BASELINE_PATH.exists():
        pytest.fail(
            f"Missing baseline at {BASELINE_PATH}. Generate it with:\n"
            f"    python -m tests.bench.latency --update-baseline"
        )
    with BASELINE_PATH.open() as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def fresh_run(baseline):
    """Run the harness now, against the same scales/runs as the baseline.

    We use module scope so all parametrised tests share the same fresh
    measurement — running it once instead of N-times keeps the gate fast.
    """
    from tests.bench.latency import run_sweep, OPS_DEFAULT

    scales = tuple(baseline["scales"])
    n_runs = int(baseline.get("runs_per_op", 100))
    n_warmup = int(baseline.get("warmup_per_op", 5))
    report = run_sweep(scales=scales, n_runs=n_runs, n_warmup=n_warmup,
                       ops=OPS_DEFAULT)
    return report.as_dict()


def _index(report: dict) -> dict:
    """Index results by (op, scale) for fast lookup."""
    return {(r["op"], r["scale"]): r for r in report["results"]}


def test_baseline_present(baseline):
    """Sanity: baseline file is loadable and has the schema we expect."""
    assert "results" in baseline
    assert "scales" in baseline
    assert baseline["results"], "baseline results array is empty"


def test_no_p95_regression_beyond_threshold(baseline, fresh_run):
    """Per-(op, scale): fresh p95 must be within REGRESSION_THRESHOLD of baseline.

    Reports ALL regressions in a single failure rather than failing on the
    first — gives the developer the full picture instead of one rabbit hole
    at a time.
    """
    base_idx = _index(baseline)
    fresh_idx = _index(fresh_run)

    failures: list[str] = []
    # If the fresh run is on a different platform than the baseline, the
    # CLI-subprocess ops have un-reconcilable cross-platform variance and
    # get excluded. Library-level ops still pass the ratio test because
    # their timing is dominated by sqlite3 + FTS5 which behave consistently.
    cross_platform = (
        baseline.get("platform") != fresh_run.get("platform")
        if isinstance(baseline, dict) and isinstance(fresh_run, dict)
        else False
    )
    for key, base_row in base_idx.items():
        op, scale = key
        # Skip scales we don't gate on. See GATED_SCALES docstring above.
        if scale not in GATED_SCALES:
            continue
        # Skip subprocess-bound ops across platforms. Documented in the
        # SUBPROCESS_BOUND_OPS constant above.
        if cross_platform and op in SUBPROCESS_BOUND_OPS:
            continue
        fresh_row = fresh_idx.get(key)
        if fresh_row is None:
            failures.append(f"{op}@{scale}: missing in fresh run")
            continue
        base_p95 = base_row["p95_ms"]
        fresh_p95 = fresh_row["p95_ms"]
        # Sub-millisecond ops are page-cache + connect-overhead bound. A
        # ratio test on (0.17 → 0.34) would flag noise as a 100% regression.
        # Use a flat +1ms absolute budget instead — anything beyond that IS
        # a real signal.
        if base_p95 < SUB_MS_BASELINE_BOUNDARY:
            limit = base_p95 + SUB_MS_BUDGET_MS
            if fresh_p95 > limit:
                failures.append(
                    f"{key[0]}@{key[1]}: baseline {base_p95:.3f}ms p95, "
                    f"fresh {fresh_p95:.3f}ms p95 (sub-ms; "
                    f"budget +{SUB_MS_BUDGET_MS:.1f}ms absolute)"
                )
        else:
            ratio = fresh_p95 / base_p95
            if ratio > REGRESSION_THRESHOLD:
                pct = (ratio - 1.0) * 100.0
                failures.append(
                    f"{key[0]}@{key[1]}: baseline {base_p95:.2f}ms p95, "
                    f"fresh {fresh_p95:.2f}ms p95 (+{pct:.0f}%, "
                    f"threshold +{(REGRESSION_THRESHOLD - 1.0) * 100:.0f}%)"
                )

    if failures:
        msg = (f"\n{len(failures)} latency regression(s) vs baseline:\n  "
               + "\n  ".join(failures)
               + "\n\nIf the regression is intentional, update the baseline:\n"
               + "  python -m tests.bench.latency --update-baseline\n"
               + "  git add tests/bench/baselines/latency.json")
        pytest.fail(msg)


def test_targets_met_at_target_scale(baseline):
    """Sanity: the committed baseline still meets its own targets at TARGET_SCALE.

    This is a guard against checking in a baseline where ``met_target=False``
    for any op — that would mean we accepted a known-failing baseline as the
    new normal, which is exactly what the gate is supposed to prevent.

    Operates on the COMMITTED baseline (not the fresh run) — it asks "is the
    baseline self-consistent?" rather than "did we regress?". The regression
    check above handles the comparison.
    """
    from tests.bench.latency import TARGET_SCALE
    misses = []
    for r in baseline["results"]:
        if r["scale"] != TARGET_SCALE:
            continue
        if r.get("met_target") is False:
            misses.append(
                f"{r['op']}: {r['p95_ms']:.2f}ms p95 vs target "
                f"{r['target_p95_ms']:.1f}ms"
            )
    if misses:
        pytest.fail(
            "Committed baseline contains target misses at the target scale:\n  "
            + "\n  ".join(misses)
            + "\n\nEither (a) ship the optimization that closes the gap, or "
            "(b) intentionally retune the target in tests/bench/latency.py "
            "with a comment explaining why."
        )
