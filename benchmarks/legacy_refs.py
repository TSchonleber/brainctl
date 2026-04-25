from __future__ import annotations

import json
from pathlib import Path

from benchmarks.framework import BenchmarkRunResult


ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_RESULTS_DIR = ROOT.parents[1] / "benchmarks" / "results"

BENCHMARK_SPECS = {
    "locomo": {
        "chart": "locomo_comparison.png",
        "metrics": ["avg_recall", "perfect_rate", "zero_rate"],
        "primary_metric": "avg_recall",
    },
    "longmemeval": {
        "chart": "longmemeval_comparison.png",
        "metrics": ["r_at_5", "r_at_10", "ndcg_at_5", "ndcg_at_10"],
        "primary_metric": "r_at_5",
    },
    "membench": {
        "chart": "membench_comparison.png",
        "metrics": ["hit_at_5"],
        "primary_metric": "hit_at_5",
    },
}

AGGREGATE_BENCHMARKS = ["locomo", "longmemeval", "membench"]
COVERAGE_BENCHMARKS = ["convomem", "locomo", "longmemeval", "membench"]


def _coerce_run(payload: dict, *, source_path: Path, used_fallback: bool) -> BenchmarkRunResult:
    system_name = str(payload["system_name"])
    series_name = "old_brainctl" if system_name == "brainctl" else "mempalace"
    notes = list(payload.get("notes") or [])
    if used_fallback:
        notes.append("Loaded from hardcoded fallback because the historical summary bundle was unavailable.")
    return BenchmarkRunResult(
        benchmark=str(payload["benchmark"]),
        system_name=system_name,
        mode=str(payload["mode"]),
        status=str(payload["status"]),
        example_count=int(payload.get("example_count") or 0),
        metrics=dict(payload.get("metrics") or {}),
        primary_metric=payload.get("primary_metric"),
        primary_metric_value=payload.get("primary_metric_value"),
        runtime_seconds=payload.get("runtime_seconds"),
        dataset_path=payload.get("dataset_path"),
        notes=notes,
        caveats=list(payload.get("caveats") or []),
        artifacts=dict(payload.get("artifacts") or {}),
        reference_kind="historical",
        series_name=series_name,
        source_path=str(source_path),
    )


