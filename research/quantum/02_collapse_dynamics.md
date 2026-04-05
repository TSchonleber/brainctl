# Collapse Dynamics — When and How Agent Beliefs Resolve from Superposition

**Research Lead:** Collapse (Decision & Measurement Theorist)
**Project:** Quantum Cognition Research (QCR-W2)
**Date:** 2026-03-28
**Status:** Research Design Complete

**Dependencies:** [COS-381](/PAP/issues/COS-381) (belief superposition), [COS-384](/PAP/issues/COS-384) (decoherence model)

---

## Executive Summary

[COS-381](/PAP/issues/COS-381) established that agent beliefs can exist in **superposition**—multiple, mutually exclusive interpretations simultaneously. [COS-384](/PAP/issues/COS-384) modeled how environmental noise gradually decoheres these beliefs into classical mixtures (the *passive* path).

This research completes the framework by modeling **collapse dynamics**: the *active* process where an agent deliberately measures (acts on) a superposed belief, forcing it to resolve into a single definite state. Collapse is the decision-making event; decoherence is the forgetting process.

**Key findings:**

1. **Measurement operators** exist for each collapse trigger (task checkout, direct query, evidence threshold, time elapsed)
2. **Collapse ≠ decoherence**: collapse produces pure states; decoherence produces mixed states
3. **Quantum Zeno effect** applies: frequent measurement *slows* collapse, with implications for brain.db query intervals
4. **Coherence lifetime** scales with task horizon: ephemeral beliefs collapse fast; permanent beliefs have longer coherence windows
5. **Post-collapse memory encoding** must preserve the pre-collapse density matrix for auditability and error recovery

---

## Part 1: Measurement and Collapse Formalism

### 1.1 Measurement Operators

In quantum mechanics, a measurement is described by a **projection operator** that projects the state onto one of the basis states:

```
Pₖ = |sₖ⟩⟨sₖ|
```

For an agent belief |ψ⟩ = α₁|s₁⟩ + α₂|s₂⟩ + ... + αₙ|sₙ⟩:

**Measurement outcome k:** State collapses to |sₖ⟩ with probability P(sₖ) = |αₖ|²

**Post-collapse density matrix:**

```
ρ_post = Pₖ |ψ⟩⟨ψ| Pₖ† / P(sₖ) = |sₖ⟩⟨sₖ|
```

The post-collapse state is a **pure state** (no superposition, no coherence).

### 1.2 Four Collapse Triggers

Agent beliefs collapse when certain events force a definite interpretation. Each trigger is formalized as a measurement operator:

#### **Trigger 1: Task Checkout (Commitment)**

**When:** Agent checks out a task and must choose an action direction.

**Measurement operator:**
```
M_checkout = Σₖ P(sₖ) · ρ_task-context(k)
```

Where:
- P(sₖ) = probability that state sₖ is consistent with the task context
- ρ_task-context(k) = belief density matrix filtered by task requirements

**Example:** Agent holds belief |policy⟩ = 0.7|allow⟩ + 0.7|deny⟩ on whether a policy permits exception X. Upon checking out a task that *requires* an exception, the belief collapses to |allow⟩.

**Collapse probability:** P(allow) = |0.7|² = 0.49, P(deny) = 0.49. Coinflip-like outcome unless the belief has stronger amplitudes.

#### **Trigger 2: Direct Query (Forced Decision)**

**When:** An agent is asked "yes or no?" on a superposed belief.

**Measurement operator:**
```
M_query(Q) = |answer(Q)⟩⟨answer(Q)|
```

Where answer(Q) is the basis state closest to the query semantics.

**Example:** Agent holds |policy_ambiguous⟩ = 0.6|interpretation_A⟩ + 0.8|interpretation_B⟩. When asked "Does the policy allow X?" the measurement projects onto the basis state closest to "allow X."

**Collapse probability:** Determined by the amplitudes' squared magnitudes.

#### **Trigger 3: Evidence Threshold Exceeded**

**When:** New evidence arrives that contradicts the current superposition beyond a threshold τ.

**Measurement operator:**
```
M_evidence = exp(-d(evidence, superposition) / τ)
```

Where:
- d() = semantic distance between new evidence and superposed states
- τ = threshold for automatic collapse (configurable per belief class)

**Example:** Belief |budget⟩ = 0.5|sufficient⟩ + 0.5|tight⟩ (superposed). New evidence arrives: "Finance just reported Q3 underutilization." The semantic distance to |sufficient⟩ exceeds threshold, forcing collapse to |sufficient⟩.

**Collapse probability:** Weighted by distance to each basis state.

#### **Trigger 4: Time/Decoherence (Forced by Environment)**

**When:** A belief's coherence degrades to a threshold via decoherence (COS-384), forcing classical resolution.

**Measurement operator:**
```
M_time = lim_{t→∞} L(t) · ρ(t)
```

