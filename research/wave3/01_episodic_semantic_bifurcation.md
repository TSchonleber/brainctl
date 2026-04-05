# Episodic vs. Semantic Memory Bifurcation
## Research Report — COS-120
**Author:** Engram (Memory Systems Lead)
**Date:** 2026-03-28
**Target:** brain.db — Episodic/semantic bifurcation for distinct decay, consolidation, and retrieval per memory type

---

## Executive Summary

The current `memories` table conflates two fundamentally distinct memory systems:
**episodic** (time-stamped event records: what happened, when, to whom) and
**semantic** (stable world-knowledge: facts, conventions, architecture decisions, preferences).

Treating them identically degrades memory health in both directions:
- Episodic entries accumulate and bloat the store because the decay rate
  appropriate for a stable fact is far too slow for event records.
- Semantic entries risk incorrect retirement because confidence decay applies
  uniformly, even to truths that have not changed.

This report recommends a **type-field bifurcation** — adding a `memory_type`
column (`'episodic'` | `'semantic'`) to the existing `memories` table —
with differentiated decay rates, consolidation paths, and query routing.
No breaking schema changes; all existing `brainctl` interfaces remain intact.

**Signal-to-noise impact:** Estimated 40-60% reduction in stale episodic
records within 30 days after deployment, with a corresponding improvement in
retrieval precision for semantic queries.

---

## 1. The Problem: Current Conflation

### 1.1 Neuroscience Basis

Tulving (1972) established the first systematic distinction:

- **Episodic memory**: autobiographical, time-indexed events. "Agent X deployed
  v2.3 at 14:30 UTC on 2026-03-15." Recall requires temporal context; the fact
  decays in relevance as time passes.
- **Semantic memory**: decontextualized world-knowledge. "The API rate limit is
  100 req/s." No timestamp is intrinsic; truth is context-independent until
  explicitly superseded.

Squire (1992) refined this within the declarative memory taxonomy: both systems
are *declarative* (consciously accessible, propositional), but they differ in
**temporal binding**, **decay dynamics**, and **consolidation path**.

Critically for agent memory: **episodic memories consolidate into semantic
memories over time** — the hippocampal consolidation process that converts
repeated experiences into stable world-knowledge. Our hippocampus.py should
model this, but currently cannot distinguish input types.

### 1.2 Current Schema State

The `memories` table has:
- `category`: {identity, user, environment, convention, project, decision,
  lesson, preference, integration} — topical tags, not memory type
- `temporal_class`: {permanent, long, medium, short, ephemeral} — intended
  lifetime, not episodic vs. semantic distinction
- `confidence`: 0.0–1.0 float, decays via hippocampus.py uniformly

No field explicitly marks whether a memory is episodic (event record) or
semantic (stable fact). The `temporal_class` field partially approximates this —
`permanent` and `long` tend to hold semantic content, `short` and `ephemeral`
tend to hold episodic content — but this is accidental and not enforced.

### 1.3 Current Decay Model

```python
DECAY_RATES = {
    "long":      0.01,   # half-life ~70 days
    "medium":    0.03,   # half-life ~23 days
    "short":     0.07,   # half-life ~10 days
    "ephemeral": 0.20,   # half-life ~3.5 days
}
```

These rates are appropriate for episodic entries at each temporal class,
but are incorrect for semantic entries: a semantic memory that is `medium`
(e.g., "CostClock uses PostgreSQL 15 on Supabase") should not decay on a
23-day half-life. It should be stable until superseded.

---

## 2. Schema Recommendation

### 2.1 Add `memory_type` Column

```sql
ALTER TABLE memories
ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episodic'
  CHECK (memory_type IN ('episodic', 'semantic'));
```

**Rationale for a type field over separate tables:**

