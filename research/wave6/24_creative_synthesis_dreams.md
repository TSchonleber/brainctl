# Dreams & Creative Synthesis — Generative Recombination During Consolidation

**Research Wave:** 6
**Issue:** [COS-247](/COS/issues/COS-247)
**Author:** Prune (Memory Hygiene Specialist)
**Date:** 2026-03-28
**Builds On:** wave1/05_consolidation_cycle.py, COS-233 (cross-scope contradiction), COS-231 (embedding backfill)
**Cross-pollinate:** Hippocampus (consolidation cycle owner), Recall (embedding infrastructure), Engram (memory systems lead)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

The consolidation cycle prunes, decays, merges, and detects contradictions. It never **creates**. Real brains dream — REM sleep recombines fragments of experience into novel configurations, generating the associations that look like "insight" upon waking. Our brain.db maintains and compresses knowledge. It doesn't imagine.

This report designs a **Dream Pass**: a creative synthesis step added to the consolidation cycle that:

1. **Bisociation** — finds cross-scope memory pairs with unexpectedly high embedding similarity and synthesizes novel connection hypotheses
2. **Incubation queue** — logs failed `brainctl search` queries and retries them during consolidation with expanded semantic matching
3. **Serendipity injection** — surfaces random-but-potentially-relevant memories outside an agent's scope into their context
4. **Generative replay** — creates synthetic "what-if" variants of high-importance memories to test future robustness

All synthetic outputs are labeled, low-trust, and separately queryable. They do not contaminate normal search unless explicitly requested. The pass is optional (`run_dream_pass=False` by default) and implemented in a new `09_creative_synthesis.py`.

---

## 1. Theoretical Grounding → System Mappings

### 1.1 Dream Function Theories

| Theory | Authors | Core Claim | Mapping to brain.db |
|---|---|---|---|
| REM creativity hypothesis | Walker & Stickgold (2009) | REM sleep loosens associative networks, enabling connections between weakly linked memories | Cross-scope bisociation pass: find pairs from different scopes that share embedding neighborhood |
| Reverse learning | Crick & Mitchison (1983) | Dreams "unlearn" spurious correlations that accumulated during waking | During dream pass, flag high-confidence synthetic memories that contradict active real memories — they're noise, not insight |
| Threat simulation | Revonsuo (2000) | Dreams rehearse responses to threatening situations | Generative replay: generate adversarial memory variants ("what if this assumption is wrong?") and test them against existing beliefs |
| Default Mode Network activation | Buckner et al. (2008) | Resting-state mind-wandering activates semantic integration across distant topics | Serendipity injection: surface memories from unrelated scopes into agent context during idle cycles |

**Key insight from Walker (2017):** The hippocampus doesn't just replay memories during sleep — it combines them into **novel grammatical sentences** never experienced before. The analogy: our consolidation cycle should produce memories that were never written by any agent but are valid inferences from their combination.

### 1.2 Computational Creativity (Boden, 2004)

Boden identifies three types:

| Type | Definition | Achievable in brain.db? |
|---|---|---|
| Combinational | New combinations of familiar ideas | **Yes** — embedding similarity across scopes finds these directly |
| Exploratory | Exploring the edges of an existing conceptual space | **Partial** — requires semantic clustering and boundary detection |
| Transformational | Breaking and restructuring conceptual space | **No** — requires world model and meta-reasoning beyond scope |

**Target:** Combinational creativity. It's the lowest-hanging fruit and directly enabled by our 100% embedding coverage from COS-231.

### 1.3 Bisociation (Koestler, 1964)

> "Creative insight = connecting two previously unrelated frames of reference."

In brain.db terms: two memories from **different scopes** that share high embedding similarity are in different semantic "frames" (projects, agents, categories) but are conceptually close. This geometric proximity is the signal of latent bisociation.

Algorithm:
```
For each pair (A, B) where A.scope ≠ B.scope:
    sim = 1 - vec_distance_cosine(embed_A, embed_B)
    if sim > BISOCIATION_THRESHOLD:
        yield (A, B, sim) as bisociation candidate
```

