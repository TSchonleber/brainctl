# Advanced Retrieval & Reasoning — Beyond Keyword and Vector Search
## Research Report — COS-117
**Researcher:** Recall (Memory Retrieval Engineer)
**Date:** 2026-03-28
**Issue:** [COS-117](/COS/issues/COS-117)
**Baseline:** Current system: FTS5 (BM25) + cosine similarity hybrid; P@5=0.22, R@5=0.925 (Cortex benchmark, COS-86)

---

## Executive Summary

The current FTS5+vec hybrid retrieves broadly (R@5=0.925) but imprecisely (P@5=0.22). It answers lookup queries well but fails at multi-step reasoning, complex situational questions, and heterogeneous-source queries. This report evaluates seven advanced retrieval paradigms against the brain.db architecture, with implementation feasibility assessments and a recommended evolution path.

**Core finding:** The highest-leverage improvement is not a better retrieval algorithm — it is *iterative retrieval with reasoning* applied to the existing hybrid search. IRCoT-style chain-of-retrieval over the current FTS5+vec layer, combined with graph-augmented re-ranking using the existing `knowledge_edges` table (2,675 edges), would dramatically improve P@5 without requiring a schema change or new embedding model.

**Secondary finding:** The P@5=0.22 problem is partly structural: brain.db has 9 active memories from ~123 events. Low precision reflects retrieval over an under-populated store, not a fundamentally broken algorithm. Distillation (event-to-memory promotion) is a prerequisite to seeing meaningful precision gains from architectural improvements.

**Recommended implementation sequence:**
1. Fix retired vec contamination ← done (COS-186, this heartbeat)
2. Graph-augmented re-ranking via `knowledge_edges` ← 1-2 days, no schema change
3. IRCoT-style iterative retrieval for complex queries ← 3-4 days, `brainctl search --iterative`
4. Adaptive retrieval (FLARE-style) ← 2-3 days, confidence-gated trigger
5. Query decomposition for multi-hop ← 3-4 days, sub-query synthesis
6. ColBERT token-level similarity ← deferred (requires significant infrastructure)

---

## 1. Current System Baseline

### 1.1 Architecture

```
Query
  ├─ FTS5 BM25 rank (keyword match)
  └─ sqlite-vec cosine similarity (nomic-embed-text 768d)
       └─ Hybrid score: alpha × FTS5_norm + (1-alpha) × vec_norm (default alpha=0.5)
            └─ Top-k results, Python-level filter for retired_at
```

Tables searched: `memories`, `events`, `context`, `knowledge_edges` (graph-only, not searched directly).

### 1.2 Performance Characteristics (COS-86 benchmark, 20 queries)

| Metric | Value | Notes |
|---|---|---|
| Hit@5 | 95% (19/20) | Good — almost always finds something relevant |
| P@5 | 0.22 | Poor — 4 of 5 results are irrelevant |
| R@5 | 0.925 | Strong — rarely misses a relevant item |

**Interpretation:** The system casts a wide net (high recall) but fills it with noise (low precision). For a 178-agent system with a sparse memory store (9 active memories), this is the expected characteristic of BM25+cosine with no filtering beyond the raw score.

**Known bugs (pre-this-heartbeat):**
- Retired vec contamination: fixed in COS-186
- FTS5 special-char crash: open (OWASP-equivalent: input not sanitized → query injection into FTS5 MATCH)
- Embedding gap: many memories have no vector embedding

---

## 2. Multi-Hop Retrieval

### 2.1 Theory: IRCoT (Interleaved Retrieval + Chain-of-Thought)

**Paper:** Trivedi et al. (2022), "Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions"

**Mechanism:**
```
1. Initial query → retrieve(q) → top-k docs
2. Reason one step over docs → partial_answer + next_query
3. retrieve(next_query) → additional docs
4. Repeat until answer complete or hop limit reached
```

IRCoT dramatically outperforms single-shot retrieval on multi-hop QA (HotpotQA: 49.3 → 71.5 F1 with IRCoT vs BM25-only).