| Approach | Pros | Cons |
|----------|------|------|
| `memory_type` column on existing `memories` | No FK breaks; FTS5 triggers unchanged; `brainctl` interfaces unchanged; additive-only | Requires query filter discipline |
| Separate `episodic_memories` + `semantic_memories` tables | Maximum isolation | Breaks all existing queries, FTS triggers, `knowledge_edges` FK assumptions, `vec_memories` join |
| Hybrid: `memories` + separate `episodes` table | Cleaner domain model | Requires migration + dual-path maintenance |

The column approach is the only additive option. The table separation
requires reworking the entire retrieval stack; defer to a future wave if
separation proves necessary after observing real usage.

**Default `'episodic'`** for safe backward compatibility: existing records
without explicit classification are more likely event logs than stable facts.
Curators can reclassify high-confidence semantic memories in bulk.

### 2.2 Migration for Existing Memories

A heuristic reclassification pass, applied once at migration time:

```sql
-- Classify as semantic: permanent or long temporal_class memories
UPDATE memories
SET memory_type = 'semantic'
WHERE temporal_class IN ('permanent', 'long')
  AND retired_at IS NULL;

-- Classify as semantic: high-confidence decision/convention/identity categories
UPDATE memories
SET memory_type = 'semantic'
WHERE category IN ('identity', 'convention', 'decision', 'preference', 'environment')
  AND confidence >= 0.8
  AND retired_at IS NULL;

-- Everything else remains episodic (default)
```

This is a best-effort pass; manual review of high-value memories is advisable
but not required for correctness. The cost of misclassifying a semantic record
as episodic is over-decay; the cost of misclassifying an episodic record as
semantic is under-decay. Both are recoverable.

### 2.3 `brainctl memory add` Flag

```bash
brainctl -a hermes memory add "API rate limit is 100 req/s" \
  -c environment -s project:costclock-ai \
  --type semantic

brainctl -a engram memory add "Hermes deployed v2.3 at 14:30 UTC" \
  -c project -s project:costclock-ai \
  --type episodic   # (default — can be omitted)
```

Default remains `episodic` so existing agent scripts require no changes.

---

## 3. Decay Rules per Type

### 3.1 Episodic Decay

Episodic memories follow the existing exponential confidence decay, with
tuned rates per `temporal_class`:

```python
DECAY_RATES_EPISODIC = {
    "long":      0.01,   # half-life ~70 days  (architecture notes, extended episodes)
    "medium":    0.04,   # half-life ~17 days  (sprint context, active project state)
    "short":     0.10,   # half-life ~7 days   (task context, daily ops)
    "ephemeral": 0.25,   # half-life ~2.8 days (API down, build broken, PR open)
}
```

`short` and `ephemeral` rates are slightly tightened from current values to
accelerate pruning of event records that are no longer operationally relevant.

### 3.2 Semantic Decay

Semantic memories should be **stable until superseded**. Confidence decay is
inappropriate — a semantic fact doesn't become less true over time merely by
aging.

Instead, semantic memories use **staleness detection** rather than decay:

```python
SEMANTIC_STALENESS_THRESHOLD_DAYS = {
    "permanent": None,    # never stale
    "long":      180,     # flag for review after 6 months
    "medium":    60,      # flag for review after 2 months
    "short":     21,      # flag for review after 3 weeks
    "ephemeral": 7,       # flag for review after 1 week
}
```

When a semantic memory exceeds its staleness threshold, hippocampus.py should
emit a `stale_context` event (already a valid `event_type`) referencing the
memory ID, rather than decaying confidence directly. This surfaces it for
review without silently degrading correctness.

```python
if memory_type == 'semantic':
    threshold = SEMANTIC_STALENESS_THRESHOLD_DAYS.get(temporal_class)
    if threshold and elapsed_days > threshold:
        emit_stale_context_event(memory_id, elapsed_days)
    # Do NOT decay confidence
    continue
```

