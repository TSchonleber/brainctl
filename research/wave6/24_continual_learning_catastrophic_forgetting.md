# Continual Learning & Catastrophic Forgetting — How to Keep Learning Without Destroying What You Know

**Research Wave:** 6
**Issue:** COS-248
**Author:** Sentinel 2 (Memory Integrity Monitor)
**Date:** 2026-03-28
**Builds On:** COS-84 (knowledge graph), COS-85 (coherence-check), COS-121 (provenance/trust chains), COS-122 (write contention/CAS), COS-179 (cross-agent contradiction detection)
**Cross-pollinate:** Hippocampus (consolidation cycle owner), Engram (episodic/semantic split), Recall (retrieval)
**Project:** Cognitive Architecture & Enhancement

---

## Executive Summary

Every consolidation pass in brain.db risks silent degradation: new memories overwrite or dilute old high-value knowledge. This is the machine-learning problem of **catastrophic forgetting** — and it is already structurally present in our system. We have no protection mechanism.

This report designs a **Continual Learning Framework (CLF)** for brain.db that:
1. Classifies memories by consolidation resistance (EWC-inspired importance scoring)
2. Implements two-speed memory acceptance (fast hippocampal intake vs. slow neocortical consolidation)
3. Schedules experience replay of high-value memories during consolidation
4. Adds provenance-based write protection for foundational memories
5. Provides a concrete implementation path inside the existing hippocampus pipeline

**Core guarantee:** A memory with high `recall_count`, `confidence ≥ 0.90`, and confirmed downstream dependents MUST NOT be degraded by routine consolidation. It can only be updated by explicit, traceable override.

---

## 1. The Problem: Catastrophic Forgetting in brain.db

### 1.1 How Our System Forgets

Catastrophic forgetting in neural networks occurs when learning new tasks overwrites weights critical to old ones. In brain.db, the analogous risks are:

| Risk | Mechanism | Current Protection |
|------|-----------|-------------------|
| **Semantic drift** | Consolidation cycle compresses semantically similar memories, losing nuance | None |
| **Confidence erosion** | Decay pass applies uniform temporal weight reduction regardless of importance | None |
| **Supersede overwrites** | A new memory with `supersedes_id` replaces the old one, breaking downstream dependents | Provenance chain (COS-121), but no write guard |
| **Contradiction resolution** | The winner of a contradiction check retires the loser — even if the loser was correct | Coherence check flags, but no replay |
| **Embedding drift** | Re-embedding old memories with a new model changes their vector neighborhood | No version tracking |

### 1.2 What the Literature Says

**Kirkpatrick et al. (2017) — Elastic Weight Consolidation (EWC):** Identifies which parameters (weights) were important to past tasks using the Fisher information matrix. Penalizes large updates to those parameters when learning new tasks. The penalty is proportional to importance × magnitude of proposed change.

**Mapping:** Memory `importance` score (already in schema) + `recall_count` + `confidence` → compute an EWC-analog "consolidation resistance" score. High-resistance memories incur a penalty when proposed for modification or supersession.

**McClelland et al. (1995) / Kumaran et al. (2016) — Complementary Learning Systems (CLS):** Fast hippocampal system binds episodic memories rapidly; slow neocortical system integrates semantic generalities over time. The key insight: slow integration prevents new specifics from overwriting old generalities. Replaying hippocampal traces during sleep (offline consolidation) lets the neocortex learn without catastrophic interference.

**Mapping:** Our system already has `temporal_class` (`ephemeral`, `short`, `medium`, `long`, `permanent`). This is CLS in embryonic form. The missing piece is: (a) a formal promotion ladder with write-protection at each tier, and (b) a replay scheduler that re-presents important episodic memories during offline consolidation.

**Lin (1992) / Schaul et al. (2015) — Experience Replay with Prioritized Sampling:** Replay old experiences during new learning. Prioritized replay samples by TD-error (surprise). Prevents forgetting by interleaving old and new.

**Mapping:** Consolidation cycle should not only process new memories — it should replay a sample of high-importance old memories, verifying they are still coherent and re-anchoring their embeddings if needed.

**Aljundi et al. (2018) — Memory-Aware Synapses:** Track which synapses (parameters) matter for which outputs. A synapse is important if changing it would change an observed output. No task labels required.

**Mapping:** Provenance graph (COS-121 / COS-84 knowledge graph) already tracks which memories derive from which. A memory is "synaptically important" if downstream memories depend on it. Track this as `downstream_dependent_count` and weight it into the consolidation resistance score.

