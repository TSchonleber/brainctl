# Research Wave 6 — Report 24: Neuroplasticity & Structural Self-Modification

**Author:** Engram (b98504a8-eb8e-4bd9-9a98-306936b5bab2)
**Date:** 2026-03-28
**Issue:** [COS-242](/COS/issues/COS-242)
**Prior art:** [COS-202](/COS/issues/COS-202) (SLO baseline), [COS-218](/COS/issues/COS-218) (memory event bus), [COS-230](/COS/issues/COS-230) (temporal classification repair)

---

## 1. Problem Statement

The brain.db is structurally static. Schema, edge taxonomy, indexes, and retrieval paths are designed once at deploy time. Real brains rewire continuously: synaptic weights adjust on millisecond timescales, new axonal projections form over days, whole cortical maps reorganize over years. Our system has the data layer for this (access_log, knowledge_edges, memory_events) but no machinery that acts on it to change structure.

**Current state:**
- 4,359 knowledge_edges — all inserted by `embed-populate` or explicit `brainctl graph add-edge`
- Edge weights are set at creation and never updated
- Relation taxonomy (6 types) is static
- Indexes are fixed at schema migration time
- No mechanism converts usage patterns into structural change

---

## 2. Neuroscience Foundations → System Mappings

### 2.1 Hebbian Learning (LTP/LTD)

**Biology:** "Neurons that fire together wire together." Long-term potentiation (LTP) strengthens synapses when pre- and post-synaptic neurons activate simultaneously. Long-term depression (LTD) weakens synapses when activation is uncorrelated. The weight of a synapse drifts toward the correlation of its endpoints' activation.

**Brain.db mapping:** knowledge_edges.weight is our synaptic strength. Two memories co-retrieved in the same query session (same agent, within a 60-second window in access_log) are "co-firing." Their edge weight should increase. Two memories never co-retrieved should see their edge weight decrease toward 0 and eventually be pruned.

**Concrete rule:**
```
Δweight = η * (co_retrieval_rate − baseline_rate)

Where:
  η = learning rate = 0.05
  co_retrieval_rate = (co_retrievals / total_retrievals_of_pair) in last 30 days
  baseline_rate = 0.01 (chance co-retrieval for uncorrelated memories)

Apply: weight += Δweight, clamp [0.0, 1.0]
Prune: if weight < 0.05 after 90 days with zero co-retrieval, DELETE the edge
```

**What's needed:**
- access_log already records (target_table, target_id, created_at, agent_id) per query
- We need a co-retrieval detection pass: group access_log by (agent_id, session window) and identify memory pairs that appear together
- A new hippocampus.py command: `hippocampus hebb --window 60 --eta 0.05 --dry-run`

### 2.2 Structural Plasticity (Axonogenesis / Synaptogenesis)

**Biology:** The brain doesn't just adjust synapse weights — it grows entirely new connections. Dendritic spines sprout, axons branch to new targets. This happens when activity patterns persist without an existing pathway to encode them.

**Brain.db mapping:** If two memories are frequently co-retrieved but have NO edge between them, the system should auto-create one. The relation type should be inferred from the co-retrieval context — if the co-retrieval happens during search queries, the new edge is `semantic_similar`; if it happens during sequential recall (one memory leading to the next via FTS), the relation is `associative_recall` (new type).

**New relation types to auto-generate:**
| New Type | Trigger | Meaning |
|---|---|---|
| `associative_recall` | Memory B appears in results after Memory A was the top result | A tends to surface B |
| `scope_drift` | Two memories share the same agent but migrated scope together over time | Organizational co-evolution |
| `usage_cluster` | High co-retrieval rate (>0.3) with no semantic similarity | Practical co-use, not semantic overlap |

**Structural plasticity rule:**
```
If co_retrieval_rate(A, B) > 0.2 over 7 days
AND no edge exists between (memories.A, memories.B)
THEN: INSERT INTO knowledge_edges (relation_type='associative_recall', weight=0.3, ...)
```

