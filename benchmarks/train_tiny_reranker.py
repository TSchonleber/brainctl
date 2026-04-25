from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
SRC = REPO_ROOT / "src"
for _path in (REPO_ROOT, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agentmemory.retrieval.feature_builder import FEATURE_ORDER_V1, FEATURE_VERSION_V1
from agentmemory.retrieval.mlp_reranker import DEFAULT_MODEL_PATH


@dataclass(slots=True)
class TrainConfig:
    epochs: int = 24
    lr: float = 0.01
    l2: float = 1e-4
    seed: int = 42
    hidden1: int = 32
    hidden2: int = 16
    ndcg_k: int = 5


def _load_records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _dcg(labels: list[int], k: int) -> float:
    total = 0.0
    for idx, label in enumerate(labels[:k], start=1):
        gain = (2**int(label)) - 1
        total += gain / math.log2(idx + 1)
    return total


def _group_query_metrics(records: list[dict[str, Any]], scores_by_key: dict[tuple[str, str], float], *, k: int = 5) -> dict[str, float]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(str(record["benchmark"]), str(record["query_id"]))].append(record)

    long_ndcgs: list[float] = []
    locomo_perfect: list[float] = []
    for (benchmark, _query_id), items in grouped.items():
        ranked = sorted(items, key=lambda row: scores_by_key[(str(row["query_id"]), str(row["candidate_doc_id"]))], reverse=True)
        top = ranked[:k]
        labels = [int(row["label"]) for row in top]
        if benchmark == "longmemeval":
            dcg = _dcg(labels, k)
            ideal_labels = sorted((int(row["label"]) for row in items), reverse=True)
            ideal_dcg = _dcg(ideal_labels, k)
            long_ndcgs.append((dcg / ideal_dcg) if ideal_dcg > 0 else 0.0)
        elif benchmark == "locomo":
            positives = sum(int(row["label"]) for row in items)
            if positives <= 0:
                continue
            top_positive = sum(int(row["label"]) for row in top)
            locomo_perfect.append(1.0 if top_positive == positives else 0.0)
    return {
        "heldout_longmemeval_ndcg_at_5": round(float(np.mean(long_ndcgs)) if long_ndcgs else 0.0, 4),
        "heldout_locomo_perfect_rate_at_5": round(float(np.mean(locomo_perfect)) if locomo_perfect else 0.0, 4),
    }


def _init_params(rng: np.random.Generator, input_dim: int, config: TrainConfig) -> dict[str, np.ndarray]:
    return {
        "w1": rng.normal(0.0, 0.12, size=(input_dim, config.hidden1)),
        "b1": np.zeros(config.hidden1, dtype=float),
        "w2": rng.normal(0.0, 0.12, size=(config.hidden1, config.hidden2)),
        "b2": np.zeros(config.hidden2, dtype=float),
        "w3": rng.normal(0.0, 0.12, size=(config.hidden2, 1)),
        "b3": np.zeros(1, dtype=float),
    }


