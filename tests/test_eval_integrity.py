from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _python_files_under(path: Path) -> list[Path]:
    return [
        item
        for item in path.rglob("*.py")
        if "__pycache__" not in item.parts
    ]


def test_historical_legacy_refs_are_not_imported_by_runtime_retrieval():
    """Frozen comparison bars must not become a runtime lookup table."""

    runtime_files = _python_files_under(ROOT / "src" / "agentmemory")
    runtime_files += [ROOT / "bin" / "intent_classifier.py"]

    forbidden = (
        "benchmarks.legacy_refs",
        "legacy_refs import",
        "import legacy_refs",
    )
    offenders: list[str] = []
    for path in runtime_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in forbidden):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_benchmark_training_and_diagnostic_helpers_are_harness_only():
    """Training/diagnostic scripts stay outside the product retrieval path."""

    runtime_files = _python_files_under(ROOT / "src" / "agentmemory")
    forbidden_modules = (
        "benchmarks.retrieval_flow_optimizer",
        "benchmarks.retrieval_flow_diagnostics",
        "benchmarks.train_tiny_reranker",
        "benchmarks.analyze_benchmark_failures",
    )
    offenders: list[str] = []
    for path in runtime_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(module in text for module in forbidden_modules):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
