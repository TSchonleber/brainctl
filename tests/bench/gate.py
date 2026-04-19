"""Strict gate logic for brainctl benchmarks.

This module adds three new regression dimensions on top of the existing
``--check`` flow in ``tests/bench/run.py`` / ``tests/bench/eval.py``:

  1. ``hit_at_10`` gating (surfaced explicit per PH5 plan).
  2. Per-slice breakdowns — per ``question_type`` on LongMemEval,
     per ``category`` on LoCoMo — with their own per-slice baselines.
     (Note: the task-spec says "per mode on LoCoMo"; LoCoMo's
     retrieval-axis column is ``category``, not ``mode``, in this
     repo's dataset loader — we slice on ``category``.)
  3. ``p95_latency_ms`` end-to-end — fail when
     ``current_p95 > baseline_p95 * latency_multiplier``.

Budgets live in ``tests/bench/budgets/<bench>.yaml`` so humans can
retune tolerances without a code change. A hardcoded default lives
here as a fallback (and as documentation) — ``load_budget`` prefers
the YAML, falls back cleanly if PyYAML is unavailable or the file is
missing.

The gate is additive: the existing ``--check`` stays untouched and
back-compat. Strict gating is opt-in via ``--check-strict``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Budget loading
# ---------------------------------------------------------------------------

BUDGETS_DIR = Path(__file__).resolve().parent / "budgets"

# Hardcoded defaults. The YAML files override these field-by-field.
# Deltas are in absolute metric units (0.002 == 0.2pp for a [0,1] metric).
DEFAULT_BUDGET: Dict[str, Any] = {
    "overall": {
        "tolerances": {
            "hit_at_1":    0.002,   # -0.2pp floor
            "hit_at_5":    0.002,
            "hit_at_10":   0.002,
            "mrr":         0.002,
            "ndcg_at_5":   0.002,
            "recall_at_5": 0.002,
        },
    },
    "slices": {
        # Per-slice gates are gentler by default — small buckets are noisier.
        "tolerances": {
            "hit_at_1":  0.010,   # -1.0pp floor
            "mrr":       0.010,
            "ndcg_at_5": 0.010,
        },
        # Slices with fewer than this many questions are logged but not gated.
        # Small buckets (<30) can wiggle by whole percentage points with a
        # single bad query, and blowing CI on that is more noise than signal.
        "min_count": 30,
    },
    "latency": {
        # p95 end-to-end latency ratio ceiling: current / baseline must be
        # <= this multiplier. 1.15 is the plan envelope.
        "p95_multiplier": 1.15,
        # Floor below which tiny p95 values aren't gated — small absolute
        # drifts on a ~5ms path should not trip a 15% ratio gate.
        "min_baseline_ms": 50.0,
    },
}


def _try_import_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:
        return None


def load_budget(bench: str) -> Dict[str, Any]:
    """Load and merge a per-bench budget with the hardcoded defaults.

    Missing keys in the YAML fall back to defaults so a sparse YAML
    doesn't force a developer to re-specify every tolerance.
    """
    merged = _deep_copy(DEFAULT_BUDGET)
    path = BUDGETS_DIR / f"{bench}.yaml"
    if not path.exists():
        return merged
    yaml = _try_import_yaml()
    if yaml is None:
        # PyYAML missing — fall back to defaults. This is fine for local
        # smoke runs but CI should have it installed via the [bench] extra.
        return merged
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return merged
    if not isinstance(raw, dict):
        return merged
    _deep_merge(merged, raw)
    return merged


def _deep_copy(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _deep_copy(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_deep_copy(v) for v in x]
    return x


def _deep_merge(dst: Dict[str, Any], src: Mapping[str, Any]) -> None:
    """In-place merge `src` into `dst`, overriding leaves."""
    for k, v in src.items():
        if isinstance(v, Mapping) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = _deep_copy(v)


# ---------------------------------------------------------------------------
# Latency helpers
# ---------------------------------------------------------------------------

def quantile(samples: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile — matches tests/bench/latency.py convention."""
    if not samples:
        return 0.0
    s = sorted(samples)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ---------------------------------------------------------------------------
# Gate comparisons — pure, no IO
# ---------------------------------------------------------------------------

@dataclass
class MetricCheck:
    metric: str
    current: float
    baseline: float
    delta: float
    tolerance: float
    passed: bool
    # ``informational=True`` means the key is reported in the output but
    # does NOT contribute to the pass/fail verdict (e.g. missing baseline).
    informational: bool = False
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "current": round(self.current, 6),
            "baseline": round(self.baseline, 6),
            "delta": round(self.delta, 6),
            "tolerance": self.tolerance,
            "pass": self.passed,
            "informational": self.informational,
            "note": self.note,
        }


