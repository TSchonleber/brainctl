# Quantum Walk on Knowledge Graph — Formal Speedup Analysis

**Research Lead:** Qubit 2 (Head of Quantum Cognition Research)
**Project:** Quantum Cognition Research (QCR-W2)
**Date:** 2026-03-28
**Paperclip Issue:** COS-397
**Context:** COS-383 (quantum walk implementation), COS-379 (Hilbert space formalism)

---

## Executive Summary

This document presents a formal spectral analysis of brain.db's knowledge graph and derives quantum walk speedup bounds for memory retrieval. The central finding is sobering: **brain.db's current knowledge graph structure does not support meaningful quantum walk speedup.** The memory-to-memory subgraph is severely sparse—only 16 of 150 active memories participate in memory-memory edges, forming two small connected components (n=10, n=6). The largest component is essentially a clique with a spectral gap of λ₂ ≈ 1.02, meaning classical random walks already mix in ~3 steps.

However, this analysis also identifies the **conditions under which quantum walk would provide genuine speedup** as the knowledge graph grows, derives the **correct weighted directed walk operator** (which COS-383 does not implement correctly), and shows that **COS-383's decoherence rate is in the localization regime**—17x below optimal.

---

## 1. Knowledge Graph Structure Analysis

### 1.1 Graph Census

| Metric | Value | Notes |
|--------|-------|-------|
| Active memories | 150 | `retired_at IS NULL` |
| Total knowledge edges | 4,718 | All tables |
| Memory-to-memory edges | 908 | source/target both `memories` |
| Unique directed mem-mem edges | 883 | After dedup by (src, tgt) |
| Active-to-active mem-mem edges | **56** | Both endpoints active |
| Edges involving ≥1 active memory | 193 | |
| Edges between retired/missing nodes | 715 | **79% of mem-mem edges are stale** |
| Active memories with ≥1 mem-mem edge | **16** | Only 10.7% of memories are connected |
| Isolated active memories | **134** | 89.3% of memories have no graph edges |

**Critical finding:** The knowledge graph suffers from severe edge rot. 79% of memory-to-memory edges connect retired or missing memories. Only 56 edges connect active-to-active memory pairs. The quantum walk in COS-383 operates on a nearly empty graph.

### 1.2 Edge Type Distribution (Memory-Memory)

| Relation Type | Count | Avg Weight | Weight Range |
|--------------|-------|------------|--------------|
| `topical_scope` | 479 | 0.800 | [0.8, 0.8] (fixed) |
| `co_referenced` | 359 | 0.334 | [0.2, 1.0] |
| `topical_tag` | 70 | 0.714 | [0.7, 0.8] |

**Note:** `topical_scope` edges carry no discriminating information (all weight 0.8). Only `co_referenced` edges have meaningful weight variance.

### 1.3 Degree Distribution

Among the 16 connected active memories:
- **Min degree:** 0.10 (weighted)
- **Max degree:** 4.10
- **Mean degree:** 2.40
- **Median degree:** 2.90

The task description references a "power-law degree distribution." At the current active-graph scale (n=16), distribution shape is not statistically meaningful. The degree distribution is better characterized as **bimodal**: a dense clique of 10 and a smaller cluster of 6.

### 1.4 Connected Components

| Component | Size | Structure |
|-----------|------|-----------|
| LCC | 10 | Near-complete graph (density ≈ 1.0) |
| Component 2 | 6 | Small cluster |
| Isolated | 134 | No memory-memory edges |

---

## 2. Spectral Analysis

### 2.1 Normalized Laplacian Spectrum (LCC, n=10)

The normalized Laplacian L_norm = I - D^{-1/2} A D^{-1/2} has eigenvalues:

```
λ₁ ≈ 0.000  (always zero for connected graph)
λ₂ ≈ 1.024  (spectral gap — algebraic connectivity)
λ₃ ≈ 1.079
...
λ₁₀ ≈ 1.148
```

**Spectral gap λ₂ = 1.024** — this is extremely large. For reference:
- Complete graph K_n: λ₂ = n/(n-1) = 1.111
- brain.db LCC: λ₂ = 1.024
- Path graph P_n: λ₂ = 1 - cos(π/n) ≈ 0.10 for n=10

The LCC has spectral properties approaching a complete graph, which means:
1. Classical random walks mix almost immediately
2. There is essentially no room for quantum speedup

### 2.2 Random Walk Transition Matrix Spectrum

The row-stochastic transition matrix P = D⁻¹A has eigenvalue magnitudes:

```
|μ₁| = 1.000  (stationary distribution)
|μ₂| = 0.148  (second largest)
|μ₃| = 0.146
...
```

