from __future__ import annotations

import csv
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FULL_SAME_MACHINE = "full_same_machine"
PARTIAL = "partial"
BLOCKED = "blocked"

SERIES_COLORS = {
    "old_brainctl": "#4c78a8",
    "new_brainctl": "#54a24b",
    "mempalace": "#f58518",
}

SERIES_ORDER = {
    "old_brainctl": 0,
    "new_brainctl": 1,
    "mempalace": 2,
}


def _load_matplotlib():
    """Import matplotlib only when chart rendering is actually requested."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return matplotlib, plt


@dataclass
class BenchmarkRunResult:
    benchmark: str
    system_name: str
    mode: str
    status: str
    example_count: int
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    primary_metric: str | None = None
    primary_metric_value: float | None = None
    runtime_seconds: float | None = None
    dataset_path: str | None = None
    notes: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    reference_kind: str = "measured"
    series_name: str | None = None
    source_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        series = self.series_name or self.system_name
        return f"{series.replace('_', ' ')}\n{self.mode}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["measured"] = self.reference_kind == "measured"
        return payload


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_artifact_dir(root: Path, label: str = "comparison") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = root / "results" / f"{label}_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "runs").mkdir(exist_ok=True)
    (path / "charts").mkdir(exist_ok=True)
    return path


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _metric_fieldnames(runs: Iterable[BenchmarkRunResult]) -> list[str]:
    keys: set[str] = set()
    for run in runs:
        keys.update(run.metrics.keys())
    return sorted(keys)


def write_summary_csv(path: Path, runs: list[BenchmarkRunResult]) -> Path:
    fieldnames = [
        "benchmark",
        "series_name",
        "system_name",
        "mode",
        "reference_kind",
        "status",
        "example_count",
        "primary_metric",
        "primary_metric_value",
        "runtime_seconds",
        "dataset_path",
        "source_path",
        "notes",
        "caveats",
    ] + _metric_fieldnames(runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for run in runs:
            row = {
                "benchmark": run.benchmark,
                "series_name": run.series_name,
                "system_name": run.system_name,
                "mode": run.mode,
                "reference_kind": run.reference_kind,
                "status": run.status,
                "example_count": run.example_count,
                "primary_metric": run.primary_metric,
                "primary_metric_value": run.primary_metric_value,
                "runtime_seconds": run.runtime_seconds,
                "dataset_path": run.dataset_path,
                "source_path": run.source_path,
                "notes": " | ".join(run.notes),
                "caveats": " | ".join(run.caveats),
            }
            row.update(run.metrics)
            writer.writerow(row)
    return path


def write_run_payload(
    path: Path,
    run: BenchmarkRunResult,
    rows: list[dict[str, Any]] | None = None,
) -> Path:
    payload = run.to_dict()
    if rows is not None:
        payload["rows"] = rows
    return write_json(path, payload)


def write_normalized_comparison(path: Path, runs: list[BenchmarkRunResult]) -> Path:
    rows: list[dict[str, Any]] = []
    for run in runs:
        for metric, value in sorted(run.metrics.items()):
            rows.append(
                {
                    "benchmark": run.benchmark,
                    "metric": metric,
                    "series_name": run.series_name,
                    "system_name": run.system_name,
                    "mode": run.mode,
                    "reference_kind": run.reference_kind,
                    "status": run.status,
                    "value": value,
                    "example_count": run.example_count,
                    "dataset_path": run.dataset_path,
                    "source_path": run.source_path,
                }
            )
    return write_json(path, rows)


def write_normalized_comparison_csv(path: Path, runs: list[BenchmarkRunResult]) -> Path:
    rows: list[dict[str, Any]] = []
    for run in runs:
        for metric, value in sorted(run.metrics.items()):
            rows.append(
                {
                    "benchmark": run.benchmark,
                    "metric": metric,
                    "series_name": run.series_name,
                    "system_name": run.system_name,
                    "mode": run.mode,
                    "reference_kind": run.reference_kind,
                    "status": run.status,
                    "value": value,
                    "example_count": run.example_count,
                    "dataset_path": run.dataset_path,
                    "source_path": run.source_path,
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "benchmark",
                "metric",
                "series_name",
                "system_name",
                "mode",
                "reference_kind",
                "status",
                "value",
                "example_count",
                "dataset_path",
                "source_path",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def _sort_runs_for_plot(runs: list[BenchmarkRunResult]) -> list[BenchmarkRunResult]:
    return sorted(
        runs,
        key=lambda run: (
            SERIES_ORDER.get(run.series_name or run.system_name, 99),
            (run.mode != "brain"),
            run.mode,
        ),
    )


def plot_benchmark_chart(
    path: Path,
    benchmark_name: str,
    runs: list[BenchmarkRunResult],
    metric_keys: list[str],
) -> Path | None:
    _matplotlib, plt = _load_matplotlib()
    plotted = [
        run
        for run in _sort_runs_for_plot(runs)
        if run.status != BLOCKED and any(run.metrics.get(key) is not None for key in metric_keys)
    ]
    if not plotted:
        return None

    x = list(range(len(metric_keys)))
    width = 0.8 / max(len(plotted), 1)

    fig, ax = plt.subplots(figsize=(max(8, len(metric_keys) * 2.0), 5))
    for idx, run in enumerate(plotted):
        offsets = [pos + (idx - (len(plotted) - 1) / 2) * width for pos in x]
        values = [float(run.metrics.get(key) or 0.0) for key in metric_keys]
        color = SERIES_COLORS.get(run.series_name or run.system_name)
        ax.bar(offsets, values, width=width, label=run.label(), color=color)

    ymax = max(float(run.metrics.get(key) or 0.0) for run in plotted for key in metric_keys)
    ax.set_title(f"{benchmark_name} comparison")
    ax.set_xticks(x)
    ax.set_xticklabels(metric_keys, rotation=20, ha="right")
    ax.set_ylim(0, max(1.0, ymax * 1.15))
    ax.set_ylabel("score")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_aggregate_primary_chart(
    path: Path,
    runs: list[BenchmarkRunResult],
    benchmarks: list[str],
) -> Path | None:
    _matplotlib, plt = _load_matplotlib()
    measured = [
        run
        for run in runs
        if run.benchmark in benchmarks
        and run.status != BLOCKED
        and run.primary_metric_value is not None
    ]
    if not measured:
        return None

    benchmark_order = [name for name in benchmarks if any(run.benchmark == name for run in measured)]
    run_labels = []
    for run in _sort_runs_for_plot(measured):
        label = f"{run.series_name}|{run.mode}"
        if label not in run_labels:
            run_labels.append(label)

    x = list(range(len(benchmark_order)))
    width = 0.8 / max(len(run_labels), 1)

    fig, ax = plt.subplots(figsize=(max(9, len(benchmark_order) * 2.5), 5))
    for idx, run_label in enumerate(run_labels):
        offsets = [pos + (idx - (len(run_labels) - 1) / 2) * width for pos in x]
        values: list[float] = []
        color = None
        pretty_label = run_label.replace("|", "\n").replace("_", " ")
        for benchmark in benchmark_order:
            match = next(
                (
                    run
                    for run in measured
                    if run.benchmark == benchmark and f"{run.series_name}|{run.mode}" == run_label
                ),
                None,
            )
            values.append(float(match.primary_metric_value or 0.0) if match else 0.0)
            if match and color is None:
                color = SERIES_COLORS.get(match.series_name or match.system_name)
        ax.bar(offsets, values, width=width, label=pretty_label, color=color)

    ymax = max(float(run.primary_metric_value or 0.0) for run in measured)
    ax.set_title("Primary metric by benchmark")
    ax.set_xticks(x)
    ax.set_xticklabels(benchmark_order, rotation=15, ha="right")
    ax.set_ylabel("primary score")
    ax.set_ylim(0, max(1.0, ymax * 1.15))
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_status_chart(path: Path, runs: list[BenchmarkRunResult], benchmarks: list[str]) -> Path:
    _matplotlib, plt = _load_matplotlib()
    status_order = [FULL_SAME_MACHINE, PARTIAL, BLOCKED]
    colors = {
        FULL_SAME_MACHINE: "#4c78a8",
        PARTIAL: "#f58518",
        BLOCKED: "#e45756",
    }

    counts: dict[str, list[int]] = {status: [] for status in status_order}
    for benchmark in benchmarks:
        benchmark_runs = [run for run in runs if run.benchmark == benchmark]
        for status in status_order:
            counts[status].append(sum(1 for run in benchmark_runs if run.status == status))

    fig, ax = plt.subplots(figsize=(max(8, len(benchmarks) * 2.0), 5))
    bottom = [0] * len(benchmarks)
    x = list(range(len(benchmarks)))
    for status in status_order:
        values = counts[status]
        ax.bar(x, values, bottom=bottom, label=status, color=colors[status])
        bottom = [a + b for a, b in zip(bottom, values)]

    ax.set_title("Benchmark coverage status")
    ax.set_xticks(x)
    ax.set_xticklabels(benchmarks, rotation=15, ha="right")
    ax.set_ylabel("run count")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def runtime_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "generated_at_utc": now_utc_iso(),
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }
    if extra:
        payload.update(extra)
    return payload


def write_bundle_summary(
    path: Path,
    runs: list[BenchmarkRunResult],
    *,
    notes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    payload = {
        "generated_at_utc": now_utc_iso(),
        "metadata": metadata or {},
        "runs": [run.to_dict() for run in runs],
        "notes": notes or [],
    }
    return write_json(path, payload)
