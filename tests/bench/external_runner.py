"""Shared plumbing for external-benchmark runners (LOCOMO, LongMemEval, ...).

Two exports:

* ``ingest_conversation_into_brain(brain, turns)`` — bulk-loads a list of
  ``(speaker, text, key)`` turn tuples into a Brain instance, embedding a
  ``[key=...]`` marker in the content so we can resolve search hits back
  to their gold ID after the FTS5 roundtrip.

* ``eval_questions(search_fn, questions, top_k=5, ks=(1, 5, 10, 20))`` —
  runs a list of ``(question, gold_keys)`` items through ``search_fn``
  and returns per-question rows + aggregated metrics (Hit@K, Recall@K,
  MRR, nDCG@K).

Both helpers are deliberately thin and pure — they don't pin a specific
backend. Callers pass in either:

* a ``Brain`` instance + the helper's default ``brain.search`` wrapping
  via ``brain_search_fn(brain)``, or
* a custom ``search_fn`` (e.g. the ``cmd_search`` hybrid path) that
  closes over the underlying DB.

This mirrors the SearchFn pattern in ``tests/bench/eval.py`` so future
benchmarks reuse one set of metric primitives.
"""

from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Reuse the metric primitives from the existing search-quality bench so
# ``ndcg_at_k`` / ``mrr`` semantics stay aligned across all benches.
import sys
_BENCH_ROOT = Path(__file__).resolve().parent
if str(_BENCH_ROOT.parent.parent / "src") not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT.parent.parent / "src"))
from tests.bench.eval import (  # noqa: E402  (path tweak above)
    dcg_at_k,
    mrr as _mrr_pure,
    ndcg_at_k,
    p_at_k,
    recall_at_k,
)

KEY_RE = re.compile(r"\[key=([^\]]+)\]")
DEFAULT_KS: Tuple[int, ...] = (1, 5, 10, 20)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SearchFn = Callable[[str, int], List[Dict[str, Any]]]


@dataclass(frozen=True)
class Turn:
    """One conversational turn to ingest."""
    key: str            # gold ID (e.g. LOCOMO "D1:5", LongMemEval session ID)
    speaker: str
    text: str
    timestamp: str = "" # optional ISO timestamp string for context


@dataclass(frozen=True)
class Question:
    """One QA pair to score."""
    question: str
    gold_keys: List[str]
    category: str = ""             # optional axis label
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QuestionResult:
    question: str
    category: str
    gold: List[str]
    ranked_keys: List[str]
    hit: Dict[int, int] = field(default_factory=dict)
    recall: Dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    ndcg: Dict[int, float] = field(default_factory=dict)
    # Trace-only fields (populated when score_question is called with
    # capture_trace=True). Not used by the metric aggregator.
    ranked_scores: List[float] = field(default_factory=list)
    timings_ms: Dict[str, float] = field(default_factory=dict)
    qid: str = ""


# ---------------------------------------------------------------------------
# Ingest helpers
# ---------------------------------------------------------------------------

def format_turn(turn: Turn) -> str:
    """Render a Turn into the canonical ingest string with embedded gold key.

    Format: ``"[<speaker> @ <timestamp>] <text> [key=<key>]"``. The ``[key=...]``
    suffix is what ``key_for_result`` parses back out post-FTS5 roundtrip.
    """
    prefix = f"[{turn.speaker}"
    if turn.timestamp:
        prefix += f" @ {turn.timestamp}"
    prefix += "]"
    return f"{prefix} {turn.text} [key={turn.key}]"


def ingest_conversation_into_brain(
    brain,
    turns: Iterable[Turn],
    *,
    category: str = "observation",
) -> int:
    """Insert each turn as one memory and return the number written.

    Empty-key turns are skipped (LOCOMO has a few; we'd never be able to
    resolve them back to a gold answer).
    """
    n = 0
    for turn in turns:
        if not turn.key:
            continue
        brain.remember(format_turn(turn), category=category)
        n += 1
    return n


def brain_search_fn(brain) -> SearchFn:
    """Wrap ``Brain.search`` into the ``SearchFn`` shape."""
    def _fn(query: str, k: int) -> List[Dict[str, Any]]:
        return brain.search(query, limit=k)
    _fn.errors = {}  # type: ignore[attr-defined]
    return _fn


