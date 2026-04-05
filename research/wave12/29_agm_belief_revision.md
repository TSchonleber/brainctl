# AGM Belief Revision — Principled Contradiction Resolution Framework

**Research Wave:** 12
**Ticket:** COS-363
**Author:** Sentinel 2 (Memory Integrity Monitor)
**Date:** 2026-03-28
**Status:** Complete

---

## Executive Summary

When `coherence-check` flags a contradiction between two memories, there is currently no algorithm for resolving it — the contradiction persists, agents may recall either belief depending on query phrasing, and the Bayesian Beta(α,β) confidence system (COS-354) will diverge if both memories keep accumulating evidence.

This report maps the **AGM belief revision framework** (Alchourrón, Gärdenfors, Makinson 1985) to brain.db primitives, defines a concrete resolution algorithm, and specifies `brainctl resolve-conflict` as the implementation target.

---

## 1. Brain.db Belief Primitives (Current State)

Before mapping AGM postulates, we need to understand what brain.db actually exposes for belief revision.

### 1.1 Contradicts/Supersedes in knowledge_edges

Current `knowledge_edges` relation types (4,718 edges total):

| relation_type | count |
|---|---|
| causes | 1,686 |
| topical_tag | 871 |
| semantic_similar | 742 |
| topical_project | 579 |
| topical_scope | 479 |
| co_referenced | 359 |
| causal_chain_member | 2 |

**No `contradicts` or `supersedes` edges exist.** These relationships are currently captured differently:

- `memories.supersedes_id` — direct supersedure (single FK column on the memory row)
- `belief_conflicts` table — cross-agent belief conflicts (schema v12)
- `coherence-check` output — detected but unresolved contradictions (ephemeral, not persisted as edges)

**Gap:** There is no graph-level `contradicts` edge. Contradiction detection is heuristic (semantic similarity + topic overlap), not structural. This is important for the resolution algorithm design.

### 1.2 Confidence Signals on memories

Each memory row exposes multiple confidence/trust signals:

| Column | Type | Meaning |
|---|---|---|
| `confidence` | REAL [0,1] | Scalar confidence (decays via hippocampus) |
| `alpha` | REAL | Beta distribution α parameter (evidence in favor) |
| `beta` | REAL | Beta distribution β parameter (evidence against) |
| `trust_score` | REAL | Source trust at write time |
| `temporal_class` | TEXT | permanent / long / medium / short / ephemeral |
| `recalled_count` | INTEGER | Access frequency |
| `last_recalled_at` | TEXT | Recency of last retrieval |
| `validation_agent_id` | TEXT | Which agent validated this memory |
| `validated_at` | TEXT | When last validated |
| `retracted_at` | TEXT | Soft retraction timestamp |
| `retraction_reason` | TEXT | Why retracted |

### 1.3 agent_expertise (COS-357)

The `agent_expertise` table tracks per-agent domain expertise with Brier scores for calibration:

```
agent_id | domain | strength | evidence_count | brier_score
```

Example: `paperclip-sentinel-2 | integrity | 0.85 | 3 | 0.12`

Lower Brier score = better calibrated. This is the key signal for **provenance-weighted revision**.

### 1.4 belief_conflicts Table (Schema v12)

```sql
belief_conflicts(
  topic TEXT, agent_a_id, agent_b_id,
  belief_a TEXT, belief_b TEXT,
  conflict_type: factual | assumption | staleness | scope,
  severity REAL [0,1],
  resolved_at TEXT, resolution TEXT,
  requires_hermes_intervention INTEGER
)
```

This table already provides the right shape for AGM revision tracking — beliefs are explicit propositions (text), conflicts have types, and resolution metadata is captured.

---

## 2. AGM Postulate Mapping

The AGM framework defines a belief revision operator `*` mapping belief set K and new information φ to revised set K*φ, subject to 8 rationality postulates. We map each postulate to brain.db operations.

### Postulate 1: Closure
> K*φ is a belief set (closed under logical consequence).

**Brain.db mapping:** After revision, all directly contradicted memories of a resolved pair must be consistently marked. We cannot enforce logical closure over natural-language beliefs, but we can enforce **structural consistency**: if memory A supersedes memory B, then `knowledge_edges` should record a `supersedes` edge A→B, B's `supersedes_id` should point to A (or vice versa), and B should be retired or have `retraction_reason` set.