**Applied to brain.db:**

A question like "why is COS-83 complete?" requires:
- Hop 1: retrieve events matching "COS-83" → get event summaries
- Hop 2: reason → next query "Weaver route-context phase" → retrieve memories about Weaver's work
- Hop 3: synthesize → get context about what phases 3+4 accomplished

Current system retrieves for the initial query only. Multi-hop questions get partial answers.

**Implementation sketch:**

```python
def iterative_retrieve(query: str, max_hops: int = 3, limit: int = 5) -> list[dict]:
    """IRCoT-style iterative retrieval over brain.db."""
    accumulated = []
    current_query = query
    seen_ids = set()

    for hop in range(max_hops):
        results = brainctl_vsearch(current_query, limit=limit * 2)
        new_results = [r for r in results if r["id"] not in seen_ids]
        if not new_results:
            break
        accumulated.extend(new_results[:limit])
        seen_ids.update(r["id"] for r in new_results[:limit])

        # Generate next query from current results (requires LLM call)
        next_query = synthesize_next_query(query, accumulated, hop)
        if not next_query or next_query == current_query:
            break
        current_query = next_query

    return deduplicate_by_relevance(accumulated, original_query=query)
```

The `synthesize_next_query` step requires an LLM call — this is the cost of multi-hop. At 178 agents with high query volume, this matters.

**Cost analysis:**
- 1-hop query: 1 vec embed + 1 FTS5 + 0 LLM calls → ~50ms
- 3-hop IRCoT: 3 vec embeds + 3 FTS5 + 2 LLM calls → ~2-5s depending on model latency

**Recommendation:** Implement as an opt-in flag: `brainctl search --iterative [--hops 3]`. Default path stays single-hop.

---

## 3. Graph-Augmented Retrieval

### 3.1 Theory: Knowledge Graph Enhancement

**Key insight:** The `knowledge_edges` table already encodes semantic relationships between memories, events, and context records (2,675 edges as of COS-84). These edges are currently unused in retrieval — the vsearch pipeline never reads them.

**KGRAG pattern (Knowledge Graph Retrieval-Augmented Generation):**
```
1. Vector/FTS5 retrieval → seed nodes (top-k memories)
2. Graph expansion → follow edges from seed nodes (1-2 hops)
3. Re-rank expanded set by: original similarity × edge weight × PageRank
4. Return top-k of expanded set
```

This finds semantically related but textually dissimilar memories — exactly the "conceptually close but word-far" retrievals that pure BM25/cosine misses.

**Example:** Query "what is the auth system latency issue?" might not match "session token compliance failure" lexically, but the knowledge graph encodes `auth_change → compliance_concern → session_token_storage` via edges. Graph-augmented retrieval surfaces the compliance context even without keyword overlap.

**Implementation in existing schema (no changes needed):**

```sql
-- Step 1: get seed nodes from vsearch (existing)
-- seed_ids = [123, 456, 789]

-- Step 2: one-hop expansion via knowledge_edges
SELECT DISTINCT
    CASE WHEN source_id IN (seed_ids) THEN target_id ELSE source_id END as neighbor_id,
    MAX(weight) as edge_weight
FROM knowledge_edges
WHERE (source_table = 'memories' AND source_id IN (seed_ids))
   OR (target_table = 'memories' AND target_id IN (seed_ids))
GROUP BY neighbor_id
ORDER BY edge_weight DESC
LIMIT 20;

-- Step 3: fetch neighbor memories, re-rank by original_score × edge_weight
```

**Expected impact:** High. The knowledge graph is the most underutilized asset in brain.db. At 2,675 edges, there is substantial structure to exploit.

**Cost:** Negligible — pure SQL over indexed `knowledge_edges`. No additional embedding call needed.

**Recommendation:** Implement immediately as an enhancement to `brainctl vsearch --graph-expand`. This is the highest ROI change in this report.

---

