# Memory Provenance & Source Trust Chains
## Research Report — COS-121
**Author:** Sentinel 2 (Memory Integrity Monitor)
**Date:** 2026-03-28
**Target:** brain.db — Provenance and trust model for memories written by 22+ agents

---

## Executive Summary

Every memory in brain.db is asserted by an agent at a point in time. The current schema records `agent_id` but has no model for *how trustworthy* that agent is, *whether the fact was validated*, or *what happens downstream* when a source is found wrong. This report designs the minimal, additive changes needed to make the memory system provenance-aware: who said it, who verified it, how reliable that source is, and how to cascade corrections when a source is wrong.

**Central finding:** The existing schema is 80% ready. Four new columns on `memories`, one new table (`memory_trust_scores`), and a new `brainctl memory retract` command deliver the full provenance stack. No existing data needs rewriting. Migration is purely additive.

---

## 1. Provenance Schema

### 1.1 Current State

The `memories` table already captures:
- `agent_id` — who wrote the memory
- `confidence` — subjective confidence at write time (0.0–1.0)
- `supersedes_id` — chain of memory versions
- `retired_at` — soft delete

**Gaps:**
- No validation record (was anyone else asked to confirm this?)
- No distinction between *confidence in the fact* vs. *trust in the source*
- No retraction mechanism (retirement is ambiguous — expired vs. wrong)
- No provenance chain for derived memories ("I believe X because Armor told me Y")

### 1.2 New Columns — Additive Migration

```sql
-- Migration: additive only — safe on existing data
ALTER TABLE memories ADD COLUMN validation_agent_id TEXT REFERENCES agents(id);
ALTER TABLE memories ADD COLUMN validation_at TEXT;           -- ISO timestamp of validation
ALTER TABLE memories ADD COLUMN trust_score REAL;             -- NULL = not yet scored; 0.0–1.0
ALTER TABLE memories ADD COLUMN derived_from_ids TEXT;        -- JSON array of memory IDs this was inferred from
ALTER TABLE memories ADD COLUMN retracted_at TEXT;            -- set on soft retraction
ALTER TABLE memories ADD COLUMN retraction_reason TEXT;       -- free text: "source unreliable", "contradicted by X"
```

**Field semantics:**

| Field | Meaning | Default |
|---|---|---|
| `validation_agent_id` | Agent that independently verified this memory | NULL (unvalidated) |
| `validation_at` | When validation occurred | NULL |
| `trust_score` | Composite trust: source reliability × validation bonus × age decay | NULL (unscored) |
| `derived_from_ids` | JSON array `[memory_id, ...]` this was inferred from | NULL |
| `retracted_at` | Soft retraction timestamp (distinct from `retired_at`) | NULL |
| `retraction_reason` | Human-readable retraction cause | NULL |

**Why separate `retracted_at` from `retired_at`:**
- `retired_at` = normal lifecycle end (superseded, expired, stale)
- `retracted_at` = explicit correction event ("this was wrong")
Retracted memories must be preserved for audit; retired memories may be pruned.

### 1.3 New Table — `memory_trust_scores`

Per-agent, per-category trust scores computed from historical accuracy signals:

```sql
CREATE TABLE memory_trust_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    category TEXT NOT NULL,           -- matches memories.category
    scope TEXT NOT NULL DEFAULT 'global',
    sample_count INTEGER NOT NULL DEFAULT 0,   -- how many memories sampled
    correct_count INTEGER NOT NULL DEFAULT 0,  -- confirmed correct (validated or not retracted after N days)
    retracted_count INTEGER NOT NULL DEFAULT 0,
    trust_score REAL NOT NULL DEFAULT 0.5,     -- rolling score 0.0–1.0
    last_computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, category, scope)  -- replace: one row per agent+category+scope
);
```

This table stores pre-computed trust scores by agent × category, refreshed on a schedule (daily or after retraction events). Query cost at memory write time is a single indexed lookup.

### 1.4 Index on Provenance Fields

```sql
CREATE INDEX idx_memories_retracted ON memories(retracted_at) WHERE retracted_at IS NOT NULL;
CREATE INDEX idx_memories_trust_score ON memories(trust_score);
CREATE INDEX idx_memories_validation ON memories(validation_agent_id) WHERE validation_agent_id IS NOT NULL;
CREATE INDEX idx_mts_agent_category ON memory_trust_scores(agent_id, category);
```

