from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.retrieval_flow_diagnostics import analyze_retrieval_flow, render_markdown_report


def _latest_bundle() -> Path:
    bundles = sorted((ROOT / "results").glob("seq_full_compare_final_*"), reverse=True)
    if not bundles:
        raise FileNotFoundError("No seq_full_compare_final_* bundle found under benchmarks/results/")
    return bundles[0]


def _load_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("rows") or [])


def _metric(row: dict, key: str, default: float) -> float:
    value = row.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize current LongMemEval/LoCoMo/MemBench failure slices.")
    parser.add_argument("--bundle-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()

    bundle_dir = args.bundle_dir or _latest_bundle()
    long_rows = _load_rows(bundle_dir / "runs" / "longmemeval_new_brainctl_cmd.json")
    locomo_rows = _load_rows(bundle_dir / "runs" / "locomo_new_brainctl_cmd_session.json")
    membench_rows = _load_rows(bundle_dir / "runs" / "membench_new_brainctl_cmd_turn.json")

    long_fail_r5 = [row for row in long_rows if _metric(row, "r_at_5", 1.0) < 1.0]
    long_fail_ndcg = [row for row in long_rows if _metric(row, "ndcg_at_5", 1.0) < 1.0]
    locomo_nonperfect = [row for row in locomo_rows if _metric(row, "recall", 1.0) < 1.0]
    locomo_zero = [row for row in locomo_rows if _metric(row, "recall", 0.0) == 0.0]
    membench_miss = [
        row for row in membench_rows
        if not bool(row.get("hit_at_k", row.get("hit_at_5", True)))
    ]

    flow = analyze_retrieval_flow(
        longmemeval_rows=long_rows,
        locomo_rows=locomo_rows,
        membench_rows=membench_rows,
        top_n=max(args.top_n, 1),
    )

    payload = {
        "bundle_dir": str(bundle_dir),
        "longmemeval": {
            "total": len(long_rows),
            "fail_r_at_5": len(long_fail_r5),
            "fail_ndcg_at_5": len(long_fail_ndcg),
            "by_question_type": dict(Counter(str(row.get("question_type")) for row in long_fail_ndcg).most_common()),
        },
        "locomo": {
            "total": len(locomo_rows),
            "nonperfect": len(locomo_nonperfect),
            "zero_recall": len(locomo_zero),
            "by_category": dict(Counter(str(row.get("category_name")) for row in locomo_nonperfect).most_common()),
        },
        "membench": {
            "total": len(membench_rows),
            "misses": len(membench_miss),
        },
        "retrieval_flow": flow,
    }

    text = json.dumps(payload, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown_report(flow), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