The threshold is tunable. Start at 0.70. At 100% coverage (39 active memories), cross-scope pairs number ≈ 780 — manageable per cycle with a limit cap.

### 1.4 Incubation Effect

> "Unconscious processing during breaks leads to 'aha' moments." — Dijksterhuis & Meurs (2006)

System mapping: when `brainctl search "X"` fails (returns 0 results), the query isn't wrong — the memory doesn't exist *yet*. Log the query to a deferred queue. During the next consolidation cycle, retry with:
- Expanded BM25 fuzzy matching
- Lower similarity threshold in vector search
- Intersection with memories written since the original query

If a match is found, emit a `deferred_query_resolved` event — the "aha."

### 1.5 Serendipity Engines

> "Beneficial accidents don't happen by accident." — van Andel (1994)

Deliberate mechanisms to expose agents to information outside their scope. Rate-limited to avoid noise:
- Every N consolidation cycles, select 1–3 memories from *outside* the current agent's typical scope
- Write a `serendipity_suggestion` event visible to the target agent
- Let the agent decide relevance — don't inject into memories directly

### 1.6 Generative Replay

In continual learning (Shin et al., 2017), replaying **transformed** versions of past experience:
1. Prevents catastrophic forgetting
2. Generates novel training data that improves generalization

Mapping: For memories with `importance > 0.8` (proxied by `recalled_count > 5` AND `confidence > 0.85`), generate a synthetic variant: same structure, slightly modified claims. Store as low-trust hypothesis. If a future real memory confirms the variant, promote it.

---

## 2. Schema Requirements

### 2.1 Existing columns we can reuse

The `memories` schema (after COS-196, COS-231 migrations) already has:

| Column | Dream Pass Use |
|---|---|
| `trust_score REAL DEFAULT 1.0` | Set to 0.2–0.4 for synthetic memories |
| `derived_from_ids TEXT` | JSON array of source memory IDs for bisociation pairs |
| `tags TEXT` | Tag with `["synthetic", "dream_bisociation"]` etc. |
| `temporal_class` | Use `'short'` for synthetic (decays in ~7 days unless promoted) |
| `confidence` | Set low (0.25) for speculative connections |

### 2.2 Migration: Extend `memory_type` CHECK constraint

The current CHECK limits `memory_type` to `('episodic','semantic')`. We need two new values:

```sql
-- Migration: extend memory_type enum
-- File: ~/agentmemory/db/migrations/009_dream_memory_types.sql

-- SQLite doesn't support ALTER COLUMN constraints.
-- Workaround: drop + recreate the CHECK via table rebuild.

BEGIN;

CREATE TABLE memories_new AS SELECT * FROM memories;
DROP TABLE memories;
CREATE TABLE memories (
    -- (identical column list, omitted for brevity)
    memory_type TEXT NOT NULL DEFAULT 'episodic'
        CHECK(memory_type IN ('episodic', 'semantic', 'synthetic', 'generative_replay'))
    -- ... rest of columns unchanged
);
INSERT INTO memories SELECT * FROM memories_new;
DROP TABLE memories_new;

COMMIT;
```

**Note for Hippocampus:** The table rebuild is required because SQLite cannot modify CHECK constraints in-place. The migration is safe — no data changes, no column additions. Existing `episodic` and `semantic` rows are preserved. All existing indexes and triggers will need to be recreated after the rebuild.

**Shortcut until migration runs:** Tag-based synthetic marking is sufficient. The `memory_type` column change is a nice-to-have for query ergonomics, not required for the pass to function. The dream pass can operate in "tag-only mode" without the migration.

### 2.3 New table: `dream_pass_log`

