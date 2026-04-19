"""Entry point for the brainctl benchmarks.

Examples:
    python -m tests.bench.run                                # search-quality (default)
    python -m tests.bench.run --bench search-quality
    python -m tests.bench.run --bench locomo --backend brain
    python -m tests.bench.run --bench locomo --backend cmd --update-baseline
    python -m tests.bench.run --bench longmemeval --update-baseline
    python -m tests.bench.run --bench locomo --check         # legacy regress-gate
    python -m tests.bench.run --bench locomo --check-strict  # + slice + latency gates
    python -m tests.bench.run --bench longmemeval --check-strict \
            --report-json reports/longmemeval.json

All bench entries share the same regression-tolerance / baseline JSON
pattern so a single CI command (``--check``) can gate any of them.

Flag overview
-------------
``--check``          : existing behaviour, gated metrics are the headline
                       overall hit/recall/mrr/ndcg set (see ``GATED_METRICS``).
                       Tolerance is the legacy 2%% floor. Back-compat.
``--check-strict``   : runs ``--check`` first AND the strict PH5 gate:
                       hit_at_10 overall, per-slice breakdowns
                       (question_type for longmemeval, category for locomo),
                       and end-to-end p95 latency. Tolerances live in
                       ``tests/bench/budgets/<bench>.yaml``.
``--report-json P``  : dump a machine-readable pass/fail summary to P.
                       Includes overall + slice + latency checks. Used by
                       the CI workflow to post a matrix on the PR.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

random.seed(42)

# Search-quality bench (the original) — keep imports lazy on the others
# so missing dataset deps don't blow up cold-start.
from tests.bench.eval import (
    BASELINE_PATH as SEARCH_QUALITY_BASELINE_PATH,
    GATED_METRICS as SEARCH_QUALITY_GATED,
    REGRESSION_TOLERANCE,
    compare_to_baseline as compare_search_quality,
    load_baseline as _load_search_quality_baseline,
    run as _run_search_quality,
    save_baseline as _save_search_quality_baseline,
)
from tests.bench import gate as _gate

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"

# Headline metrics gated for each bench. Keep these narrow on purpose —
# noisy per-category numbers regress under tolerance much more often than
# real quality issues, so they stay informational only.
LOCOMO_GATED = ("hit_at_1", "hit_at_5", "mrr", "ndcg_at_5", "recall_at_5")
LONGMEMEVAL_GATED = ("hit_at_1", "hit_at_5", "mrr", "ndcg_at_5", "recall_at_5")

# Slice key label per bench — what column we partition on when doing
# per-slice breakdowns under --check-strict. LongMemEval's axis is
# question_type; LoCoMo's is category (the repo's loader doesn't expose
# a "mode" column, so we slice on what's available).
SLICE_LABEL = {
    "longmemeval": "question_type",
    "locomo": "category",
}


def _shared_baseline_path(name: str) -> Path:
    return BASELINES_DIR / f"{name}.json"


def _save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def _load_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open() as fh:
        return json.load(fh)


def _shared_compare(
    current: Dict[str, Any],
    baseline: Dict[str, Any],
    gated: tuple,
    tolerance: float = REGRESSION_TOLERANCE,
) -> Dict[str, Any]:
    cur_o = current.get("overall", {})
    base_o = baseline.get("overall", {})
    deltas: Dict[str, Dict[str, float]] = {}
    failing = []
    for metric in gated:
        cur_v = float(cur_o.get(metric, 0.0))
        base_v = float(base_o.get(metric, 0.0))
        delta = round(cur_v - base_v, 4)
        deltas[metric] = {"current": cur_v, "baseline": base_v, "delta": delta}
        if delta < -tolerance:
            failing.append({"metric": metric, "current": cur_v, "baseline": base_v})
    return {"ok": not failing, "tolerance": tolerance, "deltas": deltas, "failing": failing}


def _trim_for_baseline(result: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the comparable fields, drop the verbose per-row payload.

    Notably we exclude ``per_query_ms`` and ``per_convo`` / ``per_entry``
    because those are ephemeral run-artifacts — baselines should only
    carry the aggregate metric shape that the gates compare against.
    """
    keep = {"overall", "by_category", "ks", "backend"}
    return {k: v for k, v in result.items() if k in keep}


# ---------------------------------------------------------------------------
# Strict gate + report assembly
# ---------------------------------------------------------------------------

def _run_strict_gate(
    bench: str,
    current: Dict[str, Any],
    baseline: Dict[str, Any],
) -> _gate.GateReport:
    """Run the PH5 slice + latency gate for ``bench``.

    current / baseline are the same dicts produced by the underlying eval
    runner — we pull ``overall`` / ``by_category`` / ``per_query_ms`` off
    ``current`` and match them against ``baseline`` + the budget YAML.
    """
    budget = _gate.load_budget(bench)
    # baseline_p95_ms comes from the budget YAML only — we explicitly do
    # NOT read it off the baseline JSON, because baseline JSONs are owned
    # by the baseline-refresh workflow (I1) and we don't want the two
    # processes to stomp on each other's keys.
    baseline_p95 = budget.get("latency", {}).get("baseline_p95_ms")
    return _gate.evaluate(
        bench=bench,
        current=current,
        baseline=baseline,
        budget=budget,
        current_latencies_ms=current.get("per_query_ms"),
        baseline_p95_ms=baseline_p95,
        slice_key_label=SLICE_LABEL.get(bench, "category"),
    )


