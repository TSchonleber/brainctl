from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
SRC = REPO_ROOT / "src"
for _path in (REPO_ROOT, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from benchmarks.brainctl_retrieval import rank_documents_with_rows
from benchmarks.datasets import resolve_dataset_paths
from benchmarks.locomo_bench import _build_corpus as _locomo_build_corpus
from benchmarks.locomo_bench import _load_samples as _locomo_load_samples
from benchmarks.longmemeval_bench import load_entries as _load_longmemeval_entries
from benchmarks.longmemeval_bench import session_document as _longmemeval_session_document
from agentmemory.retrieval.feature_builder import FEATURE_ORDER_V1, FEATURE_VERSION_V1, build_features, vectorize_features
from agentmemory.retrieval.query_planner import plan_query


def _dcg_from_labels(labels: list[int], k: int) -> float:
    total = 0.0
    for idx, label in enumerate(labels[:k], start=1):
        if label > 0:
            total += ((2 ** int(label)) - 1) / max(1.0, math.log2(idx + 1))
    return total


def _dcg_summary(gold_doc_ids: list[str], ranked_doc_ids: list[str], *, k: int) -> tuple[float, float, float]:
    labels = [1 if doc_id in set(gold_doc_ids) else 0 for doc_id in ranked_doc_ids[:k]]
    dcg = _dcg_from_labels(labels, k)
    ideal_labels = sorted(labels + [1] * max(0, len(gold_doc_ids) - len(labels)), reverse=True)[:k]
    idcg = _dcg_from_labels(ideal_labels, k)
    return round(dcg, 6), round(idcg, 6), round(max(idcg - dcg, 0.0), 6)


def _failure_bucket(*, benchmark: str, gold_doc_ids: list[str], ranked_doc_ids: list[str], query_label: str) -> str:
    top = ranked_doc_ids[:5]
    top_hits = [doc_id for doc_id in top if doc_id in set(gold_doc_ids)]
    if benchmark == "longmemeval":
        if top_hits and top[0] not in set(gold_doc_ids):
            return "late_gold"
        if len(set(top)) < len(top):
            return "duplicate_top_slate"
        if "temporal" in query_label.lower():
            return "temporal_anchor_miss"
        return "coverage_miss"
    if len(top_hits) < len(gold_doc_ids):
        return "coverage_miss"
    if "temporal" in query_label.lower():
        return "temporal_anchor_miss"
    return "late_gold"


def _latest_bundle() -> Path:
    candidates = sorted((ROOT / "results").glob("seq_full_compare_final_*"), reverse=True)
    if not candidates:
        raise FileNotFoundError("No seq_full_compare_final_* benchmark bundle found under benchmarks/results/")
    return candidates[0]


def _stable_split(key: str) -> str:
    value = int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16)
    return "heldout" if value % 5 == 0 else "train"


def _read_run_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("rows") or [])


def _serialize_feature_vector(feature_dict: dict[str, float]) -> list[float]:
    vector = vectorize_features(feature_dict, feature_version=FEATURE_VERSION_V1)
    if hasattr(vector, "tolist"):
        return [float(value) for value in vector.tolist()]
    return [float(value) for value in vector]


def _record_for_candidate(
    *,
    benchmark: str,
    query_id: str,
    query: str,
    split: str,
    gold_doc_ids: list[str],
    candidate: dict[str, Any],
    rank: int,
    query_label: str,
    slate_doc_ids: list[str],
) -> dict[str, Any]:
    plan = plan_query(query, requested_tables=["memories"])
    candidate = dict(candidate)
    candidate["bucket"] = "memories"
    candidate["type"] = "memory"
    candidate["_stage_position"] = rank
    features = build_features(query, plan, candidate)
    doc_id = str(candidate.get("doc_id") or "")
    dcg_at_5, idcg_at_5, dcg_gap_at_5 = _dcg_summary(gold_doc_ids, slate_doc_ids, k=5)
    dcg_at_10, idcg_at_10, dcg_gap_at_10 = _dcg_summary(gold_doc_ids, slate_doc_ids, k=10)
    return {
        "benchmark": benchmark,
        "query_id": query_id,
        "query": query,
        "split": split,
        "query_label": query_label,
        "gold_doc_ids": gold_doc_ids,
        "candidate_doc_id": doc_id,
        "label": 1 if doc_id in set(gold_doc_ids) else 0,
        "rank": rank,
        "slate_doc_ids": slate_doc_ids,
        "slate_labels": [1 if value in set(gold_doc_ids) else 0 for value in slate_doc_ids],
        "failure_bucket": _failure_bucket(
            benchmark=benchmark,
            gold_doc_ids=gold_doc_ids,
            ranked_doc_ids=slate_doc_ids,
            query_label=query_label,
        ),
        "dcg_at_5": dcg_at_5,
        "idcg_at_5": idcg_at_5,
        "dcg_gap_at_5": dcg_gap_at_5,
        "dcg_at_10": dcg_at_10,
        "idcg_at_10": idcg_at_10,
        "dcg_gap_at_10": dcg_gap_at_10,
        "source": candidate.get("source"),
        "base_score": candidate.get("pre_second_stage_score", candidate.get("final_score")),
        "retrieval_score": candidate.get("retrieval_score"),
        "rrf_score": candidate.get("rrf_score"),
        "final_score": candidate.get("final_score"),
        "feature_version": FEATURE_VERSION_V1,
        "feature_order": FEATURE_ORDER_V1,
        "feature_dict": features,
        "feature_vector": _serialize_feature_vector(features),
        "candidate_excerpt": (
            candidate.get("content")
            or candidate.get("summary")
            or candidate.get("title")
            or candidate.get("goal")
            or ""
        )[:800],
    }