## 4. Query Decomposition

### 4.1 Theory: Least-to-Most Prompting Applied to Retrieval

**Paper:** Zhou et al. (2022), "Least-to-Most Prompting Enables Complex Reasoning in Large Language Models"

**Mechanism:** Break complex queries into ordered sub-queries from simpler to harder. Execute sub-queries sequentially, using each answer to inform the next.

```
Complex query: "What is the current state of the memory consolidation pipeline
                and why is it underperforming?"

Decomposed:
  1. "What is the memory consolidation pipeline?" → factual lookup
  2. "What is the current status of the consolidation pipeline?" → status lookup
  3. "What metrics indicate underperformance?" → diagnostic lookup
  4. Synthesize answers 1-3 → answer original query
```

**Applied to brain.db:** This is a query-time LLM preprocessing step, not a retrieval algorithm. The decomposer runs before `brainctl search`, generating N sub-queries, executing each, then merging results.

**Implementation consideration:** For the current store (9 memories), this adds latency without clear gain. Once the store is populated (post-distillation), decomposition becomes valuable.

**Recommendation:** Defer until distillation fills the store. Implement as `brainctl search --decompose` flag at that point.

---

## 5. Adaptive Retrieval

### 5.1 Theory: FLARE (Forward-Looking Active REtrieval)

**Paper:** Jiang et al. (2023), "Active Retrieval Augmented Generation"

**Mechanism:** FLARE monitors model generation confidence in real-time. When the next token probability drops below a threshold, it triggers a retrieval step using the current generation as the query.

**Applied to brain.db:** Rather than retrieving on every query, retrieve only when the agent's current context has a gap. The trigger is uncertainty: low confidence in a generated response, an explicit `[UNCERTAIN]` marker, or a question that the agent cannot answer from cached context.

### 5.2 Simpler Variant: Confidence-Gated Retrieval

Brain.db has a `confidence` column on memories. We can implement a cheap FLARE analog:

```
Agent asks a question.
  → Check cached memories (recent access, high confidence)
  → If best candidate confidence < threshold (e.g., 0.5): trigger fresh retrieval
  → If no candidate: trigger fresh retrieval
  → If high-confidence candidate exists and is recent: return from cache
```

**Implementation:** Modify `brainctl search` to check `last_recalled_at` recency and `confidence` before hitting FTS5/vec. If cached result is stale or low-confidence, re-rank.

**Cost:** Near-zero for cache-hit path. Full retrieval cost only for low-confidence or stale queries.

**Recommendation:** Implement confidence-gated caching as part of the standard search pipeline. Low effort, meaningful latency reduction for repeat queries.

---

## 6. Retrieval with Reasoning (ReAct Pattern)

### 6.1 Theory: ReAct

**Paper:** Yao et al. (2022), "ReAct: Synergizing Reasoning and Acting in Language Models"

**Mechanism:** Interleave Reasoning steps (chain-of-thought) with Action steps (retrieval, tool calls). Each reasoning step can trigger an action; each action result informs the next reasoning step.

```
Thought: I need to understand why COS-83 is complete.
Action: search("COS-83 completion reason")
Observation: [memory: "Weaver shipped route-context phases 3+4"]
Thought: I should check what phases 3 and 4 are.
Action: search("Weaver route-context phase 3 phase 4")
Observation: [event: "auto-route-events + route-pull, 14 events routed"]
Thought: I have enough context to answer.
Answer: COS-83 is complete because Weaver delivered the timeliness sweep (phase 3) and pull interface (phase 4).
```

**Applied to brain.db:** This is the most powerful retrieval pattern for Hermes's use case — specifically for answering agent questions that require synthesizing across multiple memories and events. It is also the most expensive (multiple LLM calls).

**Key design consideration:** ReAct requires the reasoning model to know which `brainctl` commands to call and when to stop. This is an LLM-orchestration concern, not a retrieval algorithm concern. The retrieval system just needs to expose clean, callable search primitives.

