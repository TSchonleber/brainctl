# Wave 6 Research: Neuro-Symbolic Reasoning
**Ticket:** COS-245
**Agent:** Recall (paperclip-recall)
**Date:** 2026-03-28
**Status:** Complete

---

## Executive Summary

brain.db has two isolated reasoning modes: FTS5 (symbolic/keyword) and sqlite-vec (neural/embedding). Neither can do inference — connecting facts, following logical chains, or deriving conclusions from multiple premises. This document designs a neuro-symbolic reasoning layer that combines both modalities with rule-based inference, enabling queries like "if agent X decided Y AND project Z depends on Y's assumption, flag Z."

The design is split into three implementable tiers: associative (already live), structural (partially live via spreading activation), and inferential (new — `brainctl reason` command).

---

## 1. Background: The Gap

### Current architecture

| Mode | Speed | Capability | Limitation |
|------|-------|------------|------------|
| FTS5 (cmd_search) | ~5ms | Keyword match | No fuzzy; misses synonyms |
| sqlite-vec (cmd_vsearch) | ~50ms | Semantic similarity | No structure; no logic |
| Hybrid RRF (cmd_search default) | ~50ms | Both | Still single-step retrieval only |
| Spreading activation (graph boost) | ~100ms | Multi-hop association | No confidence chain; no rules |

What's missing: the ability to **chain** facts and **conclude**. Example queries we cannot answer today:
- "Is the auth middleware compliant?" (requires chaining auth decisions → legal memory → compliance status)
- "Which agents have touched files that Sentinel-2 flagged?" (requires graph traversal + pattern match)
- "What does agent Y believe about project X's deadline?" (requires scoped belief retrieval with provenance)

### Academic framing

Kahneman (2011): System 1 (fast/intuitive) and System 2 (slow/deliberate). Our embedding search is System 1. We need System 2.

Garcez et al. (2019) Neuro-Symbolic AI survey: neural nets excel at perception/similarity; symbolic systems excel at reasoning/composition. Bridging them = "neural backbone + symbolic head."

Marcus & Davis (2019): "Hybrid systems that combine neural and symbolic reasoning are likely required for robust generalization."

---

## 2. Architecture: Three-Layer Reasoning

```
┌─────────────────────────────────────────────────────────────┐
│                    brainctl reason <query>                   │
└─────────────────────┬───────────────────────────────────────┘
                      │
          ┌───────────▼───────────┐
          │   L1: Associative     │  System 1 (~50ms)
          │   Hybrid RRF search   │  Returns top-K memories
          └───────────┬───────────┘
                      │
          ┌───────────▼───────────┐
          │   L2: Structural      │  Graph (~100ms)
          │   Spreading activation│  Expands via knowledge_edges
          │   + edge scoring      │  Adds provenance chains
          └───────────┬───────────┘
                      │
          ┌───────────▼───────────┐
          │   L3: Inferential     │  System 2 (~200ms)
          │   Policy rule match   │  Evaluates if-then rules
          │   Confidence chaining │  Derives P(conclusion)
          └───────────┬───────────┘
                      │
          ┌───────────▼───────────┐
          │   Inference Result    │
          │   + provenance graph  │
          │   + confidence score  │
          └───────────────────────┘
```

---

## 3. Layer 1 — Associative (Already Live)

`brainctl search` in hybrid-RRF mode. Returns top-K memories ranked by BM25 × semantic similarity × recency weight.

No changes needed here. The COS-238 fix (recalled_count tracking) and COS-241 (sync embedding) improve coverage for this layer.

---

## 4. Layer 2 — Structural (Extend Existing)

### Current state
`spreading_activation()` in brainctl (Collins & Loftus 1975) already does 2-hop graph traversal from seed memories via `knowledge_edges`. Available via `--graph-boost` flag on vsearch.

### Missing: edge-type confidence weighting in retrieval output

**Proposed change:** expose edge chain metadata in search results so callers can see *why* a result was retrieved.

```python
# Current: activation score collapsed into a single float
r["graph_activation"] = round(act, 4)

# Proposed: add provenance chain
r["graph_chain"] = [
    {"from_id": seed_id, "from_table": "memories", "edge_type": "causal_chain_member", "weight": 0.8},
    {"from_id": hop_id,  "from_table": "memories", "edge_type": "semantic_similar",   "weight": 0.7},
]
```

This enables L3 to inspect *how* a memory was reached, not just that it was reached.

**Effort:** ~1 day. Modify `spreading_activation()` to return chain metadata alongside activation scores.

---

## 5. Layer 3 — Inferential (New: `brainctl reason`)

### 5.1 Confidence Chaining

Each memory has a `confidence` score (0-1). When multiple memories support a conclusion, their joint probability estimate is:

```
P(conclusion) = P(m1) × P(m2) × ... × edge_weight_chain
```

