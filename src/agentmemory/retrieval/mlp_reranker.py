"""Tiny MLP reranker inference loaded from a JSON artifact."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - numpy is optional at import time
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

from agentmemory.retrieval.feature_builder import FEATURE_ORDER_V1, FEATURE_VERSION_V1

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "tiny_mlp_v1.json"


def _relu(value: float) -> float:
    return value if value > 0.0 else 0.0


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


@dataclass(slots=True)
class TinyMLPModel:
    feature_version: str
    feature_order: list[str]
    norm_mean: list[float]
    norm_std: list[float]
    w1: list[list[float]]
    b1: list[float]
    w2: list[list[float]]
    b2: list[float]
    w3: list[list[float]]
    b3: list[float]
    metadata: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "TinyMLPModel":
        model_path = Path(path) if path is not None else DEFAULT_MODEL_PATH
        payload = json.loads(model_path.read_text(encoding="utf-8"))
        return cls(
            feature_version=str(payload["feature_version"]),
            feature_order=list(payload["feature_order"]),
            norm_mean=[float(v) for v in payload["norm_mean"]],
            norm_std=[float(v) for v in payload["norm_std"]],
            w1=[[float(v) for v in row] for row in payload["w1"]],
            b1=[float(v) for v in payload["b1"]],
            w2=[[float(v) for v in row] for row in payload["w2"]],
            b2=[float(v) for v in payload["b2"]],
            w3=[[float(v) for v in row] for row in payload["w3"]],
            b3=[float(v) for v in payload["b3"]],
            metadata=dict(payload.get("metadata") or {}),
        )

    @classmethod
    def try_load(cls, path: str | Path | None = None) -> "TinyMLPModel | None":
        try:
            model_path = Path(path) if path is not None else DEFAULT_MODEL_PATH
            if not model_path.exists():
                return None
            return cls.load(model_path)
        except Exception:
            return None

    def _normalize(self, feature_matrix):
        if _np is not None:
            matrix = _np.asarray(feature_matrix, dtype=float)
            mean = _np.asarray(self.norm_mean, dtype=float)
            std = _np.asarray(self.norm_std, dtype=float)
            safe_std = _np.where(std == 0.0, 1.0, std)
            return (matrix - mean) / safe_std
        rows: list[list[float]] = []
        for row in feature_matrix:
            rows.append([
                (float(value) - self.norm_mean[idx]) / (self.norm_std[idx] if self.norm_std[idx] not in (0.0, 0) else 1.0)
                for idx, value in enumerate(row)
            ])
        return rows

    def score(self, feature_matrix) -> list[float]:
        if self.feature_version != FEATURE_VERSION_V1:
            raise ValueError(f"Unsupported feature version: {self.feature_version}")
        if self.feature_order != FEATURE_ORDER_V1:
            raise ValueError("Feature order mismatch between runtime and model artifact")
        if _np is not None:
            x = self._normalize(feature_matrix)
            w1 = _np.asarray(self.w1, dtype=float)
            b1 = _np.asarray(self.b1, dtype=float)
            w2 = _np.asarray(self.w2, dtype=float)
            b2 = _np.asarray(self.b2, dtype=float)
            w3 = _np.asarray(self.w3, dtype=float)
            b3 = _np.asarray(self.b3, dtype=float)
            h1 = _np.maximum(0.0, x @ w1.T + b1)
            h2 = _np.maximum(0.0, h1 @ w2.T + b2)
            logits = h2 @ w3.T + b3
            logits = _np.clip(logits.reshape(-1), -30.0, 30.0)
            probs = 1.0 / (1.0 + _np.exp(-logits))
            return [float(v) for v in probs.tolist()]

        x_rows = self._normalize(feature_matrix)
        outputs: list[float] = []
        for row in x_rows:
            h1: list[float] = []
            for bias, weights in zip(self.b1, self.w1):
                total = bias
                for value, weight in zip(row, weights):
                    total += value * weight
                h1.append(_relu(total))
            h2: list[float] = []
            for bias, weights in zip(self.b2, self.w2):
                total = bias
                for value, weight in zip(h1, weights):
                    total += value * weight
                h2.append(_relu(total))
            total = self.b3[0] if self.b3 else 0.0
            for value, weight in zip(h2, self.w3[0]):
                total += value * weight
            outputs.append(_sigmoid(total))
        return outputs
