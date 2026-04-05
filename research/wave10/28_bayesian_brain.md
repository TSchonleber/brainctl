# Bayesian Brain — Formal Probabilistic Reasoning Over Uncertain Memories
## Wave 10 Research Deliverable

**Author:** Epoch (Temporal Cognition Engineer)
**Task:** [COS-344](/COS/issues/COS-344)
**Date:** 2026-03-28
**DB State:** 22MB brain.db · ~121 active memories · 1,117 events · 26 agents
**Prereqs:** Wave 1 (01_spaced_repetition, 05_consolidation_cycle), Wave 2 (11a causal graph, 12b advanced retrieval)

---

## Executive Summary

The current confidence system is **ad hoc**: exponential decay with fixed λ by temporal_class, plus a 15% asymptotic boost on recall. It works but has no principled uncertainty model — it cannot answer "how sure are we that X is true?" or "how much evidence do we have either way?"

The **Bayesian Brain hypothesis** (Knill & Pouget 2004) says the brain represents beliefs as probability distributions, not point estimates. Applied to brain.db, this means replacing the scalar `confidence` with a Beta distribution `Beta(α, β)` where:
- `α` = evidence in favor (successful recalls, supporting observations)
- `β` = evidence against (contradictions, disconfirmed recalls, time-as-irrelevance)
- `confidence` = `α / (α + β)` — still a scalar, still backwards-compatible

This gives proper uncertainty quantification: two memories with `confidence=0.7` but `α+β=3` vs `α+β=300` are *very* different. The first has weak prior, the second strong evidence. The current system cannot distinguish them.

**Central recommendation:** Add `confidence_alpha` and `confidence_beta` columns to `memories`. Replace the asymptotic recall boost and exponential decay multiplier with proper Beta posterior updates. Optional: add 1-hop belief propagation over `knowledge_edges` and Thompson sampling in `brainctl vsearch`.

---

## 1. The Bayesian Brain Hypothesis

### 1.1 Core Claim (Knill & Pouget 2004)

Knill & Pouget's 2004 *Nature Neuroscience* review argues that:

> "The brain represents sensory information probabilistically, in the form of probability distributions over the possible states of the world."

The claim is not metaphorical. Neurons represent posterior probability distributions — they encode uncertainty. Perception, action, and decision-making are all implementations of Bayesian inference: combining prior beliefs with new evidence to update posteriors.

Key supporting evidence:
- **Cue integration** follows optimal Bayes-weighting (visual + proprioceptive cues combine by reliability-weighted average)
- **Motor adaptation** follows Kalman filter predictions (forward model = prior, sensory feedback = likelihood)
- **Categorical perception** shows ideal Bayesian decision boundaries

### 1.2 Mapping to brain.db

| Biological | brain.db analog |
|-----------|----------------|
| Prior `P(H)` | `confidence` at encoding time (trust in source, context richness) |
| Likelihood `P(E\|H)` | How well this recall event matches expected use; `salience_score` at retrieval |
| Posterior `P(H\|E)` | Updated `confidence` after each recall event |
| Sensory noise | Embedding distance uncertainty; query mismatch |
| Perceptual prior | Project-level or temporal-class priors |

The biological insight: **confidence should be a full distribution, not a point**. A memory with 20 confirmed recalls at `confidence=0.8` is fundamentally more reliable than a new memory that starts at `confidence=0.8` by assignment. The current scalar cannot encode this difference.

### 1.3 Why This Matters Now

At 121 active memories with a retention rate shaped by ad hoc thresholds:
- Retirement threshold at `confidence < 0.15` is arbitrary — no principled basis
- Asymptotic boost (`α=0.15 * (1 - confidence)`) gives diminishing returns regardless of evidence strength
- Decay (`exp(-λ * days)`) applies identically to a memory with 50 confirmed recalls vs one never recalled

The Beta distribution encodes *evidence mass*. A well-evidenced memory resists decay; a sparsely-evidenced one should be more volatile. This is the core gain.

---

## 2. Belief Propagation (Pearl)

### 2.1 Message Passing in Memory Graphs

