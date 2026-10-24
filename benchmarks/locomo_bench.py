from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.brainctl_retrieval import rank_seeded_documents, seed_documents
from benchmarks.framework import BenchmarkRunResult, BLOCKED, FULL_SAME_MACHINE


CATEGORIES = {
    1: "Single-hop",
    2: "Temporal",
    3: "Temporal-inference",
    4: "Open-domain",
    5: "Adversarial",
}


def _load_samples(data_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    payload = json.loads(data_path.read_text(encoding="utf-8-sig"))
    samples = list(payload)
    if limit:
        samples = samples[:limit]
    return samples


def _load_sessions(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    session_num = 1
    while True:
        key = f"session_{session_num}"
        date_key = f"session_{session_num}_date_time"
        if key not in conversation:
            break
        sessions.append(
            {
                "session_num": session_num,
                "date": conversation.get(date_key, ""),
                "dialogs": conversation[key],
            }
        )
        session_num += 1
    return sessions


def _build_corpus(sessions: list[dict[str, Any]], granularity: str) -> list[tuple[str, str]]:
    corpus: list[tuple[str, str]] = []
    for session in sessions:
        if granularity == "session":
            texts = [
                f"Session ID: session_{session['session_num']}",
                f"Session Date: {session.get('date', '')}",
                "Conversation:",
            ]
            for dialog in session["dialogs"]:
                speaker = dialog.get("speaker", "?")
                text = dialog.get("text", "")
                texts.append(f'{speaker} said, "{text}"')
            corpus.append((f"session_{session['session_num']}", "\n".join(texts)))
            continue

        for dialog in session["dialogs"]:
            dialog_id = dialog.get("dia_id", f"D{session['session_num']}:?")
            speaker = dialog.get("speaker", "?")
            text = dialog.get("text", "")
            corpus.append((dialog_id, f'{speaker} said, "{text}"'))
    return corpus


def _evidence_ids(evidence: list[str], granularity: str) -> set[str]:
    if granularity == "dialog":
        return set(evidence)
    sessions: set[str] = set()
    for evidence_id in evidence:
        if evidence_id.startswith("D") and ":" in evidence_id:
            sessions.add(f"session_{evidence_id[1:].split(':', 1)[0]}")
    return sessions


def _recall(retrieved_ids: list[str], evidence_ids: set[str]) -> float:
    if not evidence_ids:
        return 1.0
    found = sum(1 for item in evidence_ids if item in retrieved_ids)
    return found / len(evidence_ids)


def _dcg_from_binary(retrieved_ids: list[str], evidence_ids: set[str], k: int) -> float:
    total = 0.0
    for idx, item in enumerate(retrieved_ids[:k], start=1):
        if item in evidence_ids:
            total += 1.0 / __import__("math").log2(idx + 1)
    return total


def run_brainctl_locomo(
    data_path: Path | None,
    *,
    pipeline: str = "cmd",
    granularity: str = "session",
    top_k: int = 10,
    limit: int | None = None,
) -> tuple[BenchmarkRunResult, list[dict[str, Any]]]:
    if data_path is None or not data_path.exists():
        run = BenchmarkRunResult(
            benchmark="locomo",
            system_name="brainctl",
            mode=f"{pipeline}_{granularity}",
            status=BLOCKED,
            example_count=0,
            metrics={},
            primary_metric="avg_recall",
            primary_metric_value=None,
            dataset_path=str(data_path) if data_path else None,
            series_name="new_brainctl",
            caveats=["LoCoMo dataset path is unavailable on this machine."],
        )
        return run, []

    samples = _load_samples(data_path, limit=limit)
    rows: list[dict[str, Any]] = []
    per_category: dict[int, list[float]] = defaultdict(list)
    recalls: list[float] = []
    started = time.perf_counter()

    for sample in samples:
        sample_id = sample.get("sample_id", "unknown")
        sessions = _load_sessions(sample["conversation"])
        corpus = _build_corpus(sessions, granularity=granularity)
        seeded = seed_documents(corpus)
        try:
            for qa in sample["qa"]:
                question = qa["question"]
                evidence_ids = _evidence_ids(qa.get("evidence", []), granularity)
                retrieved_ids = rank_seeded_documents(question, seeded, pipeline=pipeline, top_k=top_k)
                recall = _recall(retrieved_ids, evidence_ids)
                category = int(qa["category"])
                recalls.append(recall)
                per_category[category].append(recall)
                rows.append(
                    {
                        "sample_id": sample_id,
                        "question": question,
                        "category": category,
                        "category_name": CATEGORIES.get(category, str(category)),
                        "evidence_ids": sorted(evidence_ids),
                        "retrieved_ids": retrieved_ids,
                        "recall": round(recall, 4),
                        "dcg_at_5": round(_dcg_from_binary(retrieved_ids, evidence_ids, 5), 4),
                        "idcg_at_5": round(_dcg_from_binary(sorted(evidence_ids), evidence_ids, 5), 4),
                        "dcg_gap_at_5": round(max(_dcg_from_binary(sorted(evidence_ids), evidence_ids, 5) - _dcg_from_binary(retrieved_ids, evidence_ids, 5), 0.0), 4),
                        "dcg_at_10": round(_dcg_from_binary(retrieved_ids, evidence_ids, 10), 4),
                        "idcg_at_10": round(_dcg_from_binary(sorted(evidence_ids), evidence_ids, 10), 4),
                        "dcg_gap_at_10": round(max(_dcg_from_binary(sorted(evidence_ids), evidence_ids, 10) - _dcg_from_binary(retrieved_ids, evidence_ids, 10), 0.0), 4),
                        "failure_bucket": (
                            "coverage_miss"
                            if recall < 1.0 and any(item in evidence_ids for item in retrieved_ids[:top_k])
                            else "temporal_anchor_miss"
                            if "temporal" in CATEGORIES.get(category, "").lower()
                            else "late_gold"
                            if any(item in evidence_ids for item in retrieved_ids[:top_k]) and retrieved_ids[:1] and retrieved_ids[0] not in evidence_ids
                            else "grounded"
                        ),
                    }
                )
        finally:
            seeded.cleanup()

    runtime_seconds = round(time.perf_counter() - started, 3)
    example_count = len(rows)
    avg_recall = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
    perfect_rate = round(sum(1 for value in recalls if value >= 1.0) / len(recalls), 4) if recalls else 0.0
    zero_rate = round(sum(1 for value in recalls if value == 0.0) / len(recalls), 4) if recalls else 0.0
    metrics: dict[str, float | int] = {
        "avg_recall": avg_recall,
        "perfect_rate": perfect_rate,
        "zero_rate": zero_rate,
        "top_k": top_k,
    }
    for category, values in sorted(per_category.items()):
        metrics[f"cat_{category}_recall"] = round(sum(values) / len(values), 4)

    run = BenchmarkRunResult(
        benchmark="locomo",
        system_name="brainctl",
        mode=f"{pipeline}_{granularity}",
        status=FULL_SAME_MACHINE,
        example_count=example_count,
        metrics=metrics,
        primary_metric="avg_recall",
        primary_metric_value=avg_recall,
        runtime_seconds=runtime_seconds,
        dataset_path=str(data_path),
        notes=[f"granularity={granularity}", f"top_k={top_k}"],
        series_name="new_brainctl",
    )
    return run, rows