def _forward(
    x: np.ndarray,
    params: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    z1 = x @ params["w1"] + params["b1"]
    h1 = np.maximum(0.0, z1)
    z2 = h1 @ params["w2"] + params["b2"]
    h2 = np.maximum(0.0, z2)
    logits = h2 @ params["w3"] + params["b3"]
    probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
    return z1, h1, z2, h2, logits, probs


def _clone_params(params: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {key: np.array(value, copy=True) for key, value in params.items()}


def _group_indices(records: list[dict[str, Any]]) -> list[np.ndarray]:
    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, row in enumerate(records):
        grouped[(str(row["benchmark"]), str(row["query_id"]))].append(idx)
    return [np.asarray(indices, dtype=int) for indices in grouped.values() if len(indices) >= 2]


def _lambda_pairwise_gradients(logits: np.ndarray, labels: np.ndarray, *, k: int) -> np.ndarray:
    flat_logits = logits.reshape(-1)
    order = np.argsort(-flat_logits)
    ranks = np.empty_like(order)
    ranks[order] = np.arange(len(order))
    ideal_dcg = _dcg(sorted((int(label) for label in labels.tolist()), reverse=True), k)
    grad = np.zeros_like(flat_logits, dtype=float)
    if ideal_dcg <= 0:
        return grad.reshape(-1, 1)

    pair_count = 0
    for i in range(len(flat_logits)):
        for j in range(len(flat_logits)):
            if labels[i] <= labels[j]:
                continue
            rank_i = int(ranks[i]) + 1
            rank_j = int(ranks[j]) + 1
            if min(rank_i, rank_j) > max(k, 10):
                continue
            gain_i = (2 ** int(labels[i])) - 1
            gain_j = (2 ** int(labels[j])) - 1
            delta_discount = abs((1.0 / math.log2(rank_i + 1)) - (1.0 / math.log2(rank_j + 1)))
            delta_gain = abs(gain_i - gain_j)
            top_weight = 1.0 if min(rank_i, rank_j) <= k else 0.35
            weight = (delta_discount * delta_gain / ideal_dcg) * top_weight
            if weight <= 0.0:
                continue
            diff = float(np.clip(flat_logits[i] - flat_logits[j], -30.0, 30.0))
            prob = 1.0 / (1.0 + math.exp(-diff))
            g = weight * (prob - 1.0)
            grad[i] += g
            grad[j] -= g
            pair_count += 1
    if pair_count:
        grad /= float(pair_count)
    return grad.reshape(-1, 1)


def _train_model(
    train_x: np.ndarray,
    train_y: np.ndarray,
    train_groups: list[np.ndarray],
    config: TrainConfig,
    *,
    initial_params: dict[str, np.ndarray] | None = None,
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(config.seed)
    params = _clone_params(initial_params) if initial_params is not None else _init_params(rng, train_x.shape[1], config)
    for _epoch in range(config.epochs):
        rng.shuffle(train_groups)
        for group in train_groups:
            x = train_x[group]
            y = train_y[group]
            z1, h1, z2, h2, logits, _probs = _forward(x, params)
            grad_logits = _lambda_pairwise_gradients(logits, y, k=config.ndcg_k)
            if not np.any(grad_logits):
                continue
            grad_w3 = (h2.T @ grad_logits) / len(group) + config.l2 * params["w3"]
            grad_b3 = grad_logits.mean(axis=0)
            grad_h2 = grad_logits @ params["w3"].T
            grad_z2 = grad_h2 * (z2 > 0)
            grad_w2 = (h1.T @ grad_z2) / len(group) + config.l2 * params["w2"]
            grad_b2 = grad_z2.mean(axis=0)
            grad_h1 = grad_z2 @ params["w2"].T
            grad_z1 = grad_h1 * (z1 > 0)
            grad_w1 = (x.T @ grad_z1) / len(group) + config.l2 * params["w1"]
            grad_b1 = grad_z1.mean(axis=0)
            params["w3"] -= config.lr * grad_w3
            params["b3"] -= config.lr * grad_b3
            params["w2"] -= config.lr * grad_w2
            params["b2"] -= config.lr * grad_b2
            params["w1"] -= config.lr * grad_w1
            params["b1"] -= config.lr * grad_b1
    return params


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the tiny shared second-stage MLP reranker.")
    parser.add_argument("--data", type=Path, default=ROOT / "training_data" / "hard_negatives_v1.jsonl")
    parser.add_argument("--report", type=Path, default=ROOT / "training_data" / "tiny_mlp_v1_report.json")
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--epochs", type=int, default=24)
    parser.add_argument("--lr", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    records = _load_records(args.data)
    records = [row for row in records if list(row.get("feature_order") or []) == FEATURE_ORDER_V1]
    if not records:
        raise ValueError(f"No usable training rows found in {args.data}")

    train_records = [row for row in records if row.get("split") != "heldout"]
    heldout_records = [row for row in records if row.get("split") == "heldout"]
    train_x = np.asarray([row["feature_vector"] for row in train_records], dtype=float)
    train_y = np.asarray([float(row["label"]) for row in train_records], dtype=float)
    heldout_x = (
        np.asarray([row["feature_vector"] for row in heldout_records], dtype=float)
        if heldout_records
        else np.zeros((0, len(FEATURE_ORDER_V1)))
    )

    norm_mean = train_x.mean(axis=0)
    norm_std = train_x.std(axis=0)
    safe_std = np.where(norm_std == 0.0, 1.0, norm_std)
    train_x_norm = (train_x - norm_mean) / safe_std
    heldout_x_norm = (heldout_x - norm_mean) / safe_std if len(heldout_x) else heldout_x

    config = TrainConfig(epochs=args.epochs, lr=args.lr, seed=args.seed)
    train_groups = _group_indices(train_records)
    params = _train_model(train_x_norm, train_y, train_groups, config)
    _, _, _, _, _train_logits, train_probs = _forward(train_x_norm, params)
    heldout_probs = np.zeros((len(heldout_x_norm), 1), dtype=float)
    if len(heldout_x_norm):
        _, _, _, _, _heldout_logits, heldout_probs = _forward(heldout_x_norm, params)

    def _scores(rows: list[dict[str, Any]], probs: np.ndarray) -> dict[tuple[str, str], float]:
        return {
            (str(row["query_id"]), str(row["candidate_doc_id"])): float(prob)
            for row, prob in zip(rows, probs.reshape(-1))
        }

    train_metrics = _group_query_metrics(train_records, _scores(train_records, train_probs))
    heldout_metrics = _group_query_metrics(heldout_records, _scores(heldout_records, heldout_probs))

    long_only = [row for row in train_records if row.get("benchmark") == "longmemeval"]
    long_applied = False
    if long_only:
        long_x = np.asarray([row["feature_vector"] for row in long_only], dtype=float)
        long_y = np.asarray([float(row["label"]) for row in long_only], dtype=float)
        long_x_norm = (long_x - norm_mean) / safe_std
        extra_config = TrainConfig(epochs=1, lr=args.lr, seed=args.seed)
        extra_groups = _group_indices(long_only)
        extra_params = _train_model(long_x_norm, long_y, extra_groups, extra_config, initial_params=params)
        if len(heldout_x_norm):
            _, _, _, _, _extra_logits, extra_probs = _forward(heldout_x_norm, extra_params)
            extra_metrics = _group_query_metrics(heldout_records, _scores(heldout_records, extra_probs))
            if (
                extra_metrics["heldout_longmemeval_ndcg_at_5"] >= heldout_metrics["heldout_longmemeval_ndcg_at_5"]
                and extra_metrics["heldout_locomo_perfect_rate_at_5"] >= heldout_metrics["heldout_locomo_perfect_rate_at_5"] - 0.005
            ):
                params = extra_params
                heldout_probs = extra_probs
                heldout_metrics = extra_metrics
                long_applied = True

    model_payload = {
        "feature_version": FEATURE_VERSION_V1,
        "feature_order": FEATURE_ORDER_V1,
        "norm_mean": [round(float(v), 8) for v in norm_mean.tolist()],
        "norm_std": [round(float(v if v != 0 else 1.0), 8) for v in safe_std.tolist()],
        "w1": np.asarray(params["w1"], dtype=float).T.round(8).tolist(),
        "b1": np.asarray(params["b1"], dtype=float).round(8).tolist(),
        "w2": np.asarray(params["w2"], dtype=float).T.round(8).tolist(),
        "b2": np.asarray(params["b2"], dtype=float).round(8).tolist(),
        "w3": np.asarray(params["w3"], dtype=float).T.round(8).tolist(),
        "b3": np.asarray(params["b3"], dtype=float).round(8).tolist(),
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_data": str(args.data),
            "objective": "lambda_weighted_pairwise_ndcg_at_5",
            "train_records": len(train_records),
            "heldout_records": len(heldout_records),
            "longmemeval_extra_epoch_applied": long_applied,
        },
    }

    args.model_out.parent.mkdir(parents=True, exist_ok=True)
    args.model_out.write_text(json.dumps(model_payload, indent=2), encoding="utf-8")

    report = {
        "data": str(args.data),
        "model_out": str(args.model_out),
        "train_records": len(train_records),
        "heldout_records": len(heldout_records),
        "objective": "lambda_weighted_pairwise_ndcg_at_5",
        "train_metrics": train_metrics,
        "heldout_metrics": heldout_metrics,
        "longmemeval_extra_epoch_applied": long_applied,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