```sql
CREATE TABLE dream_pass_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    cycle_agent_id TEXT NOT NULL,
    bisociation_pairs_evaluated INTEGER DEFAULT 0,
    bisociation_insights_generated INTEGER DEFAULT 0,
    deferred_queries_resolved INTEGER DEFAULT 0,
    serendipity_events_emitted INTEGER DEFAULT 0,
    generative_replay_variants_created INTEGER DEFAULT 0,
    synthetic_memories_created INTEGER DEFAULT 0,
    metadata TEXT  -- JSON: config params used
);
```

### 2.4 New table: `deferred_queries`

```sql
CREATE TABLE deferred_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    query_text TEXT NOT NULL,
    query_embedding BLOB,   -- optional: pre-compute at query time
    queried_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,       -- set when incubation finds a match
    resolution_memory_id INTEGER REFERENCES memories(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT         -- purge after 30 days
);
```

---

## 3. Implementation: `09_creative_synthesis.py`

```python
"""
Creative Synthesis — Dream Pass
================================
Concept: Inspired by REM sleep's role in creative recombination. During
consolidation, after standard maintenance passes, the brain "dreams" —
it finds unexpected connections between unrelated memories, tests hypothetical
associations, and surfaces serendipitous insights.

This module implements four mechanisms:
  1. Bisociation: cross-scope memory pairs with high embedding similarity
  2. Incubation: deferred query resolution
  3. Serendipity: random injection of out-of-scope memories as suggestions
  4. Generative replay: synthetic variants of high-importance memories

All outputs are tagged 'synthetic' and carry low trust_score (0.2–0.4).
They are excluded from normal search unless --include-synthetic is passed.

Integration: called by 05_consolidation_cycle.py as optional step 9b
(run_dream_pass=False by default).
"""

import sqlite3
import json
import random
from datetime import datetime, timezone
from typing import Optional

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
CYCLE_AGENT_ID = "paperclip-engram"

# Tunable thresholds
BISOCIATION_SIMILARITY_THRESHOLD = 0.70   # cosine sim; distance < 0.30
BISOCIATION_LIMIT = 20                    # max pairs to evaluate per cycle
SYNTHETIC_TRUST_SCORE = 0.30
SYNTHETIC_CONFIDENCE = 0.25
SYNTHETIC_TEMPORAL_CLASS = "short"        # decays in ~7 days unless promoted
HIGH_IMPORTANCE_RECALLED_THRESHOLD = 5    # recalled_count for generative replay eligibility
HIGH_IMPORTANCE_CONFIDENCE_THRESHOLD = 0.85
SERENDIPITY_SAMPLE_SIZE = 3              # out-of-scope memories to surface per cycle
DEFERRED_QUERY_MAX_AGE_DAYS = 30


# ── 1. Bisociation ────────────────────────────────────────────────────────────

def find_bisociation_candidates(
    conn: sqlite3.Connection,
    min_similarity: float = BISOCIATION_SIMILARITY_THRESHOLD,
    limit: int = BISOCIATION_LIMIT,
) -> list[dict]:
    """
    Find cross-scope memory pairs with unexpectedly high embedding similarity.
    Uses sqlite-vec vec_distance_cosine (0=identical, 2=opposite for normalized vecs).
    Threshold: distance < (1 - min_similarity) maps to sim > min_similarity.
    """
    conn.row_factory = sqlite3.Row
    distance_threshold = 1.0 - min_similarity

    # Load sqlite-vec extension if not already loaded
    try:
        conn.enable_load_extension(True)
        conn.load_extension("/Users/r4vager/agentmemory/bin/vec0")
    except Exception:
        pass  # already loaded or not available

    rows = conn.execute("""
        SELECT
            a.id        AS id_a,
            b.id        AS id_b,
            a.content   AS content_a,
            b.content   AS content_b,
            a.scope     AS scope_a,
            b.scope     AS scope_b,
            a.category  AS category_a,
            b.category  AS category_b,
            a.agent_id  AS agent_a,
            b.agent_id  AS agent_b,
            a.confidence AS conf_a,
            b.confidence AS conf_b,
            vec_distance_cosine(va.embedding, vb.embedding) AS distance
        FROM memories a
        JOIN memories b ON a.id < b.id
            AND a.scope != b.scope
            AND a.retired_at IS NULL
            AND b.retired_at IS NULL
            AND a.confidence > 0.3
            AND b.confidence > 0.3
        JOIN vec_memories va ON va.id = a.id
        JOIN vec_memories vb ON vb.id = b.id
        WHERE vec_distance_cosine(va.embedding, vb.embedding) < ?
        ORDER BY vec_distance_cosine(va.embedding, vb.embedding) ASC
        LIMIT ?
    """, (distance_threshold, limit)).fetchall()

    return [dict(r) for r in rows]


def synthesize_bisociation_insight(pair: dict, synthesizer_fn=None) -> Optional[str]:
    """
    Given a cross-scope memory pair, generate a hypothetical connection insight.
    synthesizer_fn(content_a, content_b, scope_a, scope_b) -> str | None
    If None, uses a template-based fallback that produces a useful stub.
    """
    if synthesizer_fn:
        return synthesizer_fn(
            pair["content_a"], pair["content_b"],
            pair["scope_a"], pair["scope_b"]
        )

    # Fallback: template-based hypothesis generation (no LLM required)
    # Produces a reviewable stub rather than nothing.
    return (
        f"[Dream hypothesis] Memories from {pair['scope_a']} and {pair['scope_b']} "
        f"share conceptual proximity (similarity={1.0 - pair['distance']:.2f}). "
        f"Possible connection: "
        f'"{pair["content_a"][:80]}..." may relate to '
        f'"{pair["content_b"][:80]}..." — review for cross-domain insight.'
    )


def write_bisociation_memory(
    conn: sqlite3.Connection,
    pair: dict,
    insight: str,
    dry_run: bool = False,
) -> Optional[int]:
    """Write a synthetic bisociation memory with low trust/confidence."""
    if dry_run:
        return None

    tags = json.dumps(["synthetic", "dream_bisociation", f"scope:{pair['scope_a']}", f"scope:{pair['scope_b']}"])
    derived_from = json.dumps([pair["id_a"], pair["id_b"]])

    cur = conn.execute("""
        INSERT INTO memories (
            agent_id, category, scope, content, confidence, trust_score,
            temporal_class, tags, derived_from_ids, created_at, updated_at
        ) VALUES (?, 'lesson', 'global', ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    """, (
        CYCLE_AGENT_ID,
        insight,
        SYNTHETIC_CONFIDENCE,
        SYNTHETIC_TRUST_SCORE,
        SYNTHETIC_TEMPORAL_CLASS,
        tags,
        derived_from,
    ))
    return cur.lastrowid


def run_bisociation_pass(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    synthesizer_fn=None,
    min_similarity: float = BISOCIATION_SIMILARITY_THRESHOLD,
    limit: int = BISOCIATION_LIMIT,
) -> dict:
    """Run the full bisociation pipeline. Returns stats dict."""
    stats = {"pairs_evaluated": 0, "insights_generated": 0, "written": 0}

    candidates = find_bisociation_candidates(conn, min_similarity, limit)
    stats["pairs_evaluated"] = len(candidates)

    for pair in candidates:
        insight = synthesize_bisociation_insight(pair, synthesizer_fn)
        if insight:
            stats["insights_generated"] += 1
            mem_id = write_bisociation_memory(conn, pair, insight, dry_run)
            if mem_id:
                stats["written"] += 1

    return stats


# ── 2. Incubation Queue ───────────────────────────────────────────────────────

def log_deferred_query(
    conn: sqlite3.Connection,
    agent_id: str,
    query_text: str,
    query_embedding: bytes = None,
) -> int:
    """Log a failed search query to the deferred queue for later incubation."""
    cur = conn.execute("""
        INSERT INTO deferred_queries
            (agent_id, query_text, query_embedding, expires_at)
        VALUES (?, ?, ?, datetime('now', '+30 days'))
    """, (agent_id, query_text, query_embedding))
    return cur.lastrowid


def resolve_deferred_queries(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    similarity_threshold: float = 0.60,  # looser than normal search
    limit_per_query: int = 3,
) -> dict:
    """
    Retry deferred queries during consolidation with looser thresholds.
    Emits deferred_query_resolved events for any matches found.
    """
    conn.row_factory = sqlite3.Row
    stats = {"queries_checked": 0, "resolved": 0}

    pending = conn.execute("""
        SELECT id, agent_id, query_text, query_embedding, queried_at
        FROM deferred_queries
        WHERE resolved_at IS NULL
          AND (julianday('now') - julianday(queried_at)) <= ?
        ORDER BY queried_at ASC
        LIMIT 50
    """, (DEFERRED_QUERY_MAX_AGE_DAYS,)).fetchall()

    for q in pending:
        stats["queries_checked"] += 1

        # FTS retry with broader tokenization
        fts_rows = conn.execute("""
            SELECT m.id, m.content, m.scope, m.confidence
            FROM memories m
            JOIN memories_fts mf ON mf.rowid = m.id
            WHERE memories_fts MATCH ?
              AND m.retired_at IS NULL
              AND m.created_at > ?
            LIMIT ?
        """, (q["query_text"], q["queried_at"], limit_per_query)).fetchall()

        if fts_rows:
            stats["resolved"] += 1
            if not dry_run:
                # Mark resolved, emit event
                best = fts_rows[0]
                conn.execute("""
                    UPDATE deferred_queries
                    SET resolved_at = datetime('now'), resolution_memory_id = ?, attempts = attempts + 1
                    WHERE id = ?
                """, (best["id"], q["id"]))
                conn.execute("""
                    INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                    VALUES (?, 'deferred_query_resolved', ?, ?, 0.6, datetime('now'))
                """, (
                    q["agent_id"],
                    f"Incubated query resolved: '{q['query_text'][:60]}'",
                    json.dumps({
                        "query": q["query_text"],
                        "queried_at": q["queried_at"],
                        "resolution_memory_id": best["id"],
                        "resolution_content_preview": best["content"][:120],
                    }),
                ))
        else:
            if not dry_run:
                conn.execute("""
                    UPDATE deferred_queries SET attempts = attempts + 1 WHERE id = ?
                """, (q["id"],))

    return stats


# ── 3. Serendipity Injection ──────────────────────────────────────────────────

def emit_serendipity_suggestions(
    conn: sqlite3.Connection,
    target_agent_id: str,
    typical_scope: str,
    dry_run: bool = False,
    sample_size: int = SERENDIPITY_SAMPLE_SIZE,
) -> dict:
    """
    Pick random high-confidence memories outside the agent's typical scope
    and emit them as serendipity_suggestion events.
    The agent reads these in their next heartbeat and decides relevance.
    """
    conn.row_factory = sqlite3.Row
    stats = {"candidates": 0, "emitted": 0}

    # Pull high-confidence memories from outside the agent's scope
    rows = conn.execute("""
        SELECT id, content, scope, category, confidence, recalled_count
        FROM memories
        WHERE scope != ?
          AND scope != 'global'
          AND retired_at IS NULL
          AND confidence > 0.7
          AND recalled_count > 0
        ORDER BY RANDOM()
        LIMIT ?
    """, (typical_scope, sample_size * 5)).fetchall()  # oversample then pick

    stats["candidates"] = len(rows)
    selected = random.sample(list(rows), min(sample_size, len(rows)))

    for mem in selected:
        if not dry_run:
            conn.execute("""
                INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                VALUES (?, 'serendipity_suggestion', ?, ?, 0.3, datetime('now'))
            """, (
                target_agent_id,
                f"Serendipitous association from {mem['scope']}: {mem['content'][:80]}...",
                json.dumps({
                    "source_memory_id": mem["id"],
                    "source_scope": mem["scope"],
                    "source_category": mem["category"],
                    "content_preview": mem["content"][:200],
                    "confidence": mem["confidence"],
                }),
            ))
            stats["emitted"] += 1

    return stats


# ── 4. Generative Replay ──────────────────────────────────────────────────────

def find_generative_replay_candidates(
    conn: sqlite3.Connection,
    min_recalled: int = HIGH_IMPORTANCE_RECALLED_THRESHOLD,
    min_confidence: float = HIGH_IMPORTANCE_CONFIDENCE_THRESHOLD,
    limit: int = 10,
) -> list[dict]:
    """
    Find high-importance memories eligible for generative replay.
    These are memories so well-validated that a synthetic variant is worth creating.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, content, category, scope, agent_id, confidence, recalled_count, tags
        FROM memories
        WHERE recalled_count >= ?
          AND confidence >= ?
          AND retired_at IS NULL
          AND (tags NOT LIKE '%synthetic%' OR tags IS NULL)
        ORDER BY recalled_count DESC, confidence DESC
        LIMIT ?
    """, (min_recalled, min_confidence, limit)).fetchall()
    return [dict(r) for r in rows]


def generate_adversarial_variant(memory: dict, variant_fn=None) -> Optional[str]:
    """
    Generate a "what if this was slightly wrong?" variant of a high-value memory.
    variant_fn(content: str) -> str | None
    Falls back to a template stub that flags the memory for adversarial review.
    """
    if variant_fn:
        return variant_fn(memory["content"])

    # Fallback: produce a review prompt as a synthetic memory
    return (
        f"[Generative replay — adversarial probe] "
        f"What if the following belief is incorrect or incomplete? "
        f'"{memory["content"][:120]}" — '
        f"Check for: temporal drift, scope mismatch, or hidden assumptions."
    )


def run_generative_replay_pass(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    variant_fn=None,
) -> dict:
    """Run the generative replay pipeline. Returns stats dict."""
    stats = {"candidates": 0, "variants_created": 0}

    candidates = find_generative_replay_candidates(conn)
    stats["candidates"] = len(candidates)

    for mem in candidates:
        variant_content = generate_adversarial_variant(mem, variant_fn)
        if not variant_content:
            continue

        if not dry_run:
            tags = json.dumps([
                "synthetic", "generative_replay",
                f"source_memory:{mem['id']}"
            ])
            conn.execute("""
                INSERT INTO memories (
                    agent_id, category, scope, content, confidence, trust_score,
                    temporal_class, tags, derived_from_ids, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """, (
                CYCLE_AGENT_ID,
                mem["category"],
                mem["scope"],
                variant_content,
                0.20,  # very low confidence — speculative
                0.25,
                "ephemeral",  # expires quickly unless promoted
                tags,
                json.dumps([mem["id"]]),
            ))
            stats["variants_created"] += 1

    return stats


# ── Dream Pass Orchestrator ───────────────────────────────────────────────────

def run_dream_pass(
    db_path: str = DB_PATH,
    dry_run: bool = False,
    run_bisociation: bool = True,
    run_incubation: bool = True,
    run_serendipity: bool = False,   # off by default — needs target_agent_id
    run_generative_replay: bool = True,
    target_agent_id: str = None,     # required for serendipity
    typical_scope: str = "global",
    synthesizer_fn=None,
    variant_fn=None,
    bisociation_threshold: float = BISOCIATION_SIMILARITY_THRESHOLD,
) -> dict:
    """
    Full dream pass. Returns a report dict.
    Called by run_consolidation_cycle(run_dream_pass=True).
    """
    conn = sqlite3.connect(db_path)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "bisociation": {},
        "incubation": {},
        "serendipity": {},
        "generative_replay": {},
        "synthetic_memories_total": 0,
    }

    if run_bisociation:
        report["bisociation"] = run_bisociation_pass(
            conn, dry_run, synthesizer_fn, bisociation_threshold
        )
        report["synthetic_memories_total"] += report["bisociation"].get("written", 0)

    if run_incubation:
        report["incubation"] = resolve_deferred_queries(conn, dry_run)

    if run_serendipity and target_agent_id:
        report["serendipity"] = emit_serendipity_suggestions(
            conn, target_agent_id, typical_scope, dry_run
        )

    if run_generative_replay:
        report["generative_replay"] = run_generative_replay_pass(conn, dry_run, variant_fn)
        report["synthetic_memories_total"] += report["generative_replay"].get("variants_created", 0)

    # Log dream pass
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        conn.execute("""
            INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
            VALUES (?, 'dream_pass_complete', ?, ?, 0.7, datetime('now'))
        """, (
            CYCLE_AGENT_ID,
            (f"Dream pass: {report['synthetic_memories_total']} synthetic memories created, "
             f"{report['bisociation'].get('pairs_evaluated', 0)} bisociation pairs evaluated, "
             f"{report['incubation'].get('resolved', 0)} deferred queries resolved"),
            json.dumps(report),
        ))
        conn.commit()

    conn.close()
    return report


if __name__ == "__main__":
    import json as _json
    result = run_dream_pass(dry_run=True)
    print(_json.dumps(result, indent=2))
```

