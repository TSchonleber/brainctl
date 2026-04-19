"""LOCOMO benchmark eval — drop-in for the run.py / baselines pattern.

Reuses:
  * ``tests.bench.datasets.locomo_loader.load`` for the dataset
  * ``tests.bench.external_runner`` for ingest + scoring primitives
  * ``tests.bench.eval._build_cmd_search_fn`` for the cmd_search backend
    (the existing search-quality bench already pinned the right
    initialisation dance for cmd_search against an arbitrary DB path)

The legacy runner at ``tests/bench/locomo/runner.py`` stays around because
its CLI flags (``--convo``, ``--backend``, etc.) are documented in
``tests/bench/locomo/README.md`` and people use them. This module is the
new, baseline-comparable entry point exercised by ``tests/bench/run.py``
under ``--bench locomo``.

Categories (LOCOMO paper inference):
  1 single-hop   2 temporal    3 multi-hop
  4 open-domain  5 adversarial
"""

from __future__ import annotations

import gc
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# Ensure the brainctl package is importable when this module runs as a
# script under tests/bench/run.py (no editable install required).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from tests.bench.datasets.locomo_loader import load as load_locomo  # noqa: E402
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


CATEGORY_LABELS = {
    1: "single-hop",
    2: "temporal",
    3: "multi-hop",
    4: "open-domain",
    5: "adversarial",
}

# K values reported in the baseline JSON. Includes the spec-required 1, 5,
# plus the wider windows the historical results.json file uses.
BASELINE_KS: Sequence[int] = DEFAULT_KS  # (1, 5, 10, 20)


# ---------------------------------------------------------------------------
# Conversation -> Turn / Question mapping
# ---------------------------------------------------------------------------

_SESSION_KEY_RE = re.compile(r"session_\d+")


def conversation_to_turns(convo: Dict[str, Any]) -> List[Turn]:
    """Flatten one LOCOMO conversation into a list of Turn rows."""
    conv = convo["conversation"]
    session_keys = sorted(
        (k for k in conv if _SESSION_KEY_RE.fullmatch(k)),
        key=lambda s: int(s.split("_")[1]),
    )
    turns: List[Turn] = []
    for sk in session_keys:
        date = conv.get(f"{sk}_date_time", "")
        for raw in conv[sk]:
            dia_id = raw.get("dia_id", "")
            if not dia_id:
                continue
            turns.append(Turn(
                key=dia_id,
                speaker=raw.get("speaker") or "",
                text=raw.get("text") or "",
                timestamp=date,
            ))
    return turns


def conversation_to_questions(convo: Dict[str, Any]) -> List[Question]:
    """Pull the QA pairs out of a conversation, normalised to ``Question``."""
    out: List[Question] = []
    for raw in convo.get("qa", []):
        gold = list(raw.get("evidence") or [])
        if not gold:
            continue                        # no evidence -> can't score retrieval
        cat_int = int(raw.get("category", 0))
        out.append(Question(
            question=raw.get("question", ""),
            gold_keys=gold,
            category=CATEGORY_LABELS.get(cat_int, f"cat-{cat_int}"),
            metadata={"answer": raw.get("answer"), "category_int": cat_int},
        ))
    return out


# ---------------------------------------------------------------------------
# Backends — Brain.search (default) and cmd_search (full hybrid pipeline)
# ---------------------------------------------------------------------------

def _build_brain_for(db_path: Path, agent_id: str):
    from agentmemory.brain import Brain
    return Brain(db_path=str(db_path), agent_id=agent_id)