Pearl's belief propagation algorithm (1988) performs exact inference in tree-structured Bayesian networks by passing "messages" (probability factors) between nodes. For loopy graphs (like `knowledge_edges`), Loopy BP is an approximation that usually converges.

In brain.db context:
- **Nodes** = memories
- **Edges** = `knowledge_edges` (supports, contradicts, derived_from, co_referenced, supersedes)
- **Messages** = confidence updates propagating through the graph when a memory's posterior changes

### 2.2 Propagation Rules

When memory **A** is confirmed (recall boost):

```
For each edge (A → B, type='supports', weight=w):
    B.confidence_alpha += w * delta_alpha_A * hop_decay

For each edge (A → B, type='contradicts', weight=w):
    B.confidence_beta += w * delta_alpha_A * hop_decay  # A confirmed → B weakened

For each edge (A → B, type='derived_from', weight=w):
    B.confidence_alpha += w * delta_alpha_A * hop_decay * 0.5  # weaker than supports
```

When memory **A** is contradicted (recall penalty):

```
For each edge (A → B, type='supports', weight=w):
    B.confidence_beta += w * delta_beta_A * hop_decay  # A weakened → B weakened

For each edge (A → B, type='contradicts', weight=w):
    B.confidence_alpha += w * delta_beta_A * hop_decay  # A contradicted → B strengthened
```

Hop decay: `hop_decay = 0.5^hop_distance` — 1st hop gets 50%, 2nd hop 25%, stop at 2 hops.

### 2.3 Practical Scoping

Full belief propagation on the 2,675-edge `knowledge_edges` graph is overkill. The recommendation:

1. **Default:** No propagation. Standard Bayesian update only.
2. **Optional flag:** `brainctl recall --propagate` triggers 1-hop belief propagation.
3. **Consolidation cycle:** Run 1-hop propagation as part of the nightly cycle over high-confidence confirmed memories from the past 24h.

The propagation effect on a single recall is small (w × 0.5 × delta). It accumulates significance over many cycles, which makes nightly batch propagation more appropriate than per-recall propagation.

### 2.4 Causal Chain Integration

The existing causal event graph (COS-184) tracked causality at the event level. Belief propagation extends this to memory confidence: if memory A causes B (derived_from edge), and A is confirmed 10 times, B inherits partial confirmation even if B was never independently recalled. This closes a gap the current system ignores.

---

## 3. Bayesian Updating on Recall

### 3.1 The Current System

```python
# hippocampus.py — current recall boost
new_confidence = confidence + 0.15 * (1.0 - confidence)

# hippocampus.py — current decay
new_confidence = confidence * math.exp(-λ * days)
```

Problems:
- Boost is always 15% of remaining headroom, regardless of whether this is the 1st or 50th recall
- Decay destroys evidence: a memory at `confidence=0.9` after 100 recalls decays just as fast as one at `confidence=0.9` by assignment
- No distinction between "high confidence with strong evidence" and "high confidence with weak evidence"

### 3.2 Proposed Beta Update

Represent confidence as `Beta(α, β)` where point estimate = `α / (α + β)`.

**Successful recall (memory used, not contradicted):**
```python
reward = salience_score  # float in [0.5, 1.5], default 1.0
confidence_alpha += reward
# new confidence = (α + reward) / (α + β + reward)
```

**Contradicted recall (memory recalled but explicitly contradicted):**
```python
penalty = contradiction_weight  # default 1.0, higher for strong contradictions
confidence_beta += penalty
# new confidence = α / (α + β + penalty)
```

**Time passing (Bayesian decay):**
```python
# Time is evidence of irrelevance (Pearl: absence of retrieval = soft disconfirmation)
λ = temporal_class_rate  # same as current λ values
decay_beta = λ * days_since_last_recall
confidence_beta += decay_beta
# new confidence = α / (α + β + decay_beta)
# Crucially: α is preserved — evidence FOR is permanent, only β grows
```

This is *evidence accumulation*, not *multiplicative decay*. A memory with `α=100, β=10` (high evidence, high confidence) is nearly immune to a few days of non-recall. A memory with `α=2, β=1.5` decays fast. **This is the core behavioral difference.**

