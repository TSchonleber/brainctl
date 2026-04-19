"""Entry point for the brainctl benchmarks.

Examples:
    python -m tests.bench.run                                # search-quality (default)
    python -m tests.bench.run --bench search-quality
    python -m tests.bench.run --bench locomo --backend brain
    python -m tests.bench.run --bench locomo --backend cmd --update-baseline
    python -m tests.bench.run --bench longmemeval --update-baseline
    python -m tests.bench.run --bench locomo --check        # regress-gate vs baseline

All bench entries share the same regression-tolerance / baseline JSON
pattern so a single CI command (``--check``) can gate any of them.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict

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

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"

# Headline metrics gated for each bench. Keep these narrow on purpose —
# noisy per-category numbers regress under tolerance much more often than
# real quality issues, so they stay informational only.
LOCOMO_GATED = ("hit_at_1", "hit_at_5", "mrr", "ndcg_at_5", "recall_at_5")
LONGMEMEVAL_GATED = ("hit_at_1", "hit_at_5", "mrr", "ndcg_at_5", "recall_at_5")


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
    """Keep the comparable fields, drop the verbose per-row payload."""
    keep = {"overall", "by_category", "ks", "backend"}
    return {k: v for k, v in result.items() if k in keep}


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

    if args.check:
        baseline = _load_json(baseline_path)
        if baseline is None:
            print(f"no baseline at {baseline_path}; run with --update-baseline first",
                  file=sys.stderr)
            return 2
        diff = _shared_compare(result, baseline, LOCOMO_GATED)
        out["diff"] = diff
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if diff["ok"] else 1

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


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

    if args.check:
        baseline = _load_json(baseline_path)
        if baseline is None:
            print(f"no baseline at {baseline_path}; run with --update-baseline first",
                  file=sys.stderr)
            return 2
        diff = _shared_compare(result, baseline, LONGMEMEVAL_GATED)
        out["diff"] = diff
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if diff["ok"] else 1

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


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

    if args.check:
        base = _load_search_quality_baseline()
        if base is None:
            print("no baseline committed; run with --update-baseline first",
                  file=sys.stderr)
            return 2
        diff = compare_search_quality(result, base)
        out["diff"] = diff
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if diff["ok"] else 1

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


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
                   help="compare to baseline and exit non-zero on >2%% regression")

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