---

## 4. Integration into `05_consolidation_cycle.py`

Add to the orchestrator:

```python
# At top of run_consolidation_cycle(), add parameter:
def run_consolidation_cycle(
    db_path: str = DB_PATH,
    dry_run: bool = False,
    summarizer_fn=None,
    run_cross_scope: bool = False,
    run_dream_pass: bool = False,          # NEW
    dream_synthesizer_fn=None,             # NEW
    dream_target_agent_id: str = None,     # NEW (for serendipity)
) -> dict:
    ...
    report["dream_pass"] = {}              # NEW: add to report init

    # After step 7b (cross-scope contradiction), add step 9b:
    # 9b. Dream pass — creative synthesis (opt-in)
    if run_dream_pass:
        _cs = _load_mod("_creative_synth", "09_creative_synthesis.py")
        dream_result = _cs.run_dream_pass(
            db_path=db_path,
            dry_run=dry_run,
            synthesizer_fn=dream_synthesizer_fn,
            target_agent_id=dream_target_agent_id,
        )
        report["dream_pass"] = dream_result
```

---

## 5. New `brainctl` Commands

```bash
# Run the dream pass manually
brainctl dream run [--dry-run] [--bisociation-threshold 0.70] [--no-bisociation] [--no-replay]

# List synthetic memories (default: latest 20)
brainctl dream list [--limit N] [--type bisociation|replay|all]

# Promote a synthetic memory to real (raises confidence to 0.7, trust to 0.8, sets type='semantic')
brainctl dream promote <memory-id> [--reason "why this is real"]

# Prune expired/unvalidated synthetic memories
brainctl dream prune [--dry-run] [--older-than-days 7]

# Log a failed query for incubation
brainctl dream defer "query string" [--agent-id <id>]

# Check incubation queue
brainctl dream incubation-queue [--resolved] [--pending]
```

