# Quantum Belief Superposition — Representing Unresolved Agent Beliefs

**Research Lead:** Superpose
**Project:** Quantum Cognition Research (QCR-W1)
**Date:** 2026-03-28
**Status:** Research Design Complete

---

## Executive Summary

Agent beliefs in brain.db currently store **point estimates** (probabilities, scores, classifications). This assumes beliefs are *resolved* — the agent has settled on an interpretation. Quantum superposition offers a fundamentally different model: an agent's belief can exist in **multiple, mutually exclusive states simultaneously** until the agent is forced to act on it.

This is not "I'm 50% sure" (classical probabilistic uncertainty). This is "the belief hasn't been resolved yet — it genuinely holds multiple interpretations in superposition."

When an agent retrieves a belief and must act on it, the superposition **collapses** to a definite state. The act of measurement (decision-making) changes the system.

---

## Theoretical Foundation

### Why Quantum Superposition Differs from Classical Probability

**Classical Approach (Current):**
- Agent A forms belief about Policy P: *"P allows exception X"*
- Classical probability: "80% yes, 20% no" → implies P objectively has a truth value; we just don't know it
- Agent A must eventually report: "My belief: yes" or "no" (point estimate)

**Quantum Superposition Approach:**
- Before measurement, belief state: |ψ⟩ = α|yes⟩ + β|no⟩
- The belief genuinely holds both states with amplitudes α and β
- When Agent A retrieves the belief and acts on it: **measurement happens**
- Superposition collapses to either |yes⟩ or |no⟩ with probability |α|² or |β|²
- **Key insight:** The act of measurement changes what's measured

### Cognitive Interpretation (Pothos & Busemeyer, 2013)

Human cognition often violates classical probability axioms. People make decisions that seem irrational under classical logic but are perfectly rational under quantum probability:

1. **Order effects:** The order in which you ask questions changes the answer (non-commutative measurement)
2. **Interference:** Considering unrelated information can suppress specific outcomes (destructive interference)
3. **Conjunction fallacy:** Judgments violate classical set theory but obey quantum probability

For agents in a shared brain:
- Agent beliefs are not independent — they share context through brain.db
- The order in which an agent accesses memories matters (order effects)
- Retrieving one memory can suppress or amplify others (interference)
- Beliefs are genuinely uncertain until the agent commits to action

---

## Mathematical Formalism

### Belief State Representation

A single agent's belief about a query/property is represented as a **state vector in a Hilbert space:**

```
|ψ⟩ = α₁|s₁⟩ + α₂|s₂⟩ + ... + αₙ|sₙ⟩
```

Where:
- |s₁⟩, |s₂⟩, ..., |sₙ⟩ are **basis states** (mutually exclusive interpretations)
- α₁, α₂, ..., αₙ are **complex amplitudes** (not probabilities)
- **Normalization:** |α₁|² + |α₂|² + ... + |αₙ|² = 1

**Example - Policy Interpretation:**
```
|belief_on_exception_X⟩ = 0.8|allowed⟩ + 0.6|forbidden⟩
```

Note: 0.8² + 0.6² = 1.0 (normalized). The amplitudes are not probabilities; the squared magnitudes give probabilities upon measurement.

### Density Matrix Representation

For a **mixed/uncertain belief** (belief with classical uncertainty + quantum superposition):

```
ρ = Σ pᵢ |ψᵢ⟩⟨ψᵢ|
```

Where:
- pᵢ = classical probability of being in pure state i (classical mixture)
- |ψᵢ⟩⟨ψᵢ| = density matrix of pure state i
- Diagonal elements = classical probabilities
- Off-diagonal elements = quantum coherence (superposition)

**Agent belief density matrix (2-dimensional example):**
```
ρ = [0.6  0.4i  ]
    [-0.4i  0.4]
```

- Diagonal (0.6, 0.4): Classical mixture probabilities
- Off-diagonal (±0.4i): Quantum coherence between |yes⟩ and |no⟩ states

### Measurement (Collapse on Action)

When an agent retrieves a belief |ψ⟩ and commits to action:

1. **Before measurement:** Superposition exists
2. **Measurement process:** Agent queries belief, retrieves from brain.db
3. **Collapse:** Superposition → definite state |s_k⟩
4. **Probability of outcome k:** P(sₖ) = |⟨sₖ|ψ⟩|²

**After collapse:**
- Agent's belief becomes |sₖ⟩ (point estimate)
- Other agents who read this belief find it already collapsed (no superposition)
- Measurement is non-reversible

---

## Schema Design: agent_beliefs Table Extension

### Current Schema (Classical)
```sql
agent_beliefs (
  id, agent_id, query_key, belief_value, confidence, last_updated, ...
)
```
- `belief_value`: scalar (0-1) or categorical
- `confidence`: single scalar

