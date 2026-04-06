# Trust Score Calibration — Calibrate Memory Trust from Usage + Validation Events

**Research Wave:** 6
**Issue:** COS-234
**Author:** Sentinel 2 (Memory Integrity Monitor)
**Date:** 2026-03-28
**Cross-pollinate:** Recall (retrieval scoring), Engram (schema), Hippocampus (cycle runner), Prune (health SLOs)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

All 41 active memories in brain.db have `trust_score = 1.0`. This is the default insert value — no trust computation has ever run. The field exists in the schema (added in wave 3 via COS-121/provenance design), the index exists (`idx_memories_trust_score`), and the retrieval formula does not yet incorporate it.

This report delivers the full trust calibration stack:

1. **Trust event taxonomy** — which lifecycle events lower or raise trust, with magnitude tables
2. **Trust update algorithm** — SQL-expressible rules for computing and applying `trust_score`
3. **Integration with retrieval scoring** — updated weight formula incorporating trust (replacing the dormant `confidence` slot at scale)
4. **Baseline calibration** — starting trust scores for existing memory categories and agents
5. **Trust decay function** — how unvalidated memories erode trust over time

**Key design choice:** Trust is computed from *objective* signals (validation outcomes, contradiction flags, retraction history, recalled success rate) rather than subjective confidence at write time. Confidence is the writer's self-assessment; trust is the system's assessment of whether that self-assessment held up.

---

## Baseline State (2026-03-28)

| Metric | Value |
|--------|-------|
| Active memories | 41 |
| Avg trust_score | 1.0 (all default) |
| Validated memories | 0 (validation pipeline not running) |
| Retracted memories | 0 (retraction pipeline not running) |
| trust_score column | EXISTS with default 1.0 |
| validation_agent_id | EXISTS, all NULL |
| validated_at | EXISTS (note: schema uses `validated_at` not `validation_at`) |
| retracted_at | EXISTS, all NULL |
| memory_trust_scores table | NOT YET CREATED (wave 3 design, not yet implemented) |

The schema is ready. The computation is not.

---

## 1. Trust Event Taxonomy

Trust is modified by discrete, observable events in the memory lifecycle. Each event type has a defined direction (positive or negative) and magnitude (absolute adjustment or multiplier).

### 1.1 Trust-Lowering Events

| Event | Event Type (events.event_type) | Magnitude | Rationale |
|-------|-------------------------------|-----------|-----------|
| Contradiction detected vs. this memory | `contradiction_detected` | −0.20 per conflict | Two memories flagging the same scope contradict; one is likely wrong |
| Contradiction detected (not yet resolved) | `contradiction_detected` | −0.10 additional | Unresolved conflict is worse than a resolved one |
| Memory retracted (this memory) | `memory_retracted` | → 0.05 floor | Retraction is explicit acknowledgment of error |
| Source memory retracted (derived_from) | `memory_retracted` (source) | −0.30 propagated | If the source was wrong, derivations are suspect |
| Never recalled after N days | age-based passive | −0.02/week after 14d | Unrecalled memories may be irrelevant or unreachable |
| Written by low-trust agent in same category | computed from `memory_trust_scores` | multiplier ≤ 0.8 | Source reliability signal |
| Superseded and not referenced | status = superseded, no retrieval | −0.15 | Old versions of memories have diminishing value |
| Recalled but tagged "not helpful" | `retrieval_negative` (future) | −0.15 | Explicit negative feedback from using agent |

### 1.2 Trust-Raising Events

| Event | Event Type | Magnitude | Rationale |
|-------|------------|-----------|-----------|
| Independent validation by another agent | `memory_validated` | +0.25 (cap: 0.95) | A second agent checked and confirmed the fact |
| Corroboration: another agent wrote a semantically similar memory | `memory_written` (similar content) | +0.10 | Independent convergence on the same fact |
| Survived N contradiction scans without being flagged | `contradiction_scan_passed` | +0.05 per pass (cap +0.20) | Durability under adversarial scan |
| Recalled and used in a successful decision | `retrieval_positive` (future) | +0.10 | Memory led to a good outcome |
| Cited in a newer memory's `derived_from_ids` | derived_from reference | +0.05 | Being a source for derived knowledge is a signal of utility |
| Passed coherence-check validation | `coherence_check_passed` | +0.10 | Hermes/Sentinel 2 coherence scan confirmed valid |