---

## 6. Querying Synthetic Memories

By default, `brainctl search` and `brainctl memory list` exclude synthetic memories (via `tags NOT LIKE '%synthetic%'`).

To include them:
```bash
brainctl search "cross-scope insights" --include-synthetic
brainctl memory list --filter-tag synthetic
brainctl memory list --filter-tag dream_bisociation
```

The `trust_score` and `confidence` fields distinguish synthetic from real:
- Synthetic bisociation: `trust_score=0.30, confidence=0.25`
- Generative replay: `trust_score=0.25, confidence=0.20`
- Promoted (confirmed by real event): `trust_score ≥ 0.80, confidence ≥ 0.70`

---

## 7. Safety & Quality Controls

### 7.1 Synthetic memories MUST NOT contaminate normal retrieval
- Default exclusion from `brainctl search`, `route-context`, and `hippocampus.py` consolidation candidates
- The `tags` field is the source of truth: `'synthetic'` tag → excluded unless `--include-synthetic`

### 7.2 Decay is aggressive for synthetic memories
- `temporal_class='short'` → ~7 day half-life by the decay pass in `01_spaced_repetition.py`
- `temporal_class='ephemeral'` for generative replay → ~24 hour half-life
- Synthetic memories that are never recalled or promoted expire silently

### 7.3 No LLM required at baseline
- All four mechanisms have template-based fallbacks
- The bisociation fallback produces a reviewable stub, not silence
- LLM synthesis functions are passed in as optional `synthesizer_fn` / `variant_fn` parameters
- This means the dream pass runs correctly in offline/cold environments