---

## 2. Trust Propagation Algorithm

When a source memory is revised (trust drops or retraction occurs), memories *derived from it* must be flagged. The `knowledge_edges` table already provides the graph substrate. The `derived_from_ids` column on `memories` provides a direct dependency list.

### 2.1 Two Propagation Paths

**Path A — Direct derivation** (`derived_from_ids` column):
Memories that were explicitly constructed from source memory M carry M's ID in their `derived_from_ids` JSON array. This is O(1) to query per memory.

**Path B — Knowledge graph inference** (`knowledge_edges`):
Memories linked via `causal_chain_member`, `causes`, or `semantic_similar` edges may be semantically derived even without an explicit `derived_from_ids` record. This requires a graph walk.

### 2.2 Propagation Algorithm

```python
def propagate_trust_change(
    db: sqlite3.Connection,
    source_memory_id: int,
    new_trust_score: float,
    hops: int = 3,
    decay: float = 0.7
) -> list[dict]:
    """
    Propagates trust reduction from a revised source memory.
    Returns list of affected memory IDs with their computed trust impact.
    Touches no data — returns candidates for review.
    """
    affected = {}  # memory_id -> impact_score (0.0–1.0, lower = more affected)

    # Step 1: Direct derivation (from derived_from_ids column)
    direct = db.execute("""
        SELECT id, trust_score, derived_from_ids
        FROM memories
        WHERE retracted_at IS NULL
          AND derived_from_ids IS NOT NULL
          AND JSON_EACH.value = ?
        FROM memories, JSON_EACH(memories.derived_from_ids)
        WHERE JSON_EACH.value = CAST(? AS TEXT)
    """, (str(source_memory_id),)).fetchall()

    for row in direct:
        affected[row[0]] = {'trust_impact': new_trust_score, 'path': 'direct', 'hops': 1}

    # Step 2: Knowledge graph propagation
    frontier = {('memories', source_memory_id): 1.0}
    visited = set()

    for hop in range(1, hops + 1):
        next_frontier = {}
        hop_decay = decay ** hop

        for (table, node_id), activation in frontier.items():
            if (table, node_id) in visited:
                continue
            visited.add((table, node_id))

            # Find downstream edges (causal/derivation types weighted higher)
            edges = db.execute("""
                SELECT target_table, target_id, relation_type, weight
                FROM knowledge_edges
                WHERE source_table = ? AND source_id = ?
                  AND relation_type IN (
                    'causal_chain_member', 'causes', 'semantic_similar',
                    'derived_from', 'topical_tag'
                  )
            """, (table, node_id)).fetchall()

            relation_weights = {
                'causes': 1.0,
                'causal_chain_member': 0.9,
                'derived_from': 1.0,
                'semantic_similar': 0.5,
                'topical_tag': 0.2,
            }

            for (t_table, t_id, rel_type, edge_weight) in edges:
                if t_table != 'memories':
                    continue  # only propagate to memory nodes
                impact = activation * edge_weight * relation_weights.get(rel_type, 0.3) * hop_decay
                if t_id not in affected or affected[t_id]['trust_impact'] > (1.0 - impact):
                    affected[t_id] = {
                        'trust_impact': max(0.0, new_trust_score * (1.0 - impact)),
                        'path': f'graph:{rel_type}',
                        'hops': hop
                    }
                next_frontier[(t_table, t_id)] = activation * hop_decay

        frontier = next_frontier

    return [{'memory_id': mid, **data} for mid, data in affected.items()]
```

### 2.3 Propagation Modes

| Mode | When to use | Effect |
|---|---|---|
| **Flag only** | Source trust dropped, not retracted | Set `trust_score` on affected memories to propagated value |
| **Flag for review** | Source retracted, derivatives uncertain | Add tag `"provenance:suspect"` to derived memories |
| **Cascade retract** | Source provably wrong, derivatives are logic conclusions | Soft-retract derived memories with inherited `retraction_reason` |

Default: **Flag for review**. Cascade retract requires explicit operator confirmation (via `brainctl memory retract --cascade --confirm`).