def build_longmemeval_records(bundle_dir: Path, dataset_path: Path, *, top_k: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_rows = _read_run_rows(bundle_dir / "runs" / "longmemeval_new_brainctl_cmd.json")
    entries = {entry.question_id: entry for entry in _load_longmemeval_entries(dataset_path)}
    records: list[dict[str, Any]] = []
    selected = 0
    skipped_no_window = 0

    for row in run_rows:
        if row.get("r_at_5", 1.0) >= 1.0 and row.get("ndcg_at_5", 1.0) >= 1.0:
            continue
        entry = entries.get(str(row["question_id"]))
        if entry is None:
            continue
        docs = [
            (
                session_id,
                _longmemeval_session_document(session_id, session_date, turns),
            )
            for session_id, session_date, turns in zip(
                entry.haystack_session_ids,
                entry.haystack_dates,
                entry.haystack_sessions,
            )
        ]
        ranked = rank_documents_with_rows(entry.question, docs, pipeline="cmd", top_k=top_k, debug=True)
        gold_ids = list(entry.answer_session_ids)
        ranked_doc_ids = [str(candidate.get("doc_id") or "") for candidate in ranked[:top_k]]
        if not any(doc_id in set(gold_ids) for doc_id in ranked_doc_ids):
            skipped_no_window += 1
            continue
        selected += 1
        split = _stable_split(entry.question_id)
        for rank, candidate in enumerate(ranked[:top_k]):
            records.append(
                _record_for_candidate(
                    benchmark="longmemeval",
                    query_id=entry.question_id,
                    query=entry.question,
                    split=split,
                    gold_doc_ids=gold_ids,
                    candidate=candidate,
                    rank=rank,
                    query_label=entry.question_type,
                    slate_doc_ids=ranked_doc_ids,
                )
            )

    return records, {
        "selected_queries": selected,
        "skipped_no_gold_in_window": skipped_no_window,
    }


def build_locomo_records(bundle_dir: Path, dataset_path: Path, *, top_k: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_rows = _read_run_rows(bundle_dir / "runs" / "locomo_new_brainctl_cmd_session.json")
    samples = {str(sample.get("sample_id")): sample for sample in _locomo_load_samples(dataset_path)}
    records: list[dict[str, Any]] = []
    selected = 0
    skipped_no_window = 0

    for idx, row in enumerate(run_rows):
        if float(row.get("recall", 1.0) or 1.0) >= 1.0:
            continue
        sample = samples.get(str(row["sample_id"]))
        if sample is None:
            continue
        sessions = []
        session_num = 1
        while True:
            key = f"session_{session_num}"
            date_key = f"session_{session_num}_date_time"
            if key not in sample["conversation"]:
                break
            sessions.append(
                {
                    "session_num": session_num,
                    "date": sample["conversation"].get(date_key, ""),
                    "dialogs": sample["conversation"][key],
                }
            )
            session_num += 1
        docs = _locomo_build_corpus(sessions, granularity="session")
        ranked = rank_documents_with_rows(str(row["question"]), docs, pipeline="cmd", top_k=top_k, debug=True)
        gold_ids = [str(value) for value in row.get("evidence_ids", [])]
        ranked_doc_ids = [str(candidate.get("doc_id") or "") for candidate in ranked[:top_k]]
        if not any(doc_id in set(gold_ids) for doc_id in ranked_doc_ids):
            skipped_no_window += 1
            continue
        query_id = hashlib.sha1(f"{row['sample_id']}|{row['question']}|{idx}".encode("utf-8")).hexdigest()[:12]
        split = _stable_split(query_id)
        selected += 1
        for rank, candidate in enumerate(ranked[:top_k]):
            records.append(
                _record_for_candidate(
                    benchmark="locomo",
                    query_id=query_id,
                    query=str(row["question"]),
                    split=split,
                    gold_doc_ids=gold_ids,
                    candidate=candidate,
                    rank=rank,
                    query_label=str(row.get("category_name") or row.get("category") or "unknown"),
                    slate_doc_ids=ranked_doc_ids,
                )
            )
    return records, {
        "selected_queries": selected,
        "skipped_no_gold_in_window": skipped_no_window,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LongMemEval + LoCoMo hard-negative reranker data.")
    parser.add_argument("--bundle-dir", type=Path, default=None, help="Legacy comparison bundle directory.")
    parser.add_argument("--output", type=Path, default=ROOT / "training_data" / "hard_negatives_v1.jsonl")
    parser.add_argument("--summary", type=Path, default=ROOT / "training_data" / "hard_negatives_v1_summary.json")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    bundle_dir = args.bundle_dir or _latest_bundle()
    dataset_paths = resolve_dataset_paths()
    if dataset_paths.longmemeval_data is None or dataset_paths.locomo_data is None:
        raise FileNotFoundError("LongMemEval or LoCoMo dataset path is unavailable on this machine.")

    long_records, long_summary = build_longmemeval_records(bundle_dir, dataset_paths.longmemeval_data, top_k=args.top_k)
    locomo_records, locomo_summary = build_locomo_records(bundle_dir, dataset_paths.locomo_data, top_k=args.top_k)
    records = long_records + locomo_records
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")

    split_counts = Counter(record["split"] for record in records)
    label_counts = Counter(record["label"] for record in records)
    summary = {
        "bundle_dir": str(bundle_dir),
        "output": str(args.output),
        "record_count": len(records),
        "split_counts": dict(split_counts),
        "label_counts": dict(label_counts),
        "longmemeval": long_summary,
        "locomo": locomo_summary,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
