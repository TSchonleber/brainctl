# Wave 13 Research: Graph Algorithms for Knowledge Structure

**Issue:** COS-416  
**Date:** 2026-04-02  
**Agent:** Weaver (paperclip-weaver)  
**Status:** COMPLETE — all deliverables implemented in brainctl

---

## Hypothesis

brain.db has 4,750+ `knowledge_edges` but only uses them for neighbor lookup and spreading activation. Network science algorithms can extract structural insights that improve search ranking, consolidation, and entity discovery.

---

## Implementation Summary

All algorithms implemented in pure Python (no external dependencies) directly in `~/bin/brainctl`. Results cached in `agent_state` with TTL (24h PageRank/communities, 48h betweenness).

### Graph Statistics (baseline)

- **Total edges:** 4,750
- **Connected nodes:** 542 (after filtering to memories/events/context/entities)
- **Relation type distribution:**
  - `causes`: 1,686 (35.5%)
  - `topical_tag`: 871 (18.3%)
  - `semantic_similar`: 742 (15.6%)
  - `topical_project`: 579 (12.2%)
  - `topical_scope`: 479 (10.1%)
  - `co_referenced`: 388 (8.2%)
  - Other: 5 (0.1%)

---

## Research Question 1: PageRank on Entities

**Command:** `brainctl graph pagerank [--damping FLOAT] [--top-k N] [--force]`

**Algorithm:** Power iteration PageRank (weighted, undirected). Converges in ~15 iterations on this graph (tol=1e-6).

**Top-15 nodes by PageRank (damping=0.85):**

| Score    | Node          | Label                                                    |
|----------|---------------|----------------------------------------------------------|
| 0.005390 | memories#78   | CostClock invoice subsystem note                         |
| 0.004136 | memories#93   | Agent memory spine state (2026-03-28)                    |
| 0.004095 | memories#127  | brainctl push gate threshold                             |
| 0.004095 | memories#130  | CostClock AI stack description                           |
| 0.004093 | memories#125  | Kernel brainctl integration (COS-207)                    |
| 0.004071 | context#189   | Routed context from event:86 (COS-85)                    |
| 0.004071 | context#188   | Routed context from event:86 (COS-85)                    |
| 0.004071 | context#183   | COS-85: Resolved stale_assumption findings               |
| 0.003907 | events#187    | brain_tool.py integration complete (COS-207)             |
| 0.003735 | events#186    | test event                                               |
| 0.003699 | events#191    | Cortex heartbeat 3: closed COS-110                       |
| 0.003472 | memories#106  | Compressed 15 memories (costclock-ai:invoice)            |

**Findings:**
- memories#78 (CostClock invoice subsystem) is the single most central node — appropriately a project-critical compound memory
- High-PageRank nodes are all `permanent` temporal class, confirming the metric correlates with what agents already protect manually
- Context nodes cluster near events from the same COS-85 coherence run — PageRank detected the dense cross-referencing created by that activity burst
- **Recommendation:** PageRank > 0.003 threshold identifies ~15 nodes worth protecting from decay. These overlap with `temporal_class=permanent` but add ~3 nodes not yet marked permanent.

**Search integration:** `brainctl search <query> --pagerank-boost 0.3` multiplies `final_score` by `(1 + 0.3 * normalized_pagerank)`. This boosts well-connected memories' rank without overriding query relevance entirely. Alpha of 0.3 is a good starting point; higher values bias toward centrality over query match.

---

## Research Question 2: Community Detection

**Command:** `brainctl graph communities [--seed N] [--force]`

**Algorithm:** Label propagation (LPA) with weighted voting. Deterministic at seed=42. Converges in ~8 iterations.

**Results:** 55 communities from 542 nodes

**Top-10 communities by size:**

| Community | Nodes | Representative Nodes                                        |
|-----------|-------|-------------------------------------------------------------|
| 2         | 39    | memories#125, #134, #376 — Kernel/brainctl integration      |
| 49        | 37    | events#205, #182, #200 — recent event cluster               |
| 51        | 35    | events#74, #125, #102 — mid-session event cluster           |
| 17        | 33    | events#52, #61, #56 — early hippocampus events              |
| 22        | 33    | context#33, #28, #37 — context routing cluster              |
| 43        | 25    | events#196, #173, #168 — Cortex/research wave cluster       |
| 7         | 25    | context#187, #79, #186 — coherence check context            |
| 21        | 23    | memories#69, #64, #91 — trust/confidence memories           |
| 23        | 21    | memories#78, #55, #36 — CostClock-focused memories          |
| 37        | 18    | events#88, #121, #116 — distillation/consolidation events   |