---

## 2. Architecture: Two-Speed Memory Pipeline

### 2.1 The CLS Ladder

Formalize the existing `temporal_class` field into a **promotion ladder** with write rules:

```
ephemeral  →  short  →  medium  →  long  →  permanent
   (raw)      (days)    (weeks)   (months)   (frozen)
```

**Promotion rules:**
- `ephemeral → short`: automatic after TTL (existing decay logic)
- `short → medium`: requires `recall_count ≥ 3` OR explicit agent `confirm` call
- `medium → long`: requires `recall_count ≥ 10` AND `confidence ≥ 0.80`
- `long → permanent`: requires `recall_count ≥ 25` AND `confidence ≥ 0.90` AND `downstream_dependent_count ≥ 2`

**Write rules by tier:**

| Tier | Supersede allowed? | Update allowed? | Decay applies? | Compression allowed? |
|------|-------------------|-----------------|----------------|---------------------|
| ephemeral | Yes | Yes | Yes | Yes |
| short | Yes (logged) | Yes | Yes | Yes |
| medium | Yes (requires comment) | Restricted | Slowed (0.5×) | Limited |
| long | Requires approval event | Restricted | Minimal (0.1×) | Prohibited |
| permanent | Prohibited | Prohibited | None | Prohibited |

### 2.2 Consolidation Resistance Score

Each memory receives a **consolidation resistance** (CR) score at the start of each consolidation cycle:

```python
def consolidation_resistance(memory) -> float:
    """
    EWC-inspired score in [0, 1]. Higher = harder to modify.
    Combines recall history, confidence, and structural importance.
    """
    # Base: how often this memory has been useful
    recall_signal = min(memory.recall_count / 25.0, 1.0)

    # Confidence: validated accuracy
    confidence_signal = memory.confidence

    # Structural: how many downstream memories depend on this
    downstream = get_downstream_dependent_count(memory.id)
    dependency_signal = min(downstream / 5.0, 1.0)

    # Temporal tier bonus: higher tiers resist more
    tier_bonus = {
        'ephemeral': 0.0, 'short': 0.1, 'medium': 0.2,
        'long': 0.4, 'permanent': 1.0
    }.get(memory.temporal_class, 0.0)

    cr = (
        0.35 * recall_signal +
        0.30 * confidence_signal +
        0.25 * dependency_signal +
        0.10 * tier_bonus
    )
    return min(cr, 1.0)
```

**Thresholds:**
- CR < 0.30: freely consolidatable (compress, merge, supersede normally)
- 0.30 ≤ CR < 0.60: log proposed changes; apply with caution
- 0.60 ≤ CR < 0.80: require explicit justification in supersede/compress event
- CR ≥ 0.80: **PROTECTED** — no modification without an `approval` event in the provenance chain

### 2.3 The Hippocampal Intake Gate

New memories enter at `ephemeral` or `short` tier. The **intake gate** fires before `INSERT`:

```python
def intake_gate(new_memory, existing_memories_in_scope):
    """
    Before accepting a new memory, check if it would degrade protected ones.
    Returns: (accept: bool, conflict_ids: List[str], reason: str)
    """
    # Check for semantic near-duplicates of protected memories
    similar = vector_search(new_memory.embedding, top_k=5, threshold=0.85)
    for candidate in similar:
        cr = consolidation_resistance(candidate)
        if cr >= 0.60:
            # New memory semantically overlaps a protected one
            if is_contradictory(new_memory, candidate):
                return False, [candidate.id], f"contradicts protected memory {candidate.id}"
            else:
                # Compatible: accept but log the relationship
                new_memory.supersedes_id = None  # don't auto-supersede
                new_memory.linked_to = candidate.id
    return True, [], "ok"
```

---

## 3. Experience Replay Scheduler

### 3.1 Why Replay Matters

During offline consolidation, the hippocampus currently processes only *new* or *decayed* memories. This means foundational knowledge is never re-verified — it silently drifts if the embedding model changes or if the semantic neighborhood shifts due to new additions.

Experience replay forces the consolidation cycle to occasionally re-examine high-importance memories, re-anchoring them and catching drift early.

### 3.2 Replay Sampling Strategy

Inspired by prioritized experience replay (Schaul 2015), we sample memories for replay using:

```python
def replay_priority(memory) -> float:
    """
    Higher priority = more likely to be replayed.
    Prioritize: high-value memories we haven't verified recently.
    """
    cr = consolidation_resistance(memory)
    time_since_access = (now() - memory.last_recalled_at).days
    staleness = min(time_since_access / 30.0, 1.0)

    # High CR + stale = top priority for replay
    return cr * (0.4 + 0.6 * staleness)
```

**Replay quota:** Each consolidation cycle replays `ceil(0.15 * new_memories_count)` old memories, minimum 3, maximum 20.

**Replay action:** Re-run coherence check on the replayed memory. If coherence drops below its baseline, emit a `coherence_degraded` event and flag for review. If still healthy, bump `last_recalled_at` and emit a `replay_verified` event.

### 3.3 Integration Point

In `05_consolidation_cycle.py`, add after the main consolidation pass:

```python
def run_experience_replay(conn, new_memory_count):
    """Run after main consolidation. Sample and verify high-priority memories."""
    quota = max(3, min(20, math.ceil(0.15 * new_memory_count)))

    # Fetch candidate memories (medium+ tier, not accessed in 7+ days)
    candidates = conn.execute("""
        SELECT * FROM memories
        WHERE temporal_class IN ('medium', 'long', 'permanent')
          AND status = 'active'
          AND (last_recalled_at IS NULL OR last_recalled_at < datetime('now', '-7 days'))
        ORDER BY confidence DESC
        LIMIT 100
    """).fetchall()

    # Score and sample
    scored = [(replay_priority(m), m) for m in candidates]
    scored.sort(reverse=True)
    sampled = [m for _, m in scored[:quota]]

    for memory in sampled:
        result = coherence_check(memory)
        if result.score < memory.baseline_coherence * 0.80:
            emit_event('coherence_degraded', memory.id, result)
        else:
            conn.execute(
                "UPDATE memories SET last_recalled_at = ? WHERE id = ?",
                (now(), memory.id)
            )
            emit_event('replay_verified', memory.id, {'score': result.score})
```

---

## 4. Provenance-Based Write Protection

### 4.1 The Dependency Graph as a Protection Mechanism

COS-121 built provenance chains. COS-84 built the knowledge graph (2,675 edges). Neither currently gates writes.

The missing step: before any supersede, compress, or retire action on a memory, walk the knowledge graph to count confirmed downstream dependents.

```python
def get_downstream_dependent_count(memory_id) -> int:
    """
    Count active memories that cite this memory as a source,
    are epistemically derived from it, or are linked via supersedes_id.
    """
    return conn.execute("""
        SELECT COUNT(DISTINCT m.id)
        FROM memories m
        JOIN knowledge_graph kg ON kg.target_id = m.id
        WHERE kg.source_id = ?
          AND kg.edge_type IN ('derived_from', 'supersedes', 'cites')
          AND m.status = 'active'
    """, (memory_id,)).fetchone()[0]
```

### 4.2 Write Guard Implementation

Attach to all write operations:

```python
def protected_write_check(memory_id, operation: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    operation: 'supersede' | 'compress' | 'retire' | 'update'
    """
    memory = get_memory(memory_id)
    cr = consolidation_resistance(memory)

    if cr >= 0.80:
        downstream = get_downstream_dependent_count(memory_id)
        if downstream > 0:
            return False, (
                f"Memory {memory_id} has CR={cr:.2f} and {downstream} "
                f"downstream dependents. Operation '{operation}' requires "
                f"an approval event."
            )

    if memory.temporal_class == 'permanent':
        return False, "Permanent memories are immutable."

    return True, "ok"
```

---

## 5. Curriculum Ordering for Agent Context Delivery

### 5.1 The Problem

When agents retrieve context (via `brainctl search`), memories are returned ranked by vector similarity + recency. This is retrieval-optimal but not learning-optimal. Agents seeing a mix of foundational concepts and bleeding-edge specifics together may over-index on the novel and under-weight the established.

### 5.2 Curriculum-Aware Context Delivery

Add a `--curriculum` flag to `brainctl search`:

```bash
brainctl search "topic" --curriculum
```

In curriculum mode, results are re-ranked by:
1. **Foundation first:** `long`/`permanent` memories with high CR ranked at top
2. **Then novel:** `ephemeral`/`short` memories ranked by recency
3. **Intermediates:** `medium` memories fill the middle

This mirrors pedagogical curriculum learning (Bengio 2009): establish the conceptual scaffold before introducing the edge cases. Agents process foundational context before specifics, preventing recency bias from corrupting their situational model.

---

## 6. Schema Changes Required

