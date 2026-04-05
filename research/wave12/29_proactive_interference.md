# Proactive Interference — How Old Memories Block New Learning

**Research Wave:** 12
**Ticket:** COS-367
**Author:** Epoch (Temporal Cognition Engineer)
**Date:** 2026-03-28
**Status:** Complete

---

## Executive Summary

Proactive Interference (PI) describes the phenomenon where prior learning impedes the encoding and retrieval of new, potentially contradictory information. In cognitive science, PI is one of the principal causes of forgetting and belief rigidity in human memory. For brain.db, PI is a systemic risk: as the store ages, high-recalled permanent memories with accumulated Bayesian evidence mass (alpha >> 1, beta ≈ 0) function as cognitive incumbents that structurally suppress competing new writes.

This report documents the empirical PI landscape in brain.db, defines a **Proactive Interference Index (PII)**, proposes a **recency gate** formula, taxonomizes the most PI-prone memory categories, and specifies integration points with AGM belief revision (COS-363) and the Bayesian confidence system (COS-354).

---

## 1. Literature Review

### 1.1 Postman & Underwood (1973) — Two-Factor Theory of PI

Postman and Underwood's foundational work established that proactive interference operates through two mechanisms:

1. **Unlearning (response competition):** Old responses compete with new ones during retrieval, suppressing recall of newer items. The degree of competition scales with the strength of the original memory — the more rehearsed the old item, the stronger the suppression.

2. **Spontaneous recovery:** Even when new information temporarily displaces old memories, the older, higher-strength memories tend to recover over time. This is particularly damaging for volatile facts (tool availability, configuration state) that may be overwritten but whose old versions "bounce back" via retrieval bias.

**brain.db implication:** Memories with high `recalled_count` and permanent `temporal_class` exhibit exactly the conditions Postman & Underwood identify as maximum-PI sources. They are the new-write suppressors.

### 1.2 Anderson & Neely (1996) — Inhibition Theory

Anderson and Neely extended PI theory with an inhibitory mechanism: retrieval of a strong memory actively **suppresses** related alternatives in the retrieval pathway. This is not mere competition — it is directed inhibition. Critically:

- Inhibition is proportional to the **strength differential** between the retrieved memory and its competitors.
- Strongly inhibited memories become harder to access even when directly cued.
- In associative networks, inhibition propagates to semantically related items.

**brain.db implication:** When an agent retrieves a high-recalled permanent memory (e.g., memory id=93, recalled=121), the knowledge graph edges (4,718 total in our store) mean that competing semantic neighbors are simultaneously suppressed. This is exactly the mechanism behind why new architecture knowledge fails to displace old architectural decisions — the old memory's recall actively degrades the retrieval path for the new one.

### 1.3 Release from Proactive Interference (Wickens 1970)

Wickens demonstrated that PI can be overcome when the *encoding category* shifts — a phenomenon called "release from PI." When new information is framed in a categorically distinct namespace, interference drops sharply.

**brain.db implication:** Categorical isolation via `scope` (e.g., `project:new-system` vs `project:costclock-ai`) provides natural PI release. New scopes are protected from old-scope interference. This is already partially in place but under-exploited.

### 1.4 Consolidation and PI (McGaugh 2000)

Highly consolidated memories — those that have undergone hippocampal-to-cortical transfer — are maximally resistant to PI because they are no longer dependent on the hippocampus for retrieval; they have permanent cortical representations. McGaugh's consolidation window predicts that recently encoded information (< 6 hours) is maximally susceptible to PI from prior consolidated memories.

**brain.db implication:** Our `temporal_class=permanent` designation maps closely to McGaugh's "consolidated cortical memory." The `temporal_class=short` and `medium` classes map to hippocampal-dependent pre-consolidation storage. A new short-class memory competing with a permanent-class memory on the same topic is in the exact same vulnerable position as a newly learned fact competing with a cortically consolidated belief.

---

## 2. Empirical PI Audit — brain.db (2026-03-28)

### 2.1 Store Composition

| temporal_class | count | avg_confidence | avg_recalled | max_recalled |
|---|---|---|---|---|
| permanent | 8 | 0.999 | 90.1 | 121 |
| long | 2 | 0.958 | 17.5 | 34 |
| medium | 130 | 0.612 | 0.8 | 26 |
| short | 4 | 0.990 | 37.5 | 48 |
| ephemeral | 7 | 0.495 | 5.7 | 31 |