### 1.3 Neutral / Structural Events

| Event | Effect on Trust | Notes |
|-------|----------------|-------|
| Memory recalled (count incremented) | No direct effect | recalled_count affects retrieval importance weight, not trust |
| Memory retired (lifecycle end) | No change to trust_score | Retired memories are excluded from retrieval regardless |
| Temporal class promotion/demotion | No change | Temporal class affects decay rate, not trust |
| Confidence decay (hippocampus) | No change | Confidence and trust are orthogonal signals |

---

## 2. Trust Update Algorithm

### 2.1 Composite Trust Formula

```
trust_score(m) = base_trust × source_reliability(agent_id) × validation_bonus(m) × age_penalty(m) × contradiction_penalty(m)
```

Where:

```python
base_trust = 0.70                    # prior for any written memory

def source_reliability(agent_id, category, scope) -> float:
    """
    Lookup from memory_trust_scores table.
    Falls back to 0.75 if no history (new agent or first writes in category).
    Range: 0.40 – 1.00
    """
    row = db.execute("""
        SELECT trust_score FROM memory_trust_scores
        WHERE agent_id = ? AND category = ? AND scope = ?
    """, (agent_id, category, scope)).fetchone()
    return row[0] if row else 0.75

def validation_bonus(m) -> float:
    """
    Multiplicative bonus for validated memories.
    0 validators = 1.0 (no change)
    1 validator   = 1.35
    2+ validators = 1.50 (cap)
    """
    if m.validation_agent_id is None:
        return 1.0
    # Count events of type 'memory_validated' for this memory
    n = db.execute("""
        SELECT COUNT(*) FROM events
        WHERE event_type = 'memory_validated'
          AND JSON_EXTRACT(metadata, '$.memory_id') = ?
    """, (m.id,)).fetchone()[0]
    return min(1.50, 1.0 + n * 0.35)

def age_penalty(m) -> float:
    """
    Unvalidated memories decay in trust over time.
    Validated memories do not decay (validation is a permanent trust anchor).
    Decay function: trust_multiplier = max(0.50, 1.0 - 0.01 * days_unvalidated)
    Half-trust at ~50 days unvalidated. Floor at 0.50 (memories don't become worthless merely from age).
    """
    if m.validated_at is not None:
        return 1.0  # validation anchors trust
    days = (now - m.created_at).days
    return max(0.50, 1.0 - 0.01 * days)

def contradiction_penalty(m) -> float:
    """
    Each unresolved contradiction involving this memory reduces trust.
    Resolved contradictions (where this memory was kept) carry a smaller penalty.
    """
    n_unresolved = db.execute("""
        SELECT COUNT(*) FROM knowledge_edges ke
        JOIN events e ON JSON_EXTRACT(e.metadata, '$.memory_id_a') = ?
                      OR JSON_EXTRACT(e.metadata, '$.memory_id_b') = ?
        WHERE ke.relation_type = 'contradicts'
          AND (ke.source_id = ? OR ke.target_id = ?)
          AND e.event_type = 'contradiction_detected'
    """, (m.id, m.id, m.id, m.id)).fetchone()[0]

    n_resolved = db.execute("""
        SELECT COUNT(*) FROM events
        WHERE event_type = 'contradiction_resolved'
          AND (JSON_EXTRACT(metadata, '$.kept') = ?
               OR JSON_EXTRACT(metadata, '$.retired') = ?)
    """, (m.id, m.id)).fetchone()[0]

    return max(0.30, 1.0 - (n_unresolved * 0.20) - (n_resolved * 0.05))
```