### 3.3 Initialization Backfill

For existing memories, initialize from current `confidence` score:

```python
# Pseudo-count: evidence_mass = 10 (moderate prior strength)
# Range: 5 (volatile) to 50 (very stable)
evidence_mass = 10
confidence_alpha = evidence_mass * confidence
confidence_beta  = evidence_mass * (1.0 - confidence)

# Special cases:
# recalled_count > 20: evidence_mass = min(50, recalled_count)
# temporal_class = permanent: evidence_mass = 100
# temporal_class = ephemeral: evidence_mass = 3
```

This gives `recalled_count` a role in initial evidence mass — highly-recalled memories start with strong priors, as they should.

### 3.4 Retirement Threshold Revisited

Current threshold: `confidence < 0.15`.

With Beta distributions, a better criterion is **posterior mode + uncertainty**:

```
retire if: (α / (α + β) < 0.15) AND (α + β > 5)
         — OR —
retire if: β > 3 * α AND α + β > 5  # strongly disconfirmed
```

The `α + β > 5` guard prevents retiring newly-created memories with legitimate uncertainty. A memory at `α=1, β=1` (confidence=0.5) is *uncertain*, not *wrong*. One at `α=1, β=20` (confidence=0.048) is effectively disproven.

---

## 4. Hierarchical Bayesian Models

### 4.1 Structure

Hierarchical Bayesian models place priors at multiple levels, where higher-level priors inform lower-level posteriors:

```
World prior
    ↓
Project-level prior:  Beta(α_project, β_project)
    ↓
Agent-level prior:    Beta(α_agent, β_agent)
    ↓
Memory posterior:     Beta(α_memory, β_memory)
```

### 4.2 Project-Level Priors

Different projects have different "truth persistence" characteristics:

| Project type | Prior | Rationale |
|-------------|-------|-----------|
| Stable infrastructure | `Beta(8, 2)` | Config facts change rarely |
| Active feature dev | `Beta(4, 4)` | Neutral — things change often |
| Research/exploration | `Beta(2, 6)` | Expect many contradictions |
| Compliance/audit | `Beta(10, 1)` | High-confidence facts, rare change |

Schema addition:

```sql
CREATE TABLE project_memory_priors (
    project_id      TEXT NOT NULL UNIQUE,
    alpha_prior     REAL NOT NULL DEFAULT 5.0,
    beta_prior      REAL NOT NULL DEFAULT 5.0,
    evidence_mass   REAL NOT NULL DEFAULT 10.0,
    last_calibrated TIMESTAMP DEFAULT (datetime('now')),
    calibration_note TEXT
);
```

When creating a new memory in project P, initialize:

```python
prior = project_memory_priors[project_id]  # or default (5, 5)
confidence_alpha = prior.alpha_prior / (prior.alpha_prior + prior.beta_prior) * prior.evidence_mass
confidence_beta  = prior.beta_prior  / (prior.alpha_prior + prior.beta_prior) * prior.evidence_mass
```

### 4.3 Agent-Level Decay Rate Priors

Some agents work in fast-moving domains (high β-decay), others in stable ones (low β-decay). The temporal_class system partially captures this, but it applies uniformly.

Proposed: `agent_memory_profile` table providing per-agent decay multipliers:

```sql
CREATE TABLE agent_memory_profile (
    agent_id        TEXT NOT NULL UNIQUE,
    decay_multiplier REAL NOT NULL DEFAULT 1.0,  -- >1 = faster decay, <1 = slower
    alpha_scale     REAL NOT NULL DEFAULT 1.0,   -- >1 = larger recall boosts
    updated_at      TIMESTAMP DEFAULT (datetime('now'))
);
```

CostClock-AI agents (billing, auth) work in stable domains → `decay_multiplier = 0.7`.
Research agents work in ephemeral domains → `decay_multiplier = 1.5`.

This avoids forcing all project memories into a single decay regime.

### 4.4 Empirical Calibration

The project priors are initially hand-tuned. The right long-term approach:

1. After each consolidation cycle, compute `mean(confidence)` and `variance(confidence)` per project
2. Fit a Beta distribution to the project's memory confidence distribution
3. Use the fitted α, β as the prior for that project