### 7.4 Rate limiting
- Bisociation: capped at `BISOCIATION_LIMIT=20` pairs per cycle
- Generative replay: capped at 10 candidates per cycle
- Serendipity: 3 suggestions per cycle max
- The dream pass is `run_dream_pass=False` by default — explicit opt-in only

### 7.5 Contradiction detection catches bad dreams
- The standard contradiction pass (step 7) and cross-scope pass (step 7b) run BEFORE the dream pass (step 9b)
- Any synthetic memory that contradicts an active real memory will be flagged in the NEXT cycle's contradiction pass
- Synthetic memories carry `trust_score < 0.5`, which reduces their influence in any future merge operations

---

## 8. Suggested Rollout

| Phase | Action | Owner |
|---|---|---|
| P0 | Create `deferred_queries` table (migration 010) | Hippocampus |
| P0 | Create `dream_pass_log` table (migration 011) | Hippocampus |
| P1 | Add `09_creative_synthesis.py` to `~/agentmemory/research/` | Hippocampus |
| P1 | Wire `run_dream_pass` param into `05_consolidation_cycle.py` | Hippocampus |
| P2 | Add `brainctl dream` subcommands to `~/agentmemory/bin/brainctl` | Recall |
| P2 | Add `--include-synthetic` flag to `brainctl search` | Recall |
| P3 | Migration 009 to extend `memory_type` CHECK constraint | Hippocampus |
| P3 | Run first dream pass dry-run, inspect bisociation candidates | Engram |
| P4 | Wire LLM synthesizer_fn (Haiku preferred for cost) | Engram |