def key_for_result(result: Dict[str, Any]) -> str:
    """Extract the embedded gold key from a search result, or "" on miss."""
    text = (
        result.get("content")
        or result.get("summary")
        or result.get("name")
        or ""
    )
    m = KEY_RE.search(text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Per-question scoring
# ---------------------------------------------------------------------------

def score_question(
    q: Question,
    search_fn: SearchFn,
    *,
    k_max: int = 20,
    ks: Sequence[int] = DEFAULT_KS,
    capture_trace: bool = False,
) -> QuestionResult:
    """Run one question through ``search_fn`` and compute Hit/Recall/MRR/nDCG.

    When ``capture_trace=True``, additionally populate ``ranked_scores``
    and ``timings_ms`` on the result so the caller can emit per-query
    traces. Metric computation is unchanged.
    """
    t_query_start = time.perf_counter()
    raw_results = search_fn(q.question, k_max) if q.question else []
    t_query_ms = (time.perf_counter() - t_query_start) * 1000.0

    # Keep raw results aligned with keys so ranked_scores[i] <-> ranked[i].
    ranked_pairs: List[Tuple[str, float]] = []
    for r in raw_results:
        k = key_for_result(r)
        if not k:
            continue
        score = 0.0
        for field_name in ("final_score", "score", "rrf_score", "rank_score"):
            v = r.get(field_name) if isinstance(r, dict) else None
            if v is not None:
                try:
                    score = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        ranked_pairs.append((k, score))
    ranked = [k for k, _ in ranked_pairs]

    # Build a binary relevance map for ndcg_at_k (1 = gold, 0 = other).
    relevance: Dict[str, int] = {g: 1 for g in q.gold_keys}
    gold_set = set(q.gold_keys)

    qr = QuestionResult(
        question=q.question,
        category=q.category,
        gold=list(q.gold_keys),
        ranked_keys=ranked,
    )
    for K in ks:
        window = ranked[:K]
        inter = gold_set.intersection(window)
        qr.hit[K] = 1 if inter else 0
        qr.recall[K] = (len(inter) / len(gold_set)) if gold_set else 0.0
        qr.ndcg[K] = round(ndcg_at_k(ranked, relevance, K), 6) if relevance else 0.0
    qr.mrr = _mrr_pure(ranked, relevance)
    if capture_trace:
        qr.ranked_scores = [s for _, s in ranked_pairs]
        qr.timings_ms = {"query_ms": round(t_query_ms, 3)}
    return qr


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return round(statistics.mean(xs), 4) if xs else 0.0


def aggregate_results(
    rows: List[QuestionResult],
    *,
    ks: Sequence[int] = DEFAULT_KS,
) -> Dict[str, Any]:
    """Compute overall + per-category metric means for a list of QuestionResult.

    The output shape matches what ``tests/bench/run.py`` expects (overall
    + by_category sub-dict) so the same baseline-comparison code can gate
    every benchmark.
    """
    overall: Dict[str, Any] = {
        "n_questions": len(rows),
        "mrr": _mean(r.mrr for r in rows),
    }
    for K in ks:
        overall[f"hit_at_{K}"] = _mean(r.hit[K] for r in rows)
        overall[f"recall_at_{K}"] = _mean(r.recall[K] for r in rows)
        overall[f"ndcg_at_{K}"] = _mean(r.ndcg[K] for r in rows)

    by_category: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = by_category.setdefault(row.category or "_uncat", {
            "count": 0,
            "_hit": {K: [] for K in ks},
            "_recall": {K: [] for K in ks},
            "_ndcg": {K: [] for K in ks},
            "_mrr": [],
        })
        bucket["count"] += 1
        for K in ks:
            bucket["_hit"][K].append(row.hit[K])
            bucket["_recall"][K].append(row.recall[K])
            bucket["_ndcg"][K].append(row.ndcg[K])
        bucket["_mrr"].append(row.mrr)
    for cat, b in by_category.items():
        cell: Dict[str, Any] = {"count": b["count"], "mrr": _mean(b["_mrr"])}
        for K in ks:
            cell[f"hit_at_{K}"] = _mean(b["_hit"][K])
            cell[f"recall_at_{K}"] = _mean(b["_recall"][K])
            cell[f"ndcg_at_{K}"] = _mean(b["_ndcg"][K])
        by_category[cat] = cell

    return {"overall": overall, "by_category": by_category}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def eval_questions(
    search_fn: SearchFn,
    questions: Iterable[Question],
    *,
    top_k: int = 5,
    ks: Sequence[int] = DEFAULT_KS,
    return_rows: bool = False,
) -> Dict[str, Any]:
    """Run every question through ``search_fn`` and return aggregated metrics.

    Args:
        search_fn: A ``Callable[[query, k], list[dict]]`` — typically
            ``brain_search_fn(brain)`` or the ``cmd_search`` wrapper.
        questions: Iterable of ``Question`` objects with gold IDs.
        top_k: The window the headline ``hit_at`` / ``recall_at`` numbers
            report on. Stored on the result for downstream display only;
            ``ks`` controls the actual K values measured.
        ks: Tuple of K values to compute Hit@K, Recall@K, nDCG@K at.
            Must include ``top_k`` for the result to be self-consistent.
        return_rows: When True, include per-question rows in the output
            (useful for debugging / per-row CSV export). Off by default
            so JSON baselines stay small.

    Returns a dict with:
        - "overall": headline metrics
        - "by_category": per-axis metrics
        - "elapsed_s": wall time
        - "n_questions": number of questions scored
        - "rows": present only if return_rows=True
    """
    if top_k not in ks:
        ks = tuple(sorted(set(ks) | {top_k}))

    t0 = time.perf_counter()
    rows = [score_question(q, search_fn, k_max=max(ks), ks=ks) for q in questions]
    elapsed = time.perf_counter() - t0

    agg = aggregate_results(rows, ks=ks)
    agg["elapsed_s"] = round(elapsed, 2)
    agg["n_questions"] = len(rows)
    agg["top_k"] = top_k
    agg["ks"] = list(ks)
    if return_rows:
        agg["rows"] = [
            {
                "question": r.question,
                "category": r.category,
                "gold": r.gold,
                "ranked_keys": r.ranked_keys[:max(ks)],
                "mrr": r.mrr,
                **{f"hit_at_{K}": r.hit[K] for K in ks},
                **{f"recall_at_{K}": r.recall[K] for K in ks},
                **{f"ndcg_at_{K}": r.ndcg[K] for K in ks},
            }
            for r in rows
        ]
    return agg


__all__ = [
    "DEFAULT_KS",
    "Question",
    "QuestionResult",
    "SearchFn",
    "Turn",
    "aggregate_results",
    "brain_search_fn",
    "eval_questions",
    "format_turn",
    "ingest_conversation_into_brain",
    "key_for_result",
    "score_question",
]