This makes project priors **self-calibrating** over time — the system learns that `costclock-ai` memories have high persistence, while research task memories are volatile. Runs via the consolidation cycle. Timeline: implement empirical calibration in Wave 11.

---

## 5. Thompson Sampling for Exploration

### 5.1 The Exploitation-Exploration Problem

Current retrieval: rank by `salience_score = 0.45×similarity + 0.25×recency + 0.20×confidence + 0.10×importance`.

This always **exploits** — returns the memories we're most certain about. But a high-confidence memory recalled 100 times may add diminishing value. A medium-confidence memory that has never been surfaced might be exactly the right answer for a new context.

**Thompson sampling** (Thompson 1933, Chapelle & Li 2011) solves the exploration-exploitation tradeoff for Bayesian bandits:

1. For each memory candidate, **sample** a confidence value from `Beta(α, β)` rather than use the point estimate `α/(α+β)`
2. Use the sampled value in the salience formula
3. Rank as normal

Memories with low `α+β` (few observations, high uncertainty) have wide Beta distributions — they'll sometimes get high samples. Memories with high `α+β` (well-evidenced) have narrow distributions — their samples stay near the mean.

The effect: high-uncertainty memories get a chance to surface, naturally exploring the space. If they're confirmed on recall, their α grows and they become reliable. If contradicted, β grows and they fade.

### 5.2 Implementation

```python
def thompson_sample_confidence(alpha: float, beta: float) -> float:
    """Sample confidence from Beta(α, β) for exploration."""
    import random
    # Use rejection sampling or scipy.stats.beta.rvs
    # For zero deps: use the Johnk method
    while True:
        u = random.random() ** (1.0 / alpha)
        v = random.random() ** (1.0 / beta)
        if u + v <= 1.0:
            return u / (u + v)
```

Integration into `brainctl vsearch`:

```bash
brainctl vsearch "task context" --explore        # Thompson sampling mode
brainctl vsearch "task context" --explore 0.3    # 30% exploration fraction
```

In mixed mode: top 70% of results use point-estimate ranking, bottom 30% use Thompson-sampled ranking. This provides stable core results while surfacing exploratory candidates.

### 5.3 Use Cases

| Scenario | Why Thompson helps |
|---------|-------------------|
| Cold-start agent with sparse memories | Surfaces uncertain memories that might match, rather than returning empty |
| Consolidation cycle "discovery" pass | Finds memories never surfaced to test if they're actually useful |
| Research task retrieval | Exploration is valuable — finding non-obvious connections matters more than precision |
| Operational task retrieval | Exploration undesired — use point-estimate mode for predictability |

Recommendation: Thompson sampling **off by default**, available as an explicit flag. The consolidation cycle runs it once per week as a "knowledge audit" pass to identify never-surfaced memories that might deserve promotion or retirement.

---

## 6. Unified Design: Bayesian Confidence Schema

### 6.1 Schema Changes

```sql
-- Add to memories table:
ALTER TABLE memories ADD COLUMN confidence_alpha REAL;
ALTER TABLE memories ADD COLUMN confidence_beta  REAL;

-- New tables:
CREATE TABLE project_memory_priors (
    project_id       TEXT NOT NULL PRIMARY KEY,
    alpha_prior      REAL NOT NULL DEFAULT 5.0,
    beta_prior       REAL NOT NULL DEFAULT 5.0,
    evidence_mass    REAL NOT NULL DEFAULT 10.0,
    last_calibrated  TIMESTAMP DEFAULT (datetime('now')),
    calibration_note TEXT
);

CREATE TABLE agent_memory_profile (
    agent_id         TEXT NOT NULL PRIMARY KEY,
    decay_multiplier REAL NOT NULL DEFAULT 1.0,
    alpha_scale      REAL NOT NULL DEFAULT 1.0,
    updated_at       TIMESTAMP DEFAULT (datetime('now'))
);
```

The existing `confidence` column is **preserved** as the point estimate `= α / (α + β)`. All current brainctl queries continue to work without modification. The Beta parameters are additive metadata.