---

## 3. Retraction Cascade

### 3.1 Identifying Suspect Memories

Given a set of suspect memory IDs (e.g., all memories written by a low-trust agent in a category):

```sql
-- Find all memories written by agent X in category Y that are not yet retracted
SELECT m.id, m.content, m.confidence, m.trust_score, m.derived_from_ids, m.created_at
FROM memories m
WHERE m.agent_id = :agent_id
  AND m.category = :category
  AND m.retracted_at IS NULL
  AND m.retired_at IS NULL;

-- Find downstream derivations via derived_from_ids
SELECT DISTINCT m.id, m.agent_id, m.content, m.derived_from_ids
FROM memories m, JSON_EACH(m.derived_from_ids) j
WHERE CAST(j.value AS INTEGER) IN (/* suspect memory ids */)
  AND m.retracted_at IS NULL;
```

### 3.2 Soft Retraction Protocol

Retraction is **never destructive**. The memory record remains intact with an audit trail:

```sql
-- Soft retract a memory
UPDATE memories
SET retracted_at = datetime('now'),
    retraction_reason = :reason,
    trust_score = 0.0
WHERE id = :memory_id;

-- Log the retraction as an event
INSERT INTO events (agent_id, event_type, summary, detail, metadata, project, importance)
VALUES (
    :retracting_agent_id,
    'memory_retracted',
    'Memory ' || :memory_id || ' retracted: ' || :reason,
    :detail,
    JSON_OBJECT(
        'memory_id', :memory_id,
        'original_agent_id', :original_agent_id,
        'retraction_type', :retraction_type,  -- 'direct' or 'cascade'
        'cascade_root_id', :root_memory_id    -- NULL for direct retractions
    ),
    :project,
    0.8
);
```

### 3.3 Cascade Retraction Logic

```python
def retraction_cascade(
    db: sqlite3.Connection,
    suspect_ids: list[int],
    reason: str,
    retracting_agent: str,
    auto_retract_hops: int = 1,  # only direct derivations auto-retracted
    flag_hops: int = 3           # indirect: flag for review only
) -> dict:
    """
    Performs soft retraction on suspect_ids and propagates:
    - Direct derivatives (hop=1): retracted automatically
    - Indirect derivatives (hop=2-3): flagged with 'provenance:suspect' tag
    Returns audit summary.
    """
    retracted = []
    flagged = []

    for memory_id in suspect_ids:
        # Direct retraction
        db.execute("""
            UPDATE memories SET retracted_at = datetime('now'),
            retraction_reason = ?, trust_score = 0.0
            WHERE id = ? AND retracted_at IS NULL
        """, (reason, memory_id))
        retracted.append(memory_id)
        _log_retraction_event(db, memory_id, retracting_agent, reason, 'direct')

    # Propagation
    affected = propagate_trust_change(db, suspect_ids[0], 0.0, hops=flag_hops)

    for item in affected:
        mid = item['memory_id']
        if item['hops'] <= auto_retract_hops:
            db.execute("""
                UPDATE memories SET retracted_at = datetime('now'),
                retraction_reason = ?, trust_score = 0.0
                WHERE id = ? AND retracted_at IS NULL
            """, (f'cascade from {suspect_ids}: {reason}', mid))
            retracted.append(mid)
            _log_retraction_event(db, mid, retracting_agent, reason, 'cascade')
        else:
            # Flag with tag instead of retracting
            _add_suspect_tag(db, mid)
            flagged.append(mid)

    db.commit()
    return {'retracted': retracted, 'flagged_for_review': flagged}
```

### 3.4 Retraction Audit Query

After retraction, operators can audit the full cascade:

```sql
SELECT
    m.id,
    m.agent_id,
    m.category,
    m.retracted_at,
    m.retraction_reason,
    e.summary AS event_summary,
    e.metadata
FROM memories m
JOIN events e ON e.metadata LIKE '%"memory_id": ' || m.id || '%'
WHERE m.retracted_at IS NOT NULL
  AND e.event_type = 'memory_retracted'
ORDER BY m.retracted_at DESC;
```

---

## 4. brainctl Interface

### 4.1 `brainctl memory retract`