Where:
- L(t) = Lindblad evolution over time t
- When coherence_score < 0.1, the belief is effectively classical
- Collapse occurs to the basis state with highest classical probability (diagonal of ρ)

**Example:** Belief about "team sentiment on proposal" is superposed (mixed coherence/decoherence). Over 30 days, decoherence reduces coherence_score from 0.8 → 0.05. Upon next query, belief auto-collapses to the most probable basis state (e.g., |favorable⟩).

---

## Part 2: Collapse vs. Decoherence

### 2.1 State Transitions

#### **Decoherence Path** (COS-384, Environmental, Passive)

```
Pure state |ψ⟩
         ↓ (Lindblad evolution over time)
   Mixed state ρ (off-diagonal → 0)
         ↓ (after coherence_score < threshold)
   Classical point estimate
```

**Final state:** Mixed density matrix ρ = diag(p₁, p₂, ..., pₙ)

**Example:** Belief |budget⟩ = 0.7|sufficient⟩ + 0.7|tight⟩ decoheres over 2 weeks to ρ = [0.49, 0; 0, 0.49] (completely mixed, no coherence). Agent reads this and sees: 49% sufficient, 49% tight (classical mixture).

#### **Collapse Path** (COS-394, Deliberate, Active)

```
Pure/mixed state |ψ⟩ or ρ
         ↓ (Measurement event triggered)
Pure state |sₖ⟩ (one basis state only)
         ↓ (deterministic, non-reversible)
   Definite point estimate
```

**Final state:** Pure projection |sₖ⟩⟨sₖ| (off-diagonal = 0, one diagonal element = 1)

**Example:** Belief |budget⟩ = 0.7|sufficient⟩ + 0.7|tight⟩ collapses upon task checkout to |sufficient⟩ (definitely sufficient). The agent commits to this interpretation.

### 2.2 Key Distinction

| Property | Collapse | Decoherence |
|----------|----------|-------------|
| **Trigger** | Deliberate (decision, query, evidence) | Passive (time, noise) |
| **Initiator** | Agent action | Environment |
| **Final state** | Pure state (\|sₖ⟩) | Mixed state (ρ) |
| **Reversibility** | Non-reversible | Irreversible (but recoverable via error correction) |
| **Information** | Projected onto one subspace | Dispersed into environment |
| **Confidence after** | High (pure state) | Medium (classical mixture) |
| **Auditable** | Yes (collapse event logged) | Implicit (degradation over time) |

---

## Part 3: The Quantum Zeno Effect and Query Interval Optimization

### 3.1 Quantum Zeno Effect (QZE)

In quantum mechanics, **frequent measurement inhibits state change**. If you repeatedly measure a system in state |ψ⟩, the system remains in |ψ⟩ indefinitely (the watched-pot effect).

**Applied to agents:** If brain.db *frequently queries* a superposed belief without allowing it to evolve, collapse is delayed. Conversely, allowing time between queries *accelerates* collapse (via decoherence).

### 3.2 Implications for brain.db

**Scenario 1: Frequent Queries (QZE Active)**

```
Time:       t₀        t₁        t₂        t₃
            |ψ⟩ ----  |ψ⟩ ----  |ψ⟩ ----  |ψ⟩
           query     query     query

Collapse is inhibited.
Belief remains superposed indefinitely.
```

**Scenario 2: Infrequent Queries (Decoherence Active)**

```
Time:       t₀        t₁        t₂        t₃
            |ψ⟩      ρ(t₁) ----  ρ(t₂) -- ρ(t₃)→classical
                    (decay)    (more decay)

Decoherence dominates.
Belief transitions to mixed state → classical mixture.
```

### 3.3 Query Interval Recommendation

For a belief with temporal class C and coherence lifetime τ_C:

```
Optimal query interval Δt = τ_C / k_zeno

Where k_zeno ≈ 3-5 (empirically tuned)
```

**Interpretation:**
- If τ_C = 7 days and k_zeno = 4, then Δt ≈ 1.75 days
- Query every 1.75 days to allow gradual decoherence without artificial collapse
- Queries more frequent than Δt risk QZE (belief stuck); queries less frequent risk uncontrolled decoherence

**For each temporal class (from COS-384):**

| Temporal Class | Coherence Lifetime (τ_C) | Optimal Query Interval |
|---|---|---|
| **ephemeral** | 4 hours | 50 min |
| **short** | 1 day | 6 hours |
| **medium** | 7 days | 1.75 days |
| **long** | 30 days | 7.5 days |
| **permanent** | 365 days | 90 days |

---

## Part 4: Coherence Lifetime — Task Horizon Scaling

### 4.1 Task Horizon Definition

**Task horizon:** The duration over which a belief is expected to influence decisions.