### 6.2 Backfill Migration

```python
def backfill_beta_params(conn):
    """One-time migration: compute α, β from existing confidence + recalled_count."""
    rows = conn.execute("SELECT id, confidence, recalled_count, temporal_class FROM memories").fetchall()

    updates = []
    for mem_id, conf, recalls, t_class in rows:
        # Base evidence mass from temporal_class
        mass_map = {'permanent': 100, 'long': 30, 'medium': 15, 'short': 8, 'ephemeral': 3}
        base_mass = mass_map.get(t_class, 10)

        # Scale by recall count (up to 3× base mass)
        evidence_mass = min(base_mass * 3, base_mass + recalls * 0.5)

        alpha = max(0.5, evidence_mass * conf)
        beta  = max(0.5, evidence_mass * (1.0 - conf))
        updates.append((alpha, beta, mem_id))

    conn.executemany(
        "UPDATE memories SET confidence_alpha=?, confidence_beta=? WHERE id=?",
        updates
    )
    conn.commit()
    print(f"Backfilled {len(updates)} memories with Beta(α, β) parameters.")
```

### 6.3 Modified Recall Path (hippocampus.py)

```python
def bayesian_recall_update(conn, memory_id: int, salience: float = 1.0, contradicted: bool = False):
    """
    Replace: confidence += 0.15 * (1.0 - confidence)
    With: Beta posterior update
    """
    row = conn.execute(
        "SELECT confidence, confidence_alpha, confidence_beta FROM memories WHERE id=?",
        (memory_id,)
    ).fetchone()

    if row is None:
        return

    conf, alpha, beta = row

    # Fall back to legacy initialization if columns are null
    if alpha is None:
        alpha = max(0.5, 10.0 * conf)
        beta  = max(0.5, 10.0 * (1.0 - conf))

    if not contradicted:
        alpha += salience  # reward proportional to salience
    else:
        beta += salience   # penalty (salience used as contradiction weight)

    new_conf = alpha / (alpha + beta)

    conn.execute(
        """UPDATE memories
           SET confidence=?, confidence_alpha=?, confidence_beta=?,
               recalled_count = recalled_count + 1,
               last_recalled_at = datetime('now')
           WHERE id=?""",
        (new_conf, alpha, beta, memory_id)
    )
    conn.commit()
```

### 6.4 Modified Decay Path (hippocampus.py)

```python
# temporal_class λ values (unchanged from current)
DECAY_RATES = {'ephemeral': 0.20, 'short': 0.07, 'medium': 0.03, 'long': 0.01, 'permanent': 0.0}

def bayesian_decay_update(conn, memory_id: int, days: float, agent_decay_multiplier: float = 1.0):
    """
    Replace: confidence *= exp(-λ * days)
    With: Beta posterior — time adds to β, never subtracts from α
    """
    row = conn.execute(
        "SELECT confidence, confidence_alpha, confidence_beta, temporal_class FROM memories WHERE id=?",
        (memory_id,)
    ).fetchone()

    if row is None:
        return

    conf, alpha, beta, t_class = row

    if alpha is None:
        alpha = max(0.5, 10.0 * conf)
        beta  = max(0.5, 10.0 * (1.0 - conf))

    λ = DECAY_RATES.get(t_class, 0.03) * agent_decay_multiplier

    if λ == 0.0:
        return  # permanent memories: no decay

    # β grows with time — evidence of irrelevance accumulates
    # Decay effect: β_new / (α + β_new) < β / (α + β)
    beta += λ * days
    new_conf = alpha / (alpha + beta)

    conn.execute(
        "UPDATE memories SET confidence=?, confidence_alpha=?, confidence_beta=? WHERE id=?",
        (new_conf, alpha, beta, memory_id)
    )
    conn.commit()
```

**Key behavioral difference from current system:**
- Memory with `α=100, β=10` (100 confirmed recalls) at `confidence=0.91`:
  - **Current:** `0.91 * exp(-0.03 * 30) = 0.91 * 0.407 = 0.370` after 30 days (catastrophic)
  - **Proposed:** `β += 0.03 * 30 = 0.9`, new `conf = 100 / 110.9 = 0.902` (stable — evidence preserved)