```
brainctl memory retract --memory-id <id> --reason <text>
brainctl memory retract --agent-id <agent> --category <cat> --reason <text>
brainctl memory retract --agent-id <agent> --reason <text> --cascade --confirm
```

**Flags:**
| Flag | Description |
|---|---|
| `--memory-id <id>` | Retract a single memory by ID |
| `--agent-id <id>` | Retract all non-retracted memories by this agent |
| `--category <cat>` | Narrow by category (requires `--agent-id`) |
| `--reason <text>` | Required. Human-readable retraction reason |
| `--cascade` | Also retract direct derivations (hop=1). Dry-run by default |
| `--confirm` | Required with `--cascade` to execute (prevents accidents) |
| `--dry-run` | Show what would be retracted without writing (default for `--cascade`) |

**Output format:**
```
Retraction summary:
  Direct:       3 memories retracted
  Cascade (h1): 7 memories retracted
  Flagged (h2): 12 memories tagged 'provenance:suspect'

  Retracted IDs: [45, 46, 47, 88, 89, 90, ...]
  Flagged IDs:   [12, 13, 33, ...]
  Event logged: events.id = 412
```

### 4.2 `brainctl memory validate`

Validates a memory by recording a second-agent confirmation:

```
brainctl memory validate --memory-id <id>
brainctl memory validate --agent-id <me>  # validate all memories you can assess
```

Writes to `memories.validation_agent_id` and `memories.validation_at`. Increases trust score by a configured bonus (default: +0.2, capped at 1.0).

### 4.3 `brainctl memory trust-report`

Shows per-agent, per-category trust scores:

```
brainctl memory trust-report
brainctl memory trust-report --agent-id <id>
brainctl memory trust-report --category project
```

**Output:**
```
Agent Trust Report (as of 2026-03-28)

agent_id              category      scope      score   samples  retracted
-----------           ----------    --------   ------  -------  ---------
hermes                identity      global      0.97     34       0
paperclip-armor       project       global      0.84     12       1
paperclip-codex       convention    global      0.91     8        0
paperclip-sentinel-2  environment   global      0.88     5        0
```

### 4.4 `brainctl validate` Enhancement

Extend existing `brainctl validate` to include provenance checks:

```
Provenance checks:
  [OK]   memories with validation: 23/88 (26%)
  [WARN] memories with trust_score < 0.5: 4 memories (IDs: 12, 45, 78, 90)
  [OK]   retracted memories with audit event: 3/3
  [WARN] suspect-tagged memories not reviewed in 7d: 8
```

---

## 5. Trust Scoring Heuristics

Trust scores must be computable from data already in brain.db with no external signals.

### 5.1 Available Signals

| Signal | Source | Weight |
|---|---|---|
| Memory not retracted after N days | `memories.retracted_at IS NULL AND created_at < now-N` | Positive |
| Memory explicitly validated | `memories.validation_agent_id IS NOT NULL` | Strong positive |
| Memory superseded (corrected) | `memories.supersedes_id IS NOT NULL` (the old memory) | Negative |
| Memory retracted | `memories.retracted_at IS NOT NULL` | Strong negative |
| Memory high recall rate | `memories.recalled_count / (days since created)` | Weak positive |
| Agent's category accuracy history | Rolling rate from `memory_trust_scores` table | Prior |

### 5.2 Per-Memory Trust Score Formula

```
trust_score(m) = base_prior(agent, category)
               × validation_boost(m)
               × age_survival_factor(m)
               × retraction_penalty(m)
```

Where:

```python
def compute_memory_trust_score(db, memory_id: int) -> float:
    m = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()

    # 1. Base prior from historical agent+category accuracy
    prior = db.execute("""
        SELECT trust_score FROM memory_trust_scores
        WHERE agent_id = ? AND category = ? AND scope = ?
    """, (m['agent_id'], m['category'], m['scope'])).fetchone()
    base = prior['trust_score'] if prior else 0.5  # neutral prior for new agents

    # 2. Validation boost: +0.2 if independently validated, +0.0 otherwise
    validation_boost = 1.2 if m['validation_agent_id'] else 1.0

    # 3. Age survival: memories not retracted after 30 days earn a small credibility bonus
    days_alive = (datetime.now() - datetime.fromisoformat(m['created_at'])).days
    age_survival = 1.0 + min(0.1, days_alive / 300)  # max +0.1 bonus after ~30 days

    # 4. Retraction penalty: 0.0 if retracted, else 1.0
    retraction_penalty = 0.0 if m['retracted_at'] else 1.0

    score = base * validation_boost * age_survival * retraction_penalty
    return max(0.0, min(1.0, score))
```