**P0-P1 are required to ship. P2-P4 are incremental improvements.**

---

## 9. Open Questions

1. **Bisociation threshold calibration:** At 39 active memories with 100% embedding coverage, how many cross-scope pairs exceed 0.70 similarity? Engram or Recall should run a `dry_run=True` pass and report the distribution before committing to the threshold.

2. **LLM synthesizer cost model:** At BISOCIATION_LIMIT=20 pairs per cycle and ~1 daily cycle, that's ≤20 Haiku calls/day — cheap. But should the synthesizer be gated on trust score? Only call LLM for pairs where both source memories have `confidence > 0.7`?

3. **Serendipity scope targeting:** The current implementation picks random out-of-scope memories. Should it use embedding-guided "adjacent but not overlapping" selection instead? This would reduce noise but increase complexity.

4. **Promotion lifecycle:** Who decides a synthetic memory is real enough to promote? Currently manual via `brainctl dream promote`. Should the consolidation cycle auto-promote synthetic memories that have been recalled 3+ times?

---

## Conclusion

The consolidation cycle has mature maintenance capabilities but lacks generative capacity. The dream pass closes that gap with four additive mechanisms: bisociation, incubation, serendipity, and generative replay. All are optional, all produce explicitly-labeled synthetic outputs, and all have safe fallbacks.

The highest-value mechanism to implement first is bisociation: it directly exploits the 100% embedding coverage achieved in COS-231, requires no LLM, and produces reviewable output. At 39 active memories across diverse agent scopes, the first dry run is likely to surface at least 3–5 genuinely surprising cross-domain connections.

The brain learns. Now it can dream.
