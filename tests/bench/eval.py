"""Metric + runner core for the search-quality benchmark.

Pure functions first (p_at_k, recall_at_k, mrr, ndcg_at_k), then a runner
that seeds a temp DB from `fixtures`, routes each Query through
`Brain.search`, and aggregates metrics both overall and per query-category.

This deliberately uses the high-level `Brain.search` rather than the full
`_impl.cmd_search` CLI machinery. The Brain path is what MCP `memory_search`
ultimately wraps, and it's stable across refactors. When we later want to
cover the full CLI (intent routing, graph expansion, quantum reranker), we
add a second runner that shells out to `brainctl search` — the metric code
is shared.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Make the repo importable when run as a script.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from tests.bench.fixtures import (  # noqa: E402
    ENTITIES, EVENTS, MEMORIES, PROCEDURES, QUERIES, Query, key_for_result,
)


# ---------------------------------------------------------------------------
# Metric primitives (pure, no DB)
# ---------------------------------------------------------------------------

def p_at_k(ranked_keys: List[str], relevance: Dict[str, int], k: int) -> float:
    """Fraction of the top-k results that have any non-zero relevance."""
    if k <= 0:
        return 0.0
    window = ranked_keys[:k]
    if not window:
        return 0.0
    hits = sum(1 for key in window if relevance.get(key, 0) > 0)
    return hits / k


def relevant_count(relevance: Dict[str, int]) -> int:
    """Count how many fixture items are relevant for the query."""
    return sum(1 for grade in relevance.values() if grade > 0)


def p_at_k_ceiling(relevance: Dict[str, int], k: int) -> float:
    """Maximum attainable P@k for this query's relevance cardinality.

    This benchmark is sparse by design: many queries have only 1-3 relevant
    targets, so raw precision@5 cannot approach 1.0 even under a perfect
    ranking. The ceiling is therefore min(num_relevant, k) / k.

    Returns 0.0 for empty relevance sets so aggregate ceilings stay directly
    comparable to the raw macro-averaged P@k.
    """
    if k <= 0:
        return 0.0
    return min(relevant_count(relevance), k) / k


def recall_at_k(ranked_keys: List[str], relevance: Dict[str, int], k: int) -> float:
    """Of all relevant items in the fixture, how many appeared in top-k."""
    total_relevant = relevant_count(relevance)
    if total_relevant == 0:
        return 1.0  # vacuous: no relevant items => perfect recall by convention
    window = ranked_keys[:k]
    hits = sum(1 for key in window if relevance.get(key, 0) > 0)
    return hits / total_relevant


def mrr(ranked_keys: List[str], relevance: Dict[str, int]) -> float:
    """Mean reciprocal rank of the first relevant item."""
    for i, key in enumerate(ranked_keys, start=1):
        if relevance.get(key, 0) > 0:
            return 1.0 / i
    return 0.0


def dcg_at_k(ranked_keys: List[str], relevance: Dict[str, int], k: int) -> float:
    """Discounted cumulative gain — graded relevance with log2 discount."""
    total = 0.0
    for i, key in enumerate(ranked_keys[:k], start=1):
        grade = relevance.get(key, 0)
        if grade > 0:
            total += (2 ** grade - 1) / math.log2(i + 1)
    return total


def ndcg_at_k(ranked_keys: List[str], relevance: Dict[str, int], k: int) -> float:
    """Normalized DCG@k. Returns 1.0 for empty relevance sets."""
    if not relevance:
        return 1.0
    actual = dcg_at_k(ranked_keys, relevance, k)
    ideal_order = sorted(relevance.items(), key=lambda kv: kv[1], reverse=True)
    ideal_keys = [key for key, _ in ideal_order]
    ideal = dcg_at_k(ideal_keys, relevance, k)
    if ideal == 0:
        return 1.0
    return actual / ideal


# ---------------------------------------------------------------------------
# DB seeding
# ---------------------------------------------------------------------------

def _tag_key(text: str, key: str) -> str:
    """Append a stable fixture key marker to content so we can resolve
    results back to their source even after FTS5 roundtrips."""
    return f"{text} [key=mem:{key}]" if key else text


def _tag_event(text: str, key: str) -> str:
    return f"{text} [key=evt:{key}]" if key else text


def seed_brain(brain) -> None:
    """Insert all fixtures into the given Brain instance."""
    for mem in MEMORIES:
        brain.remember(
            _tag_key(mem.content, mem.key),
            category=mem.category,
            confidence=mem.confidence,
        )
    for evt in EVENTS:
        brain.log(
            _tag_event(evt.summary, evt.key),
            event_type=evt.event_type,
            project=evt.project,
            importance=evt.importance,
        )
    for ent in ENTITIES:
        brain.entity(
            ent.name, ent.entity_type,
            observations=ent.observations,
        )
    for proc in PROCEDURES:
        brain.remember_procedure(
            goal=proc.goal,
            title=proc.title,
            description=proc.description,
            steps=proc.steps,
            procedure_kind=proc.procedure_kind,
            scope=proc.scope,
            status=proc.status,
            tools_json=proc.tools,
            failure_modes_json=proc.failure_modes,
            rollback_steps_json=proc.rollback_steps,
            success_criteria_json=proc.success_criteria,
            execution_count=proc.execution_count,
            success_count=proc.success_count,
            failure_count=proc.failure_count,
            stale_after_days=proc.stale_after_days,
        )


def seed_db_direct(db_path: Path, agent_id: str = "bench-agent") -> None:
    """Initialise a fresh brain.db and seed it via direct SQL.

    Avoids the Brain class entirely so the bench can hand over the DB to
    `cmd_search` without any lingering writer connection holding a WAL lock.
    Schema init comes from the packaged init_schema.sql the same way
    Brain._init_db does — this is the "bench variant" of that path.
    """
    import sqlite3
    import json as _json
    from datetime import datetime, timezone

    init_sql = Path(__file__).resolve().parent.parent.parent / "src" / "agentmemory" / "db" / "init_schema.sql"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(init_sql.read_text())
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES (?, ?, 'bench', 'active', ?, ?)",
            (agent_id, agent_id, now, now),
        )
        # Minimal workspace/neuro defaults the real CLI expects in cmd_search.
        conn.execute(
            "INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('enabled', '0')"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO neuromodulation_state (
                id, org_state, dopamine_signal, arousal_level,
                confidence_boost_rate, confidence_decay_rate, retrieval_breadth_multiplier,
                focus_level, temporal_lambda, context_window_depth
            ) VALUES (1, 'normal', 0.0, 0.3, 0.1, 0.02, 1.0, 0.3, 0.03, 50)
            """
        )

        for mem in MEMORIES:
            conn.execute(
                "INSERT INTO memories (agent_id, category, content, confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, mem.category, _tag_key(mem.content, mem.key), mem.confidence, now, now),
            )
        for evt in EVENTS:
            conn.execute(
                "INSERT INTO events (agent_id, event_type, summary, project, importance, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, evt.event_type, _tag_event(evt.summary, evt.key),
                 evt.project, evt.importance, now),
            )
        for ent in ENTITIES:
            conn.execute(
                "INSERT INTO entities (name, entity_type, properties, observations, agent_id, created_at, updated_at) "
                "VALUES (?, ?, '{}', ?, ?, ?, ?)",
                (ent.name, ent.entity_type, _json.dumps(ent.observations), agent_id, now, now),
            )
        from agentmemory import procedural as _procedural

        for proc in PROCEDURES:
            _procedural.create_procedure(
                conn,
                agent_id=agent_id,
                payload={
                    "title": proc.title,
                    "goal": proc.goal,
                    "description": proc.description,
                    "procedure_kind": proc.procedure_kind,
                    "steps_json": [{"action": step} for step in proc.steps],
                    "tools_json": proc.tools,
                    "failure_modes_json": proc.failure_modes,
                    "rollback_steps_json": proc.rollback_steps,
                    "success_criteria_json": proc.success_criteria,
                    "status": proc.status,
                    "execution_count": proc.execution_count,
                    "success_count": proc.success_count,
                    "failure_count": proc.failure_count,
                    "stale_after_days": proc.stale_after_days,
                },
                category="convention",
                scope=proc.scope,
                confidence=0.92,
            )
        conn.commit()
        # Force WAL checkpoint so no *-wal / *-shm file lingers to block
        # subsequent connections. Critical for the benchmark runner — its
        # tight loop of cmd_search calls otherwise races the WAL.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query routing — search callable plugs in here so we can swap Brain.search