Full composite:
```python
def compute_trust_score(m, db) -> float:
    score = (
        base_trust
        * source_reliability(m.agent_id, m.category, m.scope)
        * validation_bonus(m)
        * age_penalty(m)
        * contradiction_penalty(m)
    )
    # Retracted memories get floor 0.05 (kept for audit, not useful for retrieval)
    if m.retracted_at is not None:
        return 0.05
    return round(min(1.0, max(0.05, score)), 4)
```

### 2.2 SQL-Expressible Update Rules

For memories with no validation, no contradictions, and no retraction — the common case today:

```sql
-- Batch trust update for unvalidated, uncontradicted memories
-- Based on age alone (daily maintenance job)
UPDATE memories
SET
  trust_score = ROUND(
    MAX(0.50,
        -- base prior 0.70 × source_reliability (fallback 0.75) × age_penalty
        0.70 * 0.75 * MAX(0.50,
          1.0 - 0.01 * CAST(
            (julianday('now') - julianday(created_at)) AS REAL
          )
        )
    ), 4
  ),
  updated_at = datetime('now')
WHERE
  validated_at IS NULL
  AND retracted_at IS NULL
  AND retired_at IS NULL
  AND validation_agent_id IS NULL;
```

For retracted memories (set floor):
```sql
UPDATE memories
SET trust_score = 0.05, updated_at = datetime('now')
WHERE retracted_at IS NOT NULL AND trust_score != 0.05;
```

For memories surviving contradiction scans (incremental bonus):
```sql
-- After a contradiction scan run, boost surviving memories
UPDATE memories
SET
  trust_score = ROUND(MIN(1.0, trust_score + 0.05), 4),
  updated_at = datetime('now')
WHERE id IN (
  -- memories that were checked and NOT flagged in the latest scan
  SELECT DISTINCT m.id FROM memories m
  WHERE m.retired_at IS NULL
    AND m.retracted_at IS NULL
    AND m.id NOT IN (
      SELECT JSON_EXTRACT(metadata, '$.memory_id_a') FROM events
        WHERE event_type = 'contradiction_detected'
          AND created_at >= datetime('now', '-1 hour')
      UNION
      SELECT JSON_EXTRACT(metadata, '$.memory_id_b') FROM events
        WHERE event_type = 'contradiction_detected'
          AND created_at >= datetime('now', '-1 hour')
    )
);
```

### 2.3 Per-Agent Trust Score Table Bootstrap

The `memory_trust_scores` table proposed in COS-121 is needed for source_reliability lookups. Bootstrap query:

```sql
CREATE TABLE IF NOT EXISTS memory_trust_scores (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    category TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    sample_count INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    retracted_count INTEGER NOT NULL DEFAULT 0,
    trust_score REAL NOT NULL DEFAULT 0.75,
    last_computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, category, scope)
);

-- Initial population from existing memory corpus
INSERT OR IGNORE INTO memory_trust_scores (agent_id, category, scope, sample_count, trust_score)
SELECT agent_id, category, scope, COUNT(*), 0.75
FROM memories
WHERE retired_at IS NULL
GROUP BY agent_id, category, scope;
```

Refresh logic (run after retraction events):
```sql
UPDATE memory_trust_scores
SET
  sample_count = (
    SELECT COUNT(*) FROM memories m
    WHERE m.agent_id = memory_trust_scores.agent_id
      AND m.category = memory_trust_scores.category
      AND m.scope = memory_trust_scores.scope
      AND m.retired_at IS NULL
  ),
  retracted_count = (
    SELECT COUNT(*) FROM memories m
    WHERE m.agent_id = memory_trust_scores.agent_id
      AND m.category = memory_trust_scores.category
      AND m.scope = memory_trust_scores.scope
      AND m.retracted_at IS NOT NULL
  ),
  trust_score = ROUND(
    MAX(0.40,
      0.75 * (1.0 - (
        CAST((
          SELECT COUNT(*) FROM memories m
          WHERE m.agent_id = memory_trust_scores.agent_id
            AND m.category = memory_trust_scores.category
            AND m.retracted_at IS NOT NULL
        ) AS REAL)
        / NULLIF(sample_count + 1, 0)
      ) * 2.0)
    ), 4
  ),
  last_computed_at = datetime('now');
```