### 5.3 Per-Agent-Category Trust Score Formula

Updated after each retraction event or daily refresh:

```python
def recompute_agent_category_trust(db, agent_id: str, category: str, scope: str = 'global'):
    """Recomputes rolling trust score from historical data."""

    stats = db.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN retracted_at IS NOT NULL THEN 1 ELSE 0 END) AS retracted,
            SUM(CASE WHEN validation_agent_id IS NOT NULL THEN 1 ELSE 0 END) AS validated,
            AVG(confidence) AS avg_confidence
        FROM memories
        WHERE agent_id = ? AND category = ? AND scope = ?
          AND retired_at IS NULL
          AND created_at > datetime('now', '-90 days')  -- recency window
    """, (agent_id, category, scope)).fetchone()

    if stats['total'] == 0:
        return 0.5  # no history = neutral

    retraction_rate = stats['retracted'] / stats['total']
    validation_rate = stats['validated'] / stats['total']
    avg_conf = stats['avg_confidence'] or 0.5

    # Base score: penalize retractions heavily, reward validations moderately
    score = (
        (1.0 - retraction_rate)       * 0.60   # retraction avoidance = 60% weight
        + validation_rate              * 0.20   # validation rate = 20% weight
        + avg_conf                     * 0.20   # self-reported confidence = 20% weight
    )

    db.execute("""
        INSERT INTO memory_trust_scores
            (agent_id, category, scope, sample_count, correct_count, retracted_count, trust_score, last_computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(agent_id, category, scope) DO UPDATE SET
            sample_count = excluded.sample_count,
            retracted_count = excluded.retracted_count,
            trust_score = excluded.trust_score,
            last_computed_at = excluded.last_computed_at
    """, (
        agent_id, category, scope,
        stats['total'],
        stats['total'] - stats['retracted'],
        stats['retracted'],
        score
    ))
    db.commit()
    return score
```

### 5.4 Bootstrap and Cold Start

New agents have no history. Options:
1. **Neutral prior (0.5):** Conservative — new agents get standard retrieval weight
2. **Manager prior:** Inherit trust from their `chainOfCommand` manager's scores
3. **Role prior:** Agent type defaults (hermes=0.85, paperclip=0.70, openclaw=0.75)

**Recommendation:** Use role prior for new agents, transition to computed score after 10+ memories in a category. Prevents an agent with zero retraction history (but also zero history) from appearing perfectly trustworthy.

---

## 6. Migration Plan

All changes are additive. No existing data is invalid after migration.

```sql
-- Step 1: Add columns (safe on existing rows — NULLable by default)
ALTER TABLE memories ADD COLUMN validation_agent_id TEXT REFERENCES agents(id);
ALTER TABLE memories ADD COLUMN validation_at TEXT;
ALTER TABLE memories ADD COLUMN trust_score REAL;
ALTER TABLE memories ADD COLUMN derived_from_ids TEXT;
ALTER TABLE memories ADD COLUMN retracted_at TEXT;
ALTER TABLE memories ADD COLUMN retraction_reason TEXT;

-- Step 2: Create trust scores table
CREATE TABLE IF NOT EXISTS memory_trust_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    category TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    sample_count INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    retracted_count INTEGER NOT NULL DEFAULT 0,
    trust_score REAL NOT NULL DEFAULT 0.5,
    last_computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, category, scope)
);

-- Step 3: Seed trust scores from existing data (initial bootstrap)
INSERT OR IGNORE INTO memory_trust_scores (agent_id, category, scope, sample_count, trust_score)
SELECT agent_id, category, scope, COUNT(*), 0.5
FROM memories
WHERE retired_at IS NULL
GROUP BY agent_id, category, scope;

-- Step 4: Add indexes
CREATE INDEX IF NOT EXISTS idx_memories_retracted ON memories(retracted_at) WHERE retracted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_trust_score ON memories(trust_score);
CREATE INDEX IF NOT EXISTS idx_memories_validation ON memories(validation_agent_id) WHERE validation_agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mts_agent_category ON memory_trust_scores(agent_id, category);

-- Step 5: Record schema version
INSERT INTO schema_version (version, description) VALUES (6, 'Provenance & trust chain fields');
```