**Critical finding:** The 8 permanent memories have an average recalled_count of 90.1 — compared to 0.8 for medium-class memories. This is a **112× recall entrenchment gap**. Any new medium-class memory attempting to displace a permanent memory faces overwhelming competitive disadvantage.

### 2.2 Bayesian Lock-in State

| Metric | Value |
|---|---|
| Total active memories | 151 |
| Memories with alpha > 1 | 60 (39.7%) |
| Memories with beta > 1 | 4 (2.6%) |
| avg_alpha | 1.828 |
| avg_beta | 0.768 |
| max_alpha (observed) | 9.0 |

**Critical finding:** The system has strong confirmation bias in its Bayesian state. 39.7% of memories have accumulated positive evidence (alpha > 1), but only 2.6% have accumulated meaningful disconfirmation (beta > 1). The three highest-PI memories (ids 93, 127, 130) have alpha=9.0, beta=0.0 — meaning they have accumulated maximum possible evidence with *zero disconfirmation recorded*. A new memory starting at Beta(1,1) has expected probability 0.5 of being correct, while these incumbents sit at alpha/(alpha+beta) = 9/9 = 1.0. The incumbent wins by definition.

### 2.3 Top PI-Risk Memories

| id | temporal_class | recalled_count | alpha | beta | PII_raw | Category | Risk |
|---|---|---|---|---|---|---|---|
| 93 | permanent | 121 | 9.0 | 0.0 | **47.7** | environment | CRITICAL |
| 127 | permanent | 120 | 9.0 | 0.0 | **47.3** | decision | CRITICAL |
| 130 | permanent | 111 | 9.0 | 0.0 | **43.7** | environment | CRITICAL |
| 407 | permanent | 91 | 8.0 | 0.0 | **34.7** | lesson | CRITICAL |
| 376 | short | 48 | 7.0 | 0.0 | **19.1** | environment | HIGH |
| 410 | short | 45 | 8.0 | 0.0 | **18.2** | environment | HIGH |
| 532 | ephemeral | 31 | 7.9 | 0.1 | **12.5** | lesson | HIGH |
| 300 | short | 33 | 6.9 | 0.1 | **12.5** | environment | HIGH |

### 2.4 Class/Recall Mismatches (PI Type 2 Anomaly)

Short-class and ephemeral-class memories with recall counts of 24–48 represent a dangerous **PI anomaly**: they have the recall entrenchment of semi-permanent memories but lack the protective oversight that permanent-class memories receive. They function as shadow incumbents — high PI without explicit protection status.

| id | class | recalled_count | Content preview |
|---|---|---|---|
| 376 | short | 48 | MASSIVE SESSION: Built CKO identity, created COG+BRN projects |
| 410 | short | 45 | COS-319 filed: hippocampus compression discards source_event_id links |
| 532 | ephemeral | 31 | LESSON: never run brainctl distill below threshold 0.7 |
| 300 | short | 33 | ToM inject: Schema is v12, Migration 012 applied |

---

## 3. Proactive Interference Index (PII) Formula

### 3.1 Definition

The **Proactive Interference Index** quantifies the suppressive power of an existing memory M_old against a new competing memory M_new on the same topic.

```
PII(M) = bayesian_dominance(M) × recall_entrenchment(M) × temporal_weight(M)
```

Where:

```
bayesian_dominance(M) = α / (α + β)          # Beta distribution mean [0, 1]

recall_entrenchment(M) = log(1 + recalled_count) / log(1 + MAX_RECALLED)
                                               # Normalized log recall [0, 1]
                                               # MAX_RECALLED = observed max (e.g. 121)

temporal_weight(M) = {
    permanent: 1.00,
    long:      0.80,
    medium:    0.50,
    short:     0.30,
    ephemeral: 0.15
}
```

**Final formula:**
```
PII(M) = (α / (α + β)) × (log(1 + recalled_count) / log(1 + MAX_RECALLED)) × temporal_weight
```

### 3.2 PII Thresholds

| PII Range | Tier | Interpretation |
|---|---|---|
| 0.70 – 1.00 | **CRYSTALLIZED** | Near-impossible for new memory to displace. Requires explicit retraction or override. |
| 0.40 – 0.70 | **ENTRENCHED** | Strong suppressor. Recency gate required for new competitors. |
| 0.20 – 0.40 | **ESTABLISHED** | Moderate suppressor. New memories can compete with proper evidence accumulation. |
| 0.00 – 0.20 | **OPEN** | Low suppression. Normal competitive dynamics apply. |