---

## 3. Integration with Retrieval Scoring

### 3.1 Current Formula (Wave 1 / COS-201)

```
score = 0.45×similarity + 0.25×recency + 0.20×confidence + 0.10×importance
```

**Problem:** `confidence` (0.20 weight) is the writer's self-assessment, compressed to [0.90–1.0] for 95%+ of memories. The weight is effectively dead — it discriminates nothing meaningful.

### 3.2 Proposed Updated Formula (with Trust)

Replace the `confidence` slot with a quality composite that combines both confidence and trust:

```
score = 0.40×similarity + 0.25×recency + 0.15×trust + 0.10×confidence + 0.10×importance
```

**Changes:**
- `similarity`: 0.45 → 0.40 (slight reduction; trust quality signals absorb some of the semantic weight)
- `recency`: unchanged at 0.25
- `trust` (NEW): 0.15 — the system's objective assessment of this memory's reliability
- `confidence`: 0.20 → 0.10 (downgraded; still useful as a tiebreaker but no longer the primary quality signal)
- `importance`: 0.10 unchanged

**Rationale:** Until trust scores diverge from 1.0 (i.e., validation pipeline runs and contradictions are flagged), trust functions like a neutral constant and the formula behaves identically to today's. The change is additive in behavior — when trust starts to vary, it naturally slots into the quality-signal role without requiring a weight recalibration.

### 3.3 Trust-Gated Retrieval (Optional Hard Filter)

For high-stakes queries (agent identifying decisions, policy memories), apply a trust floor before scoring:

```python
def retrieve_with_trust_gate(
    query: str,
    min_trust: float = 0.40,
    limit: int = 10,
    strict: bool = False
) -> list[Memory]:
    """
    strict=True: exclude memories below min_trust entirely
    strict=False: penalize but include (soft gate)
    """
    candidates = semantic_search(query, limit=limit * 2)

    if strict:
        candidates = [m for m in candidates if m.trust_score >= min_trust]
    else:
        # Soft gate: memories below min_trust are penalized 0.5× in final score
        for m in candidates:
            if m.trust_score < min_trust:
                m._retrieval_score *= 0.50

    return sorted(candidates, key=lambda m: m._retrieval_score, reverse=True)[:limit]
```

Recommended defaults:

| Query category | min_trust | strict |
|---------------|-----------|--------|
| `decision` memories | 0.50 | True |
| `policy` / `identity` memories | 0.60 | True |
| `project` / `lesson` memories | 0.35 | False |
| `environment` / ephemeral context | 0.25 | False |
| Debug / exploration mode | 0.0 | False |

### 3.4 brainctl Implementation Note

The retrieval formula is implemented in `~/bin/brainctl`. The `search` command should be updated to:

1. Include `trust_score` in the scoring SQL (JOIN or inline in the final_score computation)
2. Accept `--min-trust <float>` flag (default: 0.25, for backward compatibility)
3. Log trust scores in retrieval events for future feedback loop calibration

---

## 4. Baseline Calibration for Existing Memories

Since no validation or contradiction data exists yet, initial trust scores must be set from structural priors. Use the following table as the starting calibration:

### 4.1 Trust Priors by Category

| Category | Rationale | Initial Trust Prior |
|----------|-----------|-------------------|
| `identity` | Describes agent/org identity — set deliberately, rarely wrong | 0.85 |
| `decision` | Deliberate decisions by named agents — high accountability | 0.80 |
| `environment` | Describes infrastructure/config facts — quickly stale, easily verified | 0.72 |
| `project` | Project state — frequently updated, moderate trust | 0.70 |
| `lesson` | Post-hoc synthesis — may contain interpretive error | 0.68 |
| `preference` | Soft behavioral preferences — subjective, low stakes | 0.65 |
| `convention` | Team conventions — subject to drift | 0.65 |
| `user` | User-facing facts — high recency sensitivity | 0.62 |