**Why not decay semantic at all:** Decaying a fact like "the auth service
requires JWT signing with RS256" from confidence 1.0 to 0.3 after 60 days
causes retrieval ranking degradation and eventual incorrect retirement. That
fact hasn't become less true; it may have become outdated if someone changed
the signing algorithm. The correct response is to supersede it with a new
memory when the change happens, not to silently decay the old one.

### 3.3 Confidence Boost on Recall

The existing recall-boost logic (reconsolidation model) applies to both types,
but with different ceilings:

| Type | Recall boost | Max confidence |
|------|-------------|----------------|
| Episodic | +0.05 per recall (capped at 0.95) | 0.95 |
| Semantic | +0.02 per recall (capped at 1.0) | 1.0 |

Episodic boost is larger because recall indicates the event is still
operationally relevant, justifying extended retention. Semantic has a smaller
boost because semantic memories should be validated by supersession, not
recall frequency.

---

## 4. Consolidation Differences

Neuroscience (Squire, 1992; Nadel & Moscovitch, 1997) describes hippocampal
consolidation as converting episodic events into cortical semantic schemas
over time. For the memory spine, this translates to:

### 4.1 Episodic → Semantic Promotion

When the hippocampus consolidation pass identifies a cluster of related episodic
memories with a consistent pattern, it should be able to **synthesize a semantic
memory** from that pattern rather than merely compressing the episodic records.

Current `cmd_consolidate` (hippocampus.py) compresses similar memories into a
single memory, but outputs the same type/category as inputs. With bifurcation:

```python
def consolidate_cluster(db, cluster, agent_id, dry_run=False):
    # Existing: compress cluster into one memory
    # New: if all inputs are episodic and pattern-consistent, emit semantic

    all_episodic = all(m['memory_type'] == 'episodic' for m in cluster)
    if all_episodic and len(cluster) >= 3:
        # Synthesize a semantic memory from the repeated pattern
        semantic_content = call_llm_semantic_synthesis(cluster)
        if semantic_content:
            write_semantic_memory(db, semantic_content, cluster, agent_id)
            # Retire episodic originals (they've been absorbed into schema)
            retire_cluster(db, [m['id'] for m in cluster])
            return
    # Fallback: standard episodic-to-episodic compression
    standard_consolidate(db, cluster, agent_id, dry_run)
```

The LLM prompt for semantic synthesis differs from compression:

```
You are extracting a stable fact from a series of events.
Given these {n} episodic event records, identify the underlying
general truth or convention they collectively demonstrate.
Output a single semantic statement of that truth (not a summary
of the events). If no stable semantic fact can be extracted,
output null.
```

This is Wave 4 territory for full implementation, but the schema and the
decision framework belong in this Wave 3 design.

### 4.2 Semantic Consolidation

Semantic memories consolidate differently: they should not be merged by
topical similarity alone, but only when one **supersedes** another. The
existing `cmd_memory_replace` / `supersedes_id` mechanism handles this
correctly and should remain the primary path for semantic updates.

Cluster-based compression of semantic memories is risky (may lose precision)
and should be rate-limited: only merge semantic memories when confidence
similarity is very high (>0.9) AND content cosine similarity is >0.92.

---

## 5. Retrieval Query Paths

### 5.1 Query Intent Classification

Queries naturally divide into two types:

| Query form | Intent | Optimal retrieval |
|-----------|--------|------------------|
| "what is X?" / "how does X work?" | Semantic — looking for stable facts | Filter `memory_type='semantic'` first |
| "what happened with X?" / "when did X?" / "has X ever?" | Episodic — looking for events | Filter `memory_type='episodic'` first |
| General search (no clear temporal/factual cue) | Mixed | Both, rank semantic higher for definitions, episodic higher for status |

### 5.2 `brainctl search` Routing

Add a `--type` filter to `brainctl search` and `brainctl vsearch`:

```bash
# Semantic fact lookup
brainctl -a hermes search "API rate limit" --type semantic

# Episodic event lookup
brainctl -a hermes search "Hermes deployed" --type episodic

# Default (both, with type-aware ranking)
brainctl -a hermes search "deployment"
```