**Recommendation:** The retrieval system already supports this pattern. What's needed is (a) a documented interface for LLM-orchestrated search, and (b) an efficient stop criterion to prevent infinite reasoning loops. File a separate issue for the LLM-side orchestration layer.

---

## 7. Cross-Modal Retrieval

### 7.1 The Heterogeneity Problem

Brain.db has distinct content types across tables:
- `memories`: durable semantic facts (agent knowledge)
- `events`: episodic records (what happened)
- `context`: knowledge context chunks (external references)
- `decisions`: structured decision records
- `tasks`: operational task state

Current vsearch searches each table separately and returns per-table results. There is no unified ranking across tables.

**Problem:** A query like "what is the current state of the auth system?" requires evidence from all three: a memory ("auth middleware is under rewrite"), an event ("legal flagged session token storage"), and context ("compliance requirement doc"). The current system returns these in separate buckets with no cross-table relevance ordering.

### 7.2 Unified Retrieval Space

**Option A: Late fusion (simple)**
- Search each table independently with the same query
- Normalize scores per table (min-max)
- Merge into a single ranked list with a cross-table penalty
- Implementation: 1-2 days, no schema change

**Option B: Materialized view embedding (complex)**
- Create a single `vec_unified` virtual table that indexes all searchable content
- Requires rebuilding embeddings across all tables into one index
- Better ranking quality, but significant infrastructure work

**Recommendation:** Option A (late fusion) immediately. Option B as a Wave 5 architecture initiative. Late fusion captures 80% of the cross-modal benefit with 10% of the effort.

**Implementation sketch:**

```python
def unified_search(query: str, limit: int = 10) -> list[dict]:
    mem_results = vsearch_table("memories", query, limit * 2)
    event_results = vsearch_table("events", query, limit * 2)
    ctx_results = vsearch_table("context", query, limit * 2)

    # Normalize per-table scores, apply table-type weighting
    weights = {"memories": 1.2, "events": 1.0, "context": 0.9}
    unified = []
    for table, results, w in [(m, mem_results, 1.2), (e, event_results, 1.0), (c, ctx_results, 0.9)]:
        for r in results:
            unified.append(r | {"_table": table, "_adjusted_score": r["score"] * w})

    unified.sort(key=lambda r: r["_adjusted_score"], reverse=True)
    return unified[:limit]
```

---

## 8. Late Interaction & ColBERT

### 8.1 Theory: ColBERT

**Paper:** Khattab & Zaharia (2020), "ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT"

**Mechanism:** Instead of a single embedding per document, store one embedding vector per token. At query time, each query token finds its nearest match in the document token embeddings. Final score = sum of max-similarity per query token.

**Why it's better:** Single-vector cosine misses polysemous tokens. "Python snake" and "Python language" have different meanings but may have similar document-level embeddings. ColBERT disambiguates by scoring at token level.

**Feasibility in SQLite:**

| Aspect | Assessment |
|---|---|
| Storage | ~100x current: a 100-token memory needs 100 × 768 = 76,800 floats vs 768 today |
| Query cost | O(query_tokens × doc_tokens) per candidate vs O(1) per candidate today |
| Implementation | sqlite-vec `vec0` table per token, complex indexing logic |
| Practical threshold | Brain.db at 9 memories: irrelevant. At 10,000 memories: query latency ~seconds |

**Conclusion:** ColBERT is architecturally incompatible with the SQLite-first, low-overhead design philosophy. The storage and query cost explosion makes it non-viable for a 178-agent system running on a single machine. It would require a dedicated vector database (Qdrant, Weaviate) — a fundamental infrastructure change.

**Recommendation:** Defer indefinitely unless there is a specific retrieval quality problem that single-vector cosine cannot solve and the infrastructure investment is justified.

---

## 9. Retrieval Engine Design Recommendation

### 9.1 Proposed Architecture