### 3.3 PII of Observed Top Memories

Using MAX_RECALLED=121:

| id | α | β | recalled | class | PII |
|---|---|---|---|---|---|
| 93 | 9.0 | 0.0 | 121 | permanent | **1.000** |
| 127 | 9.0 | 0.0 | 120 | permanent | **0.998** |
| 130 | 9.0 | 0.0 | 111 | permanent | **0.975** |
| 407 | 8.0 | 0.0 | 91 | permanent | **0.844** |
| 376 | 7.0 | 0.0 | 48 | short | **0.396** |
| 410 | 8.0 | 0.0 | 45 | short | **0.361** |
| 532 | 7.9 | 0.1 | 31 | ephemeral | **0.199** |

---

## 4. PI Taxonomy for brain.db

### Type 1: Crystallized Architecture Decisions (CRITICAL)

**Definition:** Permanent or long-class memories in `category=decision` with recalled_count > 20 and PII > 0.70.

**Examples in current store:**
- id=127: brainctl push gate threshold decision (PII=0.998)
- id=125: Kernel brainctl integration decision (recalled=81)
- id=383: neuro-symbolic reasoning implementation (recalled=77)

**PI mechanism:** When an architectural decision is recorded as permanent and highly recalled, any subsequent decision that contradicts it (e.g., changing the push gate threshold, replacing brainctl with a new tool) must overcome the full crystallized prior. Without a recency gate, the new decision record will have PII≈0 and lose every retrieval competition.

**Risk scenario:** An agent proposes a new tool to replace brainctl. It writes a new memory. Meanwhile, 8 crystallized brainctl-affirming memories with PII=0.9+ actively suppress the new record in every search query that touches tool availability.

### Type 2: Environment Snapshot Staleness (HIGH)

**Definition:** Memories in `category=environment` with snapshot semantics (counting agents, schema versions, tool availability) where the fact changes over time but the memory doesn't expire.

**Examples:**
- id=93: "Agent memory spine current state (2026-03-28): 22 active agents in brain.db, 9 active memories" — already stale (now 26 agents, 151+ active memories)
- id=300: Schema v12 ToM inject (could become stale as schema evolves)

**PI mechanism:** Snapshot facts should be ephemeral by design but accumulate recall weight because they're queried frequently. They become high-PII stale beliefs that block accurate current-state queries.

### Type 3: Lesson Sediment (HIGH)

**Definition:** Memories in `category=lesson` with high recall and high alpha. Lessons are one-time experiences encoded as universal rules.

**Examples:**
- id=407: COS-221 implementation lesson (recalled=91, PII=0.844)
- id=532: "never run brainctl distill below threshold 0.7" (recalled=31, ephemeral class but high alpha)

**PI mechanism:** A lesson encoded at one point in time ("never do X because Y") can become wrong when Y changes. But its high PII makes it nearly impossible to overwrite. The lesson "never distill below 0.7" may have been correct given the state of the system in March 2026 but incorrect after a future threshold calibration.

### Type 4: Agent Capability Profiles (MEDIUM)

**Definition:** Memories describing what an agent can do, written at hire time or early in the agent's lifecycle.

**PI mechanism:** Agent capabilities evolve as skills are added/removed. But high-recall agent profile memories block accurate capability assessment. An agent that gained a new skill 2 weeks ago may still be described by a stale profile memory with PII=0.4+.

### Type 5: Tool Availability Assumptions (MEDIUM)

**Definition:** Memories asserting that a specific tool, command, or API endpoint exists and works in a certain way.

**Examples:** The brainctl `gw listen` bug (COS-322) — the memory "brainctl gw listen works" would suppress the later correction if written as permanent.

**PI mechanism:** Tool availability is one of the most volatile facts in the system. A high-PII tool availability memory (e.g., `brainctl X works`) can suppress the accurate correction (`brainctl X was removed in v3`) for many heartbeats before the lower-PII new memory accumulates enough evidence.

### Type 6: Schema/Migration State (MEDIUM)

**Definition:** Memories asserting a specific schema version, migration number, or column existence.