**Auto-index creation:**
When query patterns show repeated FTS searches on the same (scope, category) combination (>20 times in 7 days, no index covering that pair), the hippocampus should issue:
```sql
CREATE INDEX IF NOT EXISTS idx_memories_{scope}_{category}
ON memories (scope, category)
WHERE retired_at IS NULL;
```
This is SQLite partial index creation — safe, incremental, storage-cheap.

### 2.3 Critical Periods

**Biology:** The visual cortex has a critical period in early development — high plasticity before ~P28 (postnatal day 28) when ocular dominance columns are still malleable. After closure, structural change becomes much harder. The plasticity rate itself changes over time.

**Brain.db mapping:** New projects and agents should have elevated Hebbian learning rates. An agent in its first 14 days should update edge weights 3× faster (η=0.15) and require a lower co-retrieval threshold to auto-generate new edges (0.1 instead of 0.2). After 30 days, learning rates drop to the standard rate.

**Implementation:**
```python
def get_learning_rate(agent_id: str, db: Connection) -> float:
    row = db.execute(
        "SELECT created_at FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        return 0.05  # default
    age_days = days_since(datetime.now(), row["created_at"])
    if age_days < 14:
        return 0.15   # critical period
    elif age_days < 30:
        return 0.08   # post-critical transition
    else:
        return 0.05   # mature

def get_plasticity_threshold(agent_id: str, db: Connection) -> float:
    age_days = agent_age_days(agent_id, db)
    if age_days < 14:
        return 0.10   # critical period: easier to form new edges
    else:
        return 0.20   # mature: higher bar for new structural connections
```

**Per-project critical periods:**
New projects (via memory scope matching project name) should also trigger a 14-day elevated plasticity window. This is tracked in a new `plasticity_state` table (see Section 4).

### 2.4 Homeostatic Plasticity

**Biology:** Synaptic scaling prevents runaway excitation. If a neuron becomes too active, it globally scales down all its incoming synaptic weights to prevent it from dominating the network. This is set-point regulation for neural activity.

**Brain.db mapping:** A small number of "hub" memories risk monopolizing retrieval. memory IDs with the highest incoming knowledge_edges weights + highest recalled_count will crowd out other memories in search results. We need synaptic scaling.

**Current risk:** access_log shows 246 `search` operations. If memory #86 (identity/long, confidence=0.998) appears in >40% of all search results, it's a hub — every query routes through it.

**Homeostatic rule:**
```
For each memory M:
  activity_score = (recalled_count_30d / total_recalls_30d) +
                   (sum(incoming_edge_weights) / max_possible_incoming_weight)

  if activity_score > OVERACTIVITY_THRESHOLD (0.15):
    scale_factor = OVERACTIVITY_THRESHOLD / activity_score
    UPDATE knowledge_edges
    SET weight = weight * scale_factor
    WHERE target_table='memories' AND target_id=M.id
```

This globally scales DOWN all incoming edge weights to M when M is over-active, maintaining the network's dynamic range.

**Complementary — boosting under-recalled memories:**
```
For each memory M with recalled_count_30d = 0 and age > 14 days:
  apply homeostatic boost:
    UPDATE knowledge_edges
    SET weight = MIN(1.0, weight * 1.1)
    WHERE target_table='memories' AND target_id=M.id
```

Prevents silent drift of unused memories into permanent invisibility before they've had a chance to be evaluated.

### 2.5 Transfer Learning (Cross-Project Rewiring)

**Biology:** The motor cortex reuses movement primitives across tasks. Language skills transfer between related languages. The brain builds on structural foundations from prior learning rather than starting from scratch.

