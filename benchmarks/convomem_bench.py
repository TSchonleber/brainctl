from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.brainctl_retrieval import rank_documents
from benchmarks.framework import BenchmarkRunResult, BLOCKED, FULL_SAME_MACHINE, PARTIAL


HF_BASE = "https://huggingface.co/datasets/Salesforce/ConvoMem/resolve/main/core_benchmark/evidence_questions"
HF_TREE = "https://huggingface.co/api/datasets/Salesforce/ConvoMem/tree/main/core_benchmark/evidence_questions"

CATEGORIES = {
    "user_evidence": "User Facts",
    "assistant_facts_evidence": "Assistant Facts",
    "changing_evidence": "Changing Facts",
    "abstention_evidence": "Abstention",
    "preference_evidence": "Preferences",
    "implicit_connection_evidence": "Implicit Connections",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _download_json(url: str, path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return _read_json(path)
    last_error: Exception | None = None
    req = urllib.request.Request(url, headers={"User-Agent": "brainctl-convomem-bench/1.0"})
    for _attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 - remote dataset fetch
                payload = response.read().decode("utf-8")
            path.write_text(payload, encoding="utf-8")
            return json.loads(payload)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.4)
            if path.exists():
                return _read_json(path)
    if path.exists():
        return _read_json(path)
    raise last_error or RuntimeError(f"Failed to download {url}")


def _discover_files(category: str, cache_dir: Path) -> list[str]:
    cache_path = cache_dir / f"{category}_1_evidence_files.json"
    url = f"{HF_TREE}/{category}/1_evidence"
    payload = _download_json(url, cache_path)
    paths = []
    for entry in payload:
        raw_path = entry.get("path", "")
        if raw_path.endswith(".json") and f"{category}/" in raw_path:
            paths.append(raw_path.split(f"{category}/", 1)[1])
    return paths


def load_evidence_items(
    *,
    categories: list[str],
    limit_per_category: int,
    cache_dir: Path,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    items: list[dict[str, Any]] = []
    caveats: list[str] = []
    loaded_categories: list[str] = []
    for category in categories:
        loaded = 0
        try:
            subpaths = _discover_files(category, cache_dir)
        except Exception as exc:
            caveats.append(f"{category}: discover failed: {exc!s}")
            continue
        for subpath in subpaths:
            cache_path = cache_dir / category / subpath.replace("/", "_")
            url = f"{HF_BASE}/{category}/{subpath}"
            try:
                payload = _download_json(url, cache_path)
            except Exception as exc:
                caveats.append(f"{category}: item load failed for {subpath}: {exc!s}")
                continue
            for item in payload.get("evidence_items", []):
                item["_category_key"] = category
                items.append(item)
                loaded += 1
                if loaded >= limit_per_category:
                    break
            if loaded >= limit_per_category:
                break
        if loaded > 0:
            loaded_categories.append(category)
        else:
            caveats.append(f"{category}: no evidence items loaded")
    return items, caveats, loaded_categories


def _message_docs(item: dict[str, Any]) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    index = 0
    for conversation in item.get("conversations", []):
        for message in conversation.get("messages", []):
            docs.append((f"msg_{index}", str(message.get("text", ""))))
            index += 1
    return docs


def _evidence_texts(item: dict[str, Any]) -> set[str]:
    texts = set()
    for evidence in item.get("message_evidences", []):
        text = str(evidence.get("text", "")).strip().lower()
        if text:
            texts.add(text)
    return texts


def _recall_from_texts(retrieved_texts: list[str], evidence_texts: set[str]) -> float:
    if not evidence_texts:
        return 1.0
    found = 0
    lowered = [text.strip().lower() for text in retrieved_texts]
    for evidence_text in evidence_texts:
        if any(evidence_text in candidate or candidate in evidence_text for candidate in lowered):
            found += 1
    return found / len(evidence_texts)


def _ranked_texts_from_ids(documents: list[tuple[str, str]], ranked_ids: list[str]) -> list[str]:
    by_id = {doc_id: text for doc_id, text in documents}
    return [by_id[doc_id] for doc_id in ranked_ids if doc_id in by_id]


def run_brainctl_convomem(
    *,
    categories: list[str] | None = None,
    limit_per_category: int = 1,
    top_k: int = 10,
    pipeline: str = "cmd",
    cache_dir: Path | None = None,
) -> tuple[BenchmarkRunResult, list[dict[str, Any]]]:
    if cache_dir is None:
        run = BenchmarkRunResult(
            benchmark="convomem",
            system_name="brainctl",
            mode=pipeline,
            status=BLOCKED,
            example_count=0,
            metrics={},
            primary_metric="avg_recall",
            primary_metric_value=None,
            dataset_path=None,
            notes=[f"limit_per_category={limit_per_category}", f"top_k={top_k}"],
            series_name="new_brainctl",
            caveats=["ConvoMem cache directory is unavailable on this machine."],
        )
        return run, []

    requested_categories = categories or list(CATEGORIES.keys())
    items, caveats, loaded_categories = load_evidence_items(
        categories=requested_categories,
        limit_per_category=limit_per_category,
        cache_dir=cache_dir,
    )

    if not items:
        run = BenchmarkRunResult(
            benchmark="convomem",
            system_name="brainctl",
            mode=pipeline,
            status=BLOCKED,
            example_count=0,
            metrics={},
            primary_metric="avg_recall",
            primary_metric_value=None,
            dataset_path=str(cache_dir),
            notes=[f"limit_per_category={limit_per_category}", f"top_k={top_k}"],
            series_name="new_brainctl",
            caveats=caveats or ["Blocked because no ConvoMem evidence items could be loaded for the requested categories."],
        )
        return run, []

    rows: list[dict[str, Any]] = []
    recalls: list[float] = []
    per_category: dict[str, list[float]] = defaultdict(list)
    started = time.perf_counter()

    for item in items:
        docs = _message_docs(item)
        if not docs:
            continue
        evidence_texts = _evidence_texts(item)
        ranked_ids = rank_documents(item["question"], docs, pipeline=pipeline, top_k=top_k)
        retrieved_texts = _ranked_texts_from_ids(docs, ranked_ids)
        recall = _recall_from_texts(retrieved_texts[:top_k], evidence_texts)
        category = item.get("_category_key", "unknown")
        recalls.append(recall)
        per_category[category].append(recall)
        rows.append(
            {
                "category": category,
                "question": item["question"],
                "recall": round(recall, 4),
                "evidence_count": len(evidence_texts),
                "retrieved_ids": ranked_ids[:top_k],
            }
        )

    runtime_seconds = round(time.perf_counter() - started, 3)
    example_count = len(rows)
    avg_recall = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
    perfect_rate = round(sum(1 for value in recalls if value >= 1.0) / len(recalls), 4) if recalls else 0.0
    metrics: dict[str, float | int] = {"avg_recall": avg_recall, "perfect_rate": perfect_rate, "top_k": top_k}
    for category, values in sorted(per_category.items()):
        metrics[f"{category}_recall"] = round(sum(values) / len(values), 4)

    run = BenchmarkRunResult(
        benchmark="convomem",
        system_name="brainctl",
        mode=pipeline,
        status=FULL_SAME_MACHINE if len(loaded_categories) == len(requested_categories) else PARTIAL,
        example_count=example_count,
        metrics=metrics,
        primary_metric="avg_recall",
        primary_metric_value=avg_recall,
        runtime_seconds=runtime_seconds,
        dataset_path=str(cache_dir),
        notes=[f"categories={len(requested_categories)}", f"limit_per_category={limit_per_category}", f"top_k={top_k}"],
        caveats=(caveats + ["ConvoMem comparison is partial because it uses a bounded same-machine sample, not the full benchmark."]) if len(loaded_categories) != len(requested_categories) else caveats,
        series_name="new_brainctl",
    )
    return run, rows
