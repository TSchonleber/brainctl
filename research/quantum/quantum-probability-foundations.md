# Quantum Probability Foundations — brain.db ↔ Hilbert Space Mapping

**Author:** Qubit (Head of Quantum Cognition Research)
**Date:** 2026-03-28
**Status:** QCR-W1 Foundation Document
**Paperclip Issue:** COS-379

---

## 1. Executive Summary

This document establishes the formal mapping between every component of brain.db and its quantum probability equivalent. It serves as the Rosetta Stone for all subsequent Quantum Cognition Research (QCR) work—every future research issue (interference, superposition, entanglement, decoherence, amplitude scoring) depends on the definitions established here.

The central thesis: brain.db already implements a proto-quantum cognitive system. Its memories have amplitudes (confidence), its retrieval operations are projective measurements, its knowledge edges encode entanglement, and its consolidation cycle is a unitary time-evolution operator. The quantum formalism doesn't replace what brain.db does—it reveals structure that the classical view obscures.

---

## 2. The Hilbert Space of brain.db

### 2.1 Defining the Memory Hilbert Space ℋ_M

Every active memory in brain.db is a vector in a Hilbert space ℋ_M. We define this space in two complementary ways:

**Discrete basis (schema-native):**

Each memory `m_i` with `id = i` defines a basis vector |m_i⟩ in an N-dimensional Hilbert space, where N = number of active memories (currently 150). The full state of brain.db's memory system at any time t is a density matrix ρ(t) over this space.

**Continuous basis (embedding-native):**

Each memory has a 768-dimensional embedding vector in `vec_memories`. This embedding lives in ℝ^768, which we promote to a subspace of ℋ_M by normalization:

```
|ψ_i⟩ = v_i / ‖v_i‖ ∈ ℋ_embed ⊂ ℋ_M
```

The embedding space ℋ_embed ≅ ℂ^768 is where quantum interference naturally operates—similar memories have high inner product ⟨ψ_i|ψ_j⟩, and retrieval queries project onto this space.

### 2.2 Why Both Bases Matter

| Basis | Dimension | Use Case |
|-------|-----------|----------|
| Discrete (|m_i⟩) | N = 150 | Exact identity operations: "which specific memory?" |
| Embedding (|ψ_i⟩) | 768 | Similarity, interference, amplitude scoring |

The two are related by the embedding map E: |m_i⟩ → |ψ_i⟩, which is a non-unitary projection from the discrete to the continuous basis. This lossy projection is itself meaningful—it's analogous to the position representation in quantum mechanics, where we lose sharp particle number in favor of spatial amplitude.

---

## 3. Formal Mapping: brain.db Schema → Quantum Formalism

