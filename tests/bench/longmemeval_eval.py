"""LongMemEval (longmemeval_s split) retrieval-only benchmark.

LongMemEval is purpose-built for long-term agent memory. We use the
``longmemeval_s_cleaned.json`` split (~277 MB) because it has the full
distractor haystack — typically 50-500 sessions per entry of which only
a few are gold ``answer_session_ids``. The smaller ``oracle`` split is
unsuitable for retrieval scoring (its haystack == gold for every entry,
so retrieval is vacuously perfect — see loader docstring).

Per-axis breakdown is what the LongMemEval paper reports, so we keep
``question_type`` as the primary category dimension. Headline numbers
are computed across the four "retrieval-friendly" axes (defined in
``tests.bench.datasets.longmemeval_loader``); the two LLM-judge-only axes
(``temporal-reasoning`` and ``knowledge-update``) can still be measured
for *retrieval* quality on demand by passing ``include_judge_only=True``.

Each LongMemEval entry has many sessions; each session is a list of
``{role, content}`` turns. We treat each *session* as one Brain memory
keyed by its ``haystack_session_id`` — the gold answer is the session ID,
not an individual turn ID, so coarsening to session-level matches the
benchmark's evaluation contract exactly.
"""

from __future__ import annotations

import gc
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from tests.bench.datasets.longmemeval_loader import (  # noqa: E402
    JUDGE_ONLY_TYPES,
    RETRIEVAL_FRIENDLY_TYPES,
    load as load_longmemeval,
)
from tests.bench.external_runner import (  # noqa: E402
    DEFAULT_KS,
    Question,
    Turn,
    aggregate_results,
    brain_search_fn,
    eval_questions,
    ingest_conversation_into_brain,
    score_question,
)


BASELINE_KS: Sequence[int] = DEFAULT_KS  # (1, 5, 10, 20)


# ---------------------------------------------------------------------------
# Entry -> Turn / Question mapping
# ---------------------------------------------------------------------------

def _session_to_text(session_turns: List[Dict[str, Any]]) -> str:
    """Flatten a session's turn list into a single searchable string.

    LongMemEval scores at session granularity, so we ingest one memory per
    session. Joining role-tagged content keeps Brain.search tokenisation
    stable while still letting it match per-turn keywords.
    """
    parts = []
    for turn in session_turns:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if not content:
            continue
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def entry_to_turns(entry: Dict[str, Any]) -> List[Turn]:
    """Yield one Turn per session for a LongMemEval entry."""
    sids = entry.get("haystack_session_ids") or []
    sessions = entry.get("haystack_sessions") or []
    dates = entry.get("haystack_dates") or [""] * len(sids)
    turns: List[Turn] = []
    for sid, sess, date in zip(sids, sessions, dates):
        text = _session_to_text(sess)
        if not text:
            continue
        turns.append(Turn(
            key=str(sid),
            speaker="session",
            text=text,
            timestamp=str(date),
        ))
    return turns


def entry_to_question(entry: Dict[str, Any]) -> Question:
    """Convert one LongMemEval entry into a Question with gold session IDs."""
    return Question(
        question=entry.get("question", ""),
        gold_keys=[str(s) for s in (entry.get("answer_session_ids") or [])],
        category=entry.get("question_type", "_unknown"),
        metadata={
            "question_id": entry.get("question_id"),
            "answer": entry.get("answer"),
            "question_date": entry.get("question_date"),
        },
    )


# ---------------------------------------------------------------------------
# Backend wiring (Brain.search default)
# ---------------------------------------------------------------------------

def _build_brain_for(db_path: Path, agent_id: str):
    from agentmemory.brain import Brain
    return Brain(db_path=str(db_path), agent_id=agent_id)


# ---------------------------------------------------------------------------
# Per-entry runner
# ---------------------------------------------------------------------------

def run_entry(
    entry: Dict[str, Any],
    *,
    backend: str = "brain",
    ks: Sequence[int] = BASELINE_KS,
    capture_trace: bool = False,
) -> Dict[str, Any]:
    """Score one LongMemEval entry. Returns single-question metric row."""
    if backend != "brain":
        # cmd_search backend can be added later by mirroring locomo_eval's
        # _build_cmd_search_fn — left out of v1 to keep runtime sane.
        # (LongMemEval has ~500 oracle entries; cmd_search would 5-10x
        # the wall time. Easy to add when needed.)
        raise ValueError(
            f"backend={backend!r} not yet supported for LongMemEval. "
            "Use 'brain' for now."
        )
    question = entry_to_question(entry)
    if not question.gold_keys:
        return {}                          # not scoreable

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "longmemeval.db"
        agent_id = f"longmemeval-{entry.get('question_id', 'q')}"
        brain = _build_brain_for(db_path, agent_id=agent_id)

        t0 = time.perf_counter()
        n_sessions = ingest_conversation_into_brain(brain, entry_to_turns(entry))
        t_ingest = time.perf_counter() - t0

        search_fn = brain_search_fn(brain)
        t0 = time.perf_counter()
        row = score_question(
            question, search_fn, k_max=max(ks), ks=ks,
            capture_trace=capture_trace,
        )
        t_query = time.perf_counter() - t0

    # Annotate qid / ingest timing for trace export.
    qid = str(entry.get("question_id") or "")
    row.qid = qid
    if capture_trace:
        row.timings_ms.setdefault("ingest_ms", round(t_ingest * 1000.0, 3))
        row.timings_ms.setdefault("total_ms", round((t_ingest + t_query) * 1000.0, 3))

    return {
        "question_id": entry.get("question_id"),
        "category": question.category,
        "n_sessions": n_sessions,
        "t_ingest_s": round(t_ingest, 4),
        "t_query_s": round(t_query, 4),
        "mrr": row.mrr,
        **{f"hit_at_{K}": row.hit[K] for K in ks},
        **{f"recall_at_{K}": row.recall[K] for K in ks},
        **{f"ndcg_at_{K}": row.ndcg[K] for K in ks},
        "_question_result": row,
    }