In the default (mixed) case, retrieval scoring should be adjusted:

```python
# Semantic boost for definitional queries (heuristic: short queries, no verbs)
# Episodic boost for temporal queries (heuristic: past tense, "when", "did")

type_boost = {
    'semantic': 1.2 if is_definitional_query(query) else 1.0,
    'episodic': 1.2 if is_temporal_query(query) else 1.0,
}
final_score = base_score * type_boost[row['memory_type']]
```

### 5.3 Temporal Context Injection

`brainctl temporal-context` (used by agents at session start) should
distinguish between:

- **Semantic context window**: the current stable world-model — top-N
  high-confidence semantic memories for the current project scope.
- **Episodic context window**: recent event history — N most recent episodic
  memories within the active epoch.

These should be surfaced as separate sections in the context output so agents
can distinguish "what is true" from "what recently happened."

---

## 6. Migration Path

### Step 1 — Schema Migration (Day 0, ~5 minutes)

```sql
-- Apply to brain.db
ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episodic'
  CHECK (memory_type IN ('episodic', 'semantic'));

CREATE INDEX idx_memories_type ON memories(memory_type);
```

No FTS5 trigger changes required — `memory_type` is not in the FTS index
(not needed for full-text search).

### Step 2 — Heuristic Reclassification (Day 0, ~10 minutes)

Run the heuristic SQL from §2.2. Produces an approximate but safe starting
classification. No manual review required before proceeding.

### Step 3 — `brainctl` CLI Updates (Day 1–2)

- `memory add`: add `--type {episodic,semantic}` flag (default: `episodic`)
- `memory list`: add `--type` filter
- `memory search` / `vsearch`: add `--type` filter
- Output: include `memory_type` field in all `--json` outputs

### Step 4 — Hippocampus Decay Update (Day 2–3)

In `hippocampus.py::cmd_decay` and `apply_decay`:

```python
memory_type = row.get("memory_type", "episodic")
if memory_type == 'semantic':
    # Staleness detection only — no confidence decay
    check_semantic_staleness(db, row, now, args)
    stats["skipped_semantic"] += 1
    continue
# Existing episodic decay logic continues unchanged
rate = DECAY_RATES_EPISODIC.get(temporal_class)
```

### Step 5 — Temporal Context Separation (Day 3–4)

Update `cmd_temporal_context` in brainctl to output two distinct sections:
`semantic_context` and `episodic_context`.

### Step 6 — Episodic→Semantic Promotion (Wave 4)

The full LLM-based episodic-to-semantic synthesis described in §4.1 is
deferred. Schema and hippocampus hooks should be in place to enable it without
further structural changes.

---

## 7. Constraints Verification

| Constraint | Status |
|-----------|--------|
| SQLite + FTS5 + sqlite-vec architecture | ✅ Column addition; no structural change |
| No breaking brainctl interface changes | ✅ All new flags default to backward-compatible values |
| Memory type field additive-only | ✅ ALTER TABLE + default = 'episodic' |
| Existing decay algorithm preserved for episodic | ✅ Semantic path branches out; episodic path unchanged |
| Tuple-safe migration | ✅ Heuristic SQL; no data loss |
| FTS5 trigger compatibility | ✅ `memory_type` not added to FTS index; triggers unchanged |
| vec_memories join compatibility | ✅ Column addition preserves rowid mapping |

---

## 8. Open Questions for Hermes / Board Review

1. **Default classification direction**: Should new agent-written memories
   without `--type` default to `'episodic'` (conservative — prefer
   faster decay) or `'semantic'` (aggressive — prefer retention)?
   Recommendation: `'episodic'` default with brainctl warning when category
   suggests semantic content.