**Schema version bump:** 5 → 6.

---

## 7. Integration with Existing Systems

### 7.1 `brainctl memory add`

When writing a new memory, optionally capture derivation source:

```bash
brainctl memory add "Project X uses PostgreSQL 15" \
  --category project \
  --derived-from 42,67   # memory IDs this fact was synthesized from
```

### 7.2 `brainctl validate` (integrity check)

Add to existing validation output (already implemented stub in Sentinel 2's domain):

```
Provenance integrity:
  memories with trust_score populated:    23/88
  memories with no trust_score (unrated): 65/88 → run 'brainctl memory trust-refresh' to score
  retracted memories with audit event:    3/3 ✓
  memories tagged 'provenance:suspect':   8 → review recommended
```

### 7.3 Retrieval Layer

When `brainctl search` or `brainctl memory` retrieves facts, optionally filter by trust:

```bash
brainctl search "PostgreSQL" --min-trust 0.7     # only high-trust memories
brainctl memory list --retracted                  # audit retracted memories
brainctl memory list --suspect                    # review flagged memories
```

### 7.4 Relationship to COS-115 (Adversarial Robustness)

[COS-115](/COS/issues/COS-115) addresses *external* attacks: embedding poisoning, hallucination injection, tamper detection via hash chains.

This report (COS-121) addresses *internal* trust: honest mistakes, agent drift, reliability variation by domain. Together:
- COS-115 prevents bad data from entering the system
- COS-121 tracks and propagates trust when bad data does enter (honest errors, drift, category-specific unreliability)

The `memory_trust_scores` table can also receive signals from COS-115's integrity verification layer: if a Merkle-chain violation is detected on an agent's writes, set that agent's trust score to 0.0 in `memory_trust_scores`.

---

## 8. Implementation Priority

| Component | Effort | Impact | Priority |
|---|---|---|---|
| Schema migration (Step 1-5 SQL) | 2h | Enables everything | **P0** |
| `brainctl memory retract` command | 4h | Direct user value | **P0** |
| `brainctl memory trust-refresh` (score recompute) | 3h | Enables filtering | **P1** |
| Trust score on retrieval (`--min-trust` flag) | 2h | Retrieval quality | **P1** |
| Trust propagation graph walk | 6h | Cascade accuracy | **P2** |
| `brainctl memory validate` command | 2h | Corroboration | **P2** |
| `brainctl validate` provenance checks | 2h | Monitoring | **P2** |
| `brainctl memory trust-report` | 2h | Observability | **P3** |

**Total estimated effort:** ~23 hours for full stack. Phased delivery: P0 (6h) → P1 (5h) → P2 (10h) → P3 (2h).

---

## 9. Summary of Recommendations

1. **Add 6 columns to `memories`** — fully additive, no data migration required.
2. **Create `memory_trust_scores` table** — pre-computed per-agent × category trust, refreshed on events.
3. **Implement soft retraction** — `retracted_at` + `retraction_reason` + event log. Never delete.
4. **Trust propagation via `derived_from_ids` + `knowledge_edges`** — direct derivation first, graph walk for indirect.
5. **Trust score formula** — 60% retraction avoidance, 20% validation rate, 20% self-confidence.
6. **Cold start: role-based prior** — neutral until 10+ memories in category.
7. **`brainctl memory retract`** — single-memory, batch-by-agent, and cascade modes.
8. **Cascade is conservative by default** — direct derivatives auto-retract (hop=1), indirect get flagged for human review (hop=2-3). Cascade retract beyond hop=1 requires `--cascade --confirm`.

The trust model is intentionally conservative: it takes more signals to *raise* trust than to lower it, and retraction cascades stop at flagging unless explicitly confirmed. This avoids runaway cascades while ensuring suspect information surfaces for review.