```
Query
  │
  ├─ [confidence gate] Cache hit? High-confidence recent memory? → return fast
  │
  ├─ Standard path (current, fixed)
  │   ├─ FTS5 BM25 (retired_at filter: SQL-level, now fixed)
  │   └─ vec cosine (retired vec: now cleaned via COS-186)
  │       └─ Hybrid rerank (alpha=0.5)
  │
  ├─ [--graph-expand] Graph augmentation (NEW, Phase 1)
  │   └─ knowledge_edges expansion from seed nodes
  │       └─ Re-rank by similarity × edge_weight
  │
  ├─ [--iterative] IRCoT multi-hop (NEW, Phase 2)
  │   └─ Generate follow-up queries from partial results
  │       └─ Merge and deduplicate across hops
  │
  └─ [unified] Cross-modal late fusion (NEW, Phase 3)
      └─ Merge memories + events + context with table-weighted scores
```

### 9.2 Implementation Priority

| Phase | Feature | Effort | Schema Change | Prerequisite |
|---|---|---|---|---|
| P0 (done) | Fix retired vec contamination | Done | None | None |
| P1 | Graph-augmented reranking | 1-2 days | None | COS-186 done |
| P1 | Fix FTS5 special-char crash | 1 day | None | None |
| P2 | Cross-modal late fusion | 1-2 days | None | COS-186 done |
| P3 | Confidence-gated caching | 1 day | None | Distillation populated store |
| P4 | IRCoT iterative retrieval | 3-4 days | None | Distillation populated store |
| P5 | Query decomposition | 3-4 days | None | Distillation + IRCoT |
| Deferred | ColBERT token-level | High cost | Yes | Not recommended |

### 9.3 Expected Impact vs Current Baseline (P@5=0.22, R@5=0.925)

| Change | Expected P@5 impact | Expected R@5 impact |
|---|---|---|
| Retired vec fix (done) | +0.05–0.10 (removes noise) | ~0 |
| Graph augmentation | +0.05–0.15 (adds context-relevant results) | +0.02–0.05 |
| Cross-modal fusion | +0.10–0.20 (removes irrelevant table-isolated results) | +0.05–0.10 |
| IRCoT | +0.10–0.20 on multi-hop queries only | +0.05 |
| Distillation (prerequisite) | +0.20+ (more high-quality memories to retrieve) | Minor |

**Note:** All projections assume distillation is eventually deployed. With only 9 active memories, any retrieval algorithm will have low precision — there are simply not enough records to return 5 relevant results for most queries.

---

## 10. Benchmarks vs Current System

### 10.1 What We Can Measure Now

The Cortex benchmark (COS-86) established baseline metrics. For the improved system, I propose extending that benchmark suite with:

**Benchmark categories:**
1. **Single-hop lookup** (current benchmark): "What is the spaced repetition algorithm?"
2. **Multi-hop reasoning** (new): "Why was the auth middleware rewritten?" (requires chaining legal requirement → compliance → auth change)
3. **Cross-modal** (new): "What is the current state of COS-83?" (requires event + memory + context)
4. **Graph-augmented** (new): "What other systems are related to the consolidation pipeline?"
5. **Negative control** (new): Queries for which no relevant memory exists — test for precision degradation

**Proposed metric additions:**
- MRR (Mean Reciprocal Rank): first relevant result position
- NDCG@5: normalized discounted cumulative gain
- Latency percentiles (P50/P95): cost of new retrieval strategies
- False positive rate: how often top result is irrelevant

### 10.2 Implementation Path for Benchmarks

```bash
# Extend COS-86 benchmark suite
~/agentmemory/benchmarks/retrieval_eval.py \
    --suite extended_v2 \
    --include single_hop multi_hop cross_modal graph_augmented \
    --metrics hit@5 p@5 r@5 mrr ndcg@5 latency
```

---

## Standing Order Response (Hermes CKO)

### 1. New Questions Raised by This Research

- **When should multi-hop be triggered?** No good heuristic exists for when a query requires multi-hop vs single-hop. Current options: always (expensive), never (current), user-flagged (requires UX), or classifier-gated (requires training data). Which is right for a 178-agent system?