### 6.1 New Columns in `memories` Table

```sql
ALTER TABLE memories ADD COLUMN consolidation_resistance REAL DEFAULT NULL;
ALTER TABLE memories ADD COLUMN downstream_dependent_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN last_replayed_at DATETIME DEFAULT NULL;
ALTER TABLE memories ADD COLUMN baseline_coherence REAL DEFAULT NULL;
ALTER TABLE memories ADD COLUMN write_protected BOOLEAN DEFAULT FALSE;
```

### 6.2 New Events

| event_type | Emitted when |
|-----------|--------------|
| `coherence_degraded` | Replay finds score < 80% of baseline |
| `replay_verified` | Replay confirms memory still healthy |
| `write_blocked` | Protected write check prevents modification |
| `tier_promoted` | Memory advances up the CLS ladder |
| `intake_conflict` | Intake gate flags new memory vs. protected existing |

### 6.3 New `brainctl` Commands

```bash
# Show consolidation resistance score for a memory
brainctl memory cr <memory-id>

# Show all protected memories (CR >= 0.80)
brainctl memory list --protected

# Force a replay pass now (for testing)
brainctl maintenance replay --dry-run

# Show curriculum-ordered search results
brainctl search "term" --curriculum
```

---

## 7. Implementation Roadmap

### Phase 1 — Schema + CR Scoring (1 session)
- Add new columns
- Implement `consolidation_resistance()` in `hippocampus.py`
- Backfill CR scores for all active memories
- Add `brainctl memory cr` command

### Phase 2 — Write Guards (1 session)
- Implement `protected_write_check()`
- Hook into consolidate, compress, retire, supersede paths
- Emit `write_blocked` events
- Test: attempt to supersede a high-CR memory and verify block

### Phase 3 — Intake Gate (1 session)
- Implement `intake_gate()` in `hippocampus.py`
- Emit `intake_conflict` events
- Test: inject contradictory memory and verify gate fires

### Phase 4 — Experience Replay (1 session)
- Implement `run_experience_replay()` in `05_consolidation_cycle.py`
- Schedule as part of nightly maintenance cron
- Test: mark memory as stale, run replay, verify `replay_verified` event

### Phase 5 — Curriculum Delivery (1 session)
- Implement `--curriculum` flag in brainctl search
- Test: compare retrieval order with and without flag

**Total estimated effort:** 5 focused sessions. Phase 1-2 are highest priority as they close the write-protection gap that currently exists.

---

## 8. Risk Analysis

| Risk | Likelihood | Severity | Mitigation |
|------|-----------|----------|------------|
| CR scoring too aggressive — blocks legitimate updates | Medium | High | Approval-event override path; start threshold at 0.85, tune down |
| Replay overhead slows consolidation | Low | Medium | Cap at 20 memories/cycle; run async if needed |
| Intake gate false positives — blocks valid new memories | Medium | Medium | Dry-run mode for first 2 weeks; tune similarity threshold |
| Downstream count query is slow on large graphs | Low | Low | Index on `knowledge_graph.source_id`; cache per cycle |
| Schema migration breaks existing hippocampus tests | Low | High | All new columns are nullable with safe defaults; migration is additive |

---

## 9. Connection to Existing Systems

| System | How CLF Connects |
|--------|-----------------|
| COS-85 coherence-check | Replay uses coherence-check to verify memory health |
| COS-121 provenance chains | `downstream_dependent_count` traverses provenance graph |
| COS-122 write contention/CAS | Write guard is a pre-check before CAS attempt |
| COS-179 cross-scope contradiction | Intake gate uses cross-scope detection before accepting |
| COS-233 (Wave 6 cross-scope) | Shares contradiction detection logic |
| COS-234 (Wave 6 trust scores) | CR score incorporates trust score via `confidence` field |

---

## 10. Summary

The catastrophic forgetting problem is real and active in brain.db today. We have no write protection on foundational memories, no replay to re-verify aging knowledge, and no intake gate to prevent new learning from corrupting established wisdom.

This framework addresses all three gaps with minimal schema change, incremental implementation, and clear test criteria. The EWC-inspired CR score gives every memory a quantified resistance to modification — making protection proportional to earned importance, not arbitrary flags.

**Recommended first action:** Implement Phase 1 (CR scoring) in the next hippocampus maintenance session. Even just *measuring* consolidation resistance, without enforcing it yet, will reveal which memories are currently at risk and build the operational intuition needed to tune thresholds safely.
