from __future__ import annotations

import json
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from benchmarks.brainctl_retrieval import rank_documents
from benchmarks.framework import BenchmarkRunResult, BLOCKED, FULL_SAME_MACHINE


@dataclass
class QuestionEntry:
    question_id: str
    question_type: str
    question: str
    answer_session_ids: list[str]
    haystack_session_ids: list[str]
    haystack_dates: list[str]
    haystack_sessions: list[list[dict[str, Any]]]


def _is_abstention(raw: dict[str, Any]) -> bool:
    qid = str(raw.get("question_id", ""))
    return qid.endswith("_abs") or not raw.get("answer_session_ids")


def load_entries(
    dataset_path: Path,
    *,
    include_abstention: bool = False,
    limit: int | None = None,
) -> list[QuestionEntry]:
    payload = json.loads(dataset_path.read_text(encoding="utf-8-sig"))
    entries: list[QuestionEntry] = []
    for raw in payload:
        if not include_abstention and _is_abstention(raw):
            continue
        entries.append(
            QuestionEntry(
                question_id=str(raw["question_id"]),
                question_type=str(raw["question_type"]),
                question=str(raw["question"]),
                answer_session_ids=[str(x) for x in raw.get("answer_session_ids", [])],
                haystack_session_ids=[str(x) for x in raw.get("haystack_session_ids", [])],
                haystack_dates=[str(x) for x in raw.get("haystack_dates", [])],
                haystack_sessions=list(raw.get("haystack_sessions", [])),
            )
        )
        if limit is not None and len(entries) >= limit:
            break
    return entries