@dataclass
class SliceCheck:
    slice_key: str
    slice_name: str
    count: int
    checks: List[MetricCheck] = field(default_factory=list)
    gated: bool = True            # False when count < min_count
    skip_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slice_key": self.slice_key,
            "slice_name": self.slice_name,
            "count": self.count,
            "gated": self.gated,
            "skip_reason": self.skip_reason,
            "checks": [c.as_dict() for c in self.checks],
            "passed": all(c.passed or c.informational for c in self.checks),
        }


@dataclass
class LatencyCheck:
    current_p95_ms: float
    baseline_p95_ms: float
    ratio: float
    multiplier: float
    passed: bool
    informational: bool = False
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "current_p95_ms": round(self.current_p95_ms, 3),
            "baseline_p95_ms": round(self.baseline_p95_ms, 3),
            "ratio": round(self.ratio, 4),
            "multiplier": self.multiplier,
            "pass": self.passed,
            "informational": self.informational,
            "note": self.note,
        }


@dataclass
class GateReport:
    bench: str
    ok: bool
    overall_checks: List[MetricCheck] = field(default_factory=list)
    slice_checks: List[SliceCheck] = field(default_factory=list)
    latency_check: Optional[LatencyCheck] = None
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bench": self.bench,
            "ok": self.ok,
            "overall": [c.as_dict() for c in self.overall_checks],
            "slices": [s.as_dict() for s in self.slice_checks],
            "latency": self.latency_check.as_dict() if self.latency_check else None,
            "warnings": self.warnings,
            "failing": [
                c.as_dict() for c in self.overall_checks
                if not c.passed and not c.informational
            ] + [
                {"slice": s.slice_name, **c.as_dict()}
                for s in self.slice_checks if s.gated
                for c in s.checks if not c.passed and not c.informational
            ] + (
                [self.latency_check.as_dict()]
                if self.latency_check and not self.latency_check.passed
                and not self.latency_check.informational
                else []
            ),
        }


def _check_metric(
    metric: str,
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    tolerance: float,
) -> MetricCheck:
    """Compare one metric against its baseline under an absolute-delta tolerance.

    Convention (matches existing ``compare_to_baseline``): fail when
    ``delta < -tolerance`` (a regression deeper than tolerance). Deltas are
    absolute on [0, 1] metrics — 0.002 == 0.2pp.
    """
    cur_v = float(current.get(metric, 0.0)) if current else 0.0
    has_baseline = baseline is not None and metric in baseline
    base_v = float(baseline.get(metric, 0.0)) if has_baseline else 0.0
    delta = cur_v - base_v
    if not has_baseline:
        return MetricCheck(
            metric=metric,
            current=cur_v,
            baseline=0.0,
            delta=0.0,
            tolerance=tolerance,
            passed=True,
            informational=True,
            note="baseline missing; informational only",
        )
    passed = delta >= -tolerance - 1e-9  # epsilon for float rounding
    return MetricCheck(
        metric=metric, current=cur_v, baseline=base_v,
        delta=delta, tolerance=tolerance, passed=passed,
    )


def check_overall(
    current_overall: Mapping[str, Any],
    baseline_overall: Mapping[str, Any],
    tolerances: Mapping[str, float],
) -> List[MetricCheck]:
    return [
        _check_metric(m, current_overall, baseline_overall, tol)
        for m, tol in tolerances.items()
    ]


def check_slices(
    current_by_cat: Mapping[str, Mapping[str, Any]],
    baseline_by_cat: Mapping[str, Mapping[str, Any]],
    tolerances: Mapping[str, float],
    *,
    min_count: int = 30,
    slice_key_label: str = "category",
) -> List[SliceCheck]:
    """Compare per-slice metrics. Slices only present in one side are
    surfaced as warnings (via informational MetricChecks).
    """
    slices: List[SliceCheck] = []
    all_keys = sorted(set(current_by_cat) | set(baseline_by_cat))
    for key in all_keys:
        cur = current_by_cat.get(key, {}) or {}
        base = baseline_by_cat.get(key, {}) or {}
        count = int(cur.get("count", base.get("count", 0)) or 0)
        slc = SliceCheck(slice_key=slice_key_label, slice_name=key, count=count)
        if count < min_count:
            slc.gated = False
            slc.skip_reason = f"count {count} < min_count {min_count}"
        for m, tol in tolerances.items():
            chk = _check_metric(m, cur, base, tol)
            if not slc.gated:
                chk.informational = True
                chk.note = (chk.note + "; " if chk.note else "") + slc.skip_reason
            slc.checks.append(chk)
        slices.append(slc)
    return slices