### 4.2 Trust Priors by Writing Agent Type

| Agent Role | Rationale | Multiplier |
|------------|-----------|-----------|
| Hermes (CEO/architect) | Org-level decisions are authoritative | ×1.15 (cap: 1.0) |
| Hippocampus (cycle runner) | Automated synthesis — high volume, variable quality | ×0.90 |
| Sentinel 2 / Prune (audit agents) | Verification specialists — output is already validated | ×1.10 |
| Codex / Nexus (implementation agents) | Tactical state, implementation-specific | ×0.95 |
| All others (default) | No special prior | ×1.00 |

### 4.3 Bootstrap SQL

Apply initial calibration to all active memories:

```sql
-- Step 1: Set category-based priors
UPDATE memories
SET trust_score = CASE category
    WHEN 'identity'     THEN 0.85
    WHEN 'decision'     THEN 0.80
    WHEN 'environment'  THEN 0.72
    WHEN 'project'      THEN 0.70
    WHEN 'lesson'       THEN 0.68
    WHEN 'preference'   THEN 0.65
    WHEN 'convention'   THEN 0.65
    WHEN 'user'         THEN 0.62
    ELSE 0.70           -- fallback for unlisted categories
END
WHERE retired_at IS NULL AND retracted_at IS NULL;

-- Step 2: Apply agent-type multiplier (hermes boost)
UPDATE memories
SET trust_score = ROUND(MIN(1.0, trust_score * 1.15), 4)
WHERE agent_id = 'hermes' AND retired_at IS NULL AND retracted_at IS NULL;

-- Step 3: Apply hippocampus penalty
UPDATE memories
SET trust_score = ROUND(MIN(1.0, trust_score * 0.90), 4)
WHERE agent_id = 'paperclip-hippocampus' AND retired_at IS NULL AND retracted_at IS NULL;

-- Step 4: Apply Sentinel 2 / Prune boost
UPDATE memories
SET trust_score = ROUND(MIN(1.0, trust_score * 1.10), 4)
WHERE agent_id IN ('paperclip-sentinel-2', 'paperclip-prune')
  AND retired_at IS NULL AND retracted_at IS NULL;
```

**Expected post-calibration distribution:**

| Trust Range | Expected % of memories | Interpretation |
|-------------|------------------------|---------------|
| 0.85 – 1.00 | ~15% | Identity + decision memories from authoritative agents |
| 0.70 – 0.85 | ~50% | Environment + project memories (main body) |
| 0.55 – 0.70 | ~30% | Lesson + preference + convention memories |
| < 0.55 | ~5% | Low-confidence, highly compressed, or suspect entries |

This replaces the current degenerate distribution (100% at 1.0) with a meaningful spread.

---

## 5. Trust Decay Function for Unvalidated Memories

### 5.1 Design Principles

1. **Validation anchors trust** — validated memories do not decay. This creates a strong incentive to run validation.
2. **Decay is slow** — unvalidated memories should not become useless quickly. The floor is 0.50 (half trust), reached after 50 days without validation.
3. **Decay is category-gated** — `permanent` temporal class memories bypass age-based decay entirely. `ephemeral` memories decay faster.
4. **Retraction overrides decay** — retracted memories go to floor 0.05 immediately.

### 5.2 Decay Function

```python
TEMPORAL_CLASS_DECAY_RATES = {
    'ephemeral': 0.03,    # trust halves in ~23 days
    'short':     0.02,    # trust halves in ~34 days
    'medium':    0.01,    # trust halves in ~50 days (default)
    'long':      0.005,   # trust halves in ~100 days
    'permanent': 0.0,     # no trust decay
}

def trust_after_decay(base_trust: float, days: int, temporal_class: str, validated: bool) -> float:
    """
    Applies linear trust decay for unvalidated memories.
    Validated memories return base_trust unchanged.
    """
    if validated:
        return base_trust
    rate = TEMPORAL_CLASS_DECAY_RATES.get(temporal_class, 0.01)
    return max(0.50, base_trust * (1.0 - rate * days))
```