def session_document(session_id: str, session_date: str, turns: list[dict[str, Any]]) -> str:
    lines = [f"Session ID: {session_id}", f"Session Date: {session_date}", "Conversation:"]
    for turn in turns:
        role = str(turn.get("role", "unknown")).strip().title() or "Unknown"
        content = str(turn.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def dcg(relevances: Iterable[float], k: int) -> float:
    total = 0.0
    for i, rel in enumerate(list(relevances)[:k]):
        total += rel / math.log2(i + 2)
    return total


def ndcg(rankings: list[int], correct_ids: set[str], corpus_ids: list[str], k: int) -> float:
    relevances = [1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in rankings[:k]]
    ideal = sorted(relevances, reverse=True)
    ideal_dcg = dcg(ideal, k)
    if ideal_dcg == 0:
        return 0.0
    return dcg(relevances, k) / ideal_dcg


def recall_any(rankings: list[int], correct_ids: set[str], corpus_ids: list[str], k: int) -> float:
    top_ids = {corpus_ids[idx] for idx in rankings[:k]}
    return float(any(cid in top_ids for cid in correct_ids))


def recall_all(rankings: list[int], correct_ids: set[str], corpus_ids: list[str], k: int) -> float:
    if not correct_ids:
        return 1.0
    top_ids = {corpus_ids[idx] for idx in rankings[:k]}
    return float(all(cid in top_ids for cid in correct_ids))


def _mean(values: Iterable[float]) -> float:
    bucket = list(values)
    if not bucket:
        return 0.0
    return round(sum(bucket) / len(bucket), 4)


def run_entry(entry: QuestionEntry, *, pipeline: str = "cmd", top_k: int = 10) -> dict[str, Any]:
    docs = [
        (session_id, session_document(session_id, session_date, turns))
        for session_id, session_date, turns in zip(
            entry.haystack_session_ids,
            entry.haystack_dates,
            entry.haystack_sessions,
        )
    ]
    ranked_session_ids = rank_documents(entry.question, docs, pipeline=pipeline, top_k=top_k)
    seen = set(ranked_session_ids)
    remaining = [sid for sid in entry.haystack_session_ids if sid not in seen]
    corpus_ids = ranked_session_ids + remaining
    ranked_indices = list(range(len(ranked_session_ids)))
    correct_ids = set(entry.answer_session_ids)
    dcg_at_5 = round(dcg([1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in ranked_indices[:5]], 5), 4)
    dcg_at_10 = round(dcg([1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in ranked_indices[:10]], 10), 4)
    ideal_labels = sorted([1.0 if session_id in correct_ids else 0.0 for session_id in corpus_ids], reverse=True)
    idcg_at_5 = round(dcg(ideal_labels, 5), 4)
    idcg_at_10 = round(dcg(ideal_labels, 10), 4)
    top_ids = ranked_session_ids[:5]
    if any(session_id in correct_ids for session_id in top_ids) and top_ids and top_ids[0] not in correct_ids:
        failure_bucket = "late_gold"
    elif len(set(top_ids)) < len(top_ids):
        failure_bucket = "duplicate_top_slate"
    elif "temporal" in entry.question_type.lower():
        failure_bucket = "temporal_anchor_miss"
    elif top_ids and len([session_id for session_id in top_ids if session_id in correct_ids]) < min(len(correct_ids), 5):
        failure_bucket = "coverage_miss"
    else:
        failure_bucket = "grounded"
    return {
        "question_id": entry.question_id,
        "question_type": entry.question_type,
        "question": entry.question,
        "r_at_5": recall_any(ranked_indices, correct_ids, corpus_ids, 5),
        "r_at_10": recall_any(ranked_indices, correct_ids, corpus_ids, 10),
        "r_all_at_5": recall_all(ranked_indices, correct_ids, corpus_ids, 5),
        "r_all_at_10": recall_all(ranked_indices, correct_ids, corpus_ids, 10),
        "ndcg_at_5": round(ndcg(ranked_indices, correct_ids, corpus_ids, 5), 4),
        "ndcg_at_10": round(ndcg(ranked_indices, correct_ids, corpus_ids, 10), 4),
        "dcg_at_5": dcg_at_5,
        "idcg_at_5": idcg_at_5,
        "dcg_gap_at_5": round(max(idcg_at_5 - dcg_at_5, 0.0), 4),
        "dcg_at_10": dcg_at_10,
        "idcg_at_10": idcg_at_10,
        "dcg_gap_at_10": round(max(idcg_at_10 - dcg_at_10, 0.0), 4),
        "failure_bucket": failure_bucket,
        "answer_session_ids": entry.answer_session_ids,
        "top_session_ids": ranked_session_ids[:top_k],
    }


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    overall = {
        "n_questions": len(rows),
        "r_at_5": _mean(row["r_at_5"] for row in rows),
        "r_at_10": _mean(row["r_at_10"] for row in rows),
        "r_all_at_5": _mean(row["r_all_at_5"] for row in rows),
        "r_all_at_10": _mean(row["r_all_at_10"] for row in rows),
        "ndcg_at_5": _mean(row["ndcg_at_5"] for row in rows),
        "ndcg_at_10": _mean(row["ndcg_at_10"] for row in rows),
    }
    by_question_type: dict[str, dict[str, float]] = {}
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[row["question_type"]].append(row)
    for question_type, group in sorted(buckets.items()):
        by_question_type[question_type] = {
            "count": len(group),
            "r_at_5": _mean(row["r_at_5"] for row in group),
            "r_at_10": _mean(row["r_at_10"] for row in group),
            "r_all_at_5": _mean(row["r_all_at_5"] for row in group),
            "r_all_at_10": _mean(row["r_all_at_10"] for row in group),
            "ndcg_at_5": _mean(row["ndcg_at_5"] for row in group),
            "ndcg_at_10": _mean(row["ndcg_at_10"] for row in group),
        }
    return {"overall": overall, "by_question_type": by_question_type}


def run_brainctl_longmemeval_pipeline(
    pipeline: str,
    dataset_path: Path | None,
    *,
    limit: int | None = None,
    include_abstention: bool = False,
    top_k: int = 10,
) -> tuple[BenchmarkRunResult, list[dict[str, Any]]]:
    if dataset_path is None or not dataset_path.exists():
        run = BenchmarkRunResult(
            benchmark="longmemeval",
            system_name="brainctl",
            mode=pipeline,
            status=BLOCKED,
            example_count=0,
            metrics={},
            primary_metric="r_at_5",
            primary_metric_value=None,
            dataset_path=str(dataset_path) if dataset_path else None,
            series_name="new_brainctl",
            caveats=["LongMemEval dataset path is unavailable on this machine."],
        )
        return run, []

    random.seed(42)
    os.environ.setdefault("BRAINCTL_SILENT_MIGRATIONS", "1")
    started = time.perf_counter()
    entries = load_entries(dataset_path, include_abstention=include_abstention, limit=limit)
    rows = [run_entry(entry, pipeline=pipeline, top_k=top_k) for entry in entries]
    runtime_seconds = round(time.perf_counter() - started, 3)
    overall = aggregate_rows(rows)["overall"]
    run = BenchmarkRunResult(
        benchmark="longmemeval",
        system_name="brainctl",
        mode=pipeline,
        status=FULL_SAME_MACHINE,
        example_count=int(overall["n_questions"]),
        metrics={
            "r_at_5": overall["r_at_5"],
            "r_at_10": overall["r_at_10"],
            "ndcg_at_5": overall["ndcg_at_5"],
            "ndcg_at_10": overall["ndcg_at_10"],
        },
        primary_metric="r_at_5",
        primary_metric_value=float(overall["r_at_5"]),
        runtime_seconds=runtime_seconds,
        dataset_path=str(dataset_path),
        notes=[f"top_k={top_k}", "Legacy 470-question session-level slice."],
        series_name="new_brainctl",
    )
    return run, rows