# ---------------------------------------------------------------------------
# Multi-entry aggregation
# ---------------------------------------------------------------------------

def _aggregate_rows(
    rows: List[Dict[str, Any]],
    *,
    ks: Sequence[int] = BASELINE_KS,
) -> Dict[str, Any]:
    """Aggregate from per-entry rows into overall + per-axis metrics."""
    qrs = [r["_question_result"] for r in rows if r.get("_question_result") is not None]
    return aggregate_results(qrs, ks=ks)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _stratified_subset(
    entries: List[Dict[str, Any]],
    limit: int,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    """Return a stratified subset of ``entries`` with proportional axis coverage.

    The dataset ships sorted by ``question_type`` — naive ``entries[:50]``
    only sees one axis. Round-robin across the per-axis buckets so the
    smoke / CI default actually represents every retrieval-friendly axis.
    """
    import collections
    import random as _r
    buckets: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for e in entries:
        buckets[e.get("question_type", "_unknown")].append(e)
    rng = _r.Random(seed)
    for k in buckets:
        rng.shuffle(buckets[k])
    out: List[Dict[str, Any]] = []
    iters = {k: iter(v) for k, v in buckets.items()}
    while len(out) < limit and iters:
        for k in list(iters.keys()):
            try:
                out.append(next(iters[k]))
            except StopIteration:
                iters.pop(k, None)
                continue
            if len(out) >= limit:
                break
    return out


def run(
    *,
    backend: str = "brain",
    limit: Optional[int] = None,
    include_judge_only: bool = False,
    ks: Sequence[int] = BASELINE_KS,
    allow_download: Optional[bool] = None,
    stratify: bool = True,
    traces_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Full LongMemEval (_s split) eval. Returns the aggregated summary.

    Args:
        backend: ``"brain"`` (only supported value today).
        limit: If set, score only N entries — useful for smoke tests.
        include_judge_only: Include the two LLM-judge-only axes
            (``temporal-reasoning`` / ``knowledge-update``) in the
            headline overall numbers. Off by default; we still log
            their retrieval metrics in ``by_category`` either way.
        ks: K values to score at.
        allow_download: When False, skip the network fallback and require
            the cached file. Defaults to env-controlled.
        stratify: When ``limit`` is set and ``True`` (default), draw a
            stratified subset across question_type axes (round-robin)
            instead of taking the first N. The dataset ships sorted by
            type, so naive slicing only ever exercises one axis.
    """
    entries = load_longmemeval(
        allow_download=allow_download,
        include_judge_only=include_judge_only,
    )
    if limit is not None:
        if stratify:
            entries = _stratified_subset(entries, limit)
        else:
            entries = entries[:limit]

    t0 = time.perf_counter()
    per_entry: List[Dict[str, Any]] = []
    capture = traces_path is not None
    trace_fh = None
    if capture:
        traces_path = Path(traces_path)
        traces_path.parent.mkdir(parents=True, exist_ok=True)
        trace_fh = traces_path.open("w")
    try:
        import json as _json  # local alias to avoid top-level churn
        for e in entries:
            row = run_entry(e, backend=backend, ks=ks, capture_trace=capture)
            if row:
                per_entry.append(row)
                if capture and trace_fh is not None:
                    qr = row.get("_question_result")
                    trace_record = {
                        "qid": qr.qid,
                        "query": qr.question,
                        "retrieved_ids": qr.ranked_keys,
                        "scores": qr.ranked_scores,
                        "gold_ids": qr.gold,
                        "hit_at_k": {str(K): qr.hit[K] for K in ks},
                        "recall_at_k": {str(K): qr.recall[K] for K in ks},
                        "ndcg_at_k": {str(K): qr.ndcg[K] for K in ks},
                        "mrr_contribution": qr.mrr,
                        "category": qr.category,
                        "timings_ms": qr.timings_ms,
                    }
                    trace_fh.write(_json.dumps(trace_record) + "\n")
                    trace_fh.flush()
    finally:
        if trace_fh is not None:
            trace_fh.close()
    elapsed = time.perf_counter() - t0

    agg = _aggregate_rows(per_entry, ks=ks)
    agg["elapsed_s"] = round(elapsed, 2)
    agg["backend"] = backend
    agg["ks"] = list(ks)
    agg["n_entries"] = len(per_entry)
    agg["include_judge_only"] = include_judge_only
    agg["retrieval_friendly_types"] = list(RETRIEVAL_FRIENDLY_TYPES)
    agg["judge_only_types"] = list(JUDGE_ONLY_TYPES)

    # Strip the QuestionResult object refs from the row dump before
    # serialising to a baseline JSON.
    agg["per_entry"] = [{k: v for k, v in r.items() if k != "_question_result"}
                         for r in per_entry]
    return agg