def _build_cmd_search_fn(db_path: Path):
    """Wrap the full ``cmd_search`` hybrid pipeline against an existing DB.

    Mirrors the closure in ``tests/bench/locomo/runner.py`` so the new
    eval emits the same numbers as the legacy CLI runner. The dance is:
    pin ``_impl.DB_PATH``, capture json output by patching the module
    attrs, and disable recency reranking (LOCOMO's synthetic uniform
    timestamps make recency actively destructive — the "benchmark mode"
    note in the README documents this).
    """
    import contextlib
    import io
    import types
    import agentmemory._impl as _impl
    _impl.DB_PATH = db_path

    def search_fn(query: str, k: int):
        captured: list = []

        def _capture(data, compact=False):
            captured.append(data)

        args = types.SimpleNamespace(
            query=query, limit=k,
            tables="memories,events,context",
            no_recency=True, no_graph=True,
            budget=None, min_salience=None,
            mmr=False, mmr_lambda=0.7, explore=False,
            profile=None, pagerank_boost=0.0,
            quantum=False, benchmark=False,
            agent="bench-agent", format="json",
            oneline=False, verbose=False,
        )
        saved_json = _impl.json_out
        saved_oneline = _impl.oneline_out
        _impl.json_out = _capture
        _impl.oneline_out = _capture
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    _impl.cmd_search(args)
                except Exception as exc:
                    name = type(exc).__name__
                    search_fn.errors[name] = search_fn.errors.get(name, 0) + 1
                    return []
        finally:
            _impl.json_out = saved_json
            _impl.oneline_out = saved_oneline
            gc.collect()

        if not captured:
            return []
        payload = captured[0] if isinstance(captured[0], dict) else {}
        flat: List[Dict[str, Any]] = []
        for bucket in ("memories", "events", "context", "entities", "decisions"):
            flat.extend(payload.get(bucket, []) or [])
        flat.sort(key=lambda r: r.get("final_score", 0.0), reverse=True)
        return flat[:k]

    search_fn.errors = {}  # type: ignore[attr-defined]
    return search_fn


# ---------------------------------------------------------------------------
# Per-conversation runner
# ---------------------------------------------------------------------------

def run_conversation(
    convo: Dict[str, Any],
    *,
    backend: str = "brain",
    ks: Sequence[int] = BASELINE_KS,
    capture_trace: bool = False,
) -> Dict[str, Any]:
    """Spin up a fresh tmp brain.db, ingest one convo, score its QA set."""
    sample_id = convo.get("sample_id", "?")
    questions = conversation_to_questions(convo)

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "locomo.db"
        agent_id = f"locomo-{sample_id}"
        brain = _build_brain_for(db_path, agent_id=agent_id)

        t0 = time.perf_counter()
        n_turns = ingest_conversation_into_brain(brain, conversation_to_turns(convo))
        t_ingest = time.perf_counter() - t0

        if backend == "cmd":
            # Release the writer connection so cmd_search can open its own
            # without WAL contention. Same dance as legacy runner.
            try:
                conn = brain._get_conn()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                brain.close()
            except Exception:
                pass
            gc.collect()
            search_fn = _build_cmd_search_fn(db_path)
        else:
            search_fn = brain_search_fn(brain)

        t0 = time.perf_counter()
        rows = []
        for idx, q in enumerate(questions):
            qr = score_question(
                q, search_fn, k_max=max(ks), ks=ks,
                capture_trace=capture_trace,
            )
            if capture_trace:
                qr.qid = f"{sample_id}:q{idx}"
            rows.append(qr)
        t_query = time.perf_counter() - t0
        errs = getattr(search_fn, "errors", {})

    # Per-convo summary in the same field naming as overall (hit_at_5 etc.)
    out = aggregate_results(rows, ks=ks)
    out["sample_id"] = sample_id
    out["n_turns"] = n_turns
    out["n_questions"] = len(rows)
    out["t_ingest_s"] = round(t_ingest, 2)
    out["t_query_s"] = round(t_query, 2)
    if errs:
        out["search_errors"] = dict(errs)
    if capture_trace:
        out["_question_results"] = rows
    return out


# ---------------------------------------------------------------------------
# Multi-conversation aggregation
# ---------------------------------------------------------------------------