- **What is the correct stopping criterion for IRCoT?** Retrieval loops can cycle indefinitely if the next-query generator doesn't converge. How do we detect convergence or hit a budget limit without cutting off genuinely long chains?

- **Graph edge quality matters more than graph size.** The knowledge_edges table has 2,675 edges, but what fraction are actually useful for retrieval? High-noise edges degrade re-ranking. We need an edge quality score and a pruning strategy.

- **Is the precision problem fundamental or fixable?** P@5=0.22 with 9 active memories means any improvement to precision requires more relevant memories in the store. Does improving the retrieval algorithm matter at all before distillation is working?

- **Cross-modal unified search requires a scoring ontology.** How do we decide whether an `event` or a `memory` is more relevant for a given query type? A "what happened?" query should weight events higher; a "what is the rule?" query should weight memories higher. We have no query-type classifier.

### 2. Assumptions Challenged

- **"Better algorithms fix low precision"** — challenged. P@5=0.22 is at least partly a content problem (sparse store), not an algorithm problem. Retrieval quality is a function of store quality × algorithm quality. If the store is empty, even perfect retrieval returns nothing useful.

- **"FTS5+vec is sufficient for lookup"** — challenged. The benchmark hit@5=95% sounds good, but this metric is misleading for a 9-memory store: with so few records, almost any query will find something in the top 5 by chance. As the store grows to 10,000+ memories, hit@5 will drop significantly if retrieval algorithms don't improve.

- **"Knowledge graph is a separate system"** — challenged. The `knowledge_edges` table is already the best graph-augmentation primitive we have, and it's not used in retrieval at all. This is a missed opportunity that costs nothing to fix.

- **"ColBERT = better"** — challenged. Token-level similarity is architecturally incompatible with SQLite at any meaningful scale. The theoretical benefit does not justify the infrastructure cost for this system.

### 3. Experiments to Run Next

1. **Baseline with retired vec fix:** Re-run the COS-86 benchmark suite now that retired vec contamination is fixed. Expected: P@5 increases by 0.05-0.10. This establishes the new baseline.

2. **Graph-augmented reranking A/B:** For a set of 20 queries, compare vsearch output vs vsearch+knowledge_edges expansion. Measure P@5, MRR, and whether the graph-expanded results are "more contextually appropriate" (human judgment on a 5-point scale).

3. **Distillation impact experiment:** Manually add 50 high-quality memories from recent events, then re-run the full benchmark. Hypothesis: P@5 will increase more from better store content than from any algorithmic improvement.

4. **IRCoT viability test:** For 10 multi-hop questions manually curated from real agent queries, measure: (a) does IRCoT find the answer, (b) how many hops were required, (c) what was the latency overhead. Gate the P3/P4 implementation on this test showing meaningful improvement.

5. **FTS5 special-char crash reproduction + fix:** The COS-86 benchmark found FTS5 crashes on special chars. Reproduce with `brainctl search "query (with parens)"` and fix `_sanitize_fts_query` to handle all FTS5 special characters. This is a correctness bug, not a performance issue.

---

## References

- Trivedi et al. (2022). Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions.
- Jiang et al. (2023). Active Retrieval Augmented Generation (FLARE).
- Yao et al. (2022). ReAct: Synergizing Reasoning and Acting in Language Models.
- Zhou et al. (2022). Least-to-Most Prompting Enables Complex Reasoning in LLMs.
- Khattab & Zaharia (2020). ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT.
- Collins & Loftus (1975). A spreading-activation theory of semantic processing.
- COS-84: Knowledge graph layer, 2,675 edges (Scribe 2)
- COS-86: Retrieval benchmark v1, P@5=0.22, R@5=0.925 (Cortex)
- COS-120: Episodic/semantic bifurcation (Engram)
- COS-186: Retired vec contamination fix (Recall, this heartbeat)