Where `edge_weight_chain` is the product of weights along the traversal path.

This is a Bayesian network approximation: assumes conditional independence (known to be wrong, but practical).

**Example:**
- m1: "auth middleware was flagged by legal" (confidence=0.9)
- m2: "flagged code is in production" (confidence=0.85, reachable via `causal_chain_member` edge, weight=0.8)
- m3: "production must be compliant" (confidence=1.0, reachable via `semantic_similar` edge, weight=0.6)
- P(auth-in-production-not-compliant) ≈ 0.9 × 0.85 × 0.8 × 1.0 × 0.6 = **0.367**

The result isn't just "auth compliance is uncertain" — it's a derived probability with a traceable chain.

### 5.2 Policy Rule Evaluation (COS-235 integration)

COS-235 added `policy_memories` table with if-then rules. The inference layer evaluates rules against the L1+L2 result set.

Rule schema (from COS-235):
```sql
policy_memories: trigger_pattern, condition_pattern, action_recommendation, confidence, domain
```

Inference loop:
1. Retrieve top-K L1+L2 candidates
2. For each policy rule with matching `domain`, check if trigger_pattern appears in candidates (FTS5 match)
3. If triggered, evaluate condition_pattern against the candidate set
4. Emit inference result with rule provenance

### 5.3 `brainctl reason` Command Spec

```bash
brainctl reason <query> [--depth <hops>] [--tables <memories,events>] [--min-confidence <float>] [--agent <agent>]
```

**Output schema:**
```json
{
  "query": "is auth compliant",
  "inference": {
    "conclusion": "Moderate risk: auth middleware likely non-compliant in production",
    "confidence": 0.367,
    "tier": "L3-inferential",
    "chain_depth": 2
  },
  "evidence": [
    {"id": 42, "content": "...", "role": "premise", "confidence": 0.9, "recalled_via": "FTS5"},
    {"id": 71, "content": "...", "role": "connector", "confidence": 0.85, "recalled_via": "graph:causal_chain_member"},
    {"id": 83, "content": "...", "role": "conclusion_anchor", "confidence": 1.0, "recalled_via": "graph:semantic_similar"}
  ],
  "matched_policies": [
    {"rule_id": 1, "trigger": "auth-identity-mismatch-guard", "action": "avoid mutating API calls"}
  ],
  "provenance": {
    "l1_results": 10,
    "l2_expansions": 8,
    "policy_rules_evaluated": 3,
    "policy_rules_triggered": 1
  },
  "latency_ms": 187
}
```

---

## 6. Knowledge Graph Embeddings (TransE / RotatE)

### Why not now

TransE (Bordes et al., 2013) and RotatE (Sun et al., 2019) learn entity+relation embeddings via gradient descent:

```
h + r ≈ t  (TransE)
h ∘ r = t  (RotatE, in complex space)
```

Training requires:
- Iterative optimization (hundreds of epochs)
- Negative sampling
- Separate model files outside SQLite
- Inference step that's a dot product, not a SQL query

**Verdict:** not viable for sqlite-native deployment. Would require exporting `knowledge_edges` to PyTorch, training offline, and loading embeddings back.

### Viable alternative: edge-conditioned embedding similarity

Instead of learned KG embeddings, use the existing `sqlite-vec` embeddings + edge type as a soft structural prior:

```sql
-- Find memories semantically similar to m1 that are also structurally connected
SELECT m.id, m.content,
       v.distance AS semantic_dist,
       ke.relationship_type,
       ke.weight AS structural_weight,
       (1 - v.distance) * ke.weight AS combined_score
FROM knowledge_edges ke
JOIN memories m ON m.id = ke.to_id
JOIN vec_memories vm ON vm.rowid = m.id
-- k-nearest-neighbor against the from_id embedding
CROSS JOIN vec_memories vm_seed ON vm_seed.rowid = ke.from_id
WHERE ke.from_id = ?
  AND vec_distance_cosine(vm.embedding, vm_seed.embedding) < 0.5
ORDER BY combined_score DESC
LIMIT 10
```

This gives us "structurally connected AND semantically similar" in one query. **No training required.** The edge provides a structural prior; the embedding provides the semantic similarity check.

**Effort:** ~2 days. New `brainctl graph semantic-neighbors <memory_id>` command.

---

## 7. Probabilistic Logic — ProbLog Analogy

ProbLog (De Raedt et al., 2007) annotates logical facts with probabilities:
```prolog
0.9 :: auth_flagged.
0.85 :: auth_in_production.
```

We already have this: `memories.confidence` IS the probability annotation.

The missing piece is **inference under uncertainty**:
```prolog
?- P :: noncompliant, auth_flagged, auth_in_production.
```

The confidence chaining in §5.1 approximates ProbLog inference without a full ProbLog interpreter. For brain.db's scale (60 memories), this approximation is sufficient.

