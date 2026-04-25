"""Optional local judge reranker for top candidates."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class JudgeConfig:
    enabled: bool = False
    provider: str = "ollama"
    model: str = "llama3.2:3b"
    top_k: int = 5
    timeout_s: float = 6.0
    url: str = "http://localhost:11434/api/generate"


def _coerce_score(value: str) -> float:
    match = re.search(r"(-?\d+(?:\.\d+)?)", value or "")
    if not match:
        return 0.0
    try:
        score = float(match.group(1))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(score, 1.0))


def _candidate_synopsis(candidate: dict[str, Any]) -> str:
    for key in ("content", "summary", "title", "goal", "description", "search_text", "name", "compiled_truth"):
        value = candidate.get(key)
        if value:
            text = str(value).strip()
            return text[:1200]
    return ""


def _judge_with_ollama(query: str, candidates: list[dict[str, Any]], config: JudgeConfig) -> list[float]:
    scores: list[float] = []
    for candidate in candidates[: config.top_k]:
        prompt = (
            "You are a retrieval judge. Score relevance from 0.0 to 1.0.\n"
            "Return only the numeric score.\n\n"
            f"Query: {query}\n\n"
            f"Candidate: {_candidate_synopsis(candidate)}\n"
        )
        payload = json.dumps(
            {
                "model": config.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            config.url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:  # noqa: S310 - local optional service
                body = json.loads(resp.read().decode("utf-8"))
            scores.append(_coerce_score(str(body.get("response") or "")))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return []
    return scores


def judge_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    config: JudgeConfig | None = None,
) -> list[float]:
    """Return optional judge scores for the top candidates."""

    cfg = config or JudgeConfig()
    if not cfg.enabled or not candidates:
        return []
    if cfg.provider == "ollama":
        return _judge_with_ollama(query, candidates, cfg)
    return []