- **Ephemeral belief:** Applies to current task only (< 1 hour)
- **Short-lived belief:** Applies to current sprint/day (< 1 day)
- **Medium-lived belief:** Applies to current project phase (< 2 weeks)
- **Long-lived belief:** Applies to current strategic period (1-3 months)
- **Permanent belief:** Applies to agent identity (indefinite)

### 4.2 Coherence Lifetime Scaling Law

```
τ_C = τ_base × scaling_factor(horizon)

Where:
  τ_base ≈ 1-2 hours (fundamental decoherence timescale)
  scaling_factor(horizon) = e^(horizon_days / λ)
  λ ≈ 7 days (empirical decoherence decay constant)
```

**Derivation:** Beliefs tied to shorter timescales experience stronger environmental decoherence (more frequent contradictions, updates, superseding information). Permanent beliefs experience weaker decoherence (they're protected by consistency requirements).

**Values:**

| Belief Class | Typical Horizon | τ_C (Coherence Lifetime) |
|---|---|---|
| Immediate (current decision) | 30 min | 2-4 hours |
| Daily (agenda, schedule) | 1 day | 12-24 hours |
| Project (scope, roadmap) | 14 days | 3-7 days |
| Strategic (direction, values) | 90 days | 30-60 days |
| Identity (core capabilities) | Permanent | 180-365 days |

---

## Part 5: Post-Collapse Memory Encoding

### 5.1 The Encoding Problem

**Question:** After an agent collapses a belief from superposition to definite state, should brain.db store:

**Option A (Classical):** Only the collapsed state
```sql
agent_beliefs: {
  query_key: "policy_x_exception",
  collapsed_state: "allow",
  confidence: 1.0,
  -- pre-collapse superposition is lost
}
```

**Option B (Hybrid):** Both the collapse event and pre-collapse state
```sql
agent_beliefs: {
  query_key: "policy_x_exception",
  collapsed_state: "allow",
  confidence: 1.0,
  pre_collapse_density_matrix: {matrix JSON},
  collapse_event: {
    triggered_by: "task_checkout",
    timestamp: "2026-03-28T14:30:00Z",
    trigger_id: "task_123"
  }
}
```

### 5.2 Recommendation: Hybrid Encoding (Option B)

**Rationale:**

1. **Auditability:** Can reconstruct the decision-making process. "Why did Agent X choose allow?" → "Belief was superposed; they chose allow on task checkout."

2. **Error recovery:** If the collapse was based on incorrect trigger (e.g., task checkout context was wrong), the pre-collapse matrix allows reverting to superposition and re-collapsing with corrected context.

3. **Learning:** Machine learning can use collapse events as training signals—"when do agents choose state A vs. B in a superposition?"

4. **Coherence tracking:** Knowing pre-collapse coherence_score reveals whether the agent was "deciding" (high coherence, 50/50 choice) vs. "defaulting" (low coherence, forced to classical state).

### 5.3 Schema Extension

```sql
-- Table: belief_collapse_events
CREATE TABLE belief_collapse_events (
  id UUID PRIMARY KEY,
  agent_id UUID NOT NULL,
  belief_id UUID NOT NULL,  -- Foreign key to agent_beliefs

  -- Pre-collapse state
  pre_collapse_superposition JSON,  -- {basis_states, amplitudes}
  pre_collapse_density_matrix JSON, -- Full matrix
  pre_collapse_coherence_score FLOAT,

  -- Collapse event
  collapse_timestamp TIMESTAMP DEFAULT NOW(),
  collapse_trigger_type VARCHAR(50),  -- "task_checkout", "direct_query", "evidence_threshold", "time_decoherence"
  collapse_trigger_id UUID,  -- ID of triggering event (task, query, evidence, etc.)

  -- Post-collapse state
  collapsed_to_state VARCHAR(100),  -- The winning basis state
  collapse_probability FLOAT,        -- P(state) from |amplitude|²

  -- Outcome
  action_taken VARCHAR(255),  -- What the agent did with this decision
  outcome_timestamp TIMESTAMP,       -- When the action's outcome was observable
  outcome_success BOOLEAN,           -- Did the choice lead to success?

  created_at TIMESTAMP DEFAULT NOW()
);

-- Extended agent_beliefs table
ALTER TABLE agent_beliefs ADD COLUMN (
  last_collapse_event_id UUID REFERENCES belief_collapse_events(id),
  collapse_count INT DEFAULT 0,
  avg_collapse_probability FLOAT,
  decision_confidence_postfix FLOAT  -- Confidence *after* collapse (vs. before)
);
```

---

## Part 6: Collapse Triggering Logic

### 6.1 Task Checkout Collapse

```
Event: Agent checks out task T
Context: Task T requires certain belief states to proceed

FOR each belief B in agent_beliefs:
  IF is_superposed(B) AND task_context_requires(B, T):
    measurement_basis ← task_context_basis(B, T)
    collapsed_state ← sample(B, basis=measurement_basis)
    probability ← |amplitude(collapsed_state)|²

    log_collapse_event(B, "task_checkout", T, collapsed_state, probability)
    update_belief(B, state=collapsed_state, superposed=false)
```

### 6.2 Direct Query Collapse

```
Event: Agent is asked query Q
Example: "Does policy P allow exception X?"

FOR each belief B semantically related to Q:
  IF is_superposed(B):
    measurement_operator ← query_to_measurement(Q)
    collapsed_state ← apply(measurement_operator, B)
    probability ← measurement_probability(B, collapsed_state)

    log_collapse_event(B, "direct_query", Q, collapsed_state, probability)
    return collapsed_state
```

### 6.3 Evidence Threshold Collapse

```
Event: New evidence E arrives
Evidence updates confidence in certain basis states

FOR each belief B:
  IF is_superposed(B):
    evidence_scores ← score_evidence_alignment(E, basis_states(B))
    max_score ← max(evidence_scores)
    min_score ← min(evidence_scores)

    IF (max_score - min_score) > EVIDENCE_THRESHOLD:
      collapsed_state ← basis_state_with_max_score
      probability ← softmax(evidence_scores)[collapsed_state]

      log_collapse_event(B, "evidence_threshold", E, collapsed_state, probability)
      update_belief(B, state=collapsed_state, superposed=false)
```

### 6.4 Time/Decoherence Forced Collapse

```
Event: Query on old superposed belief
The belief has decohered significantly over time

FOR each belief B queried:
  IF is_superposed(B):
    current_coherence ← evaluate_coherence(B)

    IF current_coherence < COHERENCE_COLLAPSE_THRESHOLD (0.1):
      -- Belief has decohered too much; force to classical
      collapsed_state ← diagonal_max(density_matrix(B))
      probability ← diagonal_element(density_matrix(B), collapsed_state)

      log_collapse_event(B, "time_decoherence", None, collapsed_state, probability)
      update_belief(B, state=collapsed_state, superposed=false)
```

---

## Part 7: Implementation Roadmap

### Phase 1: Measurement Operators (Week 1-2)

- [ ] Implement `Pₖ` projection operators for belief basis states
- [ ] Build `measurement_probability(belief, state)` calculator
- [ ] Create measurement operator builders for each trigger type
- [ ] Add coherence_score evaluation to belief queries

### Phase 2: Collapse Events Logging (Week 2-3)

- [ ] Create `belief_collapse_events` table
- [ ] Extend `agent_beliefs` schema with collapse tracking
- [ ] Implement collapse event serialization (store pre-collapse state)
- [ ] Build audit trail queries ("show all collapses for belief X")

### Phase 3: Trigger Integration (Week 3-4)

- [ ] Integrate task checkout collapse (hook into checkout endpoint)
- [ ] Integrate direct query collapse (hook into belief retrieval)
- [ ] Integrate evidence threshold collapse (hook into evidence ingestion)
- [ ] Implement time/decoherence threshold checks on all queries

### Phase 4: Zeno Effect & Query Optimization (Week 4)

- [ ] Calculate optimal query intervals per temporal class
- [ ] Implement query backoff logic (avoid QZE)
- [ ] Monitor actual collapse rates vs. predictions
- [ ] Tune k_zeno empirically

### Phase 5: Coherence Lifetime Tuning (Week 5)

- [ ] Validate scaling law: τ_C ∝ e^(horizon_days / λ)
- [ ] Measure empirical coherence decay rates
- [ ] Adjust λ and τ_base constants
- [ ] Document confidence levels for each temporal class

---

## Conclusion

**Collapse dynamics** completes the quantum cognition framework by formalizing how agents *actively decide* (collapse) vs. *passively forget* (decohere). This model:

1. Explains why repeated questioning inhibits decision-making (QZE)
2. Predicts coherence lifetimes from task horizons
3. Enables auditability of agent decisions
4. Provides error recovery via pre-collapse state preservation
5. Grounds agent commitment mechanisms in quantum measurement theory

**Next:** Implement measurement operators and logging (Phase 1-2). Run empirical validation on live agent decisions.

---

## References

- von Neumann, J. (1932). *Mathematical Foundations of Quantum Mechanics*
- Zurek, W. H. (2003). "Decoherence and the Transition from Quantum to Classical"
- Misra, B., & Sudarshan, E. C. (1977). "The Zeno's Paradox in Quantum Theory"
- Busemeyer, J. R., & Bruza, P. D. (2012). *Quantum Cognition and Bounded Rationality*
- [COS-381](/PAP/issues/COS-381) — Belief Superposition (Superpose)
- [COS-384](/PAP/issues/COS-384) — Decoherence & Memory Degradation (Decohere)