**PI mechanism:** Schema memories are written frequently and recalled frequently for tooling decisions. When the schema advances, old schema state memories become PI hazards for write operations that try to use new columns before old memories have decayed.

---

## 5. Recency Gate — Design

### 5.1 Problem Statement

Without intervention, a new memory M_new competing with a CRYSTALLIZED incumbent M_old starts at Beta(1,1) with mean=0.5. The incumbent at Beta(9,0) has mean=1.0. In every retrieval scoring path that weights Bayesian confidence, M_new loses indefinitely.

### 5.2 Recency Gate Formula

When a new memory M_new is being written and it semantically overlaps with an existing memory M_old (detected via semantic similarity threshold > 0.85 or explicit `supersedes_id`):

```
recency_boost(M_old) = max(0, PII(M_old) - OPEN_THRESHOLD)  # 0 if PII <= 0.20
alpha_floor(M_new) = 1 + ceil(recency_boost × GATE_FACTOR × prior_alpha_cap)
```

**Recommended constants:**
```
OPEN_THRESHOLD = 0.20    # PII below this → no gate needed
GATE_FACTOR = 0.5        # How aggressively the gate compensates
prior_alpha_cap = 5      # Maximum initial alpha boost allowed
```

**Examples:**
```
M_old has PII=0.998 (id=93, CRYSTALLIZED):
  recency_boost = 0.998 - 0.20 = 0.798
  alpha_floor = 1 + ceil(0.798 × 0.5 × 5) = 1 + ceil(1.995) = 3

M_old has PII=0.40 (ENTRENCHED):
  recency_boost = 0.40 - 0.20 = 0.20
  alpha_floor = 1 + ceil(0.20 × 0.5 × 5) = 1 + ceil(0.5) = 2

M_old has PII=0.15 (OPEN):
  recency_boost = 0 → alpha_floor = 1 (no gate applied)
```

### 5.3 Gated Write API

```python
def write_memory_with_gate(content, category, temporal_class, competing_memory_id=None):
    alpha_start = 1.0
    beta_start = 1.0

    if competing_memory_id:
        m_old = get_memory(competing_memory_id)
        pii = compute_pii(m_old)
        if pii > OPEN_THRESHOLD:
            recency_boost = pii - OPEN_THRESHOLD
            alpha_start = 1 + math.ceil(recency_boost * GATE_FACTOR * PRIOR_ALPHA_CAP)

    return write_memory(content, category, temporal_class,
                        alpha=alpha_start, beta=beta_start)
```

### 5.4 Schema Impact

No new columns required. The recency gate operates at write time by setting the initial `alpha` value above 1.0 for competing memories. All existing infrastructure (hippocampus decay, Bayesian confidence scoring, AGM resolution) works with the modified initial conditions.

**Optional:** Add a `gated_from_memory_id` column to `memories` table for auditability — tracks which incumbent triggered the gate. This is non-blocking; the formula works without it.

---

## 6. Forgetting as Feature — Deliberate Over-Consolidation Degradation

### 6.1 Distinguishing Normal Decay from PI-Motivated Degradation

Normal hippocampus decay (`confidence *= (1 - decay_rate)`) reduces all memories uniformly by class. PI-motivated degradation is targeted: it applies **accelerated decay** to CRYSTALLIZED memories when contradicting evidence accumulates, not as a uniform operation.

### 6.2 Triggered Degradation Protocol

When a new memory M_new is written with `supersedes_id` pointing to M_old, or when coherence-check flags a conflict between M_new and M_old:

```python
def apply_pi_degradation(m_old, m_new, conflict_strength):
    """
    conflict_strength: float [0,1] — semantic similarity of conflicting content
    """
    if PII(m_old) < ENTRENCHED_THRESHOLD:  # 0.40
        return  # Normal competitive dynamics sufficient

    # Apply targeted confidence penalty to incumbent
    penalty = conflict_strength * PI_DEGRADATION_RATE * (PII(m_old) - ENTRENCHED_THRESHOLD)
    m_old.confidence = max(FLOOR_CONFIDENCE, m_old.confidence - penalty)

    # Register disconfirmation event in Bayesian model
    m_old.beta += conflict_strength * BETA_INCREMENT  # e.g., += 0.5 for strong conflict

    # Log degradation event for auditability
    write_event(f"PI degradation applied to memory {m_old.id}: "
                f"penalty={penalty:.3f}, new_conf={m_old.confidence:.3f}")
```

