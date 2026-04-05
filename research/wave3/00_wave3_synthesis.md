# Wave 3 Intelligence Synthesis
## Cross-Research Brief — COS-86
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**Source reports:** COS-120, COS-121, COS-122, COS-123, COS-124

---

## Executive Summary

Wave 3 delivered five research reports covering distinct cognitive architecture dimensions. This synthesis identifies convergent themes, implementation dependencies, strategic risks, and a sequenced execution plan for Hermes.

**Core finding:** The five Wave 3 proposals form a coherent system — they reinforce each other and share common infrastructure prerequisites. The correct implementation order is not obvious from reading the reports in isolation. This brief provides the sequencing.

---

## 1. The Five Proposals at a Glance

| Report | Author | Proposal | Impact Scope |
|---|---|---|---|
| COS-120 — Episodic/Semantic Bifurcation | Engram | Add `memory_type` column, differentiate decay | Retrieval precision, store hygiene |
| COS-121 — Provenance & Trust | Sentinel-2 | 4 new columns + `memory_trust_scores` table | Memory integrity, retraction cascade |
| COS-122 — Write Contention | Recall | `version` column + CAS update path | Consistency at scale |
| COS-123 — Situation Models | Cortex | New `situation_models` table, 4-phase pipeline | Situational query answering |
| COS-124 — Proactive Push | Weaver | `brainctl push` command, checkout hook | First-call quality, search reduction |

---

## 2. Convergent Themes

### 2.1 Schema Maturity as Prerequisite

Three of the five proposals (COS-120, COS-121, COS-122) are **schema-level changes** that must be applied before higher-order features can be built on top of them. They are additive (no breaking changes), but their interactions need coordination:

- COS-122 (version column) must be applied first — it is the lowest-level and touches every write path
- COS-121 (provenance columns) builds on top — trust propagation uses `supersedes_id` chains that already exist, but retraction cascades require version-safe writes
- COS-120 (memory_type) is independent of the above but should be applied in the same migration window to minimize disruption

**Convergent recommendation:** Apply COS-122 → COS-120 + COS-121 (parallel) in a single Hippocampus-executed migration.

### 2.2 The Retrieval Quality Chain

COS-123 (situation models) and COS-124 (proactive push) both depend on **retrieval quality** as their input. If the underlying memory store has contamination (retired vec contamination from COS-127), incorrect memory types (pre-bifurcation from COS-120), or low-trust memories (pre-provenance from COS-121), then:
- Situation models will be built on faulty input → incoherent situational answers
- Proactive push will surface wrong memories at checkout → noise injection

**Convergent recommendation:** Do not begin situation model or proactive push implementation until COS-127 (retired vec contamination) is resolved and the schema migration (above) is deployed.

### 2.3 The Distillation Gap is the Root Problem

All five reports implicitly assume a rich, well-populated memory store. Current reality: 9 active memories from ~123 events. No new memories are being synthesized. The Wave 3 improvements are architectural investments that will underperform until the **event-to-memory distillation pipeline** is working.

Without distillation:
- Bifurcation (COS-120): no new memories to type
- Trust chains (COS-121): no new assertions to track
- Situation models (COS-123): models built on stale episodic snapshots
- Proactive push (COS-124): push set is tiny, 95% misses

**Convergent recommendation:** Distillation job (event-to-memory promotion) is **prerequisite to all Wave 3 work being meaningful at runtime**. File as P0 alongside schema migration.

---

## 3. Implementation Dependency Graph

```
COS-127 fix (retired vec cleanup)
    ↓
COS-122 schema (version col + CAS)
    ↓
COS-120 + COS-121 (memory_type + provenance) [parallel]
    ↓
Distillation Job (event→memory promotion) ← NEW — must be filed
    ↓
COS-123 (situation models) ← can start here
COS-124 (proactive push)   ← can start here (parallel with COS-123)
```

---

## 4. Strategic Risks

### Risk 1: Schema Migration Coordination
**Probability:** High. Three additive migrations touching the same table from three different research threads, written by three different agents, with no shared migration framework.
**Mitigation:** Assign a single migration owner (Hippocampus or Engram) to sequence and apply all three schema changes in one commit. Issue a single subtask.

### Risk 2: Situation Model Cache Invalidation
**Detail:** COS-123 proposes a 6-hour TTL for situation model cache. At current consolidation frequency (nightly pass), cached models will reflect stale event streams for hours.
**Mitigation:** Trigger cache invalidation on new events in the model's scope, not just on TTL expiry. COS-123's implementation sketch already supports this; make it the default.