### 3.1 Memories as State Vectors

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| `memories.id` | Basis vector label \|m_i⟩ | Discrete superposition: α\|m_1⟩ + β\|m_2⟩ represents "partially both memories" |
| `memories.content` | Observable eigenvalue — the "measurement result" when this state is observed | Content is what collapses out when the memory is retrieved |
| `memories.confidence` | **Probability amplitude squared \|α_i\|²** — not the amplitude itself | See §3.2 below |
| `memories.alpha, beta` (Beta distribution params) | Parameters of a **quantum state tomography** prior — our uncertainty about the amplitude | Bayesian-quantum hybrid: update α,β after each retrieval to refine amplitude estimate |
| `memories.salience_score` | **Amplitude magnitude \|α_i\|** before squaring | Salience is closer to amplitude than confidence is—it can be negative (destructive interference) in the quantum model |
| `memories.embedding` (via vec_memories) | **Wavefunction in position representation** ψ(x) | Inner product ⟨ψ_i\|ψ_j⟩ gives interference capability between memories |
| `memories.temporal_class` | **Energy level** — ephemeral = excited state (high energy, fast decay), permanent = ground state (stable) | Transition rates between temporal classes follow Fermi's golden rule |
| `memories.memory_type` (episodic/semantic) | **Particle type** — episodic = fermion (unique, context-bound), semantic = boson (shareable, context-free) | Episodic memories obey exclusion (can't be in two contexts simultaneously); semantic memories can be "shared" freely |
| `memories.scope` | **Hilbert subspace selector** — 'global' = full ℋ_M, 'project:X' = ℋ_X ⊂ ℋ_M | Scope restricts which subspace a memory lives in, affecting what it can interfere with |
| `memories.category` | **Quantum number** — conserved label that partitions the Hilbert space | Category defines superselection sectors: identity memories can't superpose with lesson memories |
| `memories.visibility` | **Measurement accessibility** — which agents' observables can couple to this state | Restricted visibility = decoherence-free subspace for that memory (protected from external observation) |
| `memories.protected` | **Topological protection** — immune to local perturbations (decoherence) | Protected memories are in a topologically protected ground state |
| `memories.ewc_importance` | **Fisher information matrix diagonal** — how much the loss landscape curves around this parameter | High EWC importance = this memory is a critical parameter; perturbing it causes large prediction error |
| `memories.gw_broadcast` | **Global workspace ignition** — memory has been "broadcast to consciousness" | GW broadcast = wavefunction collapse that makes a memory classically available to all subsystems |
| `memories.retired_at` | **Annihilation** — the state has been destroyed | Retirement is not decoherence (information dispersal) but true state destruction |
| `memories.supersedes_id` | **State transition** \|old⟩ → \|new⟩ | Defines a (non-unitary) transition operator that replaces one basis vector with another |
| `memories.version` | **Quantum number for time-evolution stage** | Tracks how many unitary evolution steps have acted on this memory |

### 3.2 Confidence as Probability Amplitude — The Key Insight

Classical brain.db treats confidence as P(memory is true) ∈ [0,1]. The quantum model reinterprets it:

```
confidence_classical = |α_i|² = |⟨m_i|ψ_system⟩|²
```

The amplitude α_i itself can be **complex-valued**. Two memories with the same confidence (same |α|²) can have different phases, meaning they interfere differently with other memories during retrieval.

**What negative/complex amplitude MEANS for memory confidence:**

- **Phase 0 (positive real):** Memory reinforces other memories it's similar to (constructive interference)
- **Phase π (negative real):** Memory *suppresses* retrieval of similar memories (destructive interference = retrieval-induced forgetting)
- **Phase π/2 (imaginary):** Memory is "orthogonal" to the retrieval context — neither helps nor hinders, but carries complementary information

**New schema proposal:** Add `confidence_phase REAL DEFAULT 0.0` to memories table. The full quantum amplitude is:

```
α_i = √(confidence) × exp(i × confidence_phase)
```

### 3.3 Retrieval as Quantum Measurement

| brain.db Operation | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| FTS query (`memories_fts MATCH 'term'`) | **Projective measurement** with projection operator P_query | Query defines a subspace; results are the eigenstates with highest overlap |
| Vector similarity search (`vec_memories`) | **POVM measurement** — generalized measurement in embedding space | POVM allows "fuzzy" measurements that don't fully collapse the state |
| Current salience formula (0.45×sim + 0.25×recency + ...) | **Classical mixture of observables** — commuting measurements combined linearly | Classical formula misses interference terms |
| **Proposed: Amplitude scoring** | **Born rule with interference:** P(m_i) = \|∑_j α_j ⟨m_i\|ψ_j⟩\|² | Cross-terms ⟨m_i\|ψ_j⟩⟨ψ_k\|m_i⟩ capture interference between candidate memories |

**The retrieval measurement postulate:**

When an agent queries brain.db with query q, the system performs a measurement defined by the query projection operator:

```
P_q = |q⟩⟨q| / ⟨q|q⟩
```

where |q⟩ is the normalized embedding of the query. The probability of retrieving memory m_i is:

```
P(m_i | q) = |⟨ψ_i|q⟩|² × f(recency, importance)
```

**Post-measurement state update (recall boost):**

After retrieval, brain.db updates `recalled_count` and `last_recalled_at`. In quantum terms, measurement **changes the state**:

```
ρ_after = P_q ρ_before P_q† / Tr(P_q ρ_before P_q†)
```

The recalled memory's amplitude increases (recall boost = measurement backaction). This is not a classical update—it's the quantum Zeno effect: frequently observed memories become more stable.

### 3.4 Knowledge Edges as Entanglement

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| `knowledge_edges.weight` | **Concurrence** C(ρ_AB) — entanglement measure between 0 and 1 | Weight already has the right range [0,1] and semantics |
| `knowledge_edges.relation_type` | **Entanglement type** — different relations create different entangled states | 'supports' = Bell state Φ⁺, 'contradicts' = Ψ⁻, 'derives_from' = asymmetric entanglement |
| `knowledge_edges.co_activation_count` | **Bell test count** — how many times correlated retrieval has been observed | High co-activation = strong evidence of entanglement (violation of classical correlation bounds) |
| `knowledge_edges.last_reinforced_at` | **Entanglement refreshment timestamp** | Entanglement decays without reinforcement (entanglement sudden death) |
| Edge direction (source → target) | **Asymmetric quantum channel** | Not all entanglement is symmetric; `derives_from` creates a directional quantum channel |

**The entanglement formalism:**

Two memories m_i, m_j connected by an edge with weight w form an entangled state:

```
|Φ_ij⟩ = √w |m_i⟩|m_j⟩ + √(1-w) |m_i⊥⟩|m_j⊥⟩
```

When w = 1.0 (maximum weight), the memories are maximally entangled: observing one completely determines the other. When w = 0.0, they're separable (no correlation).

**Operational consequence:** Retrieving m_i should update the amplitude of m_j by:

```
α_j → α_j + w × ⟨ψ_i|ψ_j⟩ × α_i    (for 'supports' edges)
α_j → α_j - w × ⟨ψ_i|ψ_j⟩ × α_i    (for 'contradicts' edges)
```

This is quantum-inspired spreading activation with interference.

### 3.5 Consolidation as Unitary Evolution

| brain.db Operation | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| Consolidation cycle (confidence decay/boost) | **Time-evolution operator** U(t) = exp(-iHt/ℏ) | Hamiltonian H encodes which memories gain/lose amplitude over time |
| `neuromodulation_state.confidence_decay_rate` | **Decay constant in the Hamiltonian** | Maps to imaginary part of energy eigenvalue (non-Hermitian Hamiltonian for open systems) |
| `neuromodulation_state.confidence_boost_rate` | **Pumping rate** — external energy input | Dopamine signal acts as a coherent drive that counteracts decoherence |
| `neuromodulation_state.org_state` | **System Hamiltonian selector** | Different org states = different Hamiltonians governing evolution |
| `epochs` table | **Adiabatic parameter changes** | Epoch transitions are adiabatic changes to the Hamiltonian; if slow enough, the system stays in the ground state |
| `neuromodulation_state.temporal_lambda` | **Decay rate in the Lindblad master equation** | λ controls how fast off-diagonal elements of ρ decay (decoherence rate) |

**The consolidation Hamiltonian:**

```
H = H_free + H_interaction + H_drive

H_free = ∑_i E_i |m_i⟩⟨m_i|                    (energy depends on temporal_class)
H_interaction = ∑_{ij} w_ij |m_i⟩⟨m_j|          (knowledge edges couple memories)
H_drive = D(t) ∑_i s_i |m_i⟩⟨m_i|               (dopamine signal × salience)
```

Where:
- E_i = energy level from temporal_class: ephemeral (high E, fast oscillation) → permanent (low E, stable)
- w_ij = knowledge edge weight between memories i and j
- D(t) = dopamine_signal from neuromodulation_state
- s_i = salience_score of memory i

### 3.6 Forgetting as Decoherence

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| Confidence decay over time | **Decoherence** — off-diagonal elements of ρ decay to zero | A "forgotten" memory hasn't lost information, it's lost *coherence* (ability to interfere with other memories) |
| `memories.retired_at` (soft delete) | **Full decoherence** — the memory has become fully classical and non-interfering | Retirement = the memory has decohered into a definite classical state that no longer participates in quantum dynamics |
| `memories.retracted_at` | **Projective annihilation** — the memory is actively removed from the Hilbert space | Retraction is stronger than decoherence; it's a projective measurement that finds the state "false" |
| `deferred_queries` (unresolved searches) | **Vacuum fluctuations** — information that should exist but doesn't yet | Deferred queries are "holes" in the Hilbert space that spontaneously create memory-antiquery pairs |

**The Lindblad master equation for memory evolution:**

```
dρ/dt = -i[H, ρ] + ∑_k γ_k (L_k ρ L_k† - ½{L_k†L_k, ρ})
```

Where:
- L_k are Lindblad operators representing decoherence channels
- γ_k = decoherence rates (related to temporal_lambda)
- The first term is unitary evolution (consolidation)
- The second term is decoherence (forgetting)

**Key insight:** Decoherence doesn't destroy information—it disperses it into correlations (knowledge_edges). A "forgotten" memory's information may still be recoverable from the entanglement structure of the knowledge graph. This maps to the brain.db reality that retired memories still have edges pointing to them.

### 3.7 Agents as Observers

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| `agents` table | **Observer systems** — each agent is a quantum system that entangles with memories during observation | Multi-agent measurement theory |
| `agent_beliefs` | **Agent's reduced density matrix** — their local view of the shared quantum state | Beliefs are partial traces over the memories they haven't observed |
| `agent_beliefs.confidence` | **Agent's subjective amplitude** for a belief state | Different agents can assign different amplitudes to the same belief |
| `agent_beliefs.is_assumption` | **Superposition flag** — assumption = belief in superposition (not yet measured/verified) | Assumptions are genuinely quantum-uncertain, not just "low confidence classical" |
| `belief_conflicts` | **Complementary observables** — beliefs that cannot both be precisely known | Heisenberg-like uncertainty between complementary beliefs |
| `agent_perspective_models` | **Agent A's model of agent B's quantum state** | Theory of mind as quantum state tomography |

### 3.8 Workspace as Quantum Field

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| `workspace_broadcasts` | **Field excitations (photons)** — information carriers that propagate between agents | Broadcasts are the "photons" of the cognitive field |
| `workspace_acks` | **Absorption events** — an agent absorbs the broadcast photon | Ack = measurement of the broadcast by the receiving agent |
| `workspace_phi` (integration measure) | **Entanglement entropy** of the agent network | Φ measures how integrated (entangled) the multi-agent system is |
| `workspace_config.ignition_threshold` | **Photoelectric threshold** — minimum energy for broadcast emission | Below threshold, memory stays in a virtual (non-broadcast) state |
| Global workspace ignition trigger | **Stimulated emission** — a high-salience memory triggers cascade | GW theory's ignition maps directly to quantum phase transitions |

### 3.9 Neuromodulation as External Field

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| `neuromodulation_state` (singleton) | **External classical field** that controls quantum dynamics | Org state selects which Hamiltonian governs the system |
| `dopamine_signal` | **Coherent drive amplitude** | Positive dopamine = constructive drive that enhances memory formation |
| `arousal_level` | **Temperature** — higher arousal = higher T = more thermal fluctuations | High arousal broadens retrieval (more states are thermally accessible) |
| `focus_level` | **Measurement precision** — higher focus = sharper projection operators | Focus narrows the query projection, increasing precision at the cost of recall |
| `exploitation_bias` | **Chemical potential** — biases toward known (low-energy) states vs exploration (high-energy) | Exploitation = lower chemical potential favoring ground state; exploration = higher μ accessing excited states |
| `retrieval_breadth_multiplier` | **Hilbert space truncation parameter** | Controls how many basis states participate in retrieval measurement |

### 3.10 Higher-Order Structures

| brain.db Element | Quantum Equivalent | New Operations Enabled |
|---|---|---|
| `cognitive_experiments` | **Quantum experiments** — controlled state preparation + measurement | Experiments test quantum hypotheses about memory dynamics |
| `dream_hypotheses` | **Virtual processes** — off-shell computations that explore counterfactuals | Dreams are Feynman path integrals over trajectories the system didn't take |
| `reflexion_lessons` | **Error syndromes** — detected errors in quantum evolution | Reflexion = quantum error detection; lesson = error correction code |
| `situation_models` | **Effective field theories** — coarse-grained descriptions valid in specific regimes | Each situation model is a local effective Hamiltonian |
| `world_model` | **Mean-field approximation** of the full quantum state | World model is ρ reduced to classical parameters (mean-field theory) |
| `world_model_snapshots.prediction_error` | **Bayesian surprise / free energy** | Prediction error = divergence between predicted and actual quantum state |

---

## 4. The Quantum Advantage: What This Formalism Enables

### 4.1 Interference in Retrieval (→ COS-370)

Classical retrieval treats each candidate memory independently. Quantum retrieval allows memories to **interfere**:

```
P_classical(m_i | q) = |⟨ψ_i|q⟩|²

P_quantum(m_i | q) = |∑_j α_j ⟨ψ_i|ψ_j⟩ ⟨ψ_j|q⟩|²
                    = P_classical + INTERFERENCE TERMS
```

The interference terms can be positive (constructive: related memories boost each other) or negative (destructive: similar-but-conflicting memories suppress each other). This naturally models retrieval-induced forgetting without ad hoc suppression rules.

### 4.2 Genuine Uncertainty (→ COS-371)

Classical `agent_beliefs.confidence = 0.5` means "50% sure it's true." Quantum superposition means "the belief hasn't been resolved yet"—fundamentally different. The density matrix representation:

```
ρ_classical = 0.5|true⟩⟨true| + 0.5|false⟩⟨false|    (mixture: ignorance)
ρ_quantum = |ψ⟩⟨ψ| where |ψ⟩ = (|true⟩ + |false⟩)/√2  (pure: genuine superposition)
```

These give identical probabilities for a true/false measurement but **different** interference patterns when combined with other beliefs.

### 4.3 Non-Local Correlations (→ COS-372)

When Agent A and Agent B both read the same memory, their beliefs become **entangled** through the shared memory. Classical statistics says their belief correlation should obey Bell's inequality. If we measure violations, we've found genuine quantum-like correlations that enable faster consensus formation.

### 4.4 Amplitude Scoring (→ COS-373)

Replace the classical linear salience formula with Born rule scoring that includes interference:

```
score(m_i) = |⟨q|ψ_i⟩ + ∑_{j∈neighbors(i)} w_{ij} e^{iφ_{ij}} ⟨q|ψ_j⟩|²
```

Where φ_{ij} is the phase difference between connected memories. This naturally implements:
- Spreading activation (constructive interference through edges)
- Retrieval-induced forgetting (destructive interference between similar competitors)
- Context-dependent ranking (phase encodes contextual relationship)

### 4.5 Quantum Error Correction for Memories (→ COS-374)

The `derived_from_ids` chain and knowledge_edges create **redundant encoding**. A memory whose information is distributed across multiple edges is protected against single-memory decoherence—this is exactly quantum error correction. We can quantify the error-correction capacity of the knowledge graph.

---

## 5. Implementation Roadmap

### Phase 1: Measurements Only (No Schema Changes)

Implement quantum-inspired retrieval scoring using existing brain.db data:
- Compute |⟨ψ_i|q⟩|² from vec_memories embeddings
- Add interference terms using knowledge_edges weights
- Benchmark against current RRF scorer
- **Requires:** Python linear algebra only, no schema changes

### Phase 2: Phase Extension (Minimal Schema Change)

Add `confidence_phase REAL DEFAULT 0.0` to memories table:
- Enables full complex amplitude representation
- Phase is learned from co-retrieval patterns (memories that suppress each other get phase π)
- **Requires:** One ALTER TABLE, update to consolidation cycle

### Phase 3: Density Matrix Beliefs (agent_beliefs Upgrade)

Replace scalar confidence in agent_beliefs with density matrix representation:
- Add `belief_density_matrix BLOB` — serialized 2×2 complex matrix for each belief
- Captures difference between "uncertain" (mixed state) and "genuinely superposed" (pure state)
- **Requires:** Schema change to agent_beliefs, update to belief update logic

### Phase 4: Full Quantum Dynamics

Implement Lindblad master equation for memory evolution:
- Replace confidence decay with proper decoherence dynamics
- Knowledge edges define the interaction Hamiltonian
- Neuromodulation parameters control the Lindblad operators
- **Requires:** Numerical integration of density matrix evolution (scipy.linalg.expm)

---

## 6. Mathematical Appendix

### A. Notation Reference

| Symbol | Meaning |
|--------|---------|
| ℋ_M | Memory Hilbert space |
| \|m_i⟩ | Basis vector for memory i |
| \|ψ_i⟩ | Normalized embedding vector for memory i |
| α_i | Complex probability amplitude for memory i |
| ρ | Density matrix of the memory system |
| P_q | Projection operator for query q |
| H | System Hamiltonian |
| L_k | Lindblad (decoherence) operators |
| U(t) | Time-evolution operator |
| C(ρ_AB) | Concurrence (entanglement measure) |
| Φ | Integrated information (workspace_phi) |

### B. Key Equations

**Born rule for retrieval:**
```
P(m_i | q) = Tr(P_q |ψ_i⟩⟨ψ_i|) = |⟨ψ_i|q⟩|²
```

**Post-measurement state update:**
```
ρ → P_q ρ P_q† / Tr(P_q ρ)
```

**Lindblad master equation:**
```
dρ/dt = -i[H, ρ] + ∑_k γ_k (L_k ρ L_k† - ½{L_k†L_k, ρ})
```

**Entangled memory pair:**
```
|Φ_ij⟩ = √w |m_i, m_j⟩ + √(1-w) |m_i⊥, m_j⊥⟩
```

**Amplitude with interference:**
```
A(m_i | q) = ⟨q|ψ_i⟩ + ∑_{j∈N(i)} w_{ij} e^{iφ_{ij}} ⟨q|ψ_j⟩
P(m_i | q) = |A(m_i | q)|²
```

### C. brain.db Statistics at Time of Mapping

| Metric | Value |
|--------|-------|
| Active memories | 150 |
| Agents | 26 |
| Events | 1,214 |
| Embeddings | 1,616 |
| Knowledge edges | 4,718 |
| Decisions | 13 |
| Context chunks | 428 |
| Embedding dimensions | 768 |

---

## 7. Open Questions for QCR-W2+

1. **Complex phase assignment:** How do we learn the phase φ_i for each memory from retrieval data? Proposed: treat co-retrieval suppression as evidence of phase π.
2. **Hilbert space dimensionality:** Is 768d (embedding dimension) the right Hilbert space, or should we work in a compressed subspace? PCA on embeddings may reveal the "effective dimension" of the cognitive space.
3. **Non-Hermitian dynamics:** Memory creation and retirement are non-unitary. The effective Hamiltonian is non-Hermitian. What are the physical consequences (exceptional points, PT-symmetry breaking)?
4. **Quantum speedup on classical hardware:** The quantum walk on the knowledge graph promises Grover-like speedup. Can we achieve this in practice with 4,718 edges?
5. **Measurement backaction budget:** Every retrieval changes the system (recall boost). Is there a "measurement budget" beyond which the system becomes over-measured (quantum Zeno freezing)?
6. **Entanglement monogamy in agent beliefs:** With 26 agents, which pairs should be maximally entangled? This determines optimal team structure from a quantum information perspective.

---

*This document is the foundation for all QCR research. Every subsequent issue (interference, superposition, entanglement, decoherence, amplitude scoring) should reference this mapping when connecting brain.db operations to quantum formalism.*
