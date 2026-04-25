from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
GITHUB_ROOT = ROOT.parents[2]
HISTORICAL_REPO_ROOT = ROOT.parents[1]


@dataclass
class DatasetPaths:
    longmemeval_data: Path | None
    locomo_data: Path | None
    membench_data: Path | None
    convomem_cache: Path | None


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def _first_existing(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def resolve_dataset_paths() -> DatasetPaths:
    return DatasetPaths(
        longmemeval_data=_first_existing(
            [
                _env_path("BRAINCTL_LEGACY_LONGMEMEVAL_DATA"),
                GITHUB_ROOT / "LongMemEval" / "data" / "longmemeval_s_cleaned.json",
            ]
        ),
        locomo_data=_first_existing(
            [
                _env_path("BRAINCTL_LEGACY_LOCOMO_DATA"),
                GITHUB_ROOT / "locomo" / "data" / "locomo10.json",
                ROOT / "tests" / "bench" / "locomo" / "locomo10.json",
            ]
        ),
        membench_data=_first_existing(
            [
                _env_path("BRAINCTL_LEGACY_MEMBENCH_DATA"),
                GITHUB_ROOT / "Membench" / "MemData" / "FirstAgent",
            ]
        ),
        convomem_cache=_first_existing(
            [
                _env_path("BRAINCTL_LEGACY_CONVOMEM_CACHE"),
                GITHUB_ROOT / "mempalace" / "benchmarks" / "convomem_cache",
            ]
        ),
    )