**Findings:**
- Communities 49, 51, 17 are temporal event clusters (chronological activity runs), not semantic clusters — expected given `causes` edges dominate
- Community 23 (CostClock memories centered on memories#78) validates that the highest-PageRank node anchors its own semantic community
- Community 2 (Kernel brainctl) is the largest memory community — reflects dense cross-referencing during COS-207 integration work
- 55 communities from 542 nodes = ~10 nodes/community average, but top communities are 3-4x larger than average (power law)
- **Limitation:** LPA detects communities based purely on topology (edge density), not semantic content. The `topical_project` and `topical_tag` relation types already encode semantic groupings better. Communities here primarily map to *activity bursts* (temporal co-creation) rather than topic clusters.
- **Recommendation:** Community labels are useful as "activity epoch" markers, not as replacements for scope/tags. Use them to detect which memories were created in the same work session.

---

## Research Question 3: Betweenness Centrality

**Command:** `brainctl graph betweenness [--top-k N] [--force]`

**Algorithm:** Brandes algorithm (unweighted BFS). O(V·E) = O(542 × 4750) ≈ 2.6M ops. Runs in ~2s.

**Top-10 bridge nodes:**

| Score    | Node        | Label                                                     |
|----------|-------------|-----------------------------------------------------------|
| 0.038227 | events#68   | Cadence metrics refreshed                                 |
| 0.030273 | events#20   | COS-57: hippocampus-cycle.sh created                     |
| 0.027487 | events#146  | Distilled event#93 → memory#108                          |
| 0.015776 | events#163  | Wave 2+4 research complete                               |
| 0.015043 | events#126  | COS-32 closed as duplicate of COS-72                     |
| 0.014067 | events#38   | Cadence metrics refreshed                                 |
| 0.013843 | events#31   | Cadence metrics refreshed                                 |
| 0.011517 | memories#78 | CostClock invoice subsystem                               |
| 0.009520 | events#123  | COS-86: distillation policy drafted                       |
| 0.009520 | events#122  | COS-123: situation model research                         |

**Findings:**
- Cadence events (recurring metrics) dominate betweenness — they're written at regular intervals and link chronologically to events before and after them, making them structural bridges between time-adjacent clusters
- memories#78 appears in both top-PageRank AND top-betweenness — doubly confirmed as the highest-value memory to protect
- Distillation events (event#146: distilled to memory#108) have high betweenness because they connect the event graph to the memory graph via promotion edges
- Only 1 memory node appears in top-10 betweenness (memories#78 at position 8) — most bridges are events, not memories
- **Recommendation:** Apply `protect-bridges` with threshold=0.005 to catch memories with non-trivial betweenness (currently only memories#78 qualifies). Lower threshold to 0.003 after graph grows.

**EWC integration:** `brainctl graph protect-bridges` marks qualifying memory nodes with `protected=1` and sets `ewc_importance = betweenness / max_betweenness`. Currently protects 1 node (memories#78, ewc_importance=0.3013).

---

## Research Question 4: Shortest Path Queries

**Command:** `brainctl graph path <from_table> <from_id> <to_table> <to_id> [--max-hops N]`

**Algorithm:** BFS (unweighted). Finds shortest hop count.

**Example outputs:**
```
$ brainctl graph path memories 78 memories 93
Shortest path: 1 hops
  [0] memories#78  CostClock invoice subsystem...
  [1] --[co_referenced]--> memories#93  Agent memory spine state...

$ brainctl graph path memories 78 events 68
(traverses causal edges through the event graph)
```

**Findings:**
- Average shortest path between connected memories is ~2 hops via the `co_referenced` + `semantic_similar` bridge
- The diameter (longest shortest path) is bounded by the causal event chain, which forms a linear backbone
- Path queries expose relationship chains useful for the entity registry ("how is entity X related to entity Y?")

---

## Research Question 5: Graph-Based Search Reranking

**Flag:** `brainctl search <query> --pagerank-boost <alpha>`

**Implementation:** After FTS5/vector retrieval and temporal decay scoring, multiply `final_score` by `(1 + alpha * (pagerank / max_pagerank))`. The `pagerank_score` field is added to each result for transparency.

**Evaluation (qualitative):**
- Query "costclock invoice": memories#78 (highest PageRank) promoted above memories#130 at alpha=0.3
- Without boost: memories#130 (CostClock stack description, more FTS matches) ranks first
- With boost: memories#78 (invoice-specific, higher centrality) ranks first — semantically more specific
- **Recommendation:** alpha=0.2–0.3 is the sweet spot. Higher values over-weight centrality and surface hub nodes regardless of query relevance.

---

## Deliverables Checklist

| Deliverable                              | Status  | Command                                    |
|------------------------------------------|---------|--------------------------------------------|
| `brainctl graph pagerank`               | ✅ Done  | `brainctl graph pagerank [--top-k N]`      |
| `brainctl graph communities`            | ✅ Done  | `brainctl graph communities`               |
| `brainctl graph path <from> <to>`       | ✅ Done  | `brainctl graph path T1 ID1 T2 ID2`        |
| `brainctl graph betweenness`            | ✅ Done  | `brainctl graph betweenness [--top-k N]`   |
| `brainctl graph protect-bridges`        | ✅ Done  | `brainctl graph protect-bridges [--dry-run]` |
| PageRank factor in search ranking       | ✅ Done  | `brainctl search Q --pagerank-boost 0.3`   |
| Results cached (agent_state)            | ✅ Done  | PageRank/communities 24h TTL, betweenness 48h |
| No external deps (pure Python stdlib)   | ✅ Done  | NetworkX not required                      |

---

## Implementation Notes

**Performance (4,750 edges, 542 nodes):**
- PageRank: ~0.1s (50 iterations, converges ~15)
- Label propagation: ~0.3s (8 iterations to convergence)
- Betweenness (Brandes): ~2s (O(V·E), acceptable for periodic caching)
- Shortest path (BFS): ~0.05s for typical queries

**Cache strategy:** All results stored in `agent_state` under `agent_id='paperclip-weaver'`. Force-refresh with `--force`. TTL: 24h for PageRank/communities, 48h for betweenness.

**Scalability:** At current growth rate (~50 edges/day), the graph will reach 10k edges in ~50 days. Betweenness will become O(50) seconds at that scale. Mitigation: increase TTL to 7 days or sample a subgraph.

---

## Prior Art References

- [COS-84](/COS/issues/COS-84): Knowledge Graph Layer — DONE
- [COS-192](/COS/issues/COS-192): Spreading Activation — DONE
- [COS-301](/COS/issues/COS-301): Dynamic edge weights (neuroplasticity) — DONE
- [COS-314](/COS/issues/COS-314): Global Workspace broadcast — DONE
- [COS-397](/COS/issues/COS-397): Quantum Walk on Knowledge Graph — DONE (research only)
- [COS-117](/COS/issues/COS-117): Advanced Retrieval & Reasoning — found PageRank reranking to be highest-ROI graph enhancement

---

## Key Findings Summary

1. **memories#78** is the most central knowledge node by both PageRank and betweenness — it is the single most important memory to protect and has already been marked `protected=1` by `protect-bridges`.
2. **Community detection finds activity epochs, not topics** — the dominant edge type (`causes`) creates linear temporal chains, so LPA finds chronological clusters. Topical communities are better served by existing `scope` and `tags` fields.
3. **Betweenness bridge nodes are mostly cadence events** — recurring periodic events serve as structural bridges in the event graph. This is expected and not a concern.
4. **PageRank as a search signal works well at alpha=0.2–0.3** — it promotes genuinely central memories without overriding query relevance. Useful for surfacing "always-relevant" knowledge even when query match is weaker.
5. **Graph is well-connected but sparse** — 542 nodes, 4750 edges = ~8.7 edges/node. This is healthy for BFS-based algorithms. No isolated components were found in the top-ranked nodes.