def check_latency(
    current_p95_ms: Optional[float],
    baseline_p95_ms: Optional[float],
    *,
    multiplier: float = 1.15,
    min_baseline_ms: float = 50.0,
) -> LatencyCheck:
    cur = float(current_p95_ms or 0.0)
    base = float(baseline_p95_ms or 0.0)
    if base <= 0 or math.isnan(base):
        return LatencyCheck(
            current_p95_ms=cur, baseline_p95_ms=base, ratio=0.0,
            multiplier=multiplier, passed=True, informational=True,
            note="baseline p95 missing; informational only",
        )
    if base < min_baseline_ms:
        ratio = cur / base if base else 0.0
        return LatencyCheck(
            current_p95_ms=cur, baseline_p95_ms=base, ratio=ratio,
            multiplier=multiplier, passed=True, informational=True,
            note=f"baseline {base:.2f}ms < min_baseline_ms {min_baseline_ms}ms; informational only",
        )
    ratio = cur / base
    passed = ratio <= multiplier + 1e-9
    return LatencyCheck(
        current_p95_ms=cur, baseline_p95_ms=base, ratio=ratio,
        multiplier=multiplier, passed=passed,
    )


# ---------------------------------------------------------------------------
# Top-level evaluator
# ---------------------------------------------------------------------------

def evaluate(
    bench: str,
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    budget: Optional[Mapping[str, Any]] = None,
    current_latencies_ms: Optional[Sequence[float]] = None,
    baseline_p95_ms: Optional[float] = None,
    slice_key_label: str = "category",
) -> GateReport:
    """Run the full strict gate for one bench.

    Args:
        bench: bench name, used only for the report.
        current: current run payload with at minimum ``overall`` and ``by_category``.
        baseline: committed baseline with the same shape.
        budget: merged budget dict (see ``load_budget``). If None, loads.
        current_latencies_ms: per-query wall-clock samples (ms) collected during
            the current run. Used to compute p95 end-to-end.
        baseline_p95_ms: committed baseline p95. Sourced from the budget YAML
            (baseline_p95_ms key under ``latency``) or a separate baseline file;
            passed in by the caller so this function stays IO-free.
        slice_key_label: "question_type" for LongMemEval, "category" for LoCoMo.
    """
    if budget is None:
        budget = load_budget(bench)

    report = GateReport(bench=bench, ok=True)

    overall_tol = budget["overall"]["tolerances"]
    slice_tol = budget["slices"]["tolerances"]
    slice_min = int(budget["slices"].get("min_count", 30))
    lat_mult = float(budget["latency"].get("p95_multiplier", 1.15))
    lat_min_base = float(budget["latency"].get("min_baseline_ms", 50.0))

    cur_over = current.get("overall", {}) or {}
    base_over = baseline.get("overall", {}) or {}
    report.overall_checks = check_overall(cur_over, base_over, overall_tol)

    cur_cats = current.get("by_category", {}) or {}
    base_cats = baseline.get("by_category", {}) or {}
    report.slice_checks = check_slices(
        cur_cats, base_cats, slice_tol,
        min_count=slice_min,
        slice_key_label=slice_key_label,
    )

    # Latency: compute p95 from samples, compare against budget-configured
    # baseline p95. A None passthrough is handled by check_latency as
    # "informational only" so --check-strict still exits clean on missing
    # baselines (e.g. the FIRST run).
    if current_latencies_ms:
        cur_p95 = quantile(list(current_latencies_ms), 0.95)
    else:
        cur_p95 = 0.0
    if baseline_p95_ms is None:
        baseline_p95_ms = budget["latency"].get("baseline_p95_ms")
    report.latency_check = check_latency(
        cur_p95, baseline_p95_ms,
        multiplier=lat_mult, min_baseline_ms=lat_min_base,
    )

    # Verdict: any non-informational, non-passing check fails the gate.
    def _failed(mc: MetricCheck) -> bool:
        return not mc.passed and not mc.informational

    any_fail = any(_failed(c) for c in report.overall_checks)
    for slc in report.slice_checks:
        if slc.gated and any(_failed(c) for c in slc.checks):
            any_fail = True
    if report.latency_check and not report.latency_check.passed \
            and not report.latency_check.informational:
        any_fail = True
    report.ok = not any_fail

    # Add warnings for common "first-run" sharp edges.
    if not base_over:
        report.warnings.append(
            "baseline 'overall' is empty — did the baseline file parse?"
        )
    if baseline_p95_ms in (None, 0):
        report.warnings.append(
            "no baseline_p95_ms configured; latency gate is informational only. "
            f"Set budgets/{bench}.yaml:latency.baseline_p95_ms to enable."
        )

    return report


__all__ = [
    "BUDGETS_DIR",
    "DEFAULT_BUDGET",
    "GateReport",
    "LatencyCheck",
    "MetricCheck",
    "SliceCheck",
    "check_latency",
    "check_overall",
    "check_slices",
    "evaluate",
    "load_budget",
    "quantile",
]
