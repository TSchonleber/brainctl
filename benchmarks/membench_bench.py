from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.brainctl_retrieval import rank_documents
from benchmarks.framework import BenchmarkRunResult, BLOCKED, PARTIAL


CATEGORY_FILES = {
    "simple": "simple.json",
    "highlevel": "highlevel.json",
    "knowledge_update": "knowledge_update.json",
    "comparative": "comparative.json",
    "conditional": "conditional.json",
    "noisy": "noisy.json",
    "aggregative": "aggregative.json",
    "highlevel_rec": "highlevel_rec.json",
    "lowlevel_rec": "lowlevel_rec.json",
    "RecMultiSession": "RecMultiSession.json",
    "post_processing": "post_processing.json",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_items(
    data_dir: Path,
    *,
    categories: list[str] | None = None,
    topic: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected_categories = categories or list(CATEGORY_FILES.keys())
    items: list[dict[str, Any]] = []
    for category in selected_categories:
        file_name = CATEGORY_FILES.get(category)
        if not file_name:
            continue
        path = data_dir / file_name
        if not path.exists():
            continue
        raw = _load_json(path)
        for key, topic_items in raw.items():
            if topic and key not in (topic, "roles", "events"):
                continue
            for item in topic_items:
                turns = item.get("message_list", [])
                qa = item.get("QA", {})
                if not turns or not qa:
                    continue
                items.append(
                    {
                        "category": category,
                        "topic": key,
                        "tid": item.get("tid", 0),
                        "turns": turns,
                        "question": qa.get("question", ""),
                        "target_step_ids": qa.get("target_step_id", []),
                    }
                )
                if limit and len(items) >= limit:
                    return items
    return items


def _turn_text(turn: dict[str, Any]) -> str:
    user = turn.get("user") or turn.get("user_message", "")
    assistant = turn.get("assistant") or turn.get("assistant_message", "")
    when = turn.get("time", "")
    text = f"[User] {user} [Assistant] {assistant}"
    return f"[{when}] {text}" if when else text


def _flatten_turns(message_list: list[Any], item_key: str) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    sessions = [message_list] if message_list and isinstance(message_list[0], dict) else message_list
    global_idx = 0
    for session_idx, session in enumerate(sessions):
        if not isinstance(session, list):
            continue
        for turn_idx, turn in enumerate(session):
            if not isinstance(turn, dict):
                continue
            sid = turn.get("sid", turn.get("mid", global_idx))
            doc_id = f"{item_key}|sid={sid}|g={global_idx}|s={session_idx}|t={turn_idx}"
            docs.append((doc_id, _turn_text(turn)))
            global_idx += 1
    return docs


def _target_ids(target_step_ids: list[Any]) -> set[str]:
    targets: set[str] = set()
    for step in target_step_ids:
        if isinstance(step, list) and step:
            targets.add(str(step[0]))
        else:
            targets.add(str(step))
    return targets


def _hit_at_k(retrieved_ids: list[str], targets: set[str]) -> bool:
    if not targets:
        return False
    for retrieved in retrieved_ids:
        for target in targets:
            if f"sid={target}|" in retrieved or f"|g={target}|" in retrieved:
                return True
    return False


def run_brainctl_membench(
    data_dir: Path | None,
    *,
    pipeline: str = "cmd",
    categories: list[str] | None = None,
    topic: str | None = None,
    top_k: int = 5,
    limit: int | None = None,
) -> tuple[BenchmarkRunResult, list[dict[str, Any]]]:
    if data_dir is None or not data_dir.exists():
        run = BenchmarkRunResult(
            benchmark="membench",
            system_name="brainctl",
            mode=f"{pipeline}_turn",
            status=BLOCKED,
            example_count=0,
            metrics={},
            primary_metric=f"hit_at_{top_k}",
            primary_metric_value=None,
            dataset_path=str(data_dir) if data_dir else None,
            series_name="new_brainctl",
            caveats=["MemBench FirstAgent data is unavailable on this machine."],
        )
        return run, []

    items = load_items(data_dir, categories=categories, topic=topic, limit=limit)
    rows: list[dict[str, Any]] = []
    by_category: dict[str, list[bool]] = defaultdict(list)
    hits = 0
    started = time.perf_counter()

    for idx, item in enumerate(items):
        item_key = f"{item['category']}_{item['topic']}_{idx}"
        docs = _flatten_turns(item["turns"], item_key)
        if not docs:
            continue
        retrieved_ids = rank_documents(item["question"], docs, pipeline=pipeline, top_k=top_k)
        targets = _target_ids(item["target_step_ids"])
        hit = _hit_at_k(retrieved_ids, targets)
        if hit:
            hits += 1
        by_category[item["category"]].append(hit)
        rows.append(
            {
                "category": item["category"],
                "topic": item["topic"],
                "tid": item["tid"],
                "question": item["question"],
                "retrieved_ids": retrieved_ids,
                "target_ids": sorted(targets),
                "hit_at_k": hit,
            }
        )

    runtime_seconds = round(time.perf_counter() - started, 3)
    example_count = len(rows)
    hit_rate = round(hits / example_count, 4) if example_count else 0.0
    metrics: dict[str, float | int] = {f"hit_at_{top_k}": hit_rate, "top_k": top_k}
    for category, values in sorted(by_category.items()):
        metrics[f"{category}_hit_at_{top_k}"] = round(sum(1 for value in values if value) / len(values), 4)

    run = BenchmarkRunResult(
        benchmark="membench",
        system_name="brainctl",
        mode=f"{pipeline}_turn",
        status=PARTIAL,
        example_count=example_count,
        metrics=metrics,
        primary_metric=f"hit_at_{top_k}",
        primary_metric_value=hit_rate,
        runtime_seconds=runtime_seconds,
        dataset_path=str(data_dir),
        notes=["FirstAgent slice only", "turn-level retrieval", f"topic={'all' if topic is None else topic}"],
        caveats=["MemBench comparison is partial because ThirdAgent and noise-extended slices are not included."],
        series_name="new_brainctl",
    )
    return run, rows