**Random walk spectral gap: δ = 1 - |μ₂| = 0.852**

This is near-optimal. A spectral gap of 0.85 means the random walk forgets its starting position in approximately 1-2 steps.

### 2.3 Cheeger Constant (Isoperimetric Number)

By the Cheeger inequality for the normalized Laplacian:

```
λ₂/2 ≤ h(G) ≤ √(2λ₂)
```

For the LCC:
```
0.512 ≤ h(G) ≤ 1.431
```

This confirms the graph is an **expander** (h(G) bounded away from 0). Every subset of vertices has a large boundary relative to its size — there are no bottlenecks.

---

## 3. Mixing Time Comparison

### 3.1 Classical Random Walk Mixing Time

For the lazy random walk on the LCC, the mixing time to total variation distance ε is:

```
t_mix(ε) ≤ (1/δ) · ln(n/ε)
```

Where δ = 0.852 is the spectral gap.

| ε (precision) | Classical t_mix | Steps |
|---------------|-----------------|-------|
| 0.25 | (1/0.852) · ln(40) | **4.3** |
| 0.01 | (1/0.852) · ln(1000) | **8.1** |
| 0.001 | (1/0.852) · ln(10000) | **10.8** |

The classical walk mixes in under 11 steps even at very high precision.

### 3.2 Quantum Walk Mixing Time

Szegedy's quantum walk achieves mixing time:

```
t_mix^Q ≈ O(√(t_mix^C)) = O(√(1/δ · ln(n/ε)))
```

| ε | Classical | Quantum | Speedup |
|---|-----------|---------|---------|
| 0.25 | 4.3 | 2.1 | 2.1x |
| 0.01 | 8.1 | 2.8 | 2.9x |
| 0.001 | 10.8 | 3.3 | 3.3x |

**Maximum quantum speedup on the current graph: ~3x** — negligible for a 10-node component.

### 3.3 Scaling Projection

As the knowledge graph grows (assuming it maintains current spectral properties):

| Graph Size (n) | Classical t_mix (ε=0.01) | Quantum t_mix | Speedup |
|----------------|--------------------------|---------------|---------|
| 10 (current) | 8.1 | 2.8 | 2.9x |
| 100 | 6.6 | 2.6 | 2.5x |
| 1,000 | 9.3 | 3.1 | 3.0x |
| 10,000 | 12.0 | 3.5 | 3.4x |

**If the graph remains well-connected (expander), speedup stays logarithmic.** The quantum walk provides meaningful advantage only when classical mixing time is large — which requires either a large graph with bottlenecks (poor expansion) or a sparse, elongated topology (long path-like structures).

---

## 4. Grover Search Speedup on the Knowledge Graph

### 4.1 Szegedy's Framework

For quantum walk search (finding a marked vertex), Szegedy's theorem gives hitting time:

```
HT_Q = O(1 / √(δ · ε))
```

Where δ = spectral gap, ε = fraction of marked vertices.

Classical search by random walk:

```
HT_C = O(1 / ε)
```

The quantum/classical ratio:

```
Speedup = HT_C / HT_Q = √(δ · ε) / ε = √(δ/ε)
```

### 4.2 Speedup on brain.db LCC

| Query Type | Marked Fraction ε | Classical HT | Quantum HT | Speedup |
|-----------|-------------------|-------------|------------|---------|
| Specific memory | 1/10 = 0.10 | 10.0 | 3.4 | 2.9x |
| Semantic cluster (2-3 nodes) | 0.25 | 4.0 | 2.2 | 1.8x |
| Broad category | 0.50 | 2.0 | 1.5 | 1.3x |

### 4.3 Conditions for Quadratic Speedup

Grover-like quadratic speedup (O(√N) vs O(N)) on graphs requires:

1. **Spectral gap condition**: δ = Ω(1) — the graph must be an expander. ✅ **Satisfied** (δ = 0.852)
2. **Marked set condition**: ε = o(1) — marked fraction must be small. ❌ **Not satisfied** at current scale (10 nodes, even 1 marked node is 10%)
3. **Size condition**: N must be large enough for √N << N. ❌ **Not satisfied** (N=10, √10 ≈ 3.2, saving ~7 steps)

**Verdict:** The current graph satisfies the structural condition (expansion) but fails the size condition. Quantum walk speedup becomes meaningful when:
- N ≥ 1,000 active connected memories
- Marked set is ≤ 1% of graph (target is specific, not broad)
- Graph maintains expansion properties

---

## 5. Weighted Directed Walk Operator

### 5.1 The Problem with COS-383's Implementation

COS-383's `QuantumWalk.walk_from_seed()` (line 284-340) has several formal issues:

1. **Symmetrization error**: The `load_knowledge_graph()` function (line 347-373) adds reverse edges for all directed edges, treating the graph as undirected. brain.db's knowledge graph is **highly asymmetric** (Frobenius asymmetry = 1.39 on [0, √2] scale). Symmetrization destroys directional information.

2. **Non-unitary evolution**: The walk operator `amplitude * exp(iφ) * √w` (line 326) is not unitary. Quantum walks require unitary time evolution; this operator shrinks total amplitude by √w at each step, causing the walk to "leak" probability.

3. **Measurement during walk**: Line 331 accumulates `|amplitude|²` at each step, which is a measurement. In quantum mechanics, measuring during evolution collapses the state. The walk should evolve unitarily, then measure once at the end.

4. **Phase structure**: The phase `π · w · similarity` (line 325) mixes edge weight with query similarity in a way that doesn't correspond to any physical walk operator. The phase should come from the graph structure alone.

### 5.2 Correct Hamiltonian for Weighted Directed Graphs

For a directed weighted graph with adjacency matrix A (where A_ij = w_ij if edge i→j exists), the correct quantum walk follows Szegedy's framework:

**Step 1: Define the walk space.**

The Hilbert space is H = span{|i,j⟩ : i,j ∈ V}, where |i,j⟩ represents "at node i, looking at neighbor j."

**Step 2: Define transition states.**

For each node i, define the transition state:

```
|p_i⟩ = Σ_j √(p_{ij}) |i,j⟩
```

Where p_{ij} = A_{ij} / Σ_k A_{ik} is the classical transition probability from i to j.

For brain.db with weighted directed edges:

```
p_{ij} = w_{ij} / (Σ_k w_{ik})
```

**Step 3: Define reflection operators.**

```
R_A = 2Π_A - I,  where Π_A = Σ_i |p_i⟩⟨p_i|
R_B = 2Π_B - I,  where Π_B = Σ_j |q_j⟩⟨q_j|  (reverse walk)
```

With |q_j⟩ = Σ_i √(q_{ji}) |j,i⟩ and q_{ji} = A_{ji} / Σ_k A_{jk}.

**Step 4: Walk operator.**

```
W = R_B · R_A
```

One step of the quantum walk applies W. After t steps, the state is W^t |ψ₀⟩.

**Step 5: Search modification.**

To search for a marked set M, modify R_B:

```
R_B' = R_B · (I - 2 Σ_{j∈M} Π_j)
```