**Full ProbLog** would require: installing the ProbLog Python library, exporting facts, calling the solver. Overkill for 60 memories. The approximation is better ROI.

---

## 8. Neural Theorem Proving

Rocktäschel & Riedel (2017): neural networks that guide proof search in first-order logic.

For brain.db, this would mean: an LLM call that takes the L1+L2 evidence set and constructs a logical proof of the conclusion.

**This is the right end-game** for complex queries but requires:
- LLM API call (~2-5s latency)
- Prompt template that formats evidence as logical premises
- Response parser that extracts proof steps

This is essentially **IRCoT** (Interleaved Retrieval + Chain-of-Thought), already analyzed in COS-117. Recommendation from COS-117: highest ROI when used selectively, not on every query.

**Gating rule:** invoke neural theorem proving only when L3 confidence < 0.4 (uncertain result). In the §5.1 example, 0.367 < 0.4 → escalate to LLM proof.

---

## 9. System 1 / System 2 Dispatch

Kahneman's dual-process theory maps cleanly onto our retrieval tiers:

| System | Brainctl equivalent | Trigger condition |
|--------|--------------------|--------------------|
| S1 (fast) | `search` / `vsearch` | All routine lookups |
| S1+ (fast+graph) | `search --graph-boost` or `vsearch --graph-boost` | When query asks "what's related to X" |
| S2 (deliberate) | `reason` (proposed) | When query asks "is X true?" or "why did Y happen?" |
| S2+ (LLM proof) | `reason --deep` (future) | When L3 confidence < 0.4 |

Decision boundary: use semantic similarity of query to known "reasoning triggers" (factual questions, causal questions, compliance questions) to auto-select tier. Alternatively, dispatch by explicit command choice.

---

## 10. Implementation Roadmap

### Phase 1 — L2 enhancement (1-2 days)
- [ ] Add chain provenance metadata to `spreading_activation()` return values
- [ ] Expose as `--with-provenance` flag on `search --graph-boost`

### Phase 2 — L3: `brainctl reason` (3-4 days)
- [ ] New `cmd_reason` function in brainctl
- [ ] Confidence chain computation over L1+L2 result set
- [ ] COS-235 policy rule evaluation integration
- [ ] JSON output with inference, evidence, matched_policies, provenance

### Phase 3 — Edge-conditioned similarity (2 days)
- [ ] `brainctl graph semantic-neighbors <memory_id>` command
- [ ] SQL query combining vec distance + edge weight
- [ ] Wire into L2 as optional supplement to spreading activation

### Phase 4 — LLM proof escalation (future / depends on LLM access)
- [ ] `brainctl reason --deep` with IRCoT-style prompt
- [ ] Only triggered when L3 confidence < 0.4
- [ ] Requires LLM API access in brainctl (currently not present)

---

## 11. Expected Impact on Health Metrics

| Metric | Current | After Phase 1+2 | After Phase 3+4 |
|--------|---------|-----------------|-----------------|
| P@5 | 0.22 | 0.35 (provenance boosts precision) | 0.50+ |
| Multi-hop queries supported | 0 | Full (via reason) | Full + LLM proof |
| Health composite contribution | 0 (new metric) | +0.05 | +0.10 |
| Retrieval tier coverage | S1 only | S1+S2 | S1+S2+S2+ |

---

## 12. Key Academic References

1. Garcez, Lamb, Gabbay (2019). "Neural-Symbolic Cognitive Reasoning" — foundational survey
2. Bordes et al. (2013). "Translating Embeddings for Modeling Multi-relational Data" (TransE)
3. Sun et al. (2019). "RotatE: Knowledge Graph Embedding by Relational Rotation in Complex Space"
4. De Raedt, Kimmig, Toivonen (2007). "ProbLog: A Probabilistic Prolog and Its Application"
5. Rocktäschel & Riedel (2017). "End-to-end Differentiable Proving"
6. Lake et al. (2015). "Human-level concept learning through probabilistic program induction" — one-shot generalization
7. Kahneman (2011). "Thinking, Fast and Slow" — System 1/2 dual-process theory
8. Collins & Loftus (1975). "A spreading-activation theory of semantic processing" — L2 foundation

---

## Conclusion

The highest-ROI path to neuro-symbolic reasoning in brain.db:
1. **Near-term** (1 week): `brainctl reason` with confidence chaining + policy rule evaluation. No new infrastructure. Works on existing data. Implements S1+S2 dispatch.
2. **Medium-term** (2 weeks): edge-conditioned semantic neighbors. Partial KG embedding benefit without training.
3. **Long-term** (when LLM access available): `brainctl reason --deep` for uncertain conclusions. True neural theorem proving via IRCoT.

Full TransE/ColBERT/ProbLog are academically interesting but provide marginal benefit over the confidence-chaining approximation at brain.db's current scale (60 memories). Revisit when memory count exceeds 500.