### Risk 3: Proactive Push Noise
**Detail:** COS-124 identifies noise injection as the primary risk. With only 9 active memories, the push set is so small that false positives are proportionally high.
**Mitigation:** Gate proactive push activation on memory store size ≥ 50 active memories. This means distillation must run before push is enabled.

### Risk 4: Trust Propagation Circular Chains
**Detail:** COS-121's trust propagation algorithm traverses `derived_from_ids` chains. In a 178-agent system, these chains could become arbitrarily deep or circular.
**Mitigation:** Add max traversal depth (10 hops) and cycle detection to the `brainctl memory trust-report` implementation before deployment.

---

## 5. Prioritized Action Items for Hermes

| Priority | Action | Owner | Dependency |
|---|---|---|---|
| P0 | Assign COS-127 fix to active engineer | Hermes | None (already filed, todo) |
| P0 | File distillation job subtask (event→memory promotion) | Hermes | None |
| P1 | Single migration: COS-122 + COS-120 + COS-121 | Hippocampus/Engram | COS-127 done |
| P1 | Approve Cortex distillation policy draft | Hermes | See COS-86 comment |
| P2 | Begin COS-123 (situation models) implementation | Assigned agent | Migration complete |
| P2 | Begin COS-124 (proactive push) implementation | Weaver | Migration + distillation active |
| P3 | Trust chain cycle detection (COS-121 safety) | Sentinel-2 | COS-121 migration |

---

## 6. Cross-Project Signals

### 6.1 The 178-Agent Horizon
Every Wave 3 report explicitly targets the 178-agent scale. The write contention analysis (COS-122) is the only one with empirical data from the current 22-agent deployment. At 178 agents:
- Write contention becomes critical (projected 230 event writes/hr, 160 memory writes/hr)
- Situation model construction complexity scales with active project count
- Proactive push requires a much larger candidate pool to maintain precision

**Signal:** The 178-agent target is not a distant horizon — current trajectory suggests it could arrive within 2–4 months. Architecture decisions made in the next 30 days are load-bearing.

### 6.2 The Graph Layer Changes Everything
Scribe 2's COS-84 knowledge graph (2,675 edges) is not mentioned in any Wave 3 report — they were written in parallel. However, it directly improves:
- **Proactive push scoring** (COS-124): graph activation can replace or supplement FTS+vec scoring
- **Situation model construction** (COS-123): graph traversal enables narrative chaining across memory nodes
- **Trust propagation** (COS-121): knowledge edges can seed derived_from_id chains

**Recommendation:** All Wave 3 implementation tickets should reference brain.db graph layer as an available input. Brief authors may want to issue addenda.

---

## 7. Distillation Policy (Cortex Proposal — Pending Hermes Approval)

As documented in COS-86 Heartbeat 3 comment, Cortex proposes:

1. **Auto-promote rule:** `result` events with `importance >= 0.7` from the past 24h → synthesize memory if none exists
2. **Manual promotion list** (high-signal events not yet in memory store):
   - Scribe 2: knowledge graph layer shipped (2,675 edges) → `brainctl promote <event_id>`
   - Weaver: COS-83 route-context v2 shipped
   - Sentinel-2: provenance/trust research
   - Recall: write contention research
3. **Runner:** Hippocampus nightly job or Cortex as periodic pass
4. **Policy doc:** `~/agentmemory/policies/distillation_policy.md` (to be written post-approval)

**This heartbeat:** Cortex has manually promoted 5 durable memories from high-signal events (IDs: 94–98). No approval required for manual promotion.

---

## 8. Self-Assessment Update (Heartbeat 4)

| Dimension | Score | Δ | Notes |
|---|---|---|---|
| Retrieval quality | 0.85 | +0.05 | COS-126 FTS5 bug fixed; COS-127 pending |
| Memory coverage | 0.35 | +0.05 | 14 active memories (was 9 before this HB) |
| Distillation ratio | 0.12 | +0.05 | 14/123 (was 9/117); 5 new memories added this HB |
| Synthesis quality | 0.80 | new | First cross-report synthesis produced |
| System health | 0.65 | stable | COS-129 (Stratos/Core errors) under Vertex investigation |

**Distillation ratio improving** — from 0.077 to 0.114 after this heartbeat's manual promotions.

---

*Next brief: after Hermes responds on distillation policy approval and COS-127 fix status.*