def _candidate_summary_paths() -> list[Path]:
    candidates: list[Path] = []
    exact = HISTORICAL_RESULTS_DIR / "full_compare_20260418_033425" / "summary.json"
    if exact.exists():
        candidates.append(exact)
    if HISTORICAL_RESULTS_DIR.exists():
        candidates.extend(sorted(HISTORICAL_RESULTS_DIR.glob("full_compare_*/summary.json"), reverse=True))
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _fallback_payload() -> dict:
    return {
        "generated_at_utc": "2026-04-18T04:48:04.326900+00:00",
        "notes": [
            "LongMemEval and LoCoMo are configured for full same-machine retrieval comparisons when limits are unset.",
            "MemBench is intentionally marked partial because this harness compares the FirstAgent slice only.",
            "ConvoMem is intentionally marked partial because this harness uses a bounded same-machine sample per category.",
        ],
        "runs": [
            {
                "benchmark": "longmemeval",
                "system_name": "brainctl",
                "mode": "brain",
                "status": "full_same_machine",
                "example_count": 470,
                "metrics": {"r_at_5": 0.9681, "r_at_10": 0.9894, "ndcg_at_5": 0.9204, "ndcg_at_10": 0.9253},
                "primary_metric": "r_at_5",
                "primary_metric_value": 0.9681,
                "runtime_seconds": 85.439,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\LongMemEval\\data\\longmemeval_s_cleaned.json",
                "notes": ["top_k=10"],
                "caveats": [],
                "artifacts": {},
            },
            {
                "benchmark": "longmemeval",
                "system_name": "brainctl",
                "mode": "cmd",
                "status": "full_same_machine",
                "example_count": 470,
                "metrics": {"r_at_5": 0.9702, "r_at_10": 0.9894, "ndcg_at_5": 0.9206, "ndcg_at_10": 0.9247},
                "primary_metric": "r_at_5",
                "primary_metric_value": 0.9702,
                "runtime_seconds": 130.863,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\LongMemEval\\data\\longmemeval_s_cleaned.json",
                "notes": ["top_k=10"],
                "caveats": [],
                "artifacts": {},
            },
            {
                "benchmark": "longmemeval",
                "system_name": "mempalace",
                "mode": "raw_session",
                "status": "full_same_machine",
                "example_count": 470,
                "metrics": {"r_at_5": 0.9660, "r_at_10": 0.9830, "ndcg_at_5": 0.8930, "ndcg_at_10": 0.8948},
                "primary_metric": "r_at_5",
                "primary_metric_value": 0.9660,
                "runtime_seconds": 695.36,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\LongMemEval\\data\\longmemeval_s_cleaned.json",
                "notes": ["top_k=10", "Runs MemPalace benchmark module raw session retrieval logic directly."],
                "caveats": [],
                "artifacts": {},
            },
            {
                "benchmark": "locomo",
                "system_name": "brainctl",
                "mode": "cmd_session",
                "status": "full_same_machine",
                "example_count": 1986,
                "metrics": {"avg_recall": 0.9217, "perfect_rate": 0.8817, "zero_rate": 0.0438, "top_k": 10},
                "primary_metric": "avg_recall",
                "primary_metric_value": 0.9217,
                "runtime_seconds": 445.74,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\locomo\\data\\locomo10.json",
                "notes": ["granularity=session", "top_k=10"],
                "caveats": [],
                "artifacts": {},
            },
            {
                "benchmark": "locomo",
                "system_name": "mempalace",
                "mode": "raw_session",
                "status": "full_same_machine",
                "example_count": 1986,
                "metrics": {"avg_recall": 0.6028, "perfect_rate": 0.5534, "zero_rate": 0.3499, "top_k": 10},
                "primary_metric": "avg_recall",
                "primary_metric_value": 0.6028,
                "runtime_seconds": 2106.411,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\locomo\\data\\locomo10.json",
                "notes": ["granularity=session", "top_k=10"],
                "caveats": [],
                "artifacts": {},
            },
            {
                "benchmark": "membench",
                "system_name": "brainctl",
                "mode": "cmd_turn",
                "status": "partial",
                "example_count": 200,
                "metrics": {"hit_at_5": 0.9300, "top_k": 5},
                "primary_metric": "hit_at_5",
                "primary_metric_value": 0.9300,
                "runtime_seconds": 140.592,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\Membench\\MemData\\FirstAgent",
                "notes": ["FirstAgent slice only", "turn-level retrieval", "topic=all"],
                "caveats": ["MemBench comparison is partial because ThirdAgent and noise-extended slices are not included."],
                "artifacts": {},
            },
            {
                "benchmark": "membench",
                "system_name": "mempalace",
                "mode": "raw_turn",
                "status": "partial",
                "example_count": 200,
                "metrics": {"hit_at_5": 0.8850, "top_k": 5},
                "primary_metric": "hit_at_5",
                "primary_metric_value": 0.8850,
                "runtime_seconds": 804.35,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\Membench\\MemData\\FirstAgent",
                "notes": ["FirstAgent slice only", "turn-level retrieval", "topic=all"],
                "caveats": ["MemBench comparison is partial because ThirdAgent and noise-extended slices are not included."],
                "artifacts": {},
            },
            {
                "benchmark": "convomem",
                "system_name": "brainctl",
                "mode": "cmd",
                "status": "blocked",
                "example_count": 0,
                "metrics": {},
                "primary_metric": "avg_recall",
                "primary_metric_value": None,
                "runtime_seconds": None,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\mempalace\\benchmarks\\convomem_cache",
                "notes": ["limit_per_category=1", "top_k=10"],
                "caveats": ["Blocked while loading ConvoMem evidence data: <urlopen error [WinError 10054] An existing connection was forcibly closed by the remote host>"],
                "artifacts": {},
            },
            {
                "benchmark": "convomem",
                "system_name": "mempalace",
                "mode": "raw",
                "status": "blocked",
                "example_count": 0,
                "metrics": {},
                "primary_metric": "avg_recall",
                "primary_metric_value": None,
                "runtime_seconds": None,
                "dataset_path": "C:\\Users\\mario\\Documents\\GitHub\\mempalace\\benchmarks\\convomem_cache",
                "notes": ["limit_per_category=1", "top_k=10"],
                "caveats": ["Blocked while loading ConvoMem evidence data: <urlopen error [WinError 10054] An existing connection was forcibly closed by the remote host>"],
                "artifacts": {},
            },
        ],
    }


def load_historical_runs() -> tuple[list[BenchmarkRunResult], Path, bool]:
    for candidate in _candidate_summary_paths():
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        runs = [
            _coerce_run(run_payload, source_path=candidate, used_fallback=False)
            for run_payload in payload.get("runs", [])
        ]
        if runs:
            return runs, candidate, False

    fallback_path = HISTORICAL_RESULTS_DIR / "full_compare_20260418_033425" / "summary.json"
    payload = _fallback_payload()
    runs = [
        _coerce_run(run_payload, source_path=fallback_path, used_fallback=True)
        for run_payload in payload.get("runs", [])
    ]
    return runs, fallback_path, True