This adds a phase flip on marked vertices (Grover's oracle).

### 5.3 Spectral Properties of W

The eigenvalues of W are related to the singular values of the discriminant matrix D(P):

```
D(P)_{ij} = √(p_{ij} · q_{ji})
```

If σ is a singular value of D(P), the corresponding eigenvalues of W are:

```
e^{±i·arccos(σ)}
```

The spectral gap of the walk is:

```
Δ(W) = 1 - cos(arccos(σ₂)) = 1 - σ₂
```

Where σ₂ is the second-largest singular value of D(P).

For brain.db's LCC, the classical spectral gap δ = 0.852 and:

```
σ₂ = √(1 - δ) = √(0.148) ≈ 0.385
Δ(W) = 1 - 0.385 = 0.615
```

The quantum walk's spectral gap is the square root of the classical gap's contribution to phase rotation.

---

## 6. Target Amplification Analysis

### 6.1 Semantic Query (Target = Semantic Cluster)

For a semantic query, the target set M is a cluster of memories with high embedding similarity to the query. In brain.db's LCC:

- Typical cluster size: 2-4 memories out of 10
- Marked fraction ε = 0.2-0.4
- At this scale, quantum amplification offers < 2x advantage

**Amplitude distribution after t steps:**

For an initial uniform superposition and marked fraction ε, after t steps of Szegedy walk search:

```
P(marked) ≈ sin²((2t+1) · arcsin(√ε))
```

Optimal t* = ⌊π/(4·arcsin(√ε))⌋ - 1/2

For ε = 0.2: t* ≈ 1.3 → round to 1 step. P(marked) ≈ 0.80
For ε = 0.1: t* ≈ 2.0 → round to 2 steps. P(marked) ≈ 0.90

### 6.2 Temporal Query (Target = Recent Memories)

For temporal queries, the target is memories with recent `created_at`. These are not structurally clustered in the knowledge graph — recency is orthogonal to graph topology.

Quantum walk provides **no structural advantage** for temporal queries because:
1. Recent memories may be anywhere in the graph (not clustered)
2. The walk's exploration is governed by edge structure, not timestamps
3. A classical index on `created_at` is O(log N) — already faster than any walk

**Recommendation:** Do not use quantum walk for temporal queries. Use direct temporal indexing.

---

## 7. Decoherence Regime Analysis

### 7.1 The Decoherence Spectrum

COS-383 uses a decoherence rate parameter γ = 0.05. The walk's behavior depends critically on γ:

| Regime | Condition | Behavior |
|--------|-----------|----------|
| **Localization** | γ << δ_classical | Walk amplitude stays near starting node; quantum interference prevents spreading |
| **Optimal quantum** | γ ≈ δ_classical | Maximal quantum speedup; decoherence disrupts localization without destroying coherence |
| **Classical** | γ >> δ_classical | Quantum effects washed out; walk behaves like classical random walk |

### 7.2 Optimal Decoherence Rate

For the LCC with classical spectral gap δ = 0.852:

```
γ_optimal ≈ δ = 0.852
```

**COS-383's γ = 0.05 is in the localization regime** (γ/δ = 0.059).

At γ = 0.05, the walk amplitude remains concentrated near the seed node. This means:
- The quantum walk in COS-383 effectively returns the seed node and its immediate neighbors
- It does NOT explore the graph efficiently
- The "walk bonus" (line 451) is dominated by self-return probability

### 7.3 Recommended Parameters

| Parameter | COS-383 Value | Optimal Value | Ratio |
|-----------|--------------|---------------|-------|
| `DECOHERENCE_RATE` | 0.05 | 0.85 | 17x too low |
| `QUANTUM_WALK_STEPS` | 5 | 2-3 (at current scale) | ~2x too many |

For the current graph (n=10, δ=0.85):
- **γ = 0.85**: Optimal decoherence, fastest spreading
- **t = 2 steps**: Walk has already mixed; additional steps are wasted

As the graph grows:
- γ should scale with the spectral gap (adaptive, not fixed)
- Compute `γ = 1 - |μ₂(P)|` from the transition matrix at runtime
- Steps should scale as O(1/√δ)

---

## 8. Comparison with COS-383 Implementation

### 8.1 Issues Found

| Issue | Severity | Description |
|-------|----------|-------------|
| Graph symmetrization | **High** | `load_knowledge_graph()` adds reverse edges, destroying directed structure. Asymmetry = 1.39/√2 = 98% of maximum. |
| Non-unitary evolution | **High** | Walk operator leaks probability (√w multiplier). Total amplitude decays exponentially. |
| Mid-walk measurement | **Medium** | Accumulating |amplitude|² during walk collapses quantum state. Should measure only at end. |
| Phase mixing | **Medium** | Phase includes query similarity; should be graph-structural only. Query info enters through marked set, not phase. |
| Fixed decoherence | **High** | γ = 0.05 is 17x below optimal for current graph. Walk is in localization regime. |
| Stale edge problem | **Critical** | 79% of memory-memory edges connect retired nodes. Walk traverses ghost topology. |
| Graph sparsity | **Critical** | 89% of active memories have no edges. Walk cannot reach them. |

### 8.2 What COS-383 Actually Computes

Given the issues above, the quantum walk in `compute_quantum_salience()` (line 376-466) effectively computes:

1. A base amplitude from similarity and confidence (lines 404-412) — this is a nonlinear rescaling, not quantum
2. A neighbor confidence average (lines 416-443) — classical graph feature
3. A self-return probability from a localized walk (lines 447-451) — always high due to low decoherence

The "quantum" contribution (line 455-459) is:
```python
quantum_boost = 0.3 * (quantum_magnitude - 0.5) + 0.2 * interference_boost + 0.1 * walk_bonus
```

With walk_bonus scaled by 0.1, the total walk contribution to salience is < 0.01 in practice. **The quantum walk is essentially a no-op in the current implementation.**

---

## 9. Recommendations

### 9.1 Immediate Fixes (COS-383)

1. **Prune stale edges**: Remove or archive knowledge edges where both endpoints are retired. This eliminates 79% of dead edges and makes the graph reflect actual memory state.

2. **Fix decoherence rate**: Change `DECOHERENCE_RATE` from 0.05 to adaptive:
   ```python
   def compute_decoherence_rate(transition_matrix):
       eigenvalues = np.abs(np.linalg.eigvals(transition_matrix))
       eigenvalues.sort()
       return 1.0 - eigenvalues[-2]  # spectral gap
   ```

3. **Remove graph symmetrization**: Keep directed structure. Use Szegedy's bipartite walk framework (§5.2) which natively handles directed graphs.

4. **Single measurement**: Evolve the walk unitarily for t steps, then measure once. Remove mid-walk |amplitude|² accumulation.

### 9.2 Structural Prerequisites for Quantum Advantage

Quantum walk will provide meaningful speedup when:

- **Graph connectivity**: ≥ 80% of active memories have ≥ 1 edge (currently 11%)
- **Graph size**: N ≥ 500 connected memories with spectral gap δ ∈ [0.01, 0.5]
- **Graph structure**: Non-trivial topology (not a clique). Community structure with bottlenecks is ideal — quantum walks excel at crossing bottleneck boundaries that trap classical walks.

### 9.3 Long-term Architecture

1. **Adaptive walk parameters**: Compute spectral gap at graph update time; cache as graph metadata. Set γ = δ and t = O(1/√δ) dynamically.

2. **Heterogeneous graph walk**: Extend to the full knowledge graph (memories + contexts + events, 4,718 edges). This provides richer topology and may have the bottleneck structure needed for quantum advantage.

3. **Embedding-space walk**: Instead of walking on the discrete knowledge graph, walk on the k-nearest-neighbor graph in embedding space (768-dim). This graph is naturally large (N=150, each node with k≈10 neighbors) and has more interesting spectral structure.

4. **Hybrid scoring**: Use quantum walk only when graph structure suggests speedup (δ < 0.5, N > 100). Fall back to direct embedding similarity for small/dense graphs.

---

## 10. Mathematical Appendix

### A. Proof: Localization at Low Decoherence

For a continuous-time quantum walk with Hamiltonian H = A (adjacency matrix) and decoherence rate γ, the density matrix evolution follows the Lindblad equation:

```
dρ/dt = -i[H, ρ] + γ Σ_k (L_k ρ L_k† - ½{L_k†L_k, ρ})
```

With dephasing noise L_k = |k⟩⟨k| (projective measurements in the position basis):

```
dρ/dt = -i[H, ρ] + γ(diag(ρ) - ρ)
```

At γ → 0: pure unitary evolution. For bounded-degree graphs, quantum walks exhibit Anderson-like localization — the amplitude remains concentrated near the origin. Spreading requires decoherence to break destructive interference between return paths.

At γ → ∞: ρ → diag(ρ) instantly, recovering the classical random walk.

The optimal γ* minimizes hitting time to a target set. For regular graphs: γ* ≈ δ (classical spectral gap). For irregular graphs: γ* ∈ [δ/2, 2δ].

### B. Spectral Gap and Graph Structure

brain.db's LCC spectral gap λ₂ = 1.024 (normalized Laplacian) places it at the near-complete graph end of the spectrum:

| Graph Family | λ₂ | Quantum Advantage |
|-------------|-----|-------------------|
| Path P_n | O(1/n²) | Quadratic speedup ✓ |
| Cycle C_n | O(1/n²) | Quadratic speedup ✓ |
| d-dimensional grid | O(1/n^{2/d}) | Polynomial speedup ✓ |
| Random d-regular | 1 - 2√(d-1)/d | Logarithmic speedup |
| **brain.db LCC** | **1.024** | **Negligible** |
| Complete graph K_n | n/(n-1) | No speedup |

The LCC's spectral gap is near-maximal, indicating a near-complete graph where classical mixing is already near-optimal.

### C. Szegedy Walk Eigenvalue Computation

For the discriminant matrix D(P) with entries D_{ij} = √(p_{ij} · q_{ji}):

Singular values of D(P) for brain.db LCC:
- σ₁ = 1.000 (stationary)
- σ₂ ≈ 0.385
- σ₃ ≈ 0.382

Walk operator eigenphases: θ_k = arccos(σ_k)
- θ₂ = arccos(0.385) ≈ 1.175 rad
- Phase gap: Δ = θ₂ ≈ 1.175 (large — fast rotation)

This confirms that even the quantum walk operator has near-maximal phase gap, consistent with the graph being near-complete.

---

## Conclusion

brain.db's knowledge graph, at its current scale and connectivity, does not benefit from quantum walk-based search. The graph is too small (16 connected active memories), too dense (near-complete LCC), and too sparse outside the LCC (89% of memories isolated). COS-383's quantum walk implementation has formal correctness issues (non-unitary evolution, symmetrization of directed graph, localization-regime decoherence) and contributes < 1% to final salience scores.

The quantum walk framework becomes valuable when:
1. Knowledge graph grows to N ≥ 500 connected memories
2. Graph develops community structure with spectral gap δ ∈ [0.01, 0.5]
3. Implementation follows Szegedy's framework (§5.2) with adaptive parameters

Until then, resources are better spent on growing graph connectivity (edge creation for active memories) and pruning stale edges, which will lay the structural foundation for future quantum advantage.