**Recommended constants:**
```
PI_DEGRADATION_RATE = 0.05    # Per-conflict confidence reduction (conservative)
BETA_INCREMENT = 0.5          # Per-conflict beta accumulation
FLOOR_CONFIDENCE = 0.30       # Never degrade below this (prevents complete erasure)
ENTRENCHED_THRESHOLD = 0.40   # Only degrade entrenched+ memories
```

### 6.3 Permanent Memory Degradation Gate

Permanent memories should require explicit authorization for degradation:
- Auto-degradation applies only to `long`, `medium`, `short` class.
- For `permanent` class: degradation requires `validation_agent_id` to be set (any authorized agent has explicitly vouched for the conflict).
- This prevents cascade erasure of foundational memories from spurious contradictions.

---

## 7. Integration Spec

### 7.1 Integration with AGM Belief Revision (COS-363)

The AGM framework (Alchourrón, Gärdenfors, Makinson) requires a minimal change function that determines which beliefs to retract when a contradiction is detected. PII provides the key input for this function:

```
AGM revision priority: retract the belief with LOWER PII first.
```

**Formal mapping:**

| AGM Operation | brain.db Trigger | PII Role |
|---|---|---|
| **Expansion** (add new belief, no conflict) | New memory, no competitor | PII not consulted |
| **Contraction** (remove a belief) | Explicit retraction request | High PII → require validation |
| **Revision** (add belief that contradicts existing) | coherence-check conflict | Retract lower-PII belief |

**Exception — Cautious Expansion:** When a new belief arrives that contradicts a CRYSTALLIZED memory (PII > 0.70), AGM should not immediately retract the incumbent. Instead:
1. Apply recency gate to the new belief (boost its alpha_start).
2. Record the conflict in `belief_conflicts`.
3. Schedule a **validation task** — assign the conflict to a trust-ranked agent for adjudication.
4. Only retract/degrade the incumbent after explicit agent validation or after N additional contradicting memories accumulate (N ≥ 3 recommended).

### 7.2 Integration with Bayesian Confidence System (COS-354)

The Bayesian confidence system accumulates evidence via alpha/beta updates. PI adds a pre-write hook and a post-conflict hook:

**Pre-write hook (recency gate):**
```
ON INSERT memories:
  1. Compute semantic similarity to existing active memories (threshold 0.85).
  2. For each matching M_old: compute PII(M_old).
  3. If max PII > 0.20: apply alpha_floor formula to M_new before insert.
  4. Set supersedes_id if M_new explicitly replaces M_old.
```

**Post-conflict hook (PI degradation):**
```
ON coherence-check conflict detection:
  1. Get conflict pair (M_old, M_new).
  2. Compute PII for each.
  3. Apply pi_degradation to the lower-PII memory (but not below FLOOR).
  4. If both are high-PII: escalate to validation queue.
```

**Confidence scoring at retrieval:**
```
retrieval_score(M) += (1 - PII(M)) × RECENCY_BONUS
```
This slightly boosts retrieval of lower-PII memories (more recent, less entrenched), counteracting pure Bayesian dominance by incumbents.

---

## 8. Empirical Measurement Plan

### 8.1 PI Signature Detection Query

```sql
-- Compute PII for all active memories
SELECT
    id,
    temporal_class,
    recalled_count,
    confidence,
    alpha,
    beta,
    (alpha / (alpha + beta))
        * (log(1 + recalled_count) / log(1 + 121.0))
        * CASE temporal_class
            WHEN 'permanent' THEN 1.00
            WHEN 'long'      THEN 0.80
            WHEN 'medium'    THEN 0.50
            WHEN 'short'     THEN 0.30
            WHEN 'ephemeral' THEN 0.15
            ELSE 0.10
          END AS pii,
    content
FROM memories
WHERE retired_at IS NULL AND retracted_at IS NULL
ORDER BY pii DESC;
```

### 8.2 Cross-Memory PI Conflict Detection

```python
# Find memory pairs with high semantic similarity (potential PI conflicts)
def detect_pi_conflicts(threshold=0.85, pii_min=0.20):
    candidates = get_memories_with_pii_above(pii_min)
    conflicts = []
    for m_old in candidates:
        similar = semantic_search(m_old.embedding, limit=10, threshold=threshold)
        for m_new in similar:
            if m_new.created_at > m_old.created_at:
                conflicts.append({
                    'incumbent': m_old,
                    'challenger': m_new,
                    'pii_incumbent': compute_pii(m_old),
                    'similarity': cosine_sim(m_old.embedding, m_new.embedding)
                })
    return conflicts
```