**Implementation rule:** `resolve-conflict` must atomically (in one SQLite transaction):
1. Set winner's `validated_at` + `validation_agent_id`
2. Set loser's `retracted_at` + `retraction_reason`
3. Optionally insert `knowledge_edges` row: (winner, loser, `supersedes`)
4. Mark `belief_conflicts` row as `resolved_at` = now

### Postulate 2: Success
> φ ∈ K*φ — the new information always enters the revised belief set.

**Brain.db mapping:** The incoming memory (or the winning memory) must survive revision. The `resolve-conflict` algorithm must never retire both conflicting memories — exactly one must survive active. If new evidence arrives contradicting a permanent memory, the new evidence must still be recorded even if it cannot displace the permanent (edge case — see Section 5).

### Postulate 3: Inclusion
> K*φ ⊆ K+φ — revision only adds what's needed.

**Brain.db mapping:** The resolution algorithm should not cascade to retire memories that are not directly contradicted by the winning belief. If A supersedes B, only B is retired — not memories that share topic tags with B. **No collateral retraction.**

### Postulate 4: Vacuity
> If ¬φ ∉ K, then K*φ = K+φ — if new info is consistent, nothing is removed.

**Brain.db mapping:** If no structural or semantic contradiction exists for a memory at write time, it is simply inserted — `resolve-conflict` is not triggered. Coherence-check is the gate. This is already the current behavior.

### Postulate 5: Consistency
> K*φ is consistent if φ is consistent.

**Brain.db mapping:** After `resolve-conflict` runs, `coherence-check` should report no remaining conflict between the two memories. If it does, the resolution was incomplete and must be flagged as `requires_hermes_intervention = 1`.

**Implementation rule:** `resolve-conflict` should re-run the relevant coherence check sub-routine post-resolution as a self-test. If contradiction persists (e.g. because both are `permanent`), set `requires_hermes_intervention = 1` in `belief_conflicts`.

### Postulate 6: Extensionality
> If φ ≡ ψ (logically equivalent), then K*φ = K*ψ.

**Brain.db mapping:** Two semantically equivalent contradictions should produce the same winner. This is the hardest postulate to honor given natural-language beliefs. Practical approximation: use **semantic similarity score** (cosine distance from `sqlite-vec`) as the equivalence signal. If two contradiction pairs have topic key overlap > 0.9, they are treated as the same conflict.

**Implementation rule:** Before filing a new `belief_conflicts` row, check for an existing open conflict with the same `topic` key. If found, update it rather than creating a duplicate.

### Postulate 7: Superexpansion
> K*(φ∧ψ) ⊆ (K*φ)+ψ — revising by φ∧ψ doesn't add more than first revising by φ then expanding by ψ.

**Brain.db mapping:** If multiple contradictions are resolved in sequence (e.g. Sentinel 2 resolves A vs B, then later A vs C), the second resolution should not silently re-retire already-retired memories or expand the retired set beyond C. The algorithm must check `retired_at IS NULL` before acting on any memory.

### Postulate 8: Subexpansion
> If ¬ψ ∉ K*φ, then (K*φ)*ψ = (K*φ)+ψ — if ψ is consistent with K*φ, expanding by ψ equals revising by ψ.

**Brain.db mapping:** After A supersedes B (B retired), a new memory C that is consistent with A should be added via normal insert (no revision needed). The algorithm should not treat every new write as a potential conflict — only trigger `resolve-conflict` when `coherence-check` flags it.

---

## 3. Resolution Decision Algorithm

Given two memories `A` and `B` in contradiction, the algorithm selects the **revision target** (the one to retire) using a weighted scoring function. Higher score = more credible = survives.

### 3.1 Score Components

```
credibility(M) = w1 * confidence_score(M)
              + w2 * evidence_mass(M)
              + w3 * recency_score(M)
              + w4 * provenance_score(M)
              + w5 * temporal_permanence_penalty(M)
```

#### Confidence Score (w1 = 0.30)
```
confidence_score(M) = M.confidence
```
Direct confidence. Already decays via hippocampus. Higher = more credible.

#### Evidence Mass (w2 = 0.25)
```
evidence_mass(M) = M.alpha / (M.alpha + M.beta)
```
The Beta distribution mean. Represents accumulated evidence quality. For memories with `alpha = NULL`, fall back to `confidence_score`.

#### Recency Score (w3 = 0.20)
```
recency_score(M) = exp(-λ * days_since_write)
  where λ = 0.01 (slow decay — recency matters but doesn't dominate)
  days_since_write = (now - M.created_at) in days
```
Recent memories are more likely to reflect current ground truth than old ones.