def aggregate_per_convo(
    per_convo: List[Dict[str, Any]],
    *,
    ks: Sequence[int] = BASELINE_KS,
) -> Dict[str, Any]:
    """Weighted-by-QA aggregation across all conversations.

    We re-aggregate from the per-convo overall+by_category cells (same
    approach as the legacy runner) so each question contributes equally.
    """
    if not per_convo:
        return {"overall": {}, "by_category": {}}

    weights = [c.get("n_questions", 0) for c in per_convo]
    total = sum(weights) or 1

    def wmean(field: str) -> float:
        return round(
            sum(c["overall"].get(field, 0.0) * c["n_questions"] for c in per_convo) / total,
            4,
        )

    overall: Dict[str, Any] = {
        "n_questions": total,
        "n_convos": len(per_convo),
        "n_turns_total": sum(c.get("n_turns", 0) for c in per_convo),
        "mrr": wmean("mrr"),
    }
    for K in ks:
        overall[f"hit_at_{K}"] = wmean(f"hit_at_{K}")
        overall[f"recall_at_{K}"] = wmean(f"recall_at_{K}")
        overall[f"ndcg_at_{K}"] = wmean(f"ndcg_at_{K}")

    cat_acc: Dict[str, Dict[str, Any]] = {}
    for c in per_convo:
        for cat, row in c.get("by_category", {}).items():
            n = row["count"]
            acc = cat_acc.setdefault(cat, {"count": 0, "_mrr": 0.0,
                                            **{f"_hit_{K}": 0.0 for K in ks},
                                            **{f"_recall_{K}": 0.0 for K in ks},
                                            **{f"_ndcg_{K}": 0.0 for K in ks}})
            acc["count"] += n
            acc["_mrr"] += row.get("mrr", 0.0) * n
            for K in ks:
                acc[f"_hit_{K}"] += row.get(f"hit_at_{K}", 0.0) * n
                acc[f"_recall_{K}"] += row.get(f"recall_at_{K}", 0.0) * n
                acc[f"_ndcg_{K}"] += row.get(f"ndcg_at_{K}", 0.0) * n
    by_category: Dict[str, Dict[str, Any]] = {}
    for cat, acc in cat_acc.items():
        denom = acc["count"] or 1
        cell: Dict[str, Any] = {"count": acc["count"], "mrr": round(acc["_mrr"] / denom, 4)}
        for K in ks:
            cell[f"hit_at_{K}"] = round(acc[f"_hit_{K}"] / denom, 4)
            cell[f"recall_at_{K}"] = round(acc[f"_recall_{K}"] / denom, 4)
            cell[f"ndcg_at_{K}"] = round(acc[f"_ndcg_{K}"] / denom, 4)
        by_category[cat] = cell

    return {"overall": overall, "by_category": by_category}


# ---------------------------------------------------------------------------
# Public entry point — used by tests/bench/run.py and tests/test_locomo_bench.py
# ---------------------------------------------------------------------------

def run(
    *,
    backend: str = "brain",
    convo_idx: Optional[int] = None,
    ks: Sequence[int] = BASELINE_KS,
    allow_download: Optional[bool] = None,
    traces_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Full LOCOMO eval. Returns the aggregated summary.

    Args:
        backend: ``"brain"`` (FTS5-only Brain.search, fast & deterministic)
            or ``"cmd"`` (full hybrid cmd_search pipeline; what production
            traffic and MCP memory_search hit).
        convo_idx: If set, run only conversation index N. Useful for
            smoke tests. Defaults to running all 10.
        ks: K values to score at. Default ``(1, 5, 10, 20)``.
        allow_download: When False, skip the network fallback and require
            the in-tree LOCOMO copy. Defaults to env-controlled.
    """
    data = load_locomo(allow_download=allow_download)
    if convo_idx is not None:
        if not 0 <= convo_idx < len(data):
            raise IndexError(f"--convo must be in 0..{len(data) - 1}")
        convos = [data[convo_idx]]
    else:
        convos = data

    capture = traces_path is not None
    t0 = time.perf_counter()
    per_convo = [
        run_conversation(c, backend=backend, ks=ks, capture_trace=capture)
        for c in convos
    ]
    elapsed = time.perf_counter() - t0

    if capture:
        import json as _json
        tp = Path(traces_path)
        tp.parent.mkdir(parents=True, exist_ok=True)
        with tp.open("w") as fh:
            for c in per_convo:
                for qr in c.get("_question_results", []):
                    rec = {
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
                    fh.write(_json.dumps(rec) + "\n")

    agg = aggregate_per_convo(per_convo, ks=ks)
    agg["elapsed_s"] = round(elapsed, 2)
    agg["backend"] = backend
    agg["ks"] = list(ks)
    # Drop QuestionResult refs + verbose per-category cells from per_convo.
    agg["per_convo"] = [
        {k: v for k, v in c.items()
         if k not in ("by_category", "_question_results")}
        for c in per_convo
    ]
    return agg