- Memory with `α=1.5, β=1.0` (freshly created) at `confidence=0.6`:
  - **Current:** `0.6 * exp(-0.03 * 30) = 0.244` after 30 days
  - **Proposed:** `β += 0.9`, new `conf = 1.5 / 3.4 = 0.441` (decays, but less catastrophically)

The Bayesian system **protects well-evidenced memories from spurious decay** while still allowing uncertain memories to fade.

---

## 7. Uncertainty Quantification API

A key new capability: answering "how sure are we that X is true?"

### 7.1 New brainctl Commands

```bash
# Show confidence interval for a specific memory
brainctl memory confidence 45
# → Memory #45: confidence=0.87 | α=23.4, β=3.5 | 95% CI: [0.74, 0.95] | evidence_mass=26.9

# Query with uncertainty bounds
brainctl search "topic" --with-uncertainty
# Returns each result with: point_estimate, lower_95, upper_95, evidence_mass

# Aggregate belief over multiple memories
brainctl belief "Is Supabase RLS enabled on the users table?"
# → Searches for relevant memories, aggregates posteriors, returns: P=0.92 [0.78, 0.99] based on 3 memories
```

### 7.2 Belief Aggregation

When multiple memories address the same question, combine posteriors:

```python
def aggregate_beliefs(memories: list[dict]) -> dict:
    """
    Aggregate Beta posteriors from multiple memories into a combined belief.
    Uses weighted evidence pooling (not naive multiplication — assumes partial overlap).
    """
    total_alpha = sum(m['confidence_alpha'] * m['similarity_to_query'] for m in memories)
    total_beta  = sum(m['confidence_beta']  * m['similarity_to_query'] for m in memories)

    combined_confidence = total_alpha / (total_alpha + total_beta)

    # Credible interval via Beta quantiles
    from scipy.stats import beta as beta_dist
    lo = beta_dist.ppf(0.025, total_alpha, total_beta)
    hi = beta_dist.ppf(0.975, total_alpha, total_beta)

    return {
        'belief': combined_confidence,
        'credible_interval_95': (lo, hi),
        'evidence_mass': total_alpha + total_beta,
        'n_memories': len(memories)
    }
```

This enables the metacognition layer (COS-110) to answer not just "do we know X?" but "how confident are we in X, and how much evidence do we have?"

---

## 8. Implementation Roadmap

### Phase 1 — Schema + Backfill (1-2 days, no behavior change)

1. Add `confidence_alpha`, `confidence_beta` columns to `memories`
2. Add `project_memory_priors` and `agent_memory_profile` tables
3. Run backfill migration (computes α, β from existing confidence + recalled_count)
4. Verify: `point_estimate = α/(α+β)` matches existing `confidence` within ε=0.001
5. Ship: no functional change yet — columns are populated but unused

### Phase 2 — Bayesian Recall Update (2-3 days, low risk)

1. Modify `record_recall()` in `hippocampus.py` to use Beta update
2. Keep existing asymptotic boost as fallback if α/β columns are null (safety net)
3. Compare: run both paths for 1 week, log deltas, verify behavior

### Phase 3 — Bayesian Decay Update (2-3 days, medium risk)

1. Modify `decay_pass()` in `hippocampus.py` to use β-increment instead of confidence multiplier
2. Add `agent_memory_profile` lookup for per-agent `decay_multiplier`
3. Verify: permanent memories unchanged; highly-recalled memories more stable than now

### Phase 4 — 1-hop Belief Propagation (3-4 days, optional)

1. Add `propagate_belief()` function triggered by recall events
2. Default: off. Enabled by `brainctl recall --propagate`
3. Nightly consolidation: run 1-hop propagation over confirmed recalls from past 24h
4. Monitor: log propagation deltas to events table

### Phase 5 — Thompson Sampling (1-2 days, optional)

1. Add `--explore` flag to `brainctl vsearch`
2. Default: off. Research agents may opt in via `agent_memory_profile`
3. Weekly "knowledge audit" in consolidation cycle using Thompson mode

### Phase 6 — Hierarchical Priors + Calibration (ongoing)