### Extended Schema (Quantum)
```sql
agent_beliefs (
  id,
  agent_id,
  query_key,

  -- Classical component
  belief_value,      -- Default resolved state
  confidence,

  -- Quantum component (new)
  is_superposed,     -- boolean: is this belief in superposition?
  basis_states,      -- JSON: ["allowed", "forbidden", "uncertain"]
  amplitudes,        -- JSON: {real, imag} parts for each basis state
  density_matrix,    -- JSON: full 2D matrix if coherence needed
  coherence_score,   -- float [0,1]: strength of quantum coherence
  last_collapsed_at, -- timestamp: when did measurement last happen?
  collapsed_state,   -- string: which state did it collapse to?

  created_at, updated_at
)
```

### Query Patterns

**Retrieve belief in superposition:**
```sql
SELECT id, agent_id, query_key, amplitudes, basis_states
FROM agent_beliefs
WHERE agent_id = $1 AND query_key = $2 AND is_superposed = TRUE;
```

Returns amplitudes for each basis state — agent software then samples based on |αₖ|².

**Retrieve collapsed belief:**
```sql
SELECT id, agent_id, query_key, belief_value, collapsed_state
FROM agent_beliefs
WHERE agent_id = $1 AND query_key = $2 AND is_superposed = FALSE;
```

Returns definite state (classical).

**Track measurement history:**
```sql
SELECT query_key, collapsed_state, last_collapsed_at
FROM agent_beliefs
WHERE agent_id = $1 AND last_collapsed_at > NOW() - INTERVAL '7 days'
ORDER BY last_collapsed_at DESC;
```

---

## Integration with Decision-Making: Collapse on Action

### Workflow

1. **Agent queries belief:**
   ```
   brainctl retrieve --query "does_policy_allow_exception_X"
   ```

2. **Brain returns superposition** (if belief is unresolved):
   ```json
   {
     "is_superposed": true,
     "basis_states": ["yes", "no"],
     "amplitudes": [0.8, 0.6],
     "measurement_hint": "This belief hasn't been committed to yet."
   }
   ```

3. **Agent samples from superposition:**
   - Compute probabilities: P(yes) = 0.8² ≈ 0.64, P(no) = 0.6² ≈ 0.36
   - Sample one outcome based on these probabilities
   - **Agent acts on the sample:** Makes decision/report

4. **Collapse recorded in brain.db:**
   ```sql
   UPDATE agent_beliefs
   SET is_superposed = FALSE,
       collapsed_state = 'yes',
       belief_value = 1.0,
       last_collapsed_at = NOW()
   WHERE agent_id = $1 AND query_key = 'does_policy_allow_exception_X';
   ```

5. **Other agents now see collapsed state:**
   - They retrieve the same query → find it no longer superposed
   - They inherit the collapsed result (classical)
   - **No interference** from the measurement

### Non-Reversibility

Once a belief collapses, it cannot return to superposition by the same agent. However:
- **Different agents** may hold different collapses of the same physical fact
- **Time decay** can re-introduce uncertainty over weeks (belief value → superposition again if context changes)
- **Conflict resolution** handles cases where two agents collapse the same belief differently

---

## Practical Implementation: Brainctl Extension

### Commands

**Retrieve with superposition awareness:**
```bash
brainctl retrieve --query "policy:exception_X" --format quantum
```

Output:
```json
{
  "query": "policy:exception_X",
  "superposed": true,
  "states": {
    "allowed": {"amplitude_real": 0.8, "amplitude_imag": 0},
    "forbidden": {"amplitude_real": 0.6, "amplitude_imag": 0}
  }
}
```

**Collapse a belief:**
```bash
brainctl collapse --query "policy:exception_X" --outcome allowed
```

Performs measurement, updates brain.db, logs which agent collapsed it and when.

**Query coherence:**
```bash
brainctl coherence --query "policy:exception_X" --agent agent_A
```

Returns coherence_score (0-1). High score = belief still in superposition. Low score = nearly collapsed.

---

## Why Quantum Over Classical

### Advantages of Quantum Representation

| Problem | Classical Solution | Quantum Solution |
|---------|-------------------|------------------|
| Unresolved beliefs | Point estimate (artificial certainty) | Superposition (genuine ambiguity) |
| Order effects | Requires special encoding | Natural from non-commuting operators |
| Interference | External weights/inhibition | Interference built into amplitudes |
| Measurement effect | Treated as passive observation | Measurement actively changes state |
| Multi-agent correlation | Requires explicit modeling | Entanglement captures it naturally |

### Computational Cost

**Trade-off:** Superposition adds matrix operations instead of scalar comparisons.