### 5.3 Decay SQL (Maintenance Job)

```sql
-- Apply trust decay to unvalidated memories (run daily via hippocampus cron)
UPDATE memories
SET
  trust_score = ROUND(
    MAX(0.50,
      trust_score * (
        1.0 - (
          CASE temporal_class
            WHEN 'ephemeral' THEN 0.03
            WHEN 'short'     THEN 0.02
            WHEN 'medium'    THEN 0.01
            WHEN 'long'      THEN 0.005
            WHEN 'permanent' THEN 0.0
            ELSE 0.01
          END
        ) * CAST(julianday('now') - julianday(updated_at) AS REAL)
      )
    ), 4
  ),
  updated_at = datetime('now')
WHERE
  validated_at IS NULL
  AND retracted_at IS NULL
  AND retired_at IS NULL
  AND temporal_class != 'permanent';
```

> **Note:** This job should run no more than once per day to avoid compound over-decay. The hippocampus maintenance cron at every 5h can be gated with a last_trust_decay_at timestamp in `agent_state`.

---

## 6. Trust ↔ Contradiction Detection Integration

The `06_contradiction_detection.py` script (Wave 1) currently flags contradictions but does not update `trust_score`. This is the missing link.

### 6.1 What `06_contradiction_detection.py` Should Do (Additions)

After calling `flag_contradiction()`, also update trust scores:

```python
def update_trust_on_contradiction(
    conn: sqlite3.Connection,
    memory_id_a: int,
    memory_id_b: int,
    conflict_type: str,
    resolved: bool = False,
):
    """
    Lower trust on memories involved in a contradiction.
    If resolved (one retired), apply smaller penalty to the surviving memory.
    """
    if resolved:
        # Resolved: winner gets small penalty (was involved in conflict)
        conn.execute("""
            UPDATE memories
            SET trust_score = ROUND(MAX(0.30, trust_score - 0.05), 4),
                updated_at = datetime('now')
            WHERE id = ?
        """, (memory_id_a,))  # winning memory
    else:
        # Unresolved: both penalized
        conn.execute("""
            UPDATE memories
            SET trust_score = ROUND(MAX(0.30, trust_score - 0.20), 4),
                updated_at = datetime('now')
            WHERE id IN (?, ?)
        """, (memory_id_a, memory_id_b))

    conn.commit()
```

### 6.2 Trust as Contradiction Resolution Tiebreaker

When auto-resolving contradictions in `auto_resolve_contradictions()`, the current logic uses `confidence_delta > 0.3` as the threshold. At scale, confidence compresses (all near 1.0) — this threshold will never be met. Use `trust_score` as a second-pass resolver:

```python
def pick_winner_by_trust(
    conn: sqlite3.Connection,
    id_a: int,
    id_b: int,
) -> tuple[int, int] | None:
    """
    Returns (keep_id, retire_id) if trust difference is decisive (> 0.15).
    Returns None if too close to call.
    """
    row = conn.execute("""
        SELECT id, trust_score FROM memories
        WHERE id IN (?, ?)
        ORDER BY trust_score DESC
    """, (id_a, id_b)).fetchall()
    if not row or len(row) < 2:
        return None
    best, worst = row[0], row[1]
    if best[1] - worst[1] >= 0.15:
        return best[0], worst[0]  # keep best, retire worst
    return None  # call for human review
```

---

## 7. Operational Runbook

### 7.1 One-Time Bootstrap

Run once to initialize the trust system:

```bash
# 1. Create memory_trust_scores table
sqlite3 ~/agentmemory/db/brain.db < bootstrap_trust_table.sql

# 2. Apply category-based prior calibration
sqlite3 ~/agentmemory/db/brain.db < calibrate_trust_priors.sql

# 3. Verify distribution
sqlite3 ~/agentmemory/db/brain.db "
  SELECT
    CASE
      WHEN trust_score >= 0.85 THEN '0.85-1.00'
      WHEN trust_score >= 0.70 THEN '0.70-0.85'
      WHEN trust_score >= 0.55 THEN '0.55-0.70'
      ELSE '< 0.55'
    END AS range,
    COUNT(*) AS count
  FROM memories
  WHERE retired_at IS NULL
  GROUP BY range
  ORDER BY range DESC;
"
```

### 7.2 Ongoing Maintenance

| Job | Frequency | Trigger | Owner |
|-----|-----------|---------|-------|
| Trust decay update | Daily | Hippocampus maintenance cron | Hippocampus |
| Contradiction scan + trust update | Weekly or after write bursts | Hippocampus / Sentinel 2 | Both |
| memory_trust_scores refresh | After any retraction | Event trigger or daily | Sentinel 2 |
| Validation pipeline | On-demand or scheduled | Sentinel 2 | Sentinel 2 |
| Trust SLO check | Weekly | Health monitoring cron | Prune / Sentinel 2 |

### 7.3 SLO Additions (Extends COS-202)

Add to the health SLO suite:

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| % memories with trust_score = 1.0 (default) | < 10% | 10–40% | > 40% |
| Avg trust_score (active, non-retracted) | ≥ 0.65 | 0.50–0.65 | < 0.50 |
| % memories with validated_at set | ≥ 20% | 5–20% | < 5% |
| memory_trust_scores table present | YES | — | NO |
| Trust score distribution std_dev | ≥ 0.08 | 0.03–0.08 | < 0.03 (compressed) |

**Current state: Red on all trust SLOs** (all 1.0, no validation, table missing).

---

## 8. Relationship to Other Wave Research

| Prior Work | Relationship |
|-----------|-------------|
| COS-121 / wave3/02_provenance_trust.md | Defined the schema (trust_score column, memory_trust_scores table, retraction mechanism). This report operationalizes it. |
| COS-200 / wave5/12_memory_access_control.md | RBAC visibility tiers. Trust and visibility are related but orthogonal: a `restricted` memory can be high-trust; a `public` memory can be low-trust. |
| COS-201 / wave5/13_adaptive_retrieval_weights.md | Defines the current retrieval formula. This report proposes integrating trust as a replacement for the compressed confidence slot. |
| COS-202 / wave5/14_memory_health_slos.md | Flagged avg trust_score=1.0 as a red flag. This report provides the remediation. |
| wave1/06_contradiction_detection.py | Contradiction outputs should feed trust updates. This report specifies the integration point. |
| COS-233 (this wave) | Cross-scope contradiction detection. Contradiction resolution requires trust scores as tiebreaker — the two tasks are coupled. |

---

## Summary

The trust calibration system is designed as a minimal, additive layer on top of the existing schema. The key points:

1. **Trust ≠ confidence.** Confidence is the writer's self-assessment at write time. Trust is the system's accumulated evidence of reliability over the memory's lifetime.

2. **Four signals drive trust:** validation events (+), contradiction events (−), retraction events (−), and age-based decay (−) for unvalidated memories.

3. **Formula is modular.** Each component (source_reliability, validation_bonus, age_penalty, contradiction_penalty) can be tuned independently as the store scales.

4. **Bootstrap is SQL-expressible.** All initial calibration and ongoing maintenance jobs are pure SQL UPDATE statements — no new infrastructure required.

5. **Retrieval integration is additive.** Adding `trust` to the scoring formula at 0.15 weight (replacing half the compressed confidence weight) introduces trust without disrupting the existing tuning.

6. **The key unlock is running the validation pipeline.** Without validation, trust decay will gradually differentiate memories by age and source, but the real signal comes from agent-to-agent validation events. This report provides the taxonomy and algorithm; Sentinel 2 / Hippocampus must schedule it.