# for a CLI-based runner later without touching the metric code.
# ---------------------------------------------------------------------------

SearchFn = Callable[[str, int], List[Dict[str, Any]]]


def _classify_failure_mode(
    query: Query,
    ranked_keys: List[str],
    payload: Dict[str, Any],
) -> str:
    if not query.relevance:
        return "correct_abstain" if not ranked_keys else "hallucination"
    if any(query.relevance.get(key, 0) > 0 for key in ranked_keys):
        return "grounded"
    debug = payload.get("_debug") or {}
    answerability = debug.get("answerability") or {}
    top_candidates = debug.get("top_candidates") or []
    debug_keys: list[str] = []
    for candidate in top_candidates:
        probe = {"content": candidate.get("text"), "type": candidate.get("type"), "name": candidate.get("text")}
        key = key_for_result(probe)
        if key:
            debug_keys.append(key)
    if any(query.relevance.get(key, 0) > 0 for key in debug_keys):
        if answerability.get("abstain"):
            return "utilization_failure"
        return "stale_conflict" if answerability.get("reason") == "low_answerability_score" else "utilization_failure"
    return "retrieval_failure"


def run_queries(search_fn: SearchFn, k: int = 10) -> List[Dict[str, Any]]:
    """Run every fixture query through `search_fn` and collect per-query
    metric rows. Returns a flat list of dicts ready for aggregation.
    """
    rows = []
    for q in QUERIES:
        results = search_fn(q.text, k)
        payload = getattr(search_fn, "last_payload", {}) or {}
        ranked_keys = [key_for_result(r) for r in results]
        ranked_keys = [k for k in ranked_keys if k]  # drop untagged distractors
        total_relevant = relevant_count(q.relevance)
        p5 = p_at_k(ranked_keys, q.relevance, 5)
        p5_ceiling = p_at_k_ceiling(q.relevance, 5)
        rows.append({
            "query": q.text,
            "category": q.category,
            "relevance": q.relevance,
            "relevant_count": total_relevant,
            "ranked_keys": ranked_keys,
            "n_results": len(results),
            "debug": payload.get("_debug"),
            "metacognition": payload.get("metacognition"),
            "failure_mode": _classify_failure_mode(q, ranked_keys, payload),
            "p_at_1": p_at_k(ranked_keys, q.relevance, 1),
            "p_at_5": p5,
            "p_at_5_ceiling": p5_ceiling,
            "p_at_5_ratio_to_ceiling": round(p5 / p5_ceiling, 4) if p5_ceiling > 0 else None,
            "recall_at_5": recall_at_k(ranked_keys, q.relevance, 5),
            "recall_at_10": recall_at_k(ranked_keys, q.relevance, 10),
            "mrr": mrr(ranked_keys, q.relevance),
            "dcg_at_5": dcg_at_k(ranked_keys, q.relevance, 5),
            "idcg_at_5": dcg_at_k(
                [key for key, _grade in sorted(q.relevance.items(), key=lambda item: item[1], reverse=True)],
                q.relevance,
                5,
            ),
            "ndcg_at_5": ndcg_at_k(ranked_keys, q.relevance, 5),
            "dcg_gap_at_5": max(
                dcg_at_k(
                    [key for key, _grade in sorted(q.relevance.items(), key=lambda item: item[1], reverse=True)],
                    q.relevance,
                    5,
                ) - dcg_at_k(ranked_keys, q.relevance, 5),
                0.0,
            ),
            "dcg_at_10": dcg_at_k(ranked_keys, q.relevance, 10),
            "idcg_at_10": dcg_at_k(
                [key for key, _grade in sorted(q.relevance.items(), key=lambda item: item[1], reverse=True)],
                q.relevance,
                10,
            ),
            "ndcg_at_10": ndcg_at_k(ranked_keys, q.relevance, 10),
            "dcg_gap_at_10": max(
                dcg_at_k(
                    [key for key, _grade in sorted(q.relevance.items(), key=lambda item: item[1], reverse=True)],
                    q.relevance,
                    10,
                ) - dcg_at_k(ranked_keys, q.relevance, 10),
                0.0,
            ),
        })
    return rows


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute overall + per-category metric means."""
    def mean(xs):
        xs = list(xs)
        return round(statistics.mean(xs), 4) if xs else 0.0

    def mean_opt(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.mean(xs), 4) if xs else None

    answerable_rows = [r for r in rows if r["relevant_count"] > 0]
    empty_rows = [r for r in rows if r["relevant_count"] == 0]

    p_at_5_overall = mean(r["p_at_5"] for r in rows)
    p_at_5_answerable = mean(r["p_at_5"] for r in answerable_rows)
    p_at_5_ceiling = mean(r["p_at_5_ceiling"] for r in rows)
    p_at_5_answerable_ceiling = mean(r["p_at_5_ceiling"] for r in answerable_rows)

    overall = {
        "n_queries": len(rows),
        "answerable_queries": len(answerable_rows),
        "empty_relevance_queries": len(empty_rows),
        "p_at_1": mean(r["p_at_1"] for r in rows),
        "p_at_5": p_at_5_overall,
        "p_at_5_answerable": p_at_5_answerable,
        "p_at_5_ceiling": p_at_5_ceiling,
        "p_at_5_answerable_ceiling": p_at_5_answerable_ceiling,
        "p_at_5_ratio_to_ceiling": round(p_at_5_overall / p_at_5_ceiling, 4) if p_at_5_ceiling else None,
        "p_at_5_macro_ratio_to_ceiling": mean_opt(r["p_at_5_ratio_to_ceiling"] for r in rows),
        "p_at_5_answerable_ratio_to_ceiling": (
            round(p_at_5_answerable / p_at_5_answerable_ceiling, 4)
            if p_at_5_answerable_ceiling else None
        ),
        "p_at_5_answerable_macro_ratio_to_ceiling": mean_opt(
            r["p_at_5_ratio_to_ceiling"] for r in answerable_rows
        ),
        "recall_at_5": mean(r["recall_at_5"] for r in rows),
        "recall_at_10": mean(r["recall_at_10"] for r in rows),
        "mrr": mean(r["mrr"] for r in rows),
        "ndcg_at_5": mean(r["ndcg_at_5"] for r in rows),
        "ndcg_at_10": mean(r["ndcg_at_10"] for r in rows),
    }

    by_category: Dict[str, Dict[str, float]] = {}
    for row in rows:
        bucket = by_category.setdefault(row["category"], {
            "count": 0, "answerable_count": 0, "empty_relevance_count": 0,
            "p_at_1": [], "p_at_5": [], "p_at_5_ceiling": [],
            "p_at_5_ratio_to_ceiling": [],
            "recall_at_5": [], "mrr": [], "ndcg_at_5": [],
        })
        bucket["count"] += 1
        if row["relevant_count"] > 0:
            bucket["answerable_count"] += 1
        else:
            bucket["empty_relevance_count"] += 1
        bucket["p_at_1"].append(row["p_at_1"])
        bucket["p_at_5"].append(row["p_at_5"])
        bucket["p_at_5_ceiling"].append(row["p_at_5_ceiling"])
        if row["p_at_5_ratio_to_ceiling"] is not None:
            bucket["p_at_5_ratio_to_ceiling"].append(row["p_at_5_ratio_to_ceiling"])
        bucket["recall_at_5"].append(row["recall_at_5"])
        bucket["mrr"].append(row["mrr"])
        bucket["ndcg_at_5"].append(row["ndcg_at_5"])

    for cat, bucket in by_category.items():
        cat_p_at_5 = mean(bucket["p_at_5"])
        cat_p_at_5_ceiling = mean(bucket["p_at_5_ceiling"])
        by_category[cat] = {
            "count": bucket["count"],
            "answerable_count": bucket["answerable_count"],
            "empty_relevance_count": bucket["empty_relevance_count"],
            "p_at_1": mean(bucket["p_at_1"]),
            "p_at_5": cat_p_at_5,
            "p_at_5_ceiling": cat_p_at_5_ceiling,
            "p_at_5_ratio_to_ceiling": round(cat_p_at_5 / cat_p_at_5_ceiling, 4) if cat_p_at_5_ceiling else None,
            "p_at_5_macro_ratio_to_ceiling": mean_opt(bucket["p_at_5_ratio_to_ceiling"]),
            "recall_at_5": mean(bucket["recall_at_5"]),
            "mrr": mean(bucket["mrr"]),
            "ndcg_at_5": mean(bucket["ndcg_at_5"]),
        }
    failure_breakdown: Dict[str, int] = {}
    for row in rows:
        failure_breakdown[row["failure_mode"]] = failure_breakdown.get(row["failure_mode"], 0) + 1

    return {"overall": overall, "by_category": by_category, "failure_breakdown": failure_breakdown}


# ---------------------------------------------------------------------------
# Full runner with DB scaffolding
# ---------------------------------------------------------------------------

def _build_brain_search_fn(db_path: Path):
    """Return a search_fn closure wrapping `Brain.search` against db_path.

    Brain.search is the FTS5-only memories-table path — it's what the simple
    `brain.search()` API calls return. Fast and dependency-free; doesn't
    exercise the hybrid RRF blend, intent classifier, or event search.

    Assumes the caller already seeded the DB and closed the seeder connection.
    """
    from agentmemory.brain import Brain  # local import; respects sys.path tweak above
    brain = Brain(db_path=str(db_path), agent_id="bench-agent")
    def search_fn(query: str, k: int):
        results = brain.search(query, limit=k)
        search_fn.last_payload = {"memories": results}
        return results
    return brain, search_fn


def _build_cmd_search_fn(db_path: Path):
    """Return a search_fn closure wrapping the full `cmd_search` CLI path.

    This is the path exercised by `brainctl search` and the MCP
    `memory_search` tool — it runs hybrid RRF, intent classification, graph
    expansion, and adaptive salience. Returns a flat list of results across
    memories + events + context + entities + decisions buckets so the
    metric code can rank them together.

    Output is captured by patching `_impl.json_out` in place, which writes
    to an in-process buffer instead of stdout.
    """
    import io
    import contextlib
    import types

    # Pin the CLI onto this DB for the duration of the search calls.
    # Assumes caller already seeded via a throwaway Brain instance.
    import agentmemory._impl as _impl
    _impl.DB_PATH = db_path
    brain = None

    # cmd_search opens its own `db = get_db()` per call and commits near the
    # end (just before json_out). We rely on that commit + gc to release the
    # connection between iterations. No sidecar connection needed — one
    # would just fight cmd_search's db for the writer lock.

    def search_fn(query: str, k: int):
        captured: list = []

        def _capture(data, compact=False):  # matches real json_out signature
            captured.append(data)

        args = types.SimpleNamespace(
            query=query,
            limit=k,
            tables="memories,events,context,entities,decisions,procedures",
            no_recency=False,
            no_graph=True,                      # graph expansion adds noise for the bench
            budget=None,
            min_salience=None,
            mmr=False,
            mmr_lambda=0.7,
            explore=False,
            profile=None,
            pagerank_boost=0.0,
            quantum=False,
            benchmark=True,
            benchmark_ranking_mode="raw",
            agent="bench-agent",
            format="json",
            oneline=False,
            verbose=False,
        )

        saved_json = _impl.json_out
        saved_oneline = _impl.oneline_out
        _impl.json_out = _capture
        _impl.oneline_out = _capture
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                _impl.cmd_search(args)
        finally:
            _impl.json_out = saved_json
            _impl.oneline_out = saved_oneline
            # Release any lingering cmd_search connection between iterations.
            import gc
            gc.collect()

        if not captured:
            return []
        payload = captured[0] if isinstance(captured[0], dict) else {}
        search_fn.last_payload = payload

        # Flatten buckets (memories/events/context/entities/decisions/procedures) into
        # a single ranking, preserving final_score order. cmd_search already
        # sorted each bucket by final_score desc.
        flat: List[Dict[str, Any]] = []
        for bucket in ("procedures", "memories", "events", "context", "entities", "decisions"):
            flat.extend(payload.get(bucket, []) or [])
        flat.sort(key=lambda r: r.get("final_score", 0.0), reverse=True)
        return flat[:k]

    return brain, search_fn


PIPELINES: Dict[str, Callable[[Path], Tuple[Any, SearchFn]]] = {
    "brain": _build_brain_search_fn,         # FTS5-only Brain.search
    "cmd": _build_cmd_search_fn,             # full hybrid pipeline via cmd_search
}


def run(db_path: Optional[Path] = None, k: int = 10,
        pipeline: str = "cmd") -> Dict[str, Any]:
    """Full benchmark run: seed a temp brain, execute all queries, aggregate.

    Args:
        db_path: override DB location. If None, uses an in-memory temp file
            under the system tmp dir so CI/local runs don't stomp on the
            developer brain.db.
        k: top-k window for ranking metrics.
        pipeline: which search path to exercise — "cmd" (default) for the full
            hybrid CLI pipeline, or "brain" for the FTS5-only Brain.search path.
    """
    import tempfile
    os.environ.setdefault("BRAINCTL_SILENT_MIGRATIONS", "1")

    cleanup = False
    if db_path is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="brainctl-bench-"))
        db_path = tmp_dir / "bench.db"  # Brain()._init_db requires the file NOT exist
        cleanup = True

    try:
        if pipeline not in PIPELINES:
            raise ValueError(f"unknown pipeline {pipeline!r}; options: {list(PIPELINES)}")
        # Seed via direct SQL against a fresh DB so no Brain-owned connection
        # lingers and fights the pipeline search_fn for the SQLite write lock.
        seed_db_direct(db_path)
        brain, search_fn = PIPELINES[pipeline](db_path)
        rows = run_queries(search_fn, k=k)
        agg = aggregate(rows)
        agg["db_path"] = str(db_path)
        agg["k"] = k
        agg["pipeline"] = pipeline
        agg["rows"] = rows
        return agg
    finally:
        if cleanup:
            import shutil
            try:
                shutil.rmtree(db_path.parent, ignore_errors=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------

BASELINE_PATH = _ROOT / "tests" / "bench" / "baselines" / "search_quality.json"

# Headline metrics that must not regress by more than REGRESSION_TOLERANCE
# between baseline and current runs. Keep the gated set narrow so it's
# unambiguous what to fix when the regression test fails.
GATED_METRICS = ("p_at_1", "p_at_5", "recall_at_5", "mrr", "ndcg_at_5")

# Categories are tracked for diagnostics but aren't gated — per-category
# fixture counts are too small to make category-level gating stable.
REGRESSION_TOLERANCE = 0.02  # >2% drop on any gated metric fails the regression test


def compare_to_baseline(current: Dict[str, Any],
                        baseline: Dict[str, Any]) -> Dict[str, Any]:
    """Return a diff report + pass/fail flag."""
    cur_o = current["overall"]
    base_o = baseline["overall"]
    deltas = {}
    failing: List[Tuple[str, float, float]] = []
    for metric in GATED_METRICS:
        cur_v = float(cur_o.get(metric, 0.0))
        base_v = float(base_o.get(metric, 0.0))
        delta = round(cur_v - base_v, 4)
        deltas[metric] = {"current": cur_v, "baseline": base_v, "delta": delta}
        if delta < -REGRESSION_TOLERANCE:
            failing.append((metric, cur_v, base_v))
    return {
        "ok": len(failing) == 0,
        "tolerance": REGRESSION_TOLERANCE,
        "deltas": deltas,
        "failing": [{"metric": m, "current": c, "baseline": b} for m, c, b in failing],
    }


def load_baseline() -> Optional[Dict[str, Any]]:
    if not BASELINE_PATH.exists():
        return None
    with BASELINE_PATH.open() as fh:
        return json.load(fh)


def save_baseline(result: Dict[str, Any]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = {
        "overall": result["overall"],
        "by_category": result["by_category"],
        "k": result.get("k", 10),
    }
    with BASELINE_PATH.open("w") as fh:
        json.dump(trimmed, fh, indent=2, sort_keys=True)
