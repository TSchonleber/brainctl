"""Reranker signal-informativeness gate tests (2.3.1).

Covers the LOCOMO retrieval-quality gap fix. cmd_search's reranker chain
(recency / salience / Q-value / trust) was scrambling FTS+vec ranking on
cold-start brains and synthetic conversational benchmarks because the
underlying signals (timestamps, recall counts, trust scores) were uniform.

Two surfaces under test:
1. `--benchmark` flag (CLI + cmd_search arg) — hard-bypass the chain.
2. Per-reranker signal-informativeness gates — auto-skip when uniform.

The bench fixtures are themselves uniform-shape (all rows seeded in <1s, no
recall history, default trust), which is why we get to use them as the
"LOCOMO-shape fixture" the task describes — same failure mode, smaller
corpus.

Tests use direct DB seeding via SQL (no Brain class) so no writer connection
lingers to fight cmd_search for the SQLite write lock.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Ensure src/ is importable for module-level imports below.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory._impl as _impl  # noqa: E402

INIT_SQL = SRC / "agentmemory" / "db" / "init_schema.sql"


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

def _utc_iso(when: datetime | None = None) -> str:
    """Return an ISO-8601 timestamp string in the format brainctl writes."""
    when = when or datetime.now(timezone.utc)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _seed_schema(db_path: Path, agent_id: str = "robustness-agent") -> None:
    """Initialise a fresh brain.db with the production schema + minimal defaults."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(INIT_SQL.read_text())
        now = _utc_iso()
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
            "created_at, updated_at) VALUES (?, ?, 'test', 'active', ?, ?)",
            (agent_id, agent_id, now, now),
        )
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
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def _seed_locomo_shape(db_path: Path, n: int = 100,
                       agent_id: str = "robustness-agent") -> None:
    """Seed N memories with uniform timestamps + zero recall + default trust.

    Mirrors the LOCOMO synthetic-conversational shape: every row writes within
    a <1s window, recalled_count=0 everywhere, trust_score uses the schema
    default. cmd_search's recency / salience / Q-value / trust gates should
    all trip on a candidate set drawn from this corpus.

    Content is diverse enough that FTS5 has something to rank by — we use a
    rotating set of distinct keyword themes so a query picks out a non-trivial
    subset.
    """
    _seed_schema(db_path, agent_id)
    now = _utc_iso()
    themes = [
        "alice prefers dark mode interfaces and themes everywhere",
        "bob writes python code with four space indentation always",
        "carol reviews security pull requests for personal information leaks",
        "deploy to staging by running make deploy after green tests",
        "rollback procedure git revert merge commit and redeploy",
        "embeddings come from ollama nomic-embed-text 768 dimensions",
        "sqlite stores all memories with fts5 plus sqlite-vec extension",
        "we chose sqlite over postgres because zero ops overhead",
        "fts5 syntax errors on punctuation sanitize before search",
        "ollama crashes overnight blocking all embedding calls hangs",
    ]
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(n):
            theme = themes[i % len(themes)]
            content = f"{theme} [tag={i:03d}]"
            conn.execute(
                "INSERT INTO memories (agent_id, category, content, confidence, "
                "created_at, updated_at, recalled_count) "
                "VALUES (?, 'project', ?, 0.9, ?, ?, 0)",
                (agent_id, content, now, now),
            )
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def _seed_real_shape(db_path: Path, n: int = 100,
                     agent_id: str = "robustness-agent") -> None:
    """Seed N memories with realistic spread on every reranker signal.

    - Timestamps spread over 30 days (deterministic but jittered).
    - recalled_count varies 0..20 with skew toward 0 (long tail).
    - trust_score varies 0.5..1.0 (mix of human_verified and lower-trust).
    - replay_priority varies 0..1.

    This represents a "real" brain after a few weeks of use — every gate
    should report informative=True and the rerankers should fire.
    """
    _seed_schema(db_path, agent_id)
    rng = random.Random(20260416)  # deterministic
    base = datetime.now(timezone.utc) - timedelta(days=30)
    themes = [
        "alice prefers dark mode interfaces",
        "bob uses four space indentation",
        "carol reviews security changes",
        "deploy to staging by make deploy",
        "rollback procedure git revert",
        "embeddings come from ollama nomic",
        "sqlite stores memories with fts5",
        "we chose sqlite over postgres",
        "fts5 syntax errors on punctuation",
        "ollama crashes blocking embeddings",
    ]
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(n):
            theme = themes[i % len(themes)]
            content = f"{theme} [tag={i:03d}]"
            # Spread over 30 days with some jitter.
            ts = (base + timedelta(seconds=rng.randint(0, 30 * 86400))).replace(microsecond=0)
            ts_iso = ts.isoformat().replace("+00:00", "Z")
            recalls = rng.choices(
                [0, 1, 2, 5, 10, 20], weights=[60, 15, 10, 8, 5, 2]
            )[0]
            trust = round(rng.uniform(0.5, 1.0), 4)
            replay = round(rng.uniform(0.0, 1.0), 4)
            conn.execute(
                "INSERT INTO memories (agent_id, category, content, confidence, "
                "created_at, updated_at, recalled_count, trust_score, replay_priority) "
                "VALUES (?, 'project', ?, 0.9, ?, ?, ?, ?, ?)",
                (agent_id, content, ts_iso, ts_iso, recalls, trust, replay),
            )
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def _build_args(query: str, limit: int = 10, **overrides) -> types.SimpleNamespace:
    """Build a SimpleNamespace mirroring the argparse args cmd_search expects."""
    base = dict(
        query=query,
        limit=limit,
        tables="memories",            # restrict to memories so events/context don't muddy
        no_recency=False,
        no_graph=True,
        budget=None,
        min_salience=None,
        mmr=False,
        mmr_lambda=0.7,
        explore=False,
        profile=None,
        pagerank_boost=0.0,
        quantum=False,
        benchmark=False,
        benchmark_ranking_mode="full",
        second_stage=False,
        agent="robustness-agent",
        output="json",
        format="json",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _call_cmd_search(db_path: Path, args: types.SimpleNamespace) -> Dict[str, Any]:
    """Invoke cmd_search against db_path, capturing the json_out payload.

    Mirrors tests/bench/eval._build_cmd_search_fn — same in-process patching
    of json_out so we don't need to shell out or parse stdout.
    """
    _impl.DB_PATH = db_path

    captured: List[Any] = []
    def _capture(data, compact=False):
        captured.append(data)

    saved_json = _impl.json_out
    saved_oneline = _impl.oneline_out
    _impl.json_out = _capture
    _impl.oneline_out = _capture
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                _impl.cmd_search(args)
    finally:
        _impl.json_out = saved_json
        _impl.oneline_out = saved_oneline
        import gc
        gc.collect()

    if not captured:
        return {}
    return captured[0] if isinstance(captured[0], dict) else {}


# ---------------------------------------------------------------------------
# 1. Per-reranker signal-informativeness unit tests
# ---------------------------------------------------------------------------

class TestSignalCheck:
    """Unit tests for the pure _reranker_signal_check helper."""

    def test_empty_candidate_set_is_informative(self):
        out = _impl._reranker_signal_check([])
        for k in ("recency", "salience", "q_value", "trust"):
            assert out[k]["informative"] is True

    def test_recency_uniform_timestamps_trips(self):
        now = _utc_iso()
        candidates = [{"created_at": now} for _ in range(50)]
        out = _impl._reranker_signal_check(candidates)
        assert out["recency"]["informative"] is False
        assert "uniform_timestamps_stdev" in out["recency"]["reason"]
        assert out["recency"]["stat"] == 0.0

    def test_recency_spread_timestamps_fires(self):
        # Spread over 7 days -> stdev well above the 60s floor.
        base = datetime.now(timezone.utc)
        candidates = [
            {"created_at": (base - timedelta(days=i)).isoformat().replace("+00:00", "Z")}
            for i in range(7)
        ]
        out = _impl._reranker_signal_check(candidates)
        assert out["recency"]["informative"] is True
        assert out["recency"]["stat"] > _impl._RECENCY_STDEV_FLOOR_SECONDS

    def test_salience_no_priority_data_passes_through(self):
        # No replay_priority column anywhere -> treated as informative-by-default
        # (the reranker won't fire on these rows anyway).
        candidates = [{"created_at": _utc_iso()} for _ in range(10)]
        out = _impl._reranker_signal_check(candidates)
        assert out["salience"]["informative"] is True
        assert out["salience"]["reason"] == "no_priority_data"

    def test_salience_uniform_replay_priority_trips(self):
        candidates = [{"replay_priority": 0.0} for _ in range(20)]
        out = _impl._reranker_signal_check(candidates)
        assert out["salience"]["informative"] is False
        assert "uniform_replay_priority_stdev" in out["salience"]["reason"]

    def test_salience_varied_replay_priority_fires(self):
        candidates = [{"replay_priority": i * 0.1} for i in range(10)]
        out = _impl._reranker_signal_check(candidates)
        assert out["salience"]["informative"] is True

    def test_qvalue_zero_recalls_trips(self):
        candidates = [{"recalled_count": 0} for _ in range(20)]
        out = _impl._reranker_signal_check(candidates)
        assert out["q_value"]["informative"] is False
        assert "insufficient_recall_history_total_0" in out["q_value"]["reason"]

    def test_qvalue_below_floor_trips(self):
        # Total = 2 < _QVALUE_RECALL_FLOOR (3)
        candidates = [{"recalled_count": 1}, {"recalled_count": 1}, {"recalled_count": 0}]
        out = _impl._reranker_signal_check(candidates)
        assert out["q_value"]["informative"] is False

    def test_qvalue_at_floor_fires(self):
        candidates = [{"recalled_count": 3}]
        out = _impl._reranker_signal_check(candidates)
        assert out["q_value"]["informative"] is True

    def test_trust_uniform_default_trips(self):
        # All at the schema default 1.0 -> stdev 0 -> trips.
        candidates = [{"trust_score": 1.0} for _ in range(20)]
        out = _impl._reranker_signal_check(candidates)
        assert out["trust"]["informative"] is False

    def test_trust_uniform_mcp_default_trips(self):
        # Real-world fresh-DB shape: every row at 0.85 (mcp_tool source).
        candidates = [{"trust_score": 0.85} for _ in range(20)]
        out = _impl._reranker_signal_check(candidates)
        assert out["trust"]["informative"] is False

    def test_trust_varied_fires(self):
        candidates = [{"trust_score": 0.5}, {"trust_score": 0.85}, {"trust_score": 1.0}]
        out = _impl._reranker_signal_check(candidates)
        assert out["trust"]["informative"] is True

    def test_trust_no_data_passes_through(self):
        # Non-memory rows (events / context) don't carry trust_score.
        candidates = [{"created_at": _utc_iso()} for _ in range(5)]
        out = _impl._reranker_signal_check(candidates)
        assert out["trust"]["informative"] is True
        assert out["trust"]["reason"] == "no_trust_data"


# ---------------------------------------------------------------------------
# 2. LOCOMO-shape fixture: cmd_search must NOT scramble FTS+vec ranking
# ---------------------------------------------------------------------------

class TestLocomoShapeRanking:
    """100 memories, uniform timestamps, no recall, default trust."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "locomo.db"
        _seed_locomo_shape(db_path, n=100)
        return db_path

    def test_signal_gates_all_trip(self, db):
        """Every reranker should be downweighted on a LOCOMO-shape corpus."""
        args = _build_args("alice prefers dark mode")
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        # All four signal gates fired on the memories bucket.
        assert debug.get("memories.recency_skipped"), debug
        assert debug.get("memories.qvalue_skipped"), debug
        assert debug.get("memories.trust_skipped"), debug
        # Salience may or may not have data depending on whether
        # _SAL_AVAILABLE on this machine — but if it carries replay_priority
        # values they're all 0.0 (schema default), so it should also trip.
        # Don't hard-assert salience: it depends on the salience_routing
        # module being importable.

    def test_returns_fts_relevant_ranking(self, db):
        """First result should be on-topic (theme keywords match query)."""
        args = _build_args("alice prefers dark mode")
        out = _call_cmd_search(db, args)
        memories = out.get("memories", [])
        assert memories, "expected at least one match"
        # The seeded corpus has ~10 memories per theme (100/10). The top
        # result for "alice prefers dark mode" should be one of them — the
        # raw FTS+vec ranking puts those at rank 1.
        top = memories[0]["content"].lower()
        assert "alice" in top or "dark" in top, top


# ---------------------------------------------------------------------------
# 3. Real-shape fixture: at least one reranker DOES fire
# ---------------------------------------------------------------------------

class TestRealShapeRanking:
    """30-day timestamp spread + varied recall + varied trust."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "real.db"
        _seed_real_shape(db_path, n=100)
        return db_path

    def test_at_least_one_reranker_fires(self, db):
        """On a realistic corpus at least one of the gates passes."""
        args = _build_args("alice prefers dark mode")
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        # Recency MUST pass: timestamps are spread over 30 days.
        assert "memories.recency_skipped" not in debug, (
            f"recency gate should fire on real-shape corpus; debug={debug}"
        )

    def test_trust_gate_fires_with_varied_scores(self, db):
        """Trust scores in [0.5, 1.0] -> stdev > 0.02 -> gate passes."""
        args = _build_args("alice prefers dark mode")
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        assert "memories.trust_skipped" not in debug, (
            f"trust gate should fire when trust_score varies; debug={debug}"
        )


# ---------------------------------------------------------------------------
# 4. --benchmark flag (in-process API + end-to-end CLI)
# ---------------------------------------------------------------------------

class TestBenchmarkFlag:

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "bench.db"
        _seed_locomo_shape(db_path, n=50)
        return db_path

    def test_benchmark_full_mode_keeps_second_stage_opt_in(self, db):
        args = _build_args("alice prefers dark mode", benchmark=True)
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        assert debug.get("benchmark.ranking_mode") == "full"
        assert debug.get("second_stage", {}).get("enabled") is False
        assert debug.get("memories.recency_skipped") == "benchmark_mode"
        assert debug.get("memories.qvalue_skipped") == "benchmark_mode"

        args = _build_args("alice prefers dark mode", benchmark=True, second_stage=True)
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        assert debug.get("benchmark.ranking_mode") == "full"
        assert debug.get("second_stage", {}).get("enabled") is True
        assert debug.get("memories.recency_skipped") == "benchmark_mode"
        assert debug.get("memories.qvalue_skipped") == "benchmark_mode"

    def test_benchmark_full_mode_uses_normal_trust_gate(self, db):
        args = _build_args("alice prefers dark mode", benchmark=True)
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        assert "memories.trust_skipped" not in debug, debug

    def test_benchmark_emits_stderr_note(self, db):
        # Capture the stderr message.
        args = _build_args("alice prefers dark mode", benchmark=True)
        _impl.DB_PATH = db
        captured: List[Any] = []
        def _capture(data, compact=False):
            captured.append(data)
        saved_json = _impl.json_out
        _impl.json_out = _capture
        try:
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(buf_err):
                    _impl.cmd_search(args)
            assert "--benchmark" in buf_err.getvalue()
            assert "stable-eval mode" in buf_err.getvalue()
        finally:
            _impl.json_out = saved_json

    def test_benchmark_returns_results(self, db):
        """Sanity: --benchmark mode still returns matches, not empty."""
        args = _build_args("alice prefers dark mode", benchmark=True)
        out = _call_cmd_search(db, args)
        assert out.get("memories"), "expected non-empty results under --benchmark"

    def test_benchmark_cli_flag_end_to_end(self, db, tmp_path):
        """Shell out to the real `brainctl search --benchmark` and verify
        the stderr note + JSON _debug output."""
        brainctl_bin = SRC.parent / "bin" / "brainctl"
        if not brainctl_bin.exists():
            pytest.skip(f"brainctl entry not present: {brainctl_bin}")
        env = {
            **os.environ,
            "PYTHONPATH": str(SRC),
            "BRAIN_DB": str(db),
            "BRAINCTL_HOME": str(db.parent),
        }
        result = subprocess.run(
            [sys.executable, str(brainctl_bin),
             "--agent", "robustness-agent",
             "search", "alice prefers dark mode",
             "--benchmark", "--no-graph", "--limit", "10",
             "--tables", "memories"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"brainctl search failed: stdout={result.stdout[:500]} "
            f"stderr={result.stderr[:500]}"
        )
        assert "--benchmark" in result.stderr, (
            f"expected stderr note about --benchmark; got: {result.stderr[:200]}"
        )
        # Parse the JSON payload off stdout.
        payload = json.loads(result.stdout)
        debug = payload.get("_debug", {})
        assert debug.get("benchmark.ranking_mode") == "raw"

    def test_benchmark_raw_mode_preserves_legacy_ablation(self, db):
        args = _build_args("alice prefers dark mode", benchmark=True, benchmark_ranking_mode="raw")
        out = _call_cmd_search(db, args)
        debug = out.get("_debug", {})
        assert debug.get("benchmark.ranking_mode") == "raw"
        assert debug.get("memories.recency_skipped") == "benchmark_mode"
        assert debug.get("memories.salience_skipped") == "benchmark_mode"
        assert debug.get("memories.qvalue_skipped") == "benchmark_mode"


# ---------------------------------------------------------------------------
# 5. Regression guard: 5 known-good queries from the existing bench fixture
# ---------------------------------------------------------------------------

class TestExistingBenchRegression:
    """Pull the bench harness in-process and confirm 5 queries that worked
    before the change still return relevant top-1 results.

    This is a lighter-weight version of `python -m tests.bench.run --check`
    intended for CI gating without paying the full 20-query scaffold cost.
    """

    REGRESSION_QUERIES = [
        # (query, must_contain_in_top_1)
        ("What does Alice prefer?", "alice"),
        ("Who is the security reviewer?", "carol"),
        ("How do I deploy to staging?", "deploy"),
        ("Why did we choose SQLite?", "sqlite"),
        ("How do I run the tests?", "pytest"),
    ]

    @pytest.fixture
    def bench_db(self, tmp_path):
        from tests.bench.eval import seed_db_direct
        db_path = tmp_path / "bench-regression.db"
        seed_db_direct(db_path, agent_id="bench-agent")
        return db_path

    @pytest.mark.parametrize("query,must_contain", REGRESSION_QUERIES)
    def test_query_top1_still_relevant(self, bench_db, query, must_contain):
        args = _build_args(query, agent="bench-agent",
                           tables="memories,events,context")
        out = _call_cmd_search(bench_db, args)
        # Pull from any bucket; cmd_search sorts each by final_score.
        top_candidates = []
        for bucket in ("memories", "events", "context"):
            top_candidates.extend(out.get(bucket, []) or [])
        # Sort across all buckets and take the very top by final_score.
        top_candidates.sort(key=lambda r: r.get("final_score", 0.0), reverse=True)
        assert top_candidates, f"no results for query: {query}"
        top_text = (top_candidates[0].get("content")
                    or top_candidates[0].get("summary") or "").lower()
        assert must_contain in top_text, (
            f"query {query!r} top-1 should mention {must_contain!r}, "
            f"got: {top_text[:120]!r}"
        )

    def test_entity_bucket_populated_for_entity_query(self, bench_db):
        args = _build_args(
            "Who owns the consolidation daemon?",
            agent="bench-agent",
            tables="memories,events,context,entities,decisions,procedures",
            benchmark=True,
        )
        out = _call_cmd_search(bench_db, args)
        assert out.get("entities"), "entity query should populate entities bucket"
        assert out["entities"][0]["name"] == "Bob"

    def test_negative_out_of_domain_query_abstains(self, bench_db):
        args = _build_args(
            "Summary of yesterday's basketball game",
            agent="bench-agent",
            tables="memories,events,context,entities,decisions,procedures",
            benchmark=True,
        )
        out = _call_cmd_search(bench_db, args)
        assert out.get("metacognition", {}).get("abstained") is True
        for bucket in ("memories", "events", "context", "entities", "decisions", "procedures"):
            assert not out.get(bucket), f"{bucket} should be empty after abstention"


def test_entity_alias_expansion_promotes_canonical_memory(tmp_path):
    db_path = tmp_path / "alias-linking.db"
    _seed_schema(db_path)
    now = _utc_iso()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO memories (
                agent_id, category, scope, content, confidence,
                created_at, updated_at
            ) VALUES (?, 'preference', 'global', ?, 0.9, ?, ?)
            """,
            ("robustness-agent", "Bob prefers four-space indentation for Python code.", now, now),
        )
        conn.execute(
            """
            INSERT INTO entities (
                name, entity_type, properties, observations, agent_id, confidence,
                scope, created_at, updated_at, aliases, compiled_truth
            ) VALUES (?, 'person', '{}', ?, ?, 0.95, 'global', ?, ?, ?, ?)
            """,
            (
                "Bob",
                json.dumps(["Prefers four-space indentation"], ensure_ascii=True),
                "robustness-agent",
                now,
                now,
                json.dumps(["Robert"], ensure_ascii=True),
                "Bob prefers four-space indentation.",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    args = _build_args(
        "What does Robert prefer?",
        agent="robustness-agent",
        tables="memories,entities",
        benchmark=True,
    )
    out = _call_cmd_search(db_path, args)
    flat = []
    for bucket in ("entities", "memories"):
        flat.extend(out.get(bucket, []) or [])
    flat.sort(key=lambda row: row.get("final_score", 0.0), reverse=True)
    assert flat, "alias-linked query should return at least one result"
    top_text = (
        flat[0].get("content")
        or flat[0].get("name")
        or flat[0].get("summary")
        or ""
    ).lower()
    assert "bob" in top_text, top_text


# ---------------------------------------------------------------------------
# 6. Trust adjustment math
# ---------------------------------------------------------------------------

class TestTrustAdjustedScore:
    def test_neutral_at_one(self):
        assert _impl._trust_adjusted_score(1.0, 1.0) == pytest.approx(1.0)

    def test_attenuated_at_zero(self):
        # 0.7 + 0.3 * 0 = 0.7 multiplier
        assert _impl._trust_adjusted_score(1.0, 0.0) == pytest.approx(0.7)

    def test_none_treated_as_one(self):
        assert _impl._trust_adjusted_score(1.0, None) == pytest.approx(1.0)

    def test_retracted_memory_attenuated(self):
        # trust=0.05 -> 0.7 + 0.3*0.05 = 0.715
        assert _impl._trust_adjusted_score(1.0, 0.05) == pytest.approx(0.715)