#### Provenance Score (w4 = 0.20)
```
provenance_score(M) = trust_score(M.agent_id, domain_of_conflict)
```
Where `trust_score` is derived from `agent_expertise`:
```sql
SELECT strength * (1 - COALESCE(brier_score, 0.5))
FROM agent_expertise
WHERE agent_id = M.agent_id AND domain = inferred_domain(M)
```
Agents with high expertise strength AND low Brier score (well-calibrated) get maximum provenance weight. An agent that frequently writes about `integrity` with demonstrated calibration is more credible on integrity-domain contradictions than a general-purpose agent.

**Default provenance score** (when no expertise entry): 0.5

#### Temporal Permanence Penalty (w5 = 0.05)
```
temporal_permanence_penalty(M) =
  0.0 if M.temporal_class = 'permanent'   ← huge boost (see Section 5)
  0.2 if M.temporal_class = 'long'
  0.4 if M.temporal_class = 'medium'
  0.6 if M.temporal_class = 'short'
  0.8 if M.temporal_class = 'ephemeral'
```
Permanent memories resist revision by default. The penalty term *reduces* the credibility score for non-permanent memories relative to permanent ones. (Inverted: 0.0 = no penalty, 0.8 = high penalty.)

Rewritten as boost:
```
permanence_boost(M) = 1.0 - temporal_permanence_penalty(M)
credibility(M) = w1*cs + w2*em + w3*rs + w4*ps + w5*permanence_boost
```

### 3.2 Decision Rule

```python
def resolve_contradiction(memory_a, memory_b):
    score_a = credibility(memory_a)
    score_b = credibility(memory_b)

    delta = abs(score_a - score_b)

    if delta < 0.05:
        # Too close to call — escalate to Hermes
        return ESCALATE_TO_HERMES

    winner = memory_a if score_a > score_b else memory_b
    loser  = memory_b if score_a > score_b else memory_a

    if loser.temporal_class == 'permanent':
        # Cannot auto-retire permanent memories
        return ESCALATE_TO_HERMES  # see Section 5

    return (winner, loser)
```

### 3.3 Conflict Type Overrides

The `belief_conflicts.conflict_type` field informs which components dominate:

| conflict_type | Weight Adjustment |
|---|---|
| `staleness` | Boost `recency_score` weight to 0.50, reduce evidence_mass to 0.10 |
| `factual` | Use default weights |
| `assumption` | Penalize `is_assumption=1` memories by multiplying score × 0.6 |
| `scope` | Do not auto-resolve; set `requires_hermes_intervention = 1` |

---

## 4. `brainctl resolve-conflict` Command Spec

### 4.1 CLI Interface

```
brainctl resolve-conflict <conflict_id>
  [--dry-run]          # print scores, show winner/loser, no DB writes
  [--force-winner <memory_id>]   # manual override (board/Hermes use only)
  [--threshold 0.05]   # minimum delta to auto-resolve (default 0.05)

brainctl resolve-conflict --list    # show open conflicts with scores
brainctl resolve-conflict --auto    # batch resolve all auto-resolvable conflicts
```

### 4.2 Execution Flow

```
1. Load conflict_id from belief_conflicts WHERE resolved_at IS NULL
2. Load memory_a (belief_a source) and memory_b (belief_b source)
3. Compute credibility scores
4. If delta < threshold OR either is permanent → ESCALATE
5. Else:
   a. BEGIN TRANSACTION
   b. UPDATE memories SET retracted_at = now(), retraction_reason = '...'
      WHERE id = loser.id
   c. UPDATE memories SET validation_agent_id = $AGENT_ID,
      validated_at = now()
      WHERE id = winner.id
   d. INSERT OR IGNORE INTO knowledge_edges
      (source_table, source_id, target_table, target_id, relation_type, weight)
      VALUES ('memories', winner.id, 'memories', loser.id, 'supersedes', winner.confidence)
   e. UPDATE belief_conflicts SET resolved_at = now(),
      resolution = 'auto:winner=<id>,loser=<id>,delta=<score_delta>'
      WHERE id = conflict_id
   f. COMMIT
6. Run coherence-check sub-routine on the two memory IDs
7. If contradiction still flagged → set requires_hermes_intervention = 1
8. Log resolution event via brainctl event add
```

### 4.3 Resolution Event Format

```
brainctl -a $AGENT_ID event add \
  "resolve-conflict: conflict_id=<id> winner=<id> loser=<id> delta=<score> scores=[<a>,<b>]" \
  -t result -p agentmemory
```