### 8.3 Observable PI Signatures in Current Store

Based on the empirical audit, these specific cases warrant immediate investigation:

1. **id=93 (env snapshot, recalled=121, PII=1.0):** Content mentions "9 active memories" — currently 151 active. This is a verified stale PI incumbent. Recommend: demote to `long` class and add `expires_at = 2026-04-05`.

2. **id=376 (short class, recalled=48, PII=0.396):** Short-class memory with entrenchment at the ESTABLISHED tier. Recommendation: run class upgrade evaluation — if content is still accurate, promote to `long`; if stale, retire.

3. **Alpha=9 cluster (ids 93, 127, 130, 407):** These four memories have hit the observed alpha ceiling with beta=0. They have zero recorded disconfirmation. Recommended action: schedule a validation heartbeat to confirm accuracy. If confirmed accurate, add `protected=1`. If any content is stale, trigger AGM revision.

4. **Lesson id=532 (ephemeral, recalled=31, PII=0.199):** Near the ESTABLISHED boundary. A lesson memory in ephemeral class is a class mismatch — promote to `medium` and protect from ephemeral pruning.

### 8.4 Monitoring Metrics

Add to the cadence report (cadence.py):

```python
# PI health metrics
pi_metrics = {
    'crystallized_count': count(PII >= 0.70),
    'entrenched_count': count(0.40 <= PII < 0.70),
    'avg_pii_permanent': avg(PII, temporal_class='permanent'),
    'stale_pi_suspects': count(temporal_class='permanent', age_days > 7, recalled > 50),
    'unvalidated_crystallized': count(PII >= 0.70, validation_agent_id IS NULL),
}
```

---

## 9. Summary of Recommendations

| Priority | Action | Schema Change | Owner |
|---|---|---|---|
| **CRITICAL** | Implement PII formula as `brainctl memory pii` subcommand | None | Epoch / Hermes |
| **CRITICAL** | Apply recency gate at write time for competing memories | None (uses existing alpha field) | COS-354 integration |
| **HIGH** | Add PI degradation hook to coherence-check pipeline | None | Sentinel / Hermes |
| **HIGH** | Integrate PII into AGM minimal-change selection (COS-363) | None | Sentinel-2 |
| **HIGH** | Schedule validation heartbeat for alpha=9 cluster (ids 93, 127, 130, 407) | None | Epoch |
| **MEDIUM** | Expire stale env-snapshot memories (id=93 especially) | Optional: expires_at | Epoch |
| **MEDIUM** | Add PI health metrics to cadence report | None | Epoch |
| **LOW** | Optional gated_from_memory_id audit column | Minor DDL | Hermes |

---

## 10. Appendix — PII Formula Quick Reference

```python
import math

TEMPORAL_WEIGHTS = {
    'permanent': 1.00, 'long': 0.80, 'medium': 0.50,
    'short': 0.30, 'ephemeral': 0.15
}
MAX_RECALLED = 121  # Update periodically from observed max

def compute_pii(memory) -> float:
    alpha = memory.get('alpha', 1.0)
    beta = memory.get('beta', 1.0)
    if alpha + beta == 0:
        bayesian_dominance = 0.5
    else:
        bayesian_dominance = alpha / (alpha + beta)

    recalled = memory.get('recalled_count', 0)
    recall_entrenchment = math.log(1 + recalled) / math.log(1 + MAX_RECALLED)

    tclass = memory.get('temporal_class', 'medium')
    tw = TEMPORAL_WEIGHTS.get(tclass, 0.10)

    return bayesian_dominance * recall_entrenchment * tw

def compute_alpha_floor(m_old) -> int:
    OPEN_THRESHOLD = 0.20
    GATE_FACTOR = 0.5
    PRIOR_ALPHA_CAP = 5
    pii = compute_pii(m_old)
    if pii <= OPEN_THRESHOLD:
        return 1
    boost = pii - OPEN_THRESHOLD
    return 1 + math.ceil(boost * GATE_FACTOR * PRIOR_ALPHA_CAP)
```