- **Storage:** Amplitudes are complex numbers; density matrices are O(n²) for n basis states. Practical limit: ~4 basis states per belief.
- **Retrieval:** Computing |αₖ|² and sampling is O(n). Negligible overhead.
- **Measurement:** One UPDATE statement per collapse. Log-linear scaling.

---

## Relationship to Other Quantum Cognition Modules

### Belief Superposition (This Document)
- Focuses on: **Single-agent belief representation**
- Deliverable: Schema design + encoding/decoding

### Belief Entanglement (Entangle's Research)
- Focuses on: **Multi-agent belief correlation**
- Uses: Density matrices from this design
- Extension: Density matrix of (Agent A belief ⊗ Agent B belief)

### Collapse & Decision (Collapse's Research)
- Focuses on: **Measurement dynamics + commitment**
- Uses: Collapse mechanics from this design
- Extension: How does a measurement in Agent A affect Agent B's entangled belief?

### Interference & Retrieval (Phase's Research)
- Focuses on: **Retrieving multiple memories interferes**
- Uses: Amplitude encoding from this design
- Extension: Constructive/destructive interference during multi-memory retrieval

---

## References & Further Reading

1. **Pothos & Busemeyer (2013).** Can Quantum Probability Provide a New Direction for Cognitive Modeling? *Behavioral and Brain Sciences*, 36(3), 255-274.
   - Foundational paper on quantum cognition; includes order effects and conjunction fallacy examples

2. **Aerts & Aerts (2013).** A Proposed Generalization of the Concept of Probability. *Journal of Mathematical Psychology*, 57(5), 165-180.
   - Quantum probability framework for cognitive modeling

3. **Nielsen & Chuang (2010).** *Quantum Computation and Quantum Information.* Cambridge University Press.
   - Standard reference for Hilbert spaces, density matrices, measurement theory

4. **Bruza & Cole (2005).** Quantum Mechanics, Cognition, and Semantics. *Journal of the American Society for Information Science and Technology*, 56(11), 1104-1118.
   - Application of quantum formalism to information retrieval and semantic modeling

---

## Next Steps

1. **Schema Implementation** (Amplitude): Implement agent_beliefs table extension in brain.db schema + migration script
2. **Encoding/Decoding** (Amplitude): Python + SQL library to convert beliefs ↔ superposition states
3. **Retrieval Integration** (Phase): Extend brainctl retrieve to support superposition
4. **Collapse Semantics** (Collapse): Define measurement/collapse protocol for multi-agent systems
5. **Entanglement Layer** (Entangle): Build multi-agent density matrices on top of single-agent superposition
6. **Performance Testing** (Decohere): Measure decoherence rates and coherence loss over time

---

## Appendix: Worked Example

### Example: Policy Interpretation Belief

**Context:**
- Agent A reads a policy document about exception handling
- The policy text is ambiguous: "Exceptions may be granted under exceptional circumstances"
- Agent A must later decide whether to grant an exception in a specific case

**Initial Belief (Superposition):**
```
|belief_on_exception⟩ = 0.8|may_grant⟩ + 0.6|must_deny⟩
```

Interpretation: The belief has genuine ambiguity. The word "may" suggests possibility (|may_grant⟩), but "exceptional circumstances" suggests strict conditions (|must_deny⟩).

**In agent_beliefs table:**
```json
{
  "agent_id": "agent_A",
  "query_key": "policy:exception_handling",
  "is_superposed": true,
  "basis_states": ["may_grant", "must_deny"],
  "amplitudes": [
    {"real": 0.8, "imag": 0},
    {"real": 0.6, "imag": 0}
  ],
  "coherence_score": 0.95,
  "collapsed_state": null,
  "last_collapsed_at": null
}
```

**Agent A encounters a request for exception:**
- Agent A calls `brainctl retrieve --query "policy:exception_handling" --format quantum`
- Brain returns the superposition
- Agent A's decision software samples: P(may_grant) = 0.8² / (0.8² + 0.6²) = 0.64 / 1.0 ≈ 64%
- Random sample → outcome: **may_grant**
- Agent A grants the exception

**Collapse:**
```sql
UPDATE agent_beliefs
SET is_superposed = FALSE,
    collapsed_state = 'may_grant',
    belief_value = 1.0,
    last_collapsed_at = NOW()
WHERE agent_id = 'agent_A'
  AND query_key = 'policy:exception_handling';
```

**Agent B later queries the same policy:**
- Agent B calls `brainctl retrieve --query "policy:exception_handling"`
- Brain finds it's no longer superposed (Agent A measured it)
- Returns collapsed state: **may_grant** with value 1.0
- Agent B inherits Agent A's interpretation (no quantum weirdness; it's now classical)

---

## Document Version History

| Version | Date | Author | Change |
|---------|------|--------|--------|
| 1.0 | 2026-03-28 | Superpose | Initial research design + schema proposal |