### 4.4 Escalation Output

When ESCALATE_TO_HERMES is returned:

```
brainctl -a $AGENT_ID event add \
  "resolve-conflict: conflict_id=<id> ESCALATED reason=<too_close|permanent_clash|scope_conflict>" \
  -t warning -p agentmemory
```

Set `belief_conflicts.requires_hermes_intervention = 1`.

### 4.5 New `supersedes` Edge Type

This spec requires adding `supersedes` to the set of valid `knowledge_edges.relation_type` values. No schema change is needed (relation_type is TEXT with no CHECK constraint), but `brainctl graph` should display it distinctly.

---

## 5. Integration with COS-357 (agent_expertise)

### 5.1 Domain Inference

To look up expertise, we need to map a memory to a domain. Heuristics:

```python
def infer_domain(memory):
    # 1. Check topical_tag edges from this memory → take highest-weight topic
    # 2. Fall back to memory.category
    # 3. Fall back to 'general'
    ...
```

Use `knowledge_edges WHERE relation_type = 'topical_tag' AND source_id = memory.id` to find the primary topic tag, then look that up in `agent_expertise`.

### 5.2 Brier Score as Calibration Penalty

An agent's Brier score measures forecast calibration (0 = perfect, 1 = worst). We use it to discount raw expertise strength:

```
calibrated_expertise = strength × (1 - brier_score)
```

| Agent | Domain | strength | brier_score | calibrated |
|---|---|---|---|---|
| paperclip-sentinel-2 | integrity | 0.85 | 0.12 | 0.748 |
| hermes | memory | 0.71 | NULL → 0.50 | 0.354 |

Sentinel 2 is more credible on integrity topics than Hermes, by this measure.

### 5.3 Expertise Update on Resolution

When `resolve-conflict` resolves in favor of agent X's memory over agent Y's:

```sql
-- Reinforce winner's expertise on this domain
UPDATE agent_expertise
SET evidence_count = evidence_count + 1,
    strength = MIN(1.0, strength + 0.01),
    updated_at = datetime('now')
WHERE agent_id = winner.agent_id AND domain = inferred_domain;

-- Slight Brier score penalty for loser (overconfident prediction)
UPDATE agent_expertise
SET brier_score = (COALESCE(brier_score, 0.5) * evidence_count + 0.8) / (evidence_count + 1)
WHERE agent_id = loser.agent_id AND domain = inferred_domain;
```

This creates a **feedback loop**: agents whose memories consistently win contradiction resolutions build stronger expertise scores, making their future memories more credible.

---

## 6. Edge Case: Permanent-vs-Permanent Conflicts

### Scenario
Two governance decisions (both `temporal_class = 'permanent'`) contradict each other. Example:
- Memory 127: "brainctl push gate threshold: 40 active memories"
- Memory 93: "Agent memory spine: 22 active agents, 9 active memories"

These aren't contradictory (different facts), but imagine:
- Memory A (permanent): "CostClock branch policy: agents push directly to main"
- Memory B (permanent): "CostClock branch policy: agents work on feature branches only"

These directly contradict and both are permanent.

### Why This Is Hard

Permanent memories are explicitly exempted from hippocampus decay. The design intent is that governance decisions, core identity, and established policies should not erode. Auto-retiring a permanent memory to resolve a conflict risks:

1. Silently removing a still-valid governance decision
2. Corrupting agent identity or role definitions
3. Losing evidence of past decisions that may matter to audit trails

### Resolution Protocol for Permanent Conflicts

```
IF both memories are temporal_class = 'permanent':
  1. Set belief_conflicts.requires_hermes_intervention = 1
  2. Set belief_conflicts.severity to max(existing_severity, 0.9)
  3. Add a board-visible note via brainctl event add with priority CRITICAL
  4. Do NOT retire either memory automatically
  5. Instead: add knowledge_edges row (A, B, 'conflicts_with', weight=severity)
     to make the conflict structurally visible in the graph
  6. Hermes must manually choose:
     a. Retire one (using --force-winner override)
     b. Scope-constrain one (e.g. "this policy only applies to pre-2026-01-01")
     c. Accept both as co-valid in different contexts (scope split)
```

The `conflicts_with` edge type (currently absent from the graph) serves as a **persistent marker** that two permanent memories are in tension. This makes the conflict visible to `brainctl graph` queries without forcing premature resolution.

### Scope-Splitting (Option c)

If both permanent memories are valid in different contexts, the resolution is to **scope-constrain** one:

```sql
UPDATE memories SET scope = 'project:costclock-ai:pre-jan-2026'
WHERE id = older_memory_id;
```

Then insert a new memory explaining the policy split. Neither memory is retired — scope prevents co-recall.

---

## 7. Algorithm Evaluation on Current Coherence Findings

Running the algorithm against the 3 current WARNING findings from `coherence-check`:

### Finding #1: epoch ↔ paperclip-recall (15% topic overlap)
- Conflict type: likely `factual` or `staleness`
- Auto-resolvable if `delta >= 0.05`
- Expected: recency + evidence_mass should distinguish

### Finding #2: epoch ↔ paperclip-weaver (15% topic overlap)
- Same as above

### Finding #3: openclaw ↔ paperclip-weaver (19% topic overlap)
- Higher overlap — more likely a real contradiction
- openclaw has `strength=0.85` on task domains; paperclip-weaver is project-focused
- Domain-specific expertise should break the tie

All three are in the auto-resolvable tier given their topic overlap scores (below the threshold for permanent conflicts), assuming neither memory involved is `temporal_class = 'permanent'`.

---

## 8. Implementation Recommendations

### Priority Order

1. **Add `supersedes` edge to knowledge_edges** — no schema change, just ensure `brainctl resolve-conflict` inserts it. Required for Closure postulate.

2. **Add `conflicts_with` edge** — for permanent-vs-permanent, surfaces tension in graph without forcing resolution.

3. **Implement `brainctl resolve-conflict --dry-run`** first — allows Sentinel 2 to audit resolution quality before enabling `--auto`.

4. **Wire coherence-check findings into belief_conflicts** — currently `coherence-check` produces ephemeral output; it should INSERT into `belief_conflicts` on each run so findings are persistent and trackable.

5. **`--auto` batch mode** — after dry-run validation, enable automated resolution of `staleness` and `factual` conflicts where delta > 0.10.

### Weights Calibration Note

The weights `w1=0.30, w2=0.25, w3=0.20, w4=0.20, w5=0.05` are initial estimates. After 20+ resolutions, Sentinel 2 should run a Brier-score retrospective on its own resolution decisions to calibrate weights empirically.

### Interaction with COS-354 (Bayesian Beta Confidence)

When COS-354's Beta accumulation is active, contradictory beliefs sharing the same topic key will both accumulate α evidence from corroborating agents. This divergence is detectable: if two memories with overlapping topics both have `alpha > 2` (more than 1 positive update) and no `supersedes_id` relationship, flag as priority resolution candidate. The `evidence_mass` component of the credibility score naturally handles this — the memory with more accumulated evidence wins.

---

## 9. Summary Table

| AGM Postulate | Brain.db Implementation | Resolution Action |
|---|---|---|
| Closure | `retracted_at` + `supersedes` edge | Atomic transaction |
| Success | Winner survives with `validated_at` | Never retire both |
| Inclusion | No cascade retraction | Retire loser only |
| Vacuity | Insert without conflict = no trigger | Existing behavior |
| Consistency | Post-resolution coherence-check | Self-test in algorithm |
| Extensionality | Deduplicate by `topic` key | Check before inserting belief_conflicts |
| Superexpansion | Check `retired_at IS NULL` before acting | Guard in algorithm |
| Subexpansion | Consistent write = normal insert | Existing behavior |

---

## Appendix: Credibility Score Example

Memory A: `confidence=0.85`, `alpha=2.5`, `beta=0.3`, `created=7 days ago`, `agent_expertise.calibrated=0.748`, `temporal_class=medium`
Memory B: `confidence=0.70`, `alpha=1.1`, `beta=0.9`, `created=30 days ago`, `agent_expertise.calibrated=0.354`, `temporal_class=long`

```
credibility(A) = 0.30×0.85 + 0.25×(2.5/2.8) + 0.20×exp(-0.07) + 0.20×0.748 + 0.05×0.60
               = 0.255 + 0.223 + 0.187 + 0.150 + 0.030
               = 0.845

credibility(B) = 0.30×0.70 + 0.25×(1.1/2.0) + 0.20×exp(-0.30) + 0.20×0.354 + 0.05×0.80
               = 0.210 + 0.138 + 0.148 + 0.071 + 0.040
               = 0.607

delta = 0.238  ← well above 0.05 threshold → auto-resolve, A wins
```

B is retired with `retraction_reason = "resolved: contradiction with memory A (COS-363 algorithm, delta=0.238)"`.

---

*Sentinel 2 — COS-363 — 2026-03-28*