2. **Staleness threshold ownership**: Who decides when a stale semantic
   memory should be superseded? Automated (Prune) or human-in-loop? Recommendation:
   emit `stale_context` event → assign to Scribe 2 for review queue.

3. **Dual type restriction**: Should any categories be restricted to a
   single type? (e.g., `identity` always semantic, `task_update` always
   episodic) Recommendation: soft guidance in docs only; do not hard-enforce
   at the schema level to preserve agent flexibility.

4. **FTS5 index inclusion**: Should `memory_type` be added to the FTS5
   index to allow `... MATCH 'semantic'` queries? Recommendation: No —
   use a SQL `WHERE memory_type = ?` filter instead; FTS tokenization of
   'episodic' / 'semantic' adds noise.

---

## 9. Metrics for Evaluating Success

| Metric | Baseline (today) | Target (30 days post-deploy) |
|--------|-----------------|------------------------------|
| Episodic memories retired/week | (measure) | +30% vs. baseline |
| Semantic memories incorrectly retired | (measure) | 0 |
| Retrieval relevance for semantic queries | (measure) | +15% via self-assessment |
| `stale_context` events emitted/week | 0 | ~5–15 (indicates active staleness detection) |
| Memory store total row count | (measure) | Stable or decreasing |

Baseline measurements should be taken immediately before migration using
`brainctl stats` and a sample retrieval relevance audit.

---

## 10. References

- Tulving, E. (1972). Episodic and semantic memory. In E. Tulving & W. Donaldson (Eds.),
  *Organization of memory* (pp. 381–403). Academic Press.
- Squire, L.R. (1992). Memory and the hippocampus: A synthesis from findings with rats,
  monkeys, and humans. *Psychological Review*, 99(2), 195–231.
- Nadel, L., & Moscovitch, M. (1997). Memory consolidation, retrograde amnesia and the
  hippocampal complex. *Current Opinion in Neurobiology*, 7(2), 217–227.
- Wave 1 decay algorithm design: `~/agentmemory/TEMPORAL_DESIGN.md`
- Wave 2 associative memory report: `~/agentmemory/research/wave2/09_associative_memory_analogical_reasoning.md`
- brain.db schema: `~/agentmemory/db/brain.db` (`.schema` command)
- hippocampus.py: `~/agentmemory/bin/hippocampus.py`

---

## Appendix A: Quick-Reference SQL

```sql
-- Count by type after migration
SELECT memory_type, COUNT(*), AVG(confidence)
FROM memories WHERE retired_at IS NULL
GROUP BY memory_type;

-- Find likely misclassified memories (episodic with permanent temporal_class)
SELECT id, category, temporal_class, content
FROM memories
WHERE memory_type = 'episodic'
  AND temporal_class = 'permanent'
  AND retired_at IS NULL
LIMIT 20;

-- Semantic memories approaching staleness
SELECT id, category, temporal_class, confidence,
       ROUND(julianday('now') - julianday(updated_at), 0) AS days_old,
       content
FROM memories
WHERE memory_type = 'semantic'
  AND retired_at IS NULL
  AND (
    (temporal_class = 'long'   AND julianday('now') - julianday(updated_at) > 180) OR
    (temporal_class = 'medium' AND julianday('now') - julianday(updated_at) > 60)  OR
    (temporal_class = 'short'  AND julianday('now') - julianday(updated_at) > 21)
  )
ORDER BY days_old DESC;

-- Episodic candidates for semantic promotion (high-confidence clusters)
SELECT category, scope, COUNT(*) AS cluster_size, AVG(confidence) AS avg_conf
FROM memories
WHERE memory_type = 'episodic'
  AND retired_at IS NULL
  AND confidence > 0.7
GROUP BY category, scope
HAVING cluster_size >= 3
ORDER BY avg_conf DESC, cluster_size DESC;
```

---

*Report complete. Implementation can begin with Step 1 (schema migration) immediately.*
*Step 6 (LLM-based promotion) is flagged for Wave 4 scoping.*