**Brain.db mapping:** When a new project scope is created, the hippocampus should:
1. Find the 3 most similar existing project scopes (by cosine similarity of their scope names in vec_memories)
2. Copy the top-10 knowledge_edges with highest weight from those scopes into the new scope's memory neighborhood
3. Set initial edge weights to 50% of the source (halved — they're priors, not confirmed)
4. Log this as a `scope_drift` edge from source scope memories to new scope memories

**Transfer similarity query:**
```sql
-- Find memories in similar scopes to use as priors
SELECT m.id, m.scope, ke.weight, ke.relation_type
FROM memories m
JOIN knowledge_edges ke ON ke.target_table='memories' AND ke.target_id=m.id
WHERE m.scope IN (
  SELECT scope FROM memories
  WHERE scope LIKE '%costclock%'  -- similar to new project
  AND scope != 'project:new-project-name'
  LIMIT 3
)
ORDER BY ke.weight DESC
LIMIT 10;
```

---

## 3. The Plasticity Engine: hippocampus.py Extensions

### 3.1 New Command: `hippocampus hebb`

Runs one Hebbian pass: scans access_log for co-retrievals, updates edge weights, prunes dead edges, auto-creates associative_recall edges.

```
hippocampus hebb [--window SECONDS] [--eta FLOAT] [--lookback DAYS] [--dry-run]
  --window 60        Session window for co-retrieval detection (default: 60s)
  --eta 0.05         Base learning rate (default: 0.05)
  --lookback 30      Days of access_log to analyze (default: 30)
  --prune-threshold  Edge weight below which edges are pruned (default: 0.05)
  --dry-run          Print changes without applying
```

**Algorithm:**
```python
def cmd_hebb(args):
    db = get_db()
    now = datetime.now()
    cutoff = now - timedelta(days=args.lookback)

    # Step 1: Build co-retrieval map from access_log
    log_rows = db.execute("""
        SELECT agent_id, target_table, target_id, created_at
        FROM access_log
        WHERE target_table = 'memories'
        AND created_at >= ?
        ORDER BY agent_id, created_at
    """, (cutoff.isoformat(),)).fetchall()

    # Group into sessions (same agent, within window seconds)
    sessions = build_sessions(log_rows, window_seconds=args.window)

    # Step 2: Count co-retrievals per pair
    co_retrieval_counts = Counter()
    total_retrievals = Counter()
    for session in sessions:
        ids = [r["target_id"] for r in session]
        total_retrievals.update(ids)
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                pair = (min(a,b), max(a,b))
                co_retrieval_counts[pair] += 1

    # Step 3: Update existing edge weights (LTP/LTD)
    for pair, co_count in co_retrieval_counts.items():
        a_id, b_id = pair
        total = max(total_retrievals[a_id], total_retrievals[b_id])
        co_rate = co_count / total if total > 0 else 0

        eta = get_learning_rate(args.agent, db)
        delta = eta * (co_rate - BASELINE_RATE)

        # Update both directions if edge exists
        update_edge_weight(db, 'memories', a_id, 'memories', b_id, delta)
        update_edge_weight(db, 'memories', b_id, 'memories', a_id, delta)

    # Step 4: Auto-create new edges (structural plasticity)
    plasticity_threshold = get_plasticity_threshold(args.agent, db)
    for pair, co_count in co_retrieval_counts.items():
        a_id, b_id = pair
        total = max(total_retrievals[a_id], total_retrievals[b_id])
        co_rate = co_count / total if total > 0 else 0

        if co_rate > plasticity_threshold and not edge_exists(db, a_id, b_id):
            create_associative_recall_edge(db, a_id, b_id, weight=0.3)

    # Step 5: Prune dead edges
    prune_weak_edges(db, threshold=args.prune_threshold, min_age_days=90)

    # Step 6: Homeostatic scaling
    run_homeostatic_scaling(db)

    db.commit()
```

### 3.2 New Command: `hippocampus transfer`

Seeds new project scopes with priors from similar existing scopes.

```
hippocampus transfer --new-scope SCOPE [--source-scopes SCOPE1,SCOPE2] [--dry-run]
```

### 3.3 Existing Command Changes

**`hippocampus consolidate`:** After consolidation, run a mini-Hebbian pass on the consolidated memories to rebuild edge weights based on their source materials' prior co-retrieval history. Currently, consolidated memories start edge-weight cold.

**`hippocampus decay`:** Add Pass 0 (before decay): run homeostatic scaling to prevent hubs from growing during the decay interval. This prevents a feedback loop where popular memories get high confidence from frequent recall AND high weights from static edges.

---

## 4. Schema Changes

### 4.1 New Table: `plasticity_state`

Tracks per-agent and per-scope plasticity parameters that change over time (critical period tracking, learning rate history).

```sql
CREATE TABLE IF NOT EXISTS plasticity_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('agent', 'scope')),
    entity_id TEXT NOT NULL,          -- agent_id or scope string
    critical_period_opened_at TEXT,   -- when high plasticity began
    critical_period_closed_at TEXT,   -- null = still open
    learning_rate_override REAL,      -- NULL = use default schedule
    homeostatic_setpoint REAL DEFAULT 0.15,
    last_hebb_pass_at TEXT,
    total_hebb_passes INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity_type, entity_id)
);
```

### 4.2 New Relation Types in `knowledge_edges`

Extend the relation taxonomy with usage-derived types:

| New Relation Type | Created By | Meaning |
|---|---|---|
| `associative_recall` | hippocampus hebb | A frequently surfaces B in retrieval without semantic overlap |
| `scope_transfer` | hippocampus transfer | Edge copied from similar scope as prior (50% weight) |
| `homeostatic_peer` | hippocampus hebb | Two memories compete for retrieval attention (inhibitory) |

These are non-destructive additions — the existing 6 types remain.

### 4.3 New Index: `idx_access_log_session`

```sql
CREATE INDEX IF NOT EXISTS idx_access_log_session
ON access_log (agent_id, target_table, created_at)
WHERE target_table IS NOT NULL;
```

The Hebbian pass is the primary consumer of access_log. This index makes the session-building query ~10× faster (currently no composite index exists on agent_id + created_at).

### 4.4 New Index: `idx_knowledge_edges_weight`

```sql
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_weight
ON knowledge_edges (weight, source_table, source_id)
WHERE weight > 0.3;
```

Homeostatic scaling queries (find high-weight incoming edges to M) currently do a full scan. This partial index covers the hot path.

---

## 5. Hippocampus Cycle Integration

The existing cadence is:
```
Every 6h: consolidate → decay → embed-populate
```

Proposed extended cycle:
```
Every 6h:
  1. hebb pass (co-retrieval analysis, LTP/LTD, auto-edge creation)    [~15s]
  2. homeostatic scaling (prevent hub dominance)                         [~5s]
  3. consolidate (existing)                                              [variable]
  4. decay (existing, now with homeostatic pre-pass)                     [~10s]
  5. transfer scan (check for new scopes needing priors)                [~5s]
  6. embed-populate (existing)                                          [variable]
  7. prune weak edges (age > 90d, weight < 0.05)                       [~5s]
```

Total new overhead per cycle: ~35s. Acceptable.

---

## 6. Measurement & Validation

### Success Metrics

| Metric | Baseline (current) | Target after 14 days |
|---|---|---|
| Edge weight distribution StdDev | unknown (all 1.0 at creation) | StdDev > 0.2 (actual differentiation) |
| % edges with weight < 0.3 | 0% | >20% (dead edges being pruned) |
| % edges with weight > 0.8 | unknown | <30% (prevent hub dominance) |
| New `associative_recall` edges | 0 | >50 |
| Hub memory overactivity violations | unknown | <5% of active memories |
| Retrieval diversity (unique memories in top-10 results over 100 queries) | TBD | +15% vs baseline |

### Experiment Scaffold

Use `cognitive_experiments` table to track the rollout:

```python
# Register the experiment
db.execute("""
INSERT INTO cognitive_experiments
  (name, hypothesis, status, led_by_agent, baseline_metrics)
VALUES (
  'neuroplasticity-v1',
  'Hebbian weight updates + homeostatic scaling will improve retrieval diversity by 15%',
  'running',
  'b98504a8-eb8e-4bd9-9a98-306936b5bab2',
  '{"edge_weight_stddev": null, "hub_violation_rate": null}'
)
""")
```

Run outcome comparison after 14 days: if retrieval diversity improves and hub violations are <5%, mark experiment `successful` and merge the hebb pass into the permanent cadence.

---

## 7. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Runaway edge weight inflation (popular memories get weight=1.0 on all edges) | High | Homeostatic scaling cap + global weight normalization pass |
| Spurious `associative_recall` edges from one-off co-retrieval | Medium | Require co-retrieval over >7 day window before creating edge; minimum 3 co-retrievals |
| Index creation locks database during heavy write | Low | Use `CREATE INDEX IF NOT EXISTS` (SQLite re-checks); schedule during off-peak |
| Critical period abuse (always appearing new) | Low | Critical period tied to `agents.created_at`, not agent-settable |
| Access_log becomes too large for Hebbian pass | Medium | Hebbian pass only scans last 30 days; access_log pruning already in `hippocampus prune-log` |

---

## 8. Open Questions (for Hermes / next wave)

1. **Inhibitory edges:** Homeostatic peer edges currently just reduce incoming weights. True homeostasis might need an explicit inhibitory relation type that *reduces* a competitor's score in retrieval ranking. Is retrieval ranking in brainctl extensible?

2. **Consolidation interaction:** When two high-weight memories are consolidated into one, what happens to their incoming/outgoing edges? Currently they're orphaned. The consolidation pass should merge edges (union of source + target edges, averaged weights).

3. **Multi-agent Hebbian:** Two agents co-retrieving the same memory in the same time window from different agents — should cross-agent co-retrieval strengthen edges? Currently Hebbian pass is agent-scoped. Argue for: the memory is used cross-agent, so it's genuinely important. Argue against: different agents may be retrieving it for unrelated reasons.

4. **FTS index structural change:** Can we auto-add new FTS columns (e.g., for a new high-volume tag) based on query pattern analysis? This is more invasive — requires ALTER TABLE or recreation. Probably out of scope for wave 6.

---

## 9. Implementation Priority

| Component | Effort | Impact | Priority |
|---|---|---|---|
| `idx_access_log_session` index | 5 min | Unblocks all hebb analysis | **P0** |
| `hippocampus hebb` command | 3h | Core Hebbian engine | **P1** |
| `plasticity_state` table + schema | 30 min | Critical period tracking | **P1** |
| Homeostatic scaling pass | 1h | Hub prevention | **P1** |
| `idx_knowledge_edges_weight` index | 5 min | Homeostatic query perf | **P2** |
| `associative_recall` edge auto-creation | 1h | Structural plasticity | **P2** |
| `hippocampus transfer` command | 2h | Cross-scope priors | **P3** |
| Cadence.py integration | 30 min | Automated cycle | **P3** |

**Recommended implementation order:** indexes (P0) → hebb command (P1) → homeostatic pass (P1) → schema table (P1) → rest in wave 7.

---

## 10. Summary

The brain.db has all the raw material for a self-modifying architecture:
- **access_log** is the neural activity trace
- **knowledge_edges** is the synaptic weight matrix
- **memory_events** is the spike train
- **cognitive_experiments** is the experimental control framework

What it lacks is the **plasticity engine** — the process that reads activity and writes structural change. This report specifies that engine across all five neuroscience dimensions. The core implementation (Hebbian pass + homeostatic scaling) is 4-5 hours of hippocampus.py work. The structural changes (3 new relation types, 2 new indexes, 1 new table) are all additive and non-breaking.

The result: a brain.db that literally rewires itself based on experience. Edges that matter grow stronger. Edges that don't get pruned. New pathways emerge from co-usage. Overactive hubs get scaled down. New agents start plastic and mature into stable structure. This is the architecture real brains use — there's no reason ours shouldn't.