def _build_report(
    bench: str,
    current: Dict[str, Any],
    baseline: Optional[Dict[str, Any]],
    legacy_diff: Optional[Dict[str, Any]] = None,
    strict_report: Optional[_gate.GateReport] = None,
) -> Dict[str, Any]:
    """Shape the machine-readable summary written by --report-json.

    Kept stable across benches so the CI PR-comment renderer doesn't need
    per-bench special cases. ``legacy_diff`` is the dict produced by
    ``_shared_compare`` (the existing --check output); ``strict_report`` is
    the new slice + latency gate output.
    """
    out: Dict[str, Any] = {
        "bench": bench,
        "backend": current.get("backend"),
        "n_questions": (current.get("overall") or {}).get("n_questions"),
        "elapsed_s": current.get("elapsed_s"),
        "overall": current.get("overall") or {},
        "by_category": current.get("by_category") or {},
        "baseline_present": baseline is not None,
        "legacy_diff": legacy_diff,
        "strict": strict_report.as_dict() if strict_report else None,
    }
    out["pass"] = bool(
        (legacy_diff is None or legacy_diff.get("ok"))
        and (strict_report is None or strict_report.ok)
    )
    return out


def _write_report(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Per-bench dispatch
# ---------------------------------------------------------------------------

def _run_locomo(args: argparse.Namespace) -> int:
    from tests.bench.locomo_eval import run as run_locomo

    result = run_locomo(
        backend=args.backend or "brain",
        convo_idx=args.convo,
        traces_path=Path(args.traces) if args.traces else None,
    )
    baseline_name = args.baseline_name or "locomo"
    baseline_path = _shared_baseline_path(baseline_name)

    if args.update_baseline:
        _save_json(baseline_path, _trim_for_baseline(result))
        print(f"wrote baseline to {baseline_path}", file=sys.stderr)

    out: Dict[str, Any] = {
        "bench": "locomo",
        "backend": result.get("backend"),
        "elapsed_s": result.get("elapsed_s"),
        "n_convos": result["overall"].get("n_convos"),
        "n_questions": result["overall"].get("n_questions"),
        "overall": result["overall"],
        "by_category": result["by_category"],
    }

    return _finalise(args, "locomo", result, out, baseline_path,
                     gated=LOCOMO_GATED)


def _run_longmemeval(args: argparse.Namespace) -> int:
    from tests.bench.longmemeval_eval import run as run_longmemeval

    result = run_longmemeval(
        backend=args.backend or "brain",
        limit=args.limit,
        include_judge_only=args.include_judge_only,
        traces_path=Path(args.traces) if args.traces else None,
    )
    baseline_name = args.baseline_name or "longmemeval"
    baseline_path = _shared_baseline_path(baseline_name)

    if args.update_baseline:
        _save_json(baseline_path, _trim_for_baseline(result))
        print(f"wrote baseline to {baseline_path}", file=sys.stderr)

    out: Dict[str, Any] = {
        "bench": "longmemeval",
        "backend": result.get("backend"),
        "elapsed_s": result.get("elapsed_s"),
        "n_entries": result.get("n_entries"),
        "overall": result["overall"],
        "by_category": result["by_category"],
        "include_judge_only": result.get("include_judge_only"),
    }

    return _finalise(args, "longmemeval", result, out, baseline_path,
                     gated=LONGMEMEVAL_GATED)


def _finalise(
    args: argparse.Namespace,
    bench: str,
    result: Dict[str, Any],
    out: Dict[str, Any],
    baseline_path: Path,
    *,
    gated: tuple,
) -> int:
    """Shared trailer for --check / --check-strict / --report-json across benches.

    Returns the intended process exit code.
    """
    baseline = _load_json(baseline_path)
    legacy_diff: Optional[Dict[str, Any]] = None
    strict_report: Optional[_gate.GateReport] = None
    exit_code = 0

    need_baseline = args.check or args.check_strict
    if need_baseline and baseline is None:
        print(f"no baseline at {baseline_path}; run with --update-baseline first",
              file=sys.stderr)
        # Still dump a report if requested so CI can surface the state.
        if args.report_json:
            _write_report(Path(args.report_json),
                          _build_report(bench, result, None))
        return 2

    if args.check:
        legacy_diff = _shared_compare(result, baseline, gated)
        out["diff"] = legacy_diff
        if not legacy_diff["ok"]:
            exit_code = 1

    if args.check_strict:
        strict_report = _run_strict_gate(bench, result, baseline or {})
        out["strict"] = strict_report.as_dict()
        if not strict_report.ok:
            exit_code = 1
        # Emit any warnings (e.g. missing latency baseline) to stderr so they
        # show up in CI logs even when the gate is "pass, informational".
        for warn in strict_report.warnings:
            print(f"[gate warning] {bench}: {warn}", file=sys.stderr)

    if args.report_json:
        _write_report(
            Path(args.report_json),
            _build_report(bench, result, baseline, legacy_diff, strict_report),
        )

    print(json.dumps(out, indent=2, sort_keys=True))
    return exit_code


def _run_search_quality_cli(args: argparse.Namespace) -> int:
    """Original search-quality bench — preserved verbatim from prior versions."""
    result = _run_search_quality(k=args.k, pipeline=args.pipeline)

    if args.update_baseline:
        _save_search_quality_baseline(result)
        print(f"wrote baseline to {SEARCH_QUALITY_BASELINE_PATH}", file=sys.stderr)

    out = {
        "bench": "search-quality",
        "overall": result["overall"],
        "by_category": result["by_category"],
        "k": result["k"],
        "pipeline": result.get("pipeline", "cmd"),
    }
    if args.rows:
        out["rows"] = result["rows"]

    exit_code = 0
    legacy_diff: Optional[Dict[str, Any]] = None
    base = _load_search_quality_baseline() if (args.check or args.check_strict) else None

    if args.check or args.check_strict:
        if base is None:
            print("no baseline committed; run with --update-baseline first",
                  file=sys.stderr)
            if args.report_json:
                _write_report(Path(args.report_json),
                              _build_report("search-quality", result, None))
            return 2

    if args.check:
        legacy_diff = compare_search_quality(result, base)
        out["diff"] = legacy_diff
        if not legacy_diff["ok"]:
            exit_code = 1

    # search-quality has no per-slice or latency axis the plan gates on —
    # --check-strict is a no-op here but we keep it accepted so CI's matrix
    # call shape is identical across benches.
    if args.check_strict and not args.check:
        # Fall back to legacy check so --check-strict on its own is never
        # a "nothing happened" no-op.
        legacy_diff = compare_search_quality(result, base)
        out["diff"] = legacy_diff
        if not legacy_diff["ok"]:
            exit_code = 1

    if args.report_json:
        _write_report(
            Path(args.report_json),
            _build_report("search-quality", result, base, legacy_diff, None),
        )

    print(json.dumps(out, indent=2, sort_keys=True))
    return exit_code


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="brainctl benchmark harness (search-quality, locomo, longmemeval)"
    )
    p.add_argument(
        "--bench",
        choices=("search-quality", "locomo", "longmemeval"),
        default="search-quality",
        help="Which benchmark to run (default: search-quality)",
    )
    p.add_argument("--update-baseline", action="store_true",
                   help="rewrite the committed baseline JSON for this bench")
    p.add_argument("--baseline-name", default=None,
                   help="Override baseline filename (without .json). Useful for "
                        "writing dated/preserved baselines like "
                        "'locomo_pre_fix_2026_04_18'.")
    p.add_argument("--check", action="store_true",
                   help="compare to baseline and exit non-zero on >2%% regression "
                        "(legacy headline-metric gate; back-compat)")
    p.add_argument("--check-strict", action="store_true",
                   help="also run the PH5 slice + latency gate: hit_at_10 overall, "
                        "per-slice hit_at_1/mrr/ndcg_at_5 (question_type for "
                        "longmemeval, category for locomo), and end-to-end p95 "
                        "latency. Tolerances live in tests/bench/budgets/*.yaml.")
    p.add_argument("--report-json", default=None, metavar="PATH",
                   help="write a machine-readable pass/fail summary (overall + "
                        "slice + latency) to PATH for PR-comment rendering.")

    # search-quality flags
    p.add_argument("--k", type=int, default=10,
                   help="(search-quality) top-k window for ranking metrics")
    p.add_argument("--rows", action="store_true",
                   help="(search-quality) include per-query rows in JSON output")
    p.add_argument("--pipeline", choices=("cmd", "brain"), default="cmd",
                   help="(search-quality) search path: cmd (full hybrid) or brain (FTS5)")

    # external-bench flags
    p.add_argument("--backend", choices=("brain", "cmd"), default=None,
                   help="(locomo/longmemeval) retrieval backend")
    p.add_argument("--convo", type=int, default=None,
                   help="(locomo) run only conversation index N (0..9)")
    p.add_argument("--limit", type=int, default=None,
                   help="(longmemeval) score only the first N entries")
    p.add_argument("--include-judge-only", action="store_true",
                   help="(longmemeval) include the LLM-judge-only axes in headline")
    p.add_argument("--traces", default=None,
                   help="(locomo/longmemeval) write per-query JSONL trace "
                        "records to PATH (qid, query, retrieved_ids, scores, "
                        "gold_ids, hit@k, mrr, category, timings_ms). "
                        "No effect on search-quality bench.")

    return p


def main() -> int:
    args = _build_parser().parse_args()
    if args.bench == "locomo":
        return _run_locomo(args)
    if args.bench == "longmemeval":
        return _run_longmemeval(args)
    return _run_search_quality_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