1. Populate `project_memory_priors` with hand-tuned values
2. Add calibration pass to consolidation cycle (fits Beta to project memory distribution)
3. Quarterly: review and adjust prior strength

---

## 9. Risk Analysis

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Backfill produces wrong α/β for edge-case memories | Low | Clamp α, β ≥ 0.5; validate point estimate matches within ε |
| β-growth decay more aggressive than exponential for low-evidence memories | Medium | Shadow-run both systems for 1 week before switching |
| Belief propagation creates confidence inflation cycles | Medium | Cap single-propagation delta at 0.1; max 2 hops; monitor via events table |
| Thompson sampling surfaces stale memories | Low | Thompson mode only for explicit `--explore` flag; disabled for operational tasks |
| scipy dependency for credible intervals | Low | Use Beta distribution approximation formula instead; or add scipy to brainctl deps |
| Calibration diverges for active projects | Low | Evidence mass cap at 100 × temporal_class_base prevents over-concentration |

---

## 10. Open Questions

1. **What evidence mass (`α+β`) is right for initialization?** 10 is a starting guess. Too low → volatile, too high → slow to adapt. Needs empirical study using held-out recall patterns.

2. **Should confirmed recall always have salience_weight=1.0?** Or should the salience score itself modulate the reward? A high-salience recall (exactly the right memory at the right time) should contribute more evidence than a low-salience recall.

3. **Does 1-hop propagation create coherence issues with the contradiction detection system (06_contradiction_detection)?** If memory A contradicts memory B, and both get confirmed, belief propagation needs to resolve the conflict rather than silently boost both.

4. **Can we validate the Beta model empirically?** COS-110 identified calibration as the single most valuable empirical study. Proposed: take 20 memories with known "ground truth" status (explicitly confirmed or retired), see if the Beta posteriors predict their outcomes better than current scalar confidence.

5. **Interaction with the distillation pipeline:** When a set of episodic memories is consolidated into a semantic memory, what are the initial α, β for the new memory? Option A: sum the evidence (α_new = Σα_i). Option B: use the project prior. Option C: weight by similarity to the consolidated content. This needs a design decision before implementing hierarchical consolidation.

---

## 11. Connections to Prior Research

| Prior deliverable | How Bayesian Brain extends it |
|-------------------|-------------------------------|
| 01_spaced_repetition | Replaces exponential decay with β-increment; recall boost becomes α-increment |
| 05_consolidation_cycle | Propagation pass added to nightly cycle; consolidation inherits evidence mass |
| 03_knowledge_graph | Belief propagation uses existing `knowledge_edges` — no schema change needed |
| 11a_causal_event_graph | Causal edges carry confidence; causal confirmation propagates via derived_from edges |
| 11b_metacognition | Uncertainty quantification (`credible_interval_95`) directly feeds knowledge gap detection |
| 12b_advanced_retrieval | Thompson sampling adds exploration tier to existing FTS5+vec retrieval |
| 12c_adversarial_robustness | Evidence mass guards against confidence inflation via adversarial recalls — β grows under repeated contradiction |

---

## Summary Table

| Component | Status | Priority | Effort |
|-----------|--------|----------|--------|
| Schema: add α, β columns | Design complete | High | 0.5d |
| Backfill migration | Design complete | High | 0.5d |
| Bayesian recall update | Design complete | High | 1-2d |
| Bayesian decay update | Design complete | High | 1-2d |
| Project-level priors | Design complete | Medium | 1d |
| Agent decay profiles | Design complete | Medium | 1d |
| 1-hop belief propagation | Design complete | Low | 2-3d |
| Thompson sampling | Design complete | Low | 1d |
| Uncertainty quantification API | Design complete | Medium | 1-2d |
| Empirical calibration pass | Requires Wave 11 | Low | ongoing |

**Recommended entry point:** Phase 1 (schema + backfill) is zero-risk and unlocks all subsequent phases. Ship that first, then Phase 2 (recall update) as the highest-value behavioral change.

---

*Deliverable for [COS-344](/COS/issues/COS-344). Filed under ~/agentmemory/research/wave10/.*
