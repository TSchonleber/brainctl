#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from benchmarks.convomem_bench import run_brainctl_convomem
from benchmarks.datasets import resolve_dataset_paths
from benchmarks.framework import (
    BenchmarkRunResult,
    new_artifact_dir,
    plot_aggregate_primary_chart,
    plot_benchmark_chart,
    plot_status_chart,
    runtime_metadata,
    write_bundle_summary,
    write_json,
    write_normalized_comparison,
    write_normalized_comparison_csv,
    write_run_payload,
    write_summary_csv,
    write_text,
)
from benchmarks.legacy_refs import AGGREGATE_BENCHMARKS, BENCHMARK_SPECS, COVERAGE_BENCHMARKS, load_historical_runs
from benchmarks.locomo_bench import run_brainctl_locomo
from benchmarks.longmemeval_bench import run_brainctl_longmemeval_pipeline
from benchmarks.membench_bench import run_brainctl_membench


def _git_commit() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
        )
    except Exception:
        return None


def _write_run_artifact(
    artifact_dir: Path,
    run: BenchmarkRunResult,
    rows: list[dict] | None = None,
) -> None:
    run_path = artifact_dir / "runs" / f"{run.benchmark}_{run.series_name}_{run.mode}.json"
    write_run_payload(run_path, run, rows=rows)
    run.artifacts["run_json"] = str(run_path)


def _provenance_readme(
    *,
    artifact_dir: Path,
    historical_source,
    used_fallback: bool,
    dataset_paths,
    argv: list[str],
    runs: list[BenchmarkRunResult],
) -> None:
    limited = [run for run in runs if run.status in {"blocked", "partial"}]
    text = "\n".join(
        [
            "# Legacy BrainCTL vs MemPalace comparison bundle",
            "",
            f"- Current repo commit: `{_git_commit() or 'unknown'}`",
            f"- Historical reference source: `{historical_source}`",
            f"- Historical source mode: `{'fallback' if used_fallback else 'recovered summary bundle'}`",
            f"- Command: `{' '.join(argv)}`",
            "",
            "## Datasets",
            "",
            f"- LongMemEval: `{dataset_paths.longmemeval_data}`",
            f"- LoCoMo: `{dataset_paths.locomo_data}`",
            f"- MemBench FirstAgent: `{dataset_paths.membench_data}`",
            f"- ConvoMem cache: `{dataset_paths.convomem_cache}`",
            "",
            "## What is measured now",
            "",
            "- New BrainCTL reruns: LongMemEval `brain` and `cmd`, LoCoMo `cmd_session`, MemBench FirstAgent `cmd_turn`, and ConvoMem `cmd` coverage/status.",
            "- Old BrainCTL and MemPalace are frozen historical reference series loaded from the recovered 2026-04-18 bundle.",
            "",
            "## Blocked or partial runs",
            "",
        ]
        + ([f"- {run.benchmark} {run.series_name} {run.mode}: {' | '.join(run.caveats) or run.status}" for run in limited] if limited else ["- none"])
        + [
            "",
            "## Output files",
            "",
            "- `summary.json` and `summary.csv`: all series in one table.",
            "- `comparison_table.json` and `comparison_table.csv`: long-form metric rows.",
            "- `runs/*.json`: per-run payloads.",
            "- `charts/*.png`: regenerated charts with old BrainCTL, new BrainCTL, and MemPalace together.",
        ]
    )
    write_text(artifact_dir / "README.md", text + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the legacy BrainCTL vs MemPalace comparison charts.")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Base directory for results/charts output (default: benchmarks/)",
    )
    parser.add_argument("--label", default="legacy_compare_refresh", help="Artifact directory prefix label.")
    parser.add_argument("--longmemeval-limit", type=int, default=None)
    parser.add_argument("--locomo-limit", type=int, default=None)
    parser.add_argument("--membench-limit", type=int, default=None)
    parser.add_argument("--membench-top-k", type=int, default=5)
    parser.add_argument("--convomem-limit-per-category", type=int, default=1)
    parser.add_argument("--convomem-top-k", type=int, default=10)
    parser.add_argument("--skip-convomem", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("BRAINCTL_SILENT_MIGRATIONS", "1")
    artifact_dir = new_artifact_dir(args.artifact_dir, label=args.label)
    dataset_paths = resolve_dataset_paths()
    historical_runs, historical_source, used_fallback = load_historical_runs()

    measured_runs_with_rows = [
        run_brainctl_longmemeval_pipeline("brain", dataset_paths.longmemeval_data, limit=args.longmemeval_limit),
        run_brainctl_longmemeval_pipeline("cmd", dataset_paths.longmemeval_data, limit=args.longmemeval_limit),
        run_brainctl_locomo(
            dataset_paths.locomo_data,
            pipeline="cmd",
            granularity="session",
            limit=args.locomo_limit,
        ),
        run_brainctl_membench(
            dataset_paths.membench_data,
            pipeline="cmd",
            top_k=args.membench_top_k,
            limit=args.membench_limit,
        ),
    ]
    if not args.skip_convomem:
        measured_runs_with_rows.append(
            run_brainctl_convomem(
                limit_per_category=args.convomem_limit_per_category,
                top_k=args.convomem_top_k,
                cache_dir=dataset_paths.convomem_cache,
            )
        )

    measured_runs = [run for run, _rows in measured_runs_with_rows]
    all_runs = historical_runs + measured_runs

    for run in historical_runs:
        _write_run_artifact(artifact_dir, run, rows=None)
    for run, rows in measured_runs_with_rows:
        _write_run_artifact(artifact_dir, run, rows=rows)

    for benchmark_name, spec in BENCHMARK_SPECS.items():
        chart_path = plot_benchmark_chart(
            artifact_dir / "charts" / spec["chart"],
            benchmark_name,
            [run for run in all_runs if run.benchmark == benchmark_name],
            spec["metrics"],
        )
        if chart_path is not None:
            for run in all_runs:
                if run.benchmark == benchmark_name:
                    run.artifacts["benchmark_chart"] = str(chart_path)

    aggregate_chart = plot_aggregate_primary_chart(
        artifact_dir / "charts" / "aggregate_primary_metrics.png",
        all_runs,
        AGGREGATE_BENCHMARKS,
    )
    if aggregate_chart is not None:
        for run in all_runs:
            if run.benchmark in AGGREGATE_BENCHMARKS:
                run.artifacts["aggregate_chart"] = str(aggregate_chart)

    status_chart = plot_status_chart(
        artifact_dir / "charts" / "coverage_status.png",
        all_runs,
        COVERAGE_BENCHMARKS,
    )
    for run in all_runs:
        run.artifacts["status_chart"] = str(status_chart)

    # Rewrite per-run payloads after chart paths are attached so every JSON
    # artifact is self-contained.
    for run in all_runs:
        rows = None
        for measured_run, measured_rows in measured_runs_with_rows:
            if measured_run is run:
                rows = measured_rows
                break
        _write_run_artifact(artifact_dir, run, rows=rows)

    metadata = runtime_metadata(
        {
            "git_commit": _git_commit(),
            "cwd": str(ROOT),
            "argv": sys.argv,
            "historical_summary_path": str(historical_source),
            "historical_summary_mode": "fallback" if used_fallback else "recovered",
            "datasets": {
                "longmemeval_data": str(dataset_paths.longmemeval_data) if dataset_paths.longmemeval_data else None,
                "locomo_data": str(dataset_paths.locomo_data) if dataset_paths.locomo_data else None,
                "membench_data": str(dataset_paths.membench_data) if dataset_paths.membench_data else None,
                "convomem_cache": str(dataset_paths.convomem_cache) if dataset_paths.convomem_cache else None,
            },
        }
    )

    write_bundle_summary(
        artifact_dir / "summary.json",
        all_runs,
        notes=[
            "Historical old-BrainCTL and MemPalace series come from the recovered 2026-04-18 comparison bundle.",
            "New BrainCTL series are rerun in the current checked-out repo using the legacy benchmark definitions.",
            "MemBench remains intentionally partial because the legacy comparison only covered the FirstAgent slice.",
            "ConvoMem remains a coverage/status benchmark here; it has no dedicated comparison chart in the legacy chart pack.",
        ],
        metadata=metadata,
    )
    write_summary_csv(artifact_dir / "summary.csv", all_runs)
    write_normalized_comparison(artifact_dir / "comparison_table.json", all_runs)
    write_normalized_comparison_csv(artifact_dir / "comparison_table.csv", all_runs)
    write_json(artifact_dir / "metadata.json", metadata)
    _provenance_readme(
        artifact_dir=artifact_dir,
        historical_source=historical_source,
        used_fallback=used_fallback,
        dataset_paths=dataset_paths,
        argv=sys.argv,
        runs=all_runs,
    )

    print(artifact_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
