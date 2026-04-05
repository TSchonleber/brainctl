# AgentMemory Research Digest v2
## 9 Waves | 47 Research Documents | Current Status
**Compiled:** 2026-03-28 | **System:** brain.db (SQLite + FTS5 + sqlite-vec) | **Agents:** 178 target, 26 active
**Health:** RED (composite 0.30) | **Tables:** 80+ | **brainctl commands:** 37+

---

## WAVE 1: FOUNDATIONS (8 documents)
*All delivered as research prototypes. Core algorithmic layer.*

### 01 — Spaced Repetition (01_spaced_repetition.py)
**Author:** Engram | **Status:** SHIPPED
Implements Ebbinghaus forgetting curve with five-tier temporal decay: ephemeral λ=0.5, short λ=0.2, medium λ=0.05, long λ=0.01, permanent=none. On each retrieval, confidence gets asymptotic 15% boost. Memories below 0.15 confidence are retirement candidates. Both Python and pure-SQL implementations. Integrated into hippocampus.py consolidation cycle.

### 02 — Semantic Forgetting (02_semantic_forgetting.py)
**Author:** Engram | **Status:** SHIPPED
Temporal class promotion/demotion based on access patterns. Promotion requires minimum recall thresholds (3 for ephemeral→short, up to 15 for long→permanent) with confidence ≥0.85. Demotion triggers on confidence floor breach and no recall within demotion window. Integrated into hippocampus.py consolidation cycle.

### 03 — Knowledge Graph (03_knowledge_graph.py)
**Author:** Engram | **Status:** SHIPPED
PageRank + BFS expansion over knowledge_edges table. Five relation types: supports (0.8), contradicts (0.9), derived_from (0.7), co_referenced (0.5), supersedes (1.0). BFS context expansion up to 2 hops/50 nodes. Production graph grew to 5,359 edges. Used by Hebbian learning pass in consolidation.

### 03b — AI Memory Systems Survey (03_ai_memory_systems.md)
**Author:** Cortex | **Status:** SHIPPED (research reference)
Comprehensive survey of MemGPT/Letta, Advanced RAG (hybrid BM25+vector with RRF showing 10-20% improvement), Lost-in-the-Middle context bias, Reflexion (20-40% reasoning boost), SOAR/ACT-R cognitive architectures, embedding strategies, memory-augmented transformers, and multi-agent shared memory patterns. Key recommendation: hybrid BM25+vector with RRF is highest-impact, lowest-effort.

### 04 — Neuroscience of Memory (04_neuroscience_memory.md)
**Author:** Cortex + Epoch | **Status:** SHIPPED (research reference)
Maps six neuroscience principles to brain.db: hippocampal consolidation (temporal_class promotion), reconsolidation (confidence evolves on retrieval), synaptic pruning (adaptive forgetting), sleep consolidation (offline batch processing), emotional tagging (salience weighting), and engram theory (embeddings as content-addressable artificial engrams). Central insight: nightly sleep cycle is the universal integration point.

### 04b — Attention/Salience Routing (04_attention_salience_routing.py)
**Author:** Engram | **Status:** SHIPPED
Weighted salience scoring: 0.45×similarity + 0.25×recency + 0.20×confidence + 0.10×importance. Three routing modes: FOCUSED (single agent), BROADCAST (multi-agent), HIERARCHICAL (escalation). Uses FTS5 BM25 with sqlite-vec cosine similarity as precision path. Integrated into brainctl search.

### 05 — Consolidation Cycle (05_consolidation_cycle.py)
**Author:** Engram | **Status:** SHIPPED
Sleep-cycle orchestrator: collect aged memories → cluster by scope → consolidate clusters → retire sources with edges → decay pass → demotion pass → dedup → contradiction detection → cycle report. Now runs as hippocampus.py with 11-pass cycle including dream pass, Hebbian learning, trust updates, neuromodulation, and health snapshots.

### 06 — Contradiction Detection (06_contradiction_detection.py)
**Author:** Engram | **Status:** SHIPPED
Two strategies: supersession chain breaks and FTS negation pattern matching (8 patterns: is/is not, can/cannot, etc.). Contradictions logged to events table. Auto-resolution retires lower-confidence memory when delta > 0.3. Integrated into consolidation cycle.

### 07 — Emergence Detection (07_emergence_detection.py)
**Author:** Engram | **Status:** SHIPPED
Five algorithms: topic frequency trending, agent behavioral drift detection (KL divergence), confidence distribution health, recall cluster analysis, and recurring error chain detection. Feeds consolidation cycle reporting.

### 08 — Context Compression (08_context_compression.py)
**Author:** Engram | **Status:** SHIPPED
Token-budget selection with redundancy pruning (Jaccard overlap threshold 0.55). Temporal compression: recent as full text, older as summaries. Greedy highest-salience fit within budget (default 2000 tokens). Renders markdown context blocks.

---

## WAVE 2: DEEP COGNITION (9 documents)
*Theoretical frameworks with implementation designs.*

### 09 — Associative Memory & Analogical Reasoning (COS-111)
**Author:** Engram | **Status:** PARTIAL
Analyzes six frameworks: spreading activation (Collins & Loftus 1975), structure-mapping (Gentner), concept blending, episodic-semantic cross-pollination, HRR, and SDM. Central finding: spreading activation over knowledge_edges + structural analogy fingerprinting is highest-impact. Spreading activation is now partially live via `brainctl reason`; full structural analogy matching not yet built.

### 10a — Predictive Cognition (COS-112)
**Author:** Weaver | **Status:** PARTIAL
Designs predictive routing engine based on Friston's free energy principle, collaborative filtering, anticipatory computing, proactive information retrieval, and temporal pattern mining. Three prediction horizons: immediate, session, background. Architecture designed; `brainctl push` implemented for checkout-time push but predictive model not trained.

### 10b — Temporal Reasoning & Causal Inference (COS-114)
**Author:** Epoch | **Status:** PARTIAL
Event calculus (Kowalski & Sergot 1986) as SQL views, lightweight causal DAG from temporal co-occurrence, bitemporal modeling, temporal abstraction. Pearl's do-calculus and Granger causality deferred. `brainctl temporal` commands partially available via temporal-context.

### 10c — Cognitive Compression & Abstraction (COS-116)
**Author:** Prune | **Status:** PARTIAL
Three-tier hierarchical memory (Raw → Episode → Abstraction) with progressive summarization. Power-law forgetting (Anderson & Schooler). Matryoshka embeddings for multi-resolution search. Estimated 90-95% footprint reduction at 1M records. Consolidation cycle does compression but full hierarchical abstraction not yet implemented.

### 11a — Causal Event Graph (COS-184)
**Author:** Epoch | **Status:** PARTIAL
Three-tier causal edges: auto-detected (temporal proximity), type-based templates, explicit reference chains. Agent-reported causation via `brainctl event link`. Recursive CTEs for forward/backward traversal. ~60-70% automatic detection accuracy. Schema exists in knowledge_edges but automatic causal edge generation not fully wired.

### 11b — Metacognition & Self-Modeling (COS-110)
**Author:** Cortex | **Status:** PARTIAL
Nelson & Narens monitoring/control framework. Four metacognitive judgments: Ease of Learning, Judgment of Learning, Feeling of Knowing, Confidence Judgment. Gap detection is the highest-value capability. `knowledge_coverage` and `knowledge_gaps` tables exist in brain.db. `brainctl gaps` command available. Nightly gap scan not scheduled.

### 12a — Collective Intelligence Emergence (COS-113)
**Author:** Cortex | **Status:** PARTIAL
Six frameworks: swarm/ACO stigmergy, wisdom of crowds, transactive memory systems (Wegner), network topology, computational social choice, evolutionary epistemology. Highest-impact: `agent_expertise` table for capability-aware routing. Table exists with 1131 rows; `brainctl expertise` and `brainctl whosknows` commands live. Strength values need recalibration from access patterns.

### 12b — Advanced Retrieval & Reasoning (COS-117)
**Author:** Recall | **Status:** PARTIAL
Evaluated seven retrieval paradigms. Graph-augmented reranking identified as highest ROI. IRCoT iterative retrieval designed. Current P@5=0.22 partly a content problem. `brainctl reason` and `brainctl reason-chain` now live, providing multi-step inferential retrieval. Graph augmentation partially integrated.

### 12c — Adversarial Robustness & Memory Integrity (COS-115)
**Author:** Sentinel 2 | **Status:** PARTIAL
Six threat vectors analyzed. Embedding poisoning is highest risk. Content-addressable hashing, embedding anomaly detection, reputation-weighted validation, three-tier self-healing escalation. Trust scoring now live via trust_update_pass(). Full hash chain verification and embedding anomaly detection not implemented.

---

## WAVE 3: ARCHITECTURE PATTERNS (6 documents)
*Implementation-ready designs. All complete.*

### 00 — Wave 3 Synthesis (COS-86)
**Author:** Cortex | **Status:** SHIPPED (reference)
Cross-report brief identifying dependency chain: COS-127 fix → COS-122 schema → COS-120 + COS-121 parallel → distillation → COS-123 + COS-124 parallel. Root finding: all improvements underperform until distillation works. Knowledge graph (2,675+ edges) is the most underutilized asset.

### 01 — Episodic vs. Semantic Bifurcation (COS-120)
**Author:** Engram | **Status:** PARTIAL
Adds `memory_type` column (episodic/semantic) with differentiated decay. Episodic follows exponential decay; semantic uses staleness detection. Episodic→semantic promotion via LLM synthesis of 3+ related episodic clusters. Column likely exists but episodic→semantic promotion pipeline not fully wired.

### 02 — Provenance & Trust Chains (COS-121)
**Author:** Sentinel 2 | **Status:** SHIPPED
Four new columns on memories (validation_agent_id, trust_score, derived_from_ids, retracted_at). `memory_trust_scores` table live. Trust formula: base_prior × validation_boost × age_survival × retraction_penalty. Retraction cascade through derived_from_ids. `brainctl trust` commands operational. Trust score update pass running in consolidation.

### 03 — Write Contention & Consistency (COS-122)
**Author:** Recall | **Status:** PARTIAL
Empirical analysis confirming same-second multi-agent writes, bypass of supersede chains, mixed timestamp formats. Recommends version column with CAS. Analysis complete; version column and CAS pattern not confirmed as fully deployed.

### 04 — Situation Models (COS-123)
**Author:** Cortex | **Status:** PARTIAL
Four-phase pipeline: anchor resolution → multi-strategy retrieval → integration (temporal ordering, contradiction detection, causal chains) → caching. `situation_models` and `situation_model_contradictions` tables exist in brain.db. situation_model_builder.py prototype exists but full brainctl integration unclear.

### 05 — Proactive Push (COS-124)
**Author:** Weaver | **Status:** SHIPPED
Push-based memory delivery at checkout. Three-layer scoring: FTS5 keyword gate → vector similarity → graph activation bonus. Hard cap of 5 memories per push. `brainctl push` command is live. Anti-noise safeguards and utility tracking via push_log designed.

---

## WAVE 4: ADVANCED CAPABILITIES (7 documents)
*Frontier capabilities. All research delivered.*

### 01a — Agent-to-Agent Knowledge Transfer (COS-177)
**Author:** Weaver | **Status:** SHIPPED
Memory Event Bus (MEB): SQLite trigger → memory_events table → agent polling. <500ms propagation, zero external dependencies. `brainctl meb tail --since <watermark>` live. At-least-once delivery, strict AUTOINCREMENT ordering, 24h TTL.

### 01b — Memory Granularity Calibration (COS-178)
**Author:** Prune | **Status:** PARTIAL
Three distinct failures: memories too fine (p50=33 tokens), context catastrophically coarse (894K tokens), events undifferentiated (82% at 0.5 importance). Target: memories 80-250 tokens, context 200-400 tokens. Auto-chunking rules designed but not confirmed as enforced at write time.

### 03 — Cross-Agent Belief Reconciliation (COS-179)
**Author:** Cortex | **Status:** SHIPPED
Five divergence types. Cross-scope entity extraction and comparison. `brainctl belief set/get` and `brainctl belief-conflicts` commands live. `agent_beliefs` and `belief_conflicts` tables exist in brain.db. Implicit belief detection via behavioral mining deferred.

### 05 — Distributed brain.db (COS-181)
**Author:** Bedrock | **Status:** NOT STARTED
Team-sharded SQLite federation (5-7 shards + global index). Burst contention is the failure mode — 100 writes from 15 agents in 1 second already observed. Migration ~500 LOC, zero schema changes. Research complete; sharding not implemented.

### 10 — Memory-to-Goal Feedback Loop (COS-180)
**Author:** Neuron | **Status:** PARTIAL
Five-stage SQL-first pipeline: signal extraction → clustering → proposal generation → ranking → dedup. No LLM in critical path. Thresholds defined for topic surges, error clusters, confidence decay, drift, dead zones. Research and Python implementation delivered; integration with task management unclear.

### 11 — Continuous LLM Consolidation (COS-183)
**Author:** Tensor | **Status:** PARTIAL
Hybrid event-driven + polling consolidation replacing nightly batch. Write-time deduplication and incremental summarization. Consolidation cycle now runs but frequency/continuity improvements may be partial.

### 12 — Memory-Driven Agent Specialization (COS-182)
**Author:** Oracle | **Status:** SHIPPED
Confirmed agent_expertise table (1131 rows), access_log (755 records), knowledge_edges (5,359 edges) provide clear specialization signals. `brainctl expertise` and `brainctl whosknows` commands live. Strength recalibration from retrieval history still needed.

---

## WAVE 5: PRODUCTION HARDENING (6 documents)
*Operational excellence focus.*

### 11 — Reflexion Failure Taxonomy (COS-199)
**Author:** Hermes | **Status:** SHIPPED
Five canonical failure classes: REASONING_ERROR, CONTEXT_LOSS, HALLUCINATION, COORDINATION_FAILURE (dominant), TOOL_MISUSE. Event-driven expiration. Three injection levels: HARD_OVERRIDE, SOFT_HINT, SILENT_LOG. `reflexion_lessons` table with FTS5 index exists. `brainctl reflexion` commands live. Cross-agent propagation via `generalizable_to` field designed.

### 12 — Memory Access Control & RBAC (COS-200)
**Author:** Sentinel 2 | **Status:** SHIPPED
Four-tier visibility: public, project, agent, restricted. `visibility` column + `read_acl` JSON on memories. Migration 017_memory_rbac.sql deployed. Enforced at brainctl CLI query-time filtering. Knowledge graph traversal gap is Phase 2.

### 13 — Adaptive Retrieval Weights (COS-201)
**Author:** Recall | **Status:** PARTIAL
Sensitivity analysis showing fixed weights suboptimal at 10×. Confidence compression (36/39 at ≥0.90) makes weight noise. Three query-type profiles designed (temporal, factual, procedural). Adaptive computation from store statistics designed but not confirmed as deployed.

### 14 — Memory Store Health SLOs (COS-202)
**Author:** Prune | **Status:** SHIPPED
Five dimensions: Coverage (distillation ratio), Freshness (event-to-memory lag), Precision (engagement rate), Diversity (HHI), Temporal Balance. `brainctl health` command live. `health_snapshots` table exists. Current composite: 0.30 (RED/CRITICAL).

### 15 — Memory as a Policy Engine (COS-204)
**Author:** Claude Code / Hermes | **Status:** SHIPPED
Memory-driven distributed decisions. Safe delegation classes defined. Five failure modes cataloged. `policy_memories` table with FTS5 exists. `brainctl policy match/add/feedback` commands live. Wisdom half-life concept for staleness decay.

### 16 — Embedding-First Writes (COS-205)
**Author:** Kokoro / Recall | **Status:** SHIPPED
sqlite-vec confirmed operational. nomic-embed-text 768d via Ollama at 20-50ms. Synchronous inline embedding in write path. BM25+vector fusion with RRF. Embedding coverage now 95.5% (was 21.1%). `brainctl search` uses hybrid retrieval by default.

---

## WAVE 6: REPAIR & NEUROSCIENCE (15 documents)
*Operational repair + deep neuroscience track.*

### 17 — Retrieval Utility Analysis (COS-229)
**Author:** Recall | **Status:** SHIPPED
Root cause of 97.6% zero-recall: `brainctl search` never updated recalled_count. 67.8% of searches returned zero results. Fix deployed — recall engagement now at 81.8% (18/22 memories recalled).

### 18 — Temporal Classification Repair (COS-230)
**Author:** Engram | **Status:** SHIPPED
Diagnosed structural classification failure (96% medium, 0% ephemeral/short). Root causes: default medium at write time, decay pass not running. Fix: temporal decay pass added to hippocampus.py. Current distribution: 63.6% ephemeral, 4.5% medium, 4.5% long, 27.3% permanent — dramatically improved.

### 19 — Embedding Backfill (COS-231)
**Author:** Recall | **Status:** SHIPPED
Full backfill achieved: 21.1% → 100% (now 95.5% with new unembedded writes). Sync write path spec delivered. Hybrid BM25+vector retrieval validated as qualitatively superior.

### 20 — Memory Event Bus Implementation (COS-232)
**Author:** Weaver | **Status:** SHIPPED
SQLite trigger-based MEB live. `brainctl meb tail --since <watermark>`. Zero external dependencies. <500ms propagation. memory_events table with triggers on INSERT/UPDATE.

### 21 — Cross-Scope Contradiction Detection (COS-233)
**Author:** Sentinel 2 | **Status:** SHIPPED
Entity extraction → scope bridging → negation matching. Additive pass integrated into consolidation cycle. Emits contradiction_detected events. Cross-scope pairs flagged for review. Currently 0 unresolved contradictions.

### 22 — Trust Score Calibration (COS-234)
**Author:** Sentinel 2 | **Status:** SHIPPED
Trust event taxonomy with magnitude tables. Trust update algorithm as SQL rules. Trust decay for unvalidated memories. trust_update_pass() integrated into consolidation cycle. trust_score now computed from objective signals rather than uniform 1.0.

### 23 — Policy Memory Schema (COS-235)
**Author:** Cortex | **Status:** SHIPPED
MVP implementation of COS-204 spec. `policy_memories` table with SQL migration. `brainctl policy match/add/feedback` interface. Three seed policies deployed from existing organizational decisions.

### 24a — Neuroplasticity & Structural Self-Modification (COS-242)
**Author:** Engram | **Status:** SHIPPED
Hebbian learning: co-activated memories strengthen shared edges. LTP (weight increase on co-retrieval) and LTD (weight decrease on disuse). Dynamic edge weight updates now running in consolidation via Hebbian learning pass producing ~33 edges/cycle.

### 24b — Neuromodulation & Dynamic Learning Rates (COS-244)
**Author:** Epoch | **Status:** SHIPPED
Four computational neuromodulators: dopamine (reward/learning rate), norepinephrine (alertness/retrieval breadth), acetylcholine (focus/precision), serotonin (patience/exploration). `brainctl neurostate` command live. neuromodulation_state table with transitions. Runtime parameters adjust consolidation behavior.

### 24c — Creative Synthesis / Dreams (COS-247)
**Author:** Prune | **Status:** SHIPPED
Dream pass in consolidation: bisociation of high-similarity cross-scope pairs, incubation queue for failed searches, serendipity injection, generative replay. `09_creative_synthesis.py` live. Dream pass producing ~16 hypotheses per cycle. `brainctl dreams` command available.

### 24d — Theory of Mind & Agent Modeling (COS-246)
**Author:** Weaver | **Status:** SHIPPED
Six cognitive-science frameworks mapped to engineering primitives. `agent_beliefs`, `belief_conflicts`, `agent_perspective_models`, `agent_bdi_state` tables all exist. `brainctl tom`, `brainctl belief set/get`, `brainctl belief-conflicts` commands live.

### 24e — Continual Learning & Catastrophic Forgetting (COS-248)
**Author:** Sentinel 2 | **Status:** SHIPPED
EWC-inspired importance scoring. Two-speed memory acceptance. Experience replay during consolidation. Migration 018_ewc_importance.sql deployed. ewc_importance column with consolidation guard preventing degradation of high-value memories.

### 24f — World Models & Internal Simulation (COS-249)
**Author:** Cortex | **Status:** PARTIAL
World model layer: org structure, agent capabilities, project dependencies, causal dynamics. `world_model` and `world_model_snapshots` tables exist. `brainctl world` command available. Full simulation capability (if-then scenario testing) not yet implemented.

### 25 — Global Workspace Theory (COS-243)
**Author:** Cortex | **Status:** SHIPPED
Baars' GWT mapped to 178-agent system. Salience-based broadcast mechanism. `workspace_broadcasts`, `workspace_acks`, `workspace_config`, `workspace_phi` tables exist. `brainctl workspace` command live. salience_score + gw_broadcast columns on memories.

### 26 — Neuro-Symbolic Reasoning (COS-245)
**Author:** Recall | **Status:** SHIPPED
Three-tier reasoning: associative (FTS5+vec), structural (spreading activation), inferential (rule engine). `brainctl reason` and `brainctl infer` commands live. Combines FTS5 + vectors + knowledge_edges + rule-based inference for multi-step queries.

---

## WAVE 7: IMPLEMENTATION SPRINT (0 documents — execution wave)
*No new research documents. Implementation of Waves 1-6 designs.*

| Issue | What Was Done | Status |
|---|---|---|
| COS-299 | recalled_count tracking in search | SHIPPED (was already implemented) |
| COS-300 | Temporal classification decay pass | SHIPPED (in hippocampus.py) |
| COS-301 | Hebbian dynamic edge weights | SHIPPED (consolidation pass) |
| COS-302 | Trust score update rules | SHIPPED (trust_update_pass) |
| COS-303 | Dream pass (bisociation) | SHIPPED (09_creative_synthesis.py) |
| COS-304 | Neuromodulation layer | SHIPPED (brainctl neurostate) |
| COS-305 | Neuro-symbolic reasoning | SHIPPED (brainctl reason + reason-chain) |

---

## WAVE 8: INTEGRATION (0 documents — execution wave)
*Higher-order capabilities built from Wave 6 research.*

| Issue | What Was Done | Status |
|---|---|---|
| COS-314 | Global Workspace broadcast | SHIPPED (brainctl gw, salience columns) |
| COS-315 | Memory RBAC visibility | SHIPPED (migration 017, visibility column) |
| COS-316 | EWC importance scoring | SHIPPED (migration 018, ewc_importance) |
| COS-317 | Cognitive health dashboard | SHIPPED (brainctl health, health_snapshots) |
| COS-318 | Agent belief model | SHIPPED (brainctl belief, agent_beliefs table) |
| COS-319 | Bug fix: compression source_event_id | SHIPPED |

---

## WAVE 9: OVERHEAD & GAPS (1 document)
*Measuring the cost of what we built.*

### 27 — Cognitive Protocol Overhead Audit (COS-322)
**Author:** Recall | **Status:** SHIPPED
Full orientation protocol costs ~42K tokens/heartbeat and ~830ms. Two critical bugs: `brainctl gw listen` doesn't exist, default search dumps 23K tokens via unguarded graph expansion. Fast tier (3 commands): ~2K tokens, ~320ms, 80%+ actionable signal. At 178 agents × 15 heartbeats/day, fast tier saves ~107M tokens/day. Recommended: tiered protocol with fast default.

---

## WHAT'S FULLY IMPLEMENTED AND WORKING

**Core Infrastructure:**
- brain.db with 80+ tables (including FTS5 indexes, vec tables, config tables)
- sqlite-vec installed and operational (nomic-embed-text 768d via Ollama, 20-50ms)
- FTS5 indexing on memories, events, context, policy_memories, reflexion_lessons
- Embedding coverage at 95.5% (21/22 active memories embedded)
- WAL mode for concurrent read/write access

**Consolidation Cycle (hippocampus.py — 11 passes):**
- Spaced repetition decay pass (confidence decay by temporal class)
- Semantic forgetting (temporal class promotion/demotion)
- Contradiction detection (within-scope + cross-scope)
- Dream pass (bisociation, ~16 hypotheses/cycle)
- Hebbian learning (co-activation edge updates, ~33 edges/cycle)
- Trust score update pass
- Neuromodulation state updates
- Health snapshot capture
- Temporal classification repair (distribution now healthy: 63.6% ephemeral)

**Knowledge Infrastructure:**
- Knowledge graph: 5,359 edges with 5 relation types
- Agent expertise directory: 1,131 rows, `brainctl expertise` + `brainctl whosknows`
- Memory Event Bus: triggers on write, `brainctl meb tail`
- Agent beliefs: `brainctl belief set/get`, `brainctl belief-conflicts`
- Policy engine: `policy_memories` table, `brainctl policy match/add/feedback`
- Reflexion lessons: `reflexion_lessons` table, `brainctl reflexion`
- Global Workspace: broadcast tables, `brainctl workspace`
- World model: `world_model` table, `brainctl world`

**Retrieval:**
- Hybrid BM25+vector search with RRF fusion (default in brainctl search)
- Neuro-symbolic reasoning: `brainctl reason` + `brainctl infer`
- Proactive push: `brainctl push` at checkout
- Recall tracking: recalled_count properly incremented (81.8% engagement)

**Monitoring:**
- `brainctl health` — five-dimension SLO dashboard with alerts
- health_snapshots table for longitudinal tracking
- `brainctl neurostate` — org-level cognitive state

**Access Control:**
- RBAC visibility column (public/project/agent/restricted) with read_acl
- Enforced at brainctl CLI layer

---

## WHAT'S PARTIALLY IMPLEMENTED

**Distillation Pipeline (ROOT BLOCKER):**
- Event-to-memory distillation ratio is 0.012 (target ≥0.10) — only 1.2% of events become memories
- `brainctl distill` command exists but pipeline doesn't auto-link source events reliably
- COS-319 bug fix improved source_event_id linking but ratio still critically low

**Adaptive Retrieval Weights (COS-201):**
- Fixed weights (0.45/0.25/0.20/0.10) deployed
- Query-type profiles designed but not dynamically selected
- Store-statistics-based adaptive computation not wired

**Episodic/Semantic Bifurcation (COS-120):**
- memory_type concept established
- Separate decay rates not confirmed as differentiated in production
- Episodic→semantic LLM promotion not running

**Write Contention / CAS (COS-122):**
- Analysis complete, WAL mode provides basic protection
- Version column + compare-and-swap pattern designed but unclear if deployed

**Memory Granularity (COS-178):**
- Problem diagnosed (memories too fine, context too coarse)
- Auto-chunking rules for context not enforced at write time
- No aggregation of micro-memories running

**Situation Models (COS-123):**
- Tables exist (situation_models, situation_model_contradictions)
- Prototype builder exists
- Not confirmed as wired to brainctl subcommand

**World Model Simulation (COS-249):**
- Tables and brainctl command exist
- Full if-then simulation capability not built

**Confidence Score:**
- Average confidence is 0.552 (target ≥0.80)
- Decay and boost mechanics work but starting values and calibration need tuning

---

## WHAT'S RESEARCHED BUT NOT BUILT

**Distributed brain.db / Federation (COS-181):**
- Team-sharded SQLite fully designed (5-7 shards + global index)
- ~500 LOC migration spec complete
- Zero implementation — not needed until agent count creates burst contention issues

**Full Causal Event Graph (COS-184 / COS-114):**
- Three-tier causal edge system designed
- Agent-reported causation spec ready
- Automatic causal edge generation not running as consolidation pass
- Event calculus SQL views not created
- Counterfactual reasoning deferred

**Cross-Agent Reflexion Propagation (COS-320):**
- generalizable_to field designed
- Automatic propagation of lessons across matching agent types not wired

**Predictive Routing Engine (COS-112):**
- Full architecture designed (collaborative filtering, free energy model, sequential patterns)
- Three prediction horizons specified
- Only checkout-time push implemented; proactive model not trained

**ColBERT Token-Level Similarity:**
- Evaluated and deferred as architecturally incompatible with SQLite-first design

**Full Pearl Causal Framework:**
- Requires intervention data not collected; deferred to future wave

**Cognitive Experiments Tracking:**
- `cognitive_experiments` table exists
- No experiment framework running to measure improvement impact

---

## REMAINING HEALTH ISSUES

**From `brainctl health` (current output):**

| Metric | Value | Target | Status |
|---|---|---|---|
| Composite health score | 0.30 | ≥0.60 | RED |
| Distillation ratio | 0.012 | ≥0.10 | RED |
| High-importance coverage | 0.192 | ≥0.50 | RED |
| Median event→memory lag | 246 min | ≤60 min | RED |
| Average confidence | 0.552 | ≥0.80 | RED |
| Permanent memory share | 27.3% | ≤20% | RED (over-promotion) |
| 30-day recall engagement | 81.8% | ≥0.30 | GREEN |
| Category HHI | 0.302 | ≤0.35 | GREEN |
| Scope HHI | 0.178 | ≤0.40 | GREEN |
| Vector embedding coverage | 95.5% | ≥0.90 | GREEN |
| Unresolved contradictions | 0 | 0 | GREEN |

**Critical Issues (RED):**
1. **Distillation ratio 0.012** — The #1 blocker. Only 1.2% of events produce linked memories. The pipeline exists but isn't converting events to memories at scale. Every downstream improvement is multiplicatively gated on this.
2. **Event→memory lag 246min** — Memories that do get created take 4+ hours. Target is 60 minutes. The consolidation cycle runs but not frequently enough for operational relevance.
3. **Confidence 0.552** — Memories start at 1.0 but decay too aggressively relative to recall frequency. Calibration of initial confidence and decay rates needed.
4. **Over-promotion to permanent (27.3%)** — Temporal classification repair overcorrected. Too many memories promoted to permanent, resisting healthy decay.
5. **High-importance coverage 0.192** — Important events (importance ≥0.8) are not being distilled into memories at adequate rates.

---

## RECOMMENDED NEXT RESEARCH

### 1. Attention Economics & Cognitive Load Theory
**Gap:** We measure what's in memory but not what agents actually use or are overwhelmed by. Sweller's Cognitive Load Theory, Kahneman's attention as a scarce resource, and Wickens' multiple resource theory could inform optimal context injection volume and format.
**Impact:** The overhead audit (COS-322) found 42K tokens/heartbeat — attention economics would give us a principled framework for what to include vs. exclude.

### 2. Memory Interference & Retrieval-Induced Forgetting
**Gap:** Anderson et al. (1994) showed that retrieving some memories actively inhibits retrieval of related competitors. Our system boosts recalled memories but never considers whether this suppresses equally valid alternatives.
**Impact:** Could explain and fix precision issues. Retrieval-induced forgetting is a known cognitive bias we're likely replicating.

### 3. Distributed Cognition & Extended Mind Thesis
**Gap:** Clark & Chalmers (1998) extended mind thesis — cognition extends beyond the brain into tools and environment. Our agents treat brain.db as passive storage, not as an active cognitive partner. Research into how the memory system itself participates in reasoning (not just stores results) is unexplored.
**Impact:** Could transform the architecture from "store + retrieve" to "think with memory."

### 4. Predictive Processing & Active Inference (Implementation)
**Gap:** Friston's framework was researched (COS-112) but the implementation gap is huge. Active inference — agents minimizing surprise by both acting on the world and updating their model — could drive autonomous memory maintenance.
**Impact:** Self-healing memory: agents that detect and fix their own knowledge gaps without human intervention.

### 5. Social Epistemology & Testimony
**Gap:** How should agents weight knowledge from other agents vs. their own experience? Goldman's social epistemology and the philosophy of testimony address exactly this. Our trust scoring is mechanistic; we haven't researched the epistemological foundations.
**Impact:** Better trust calibration, especially for cross-agent knowledge transfer and belief reconciliation.

### 6. Embodied Cognition & Grounding
**Gap:** Barsalou's perceptual symbol systems and Lakoff/Johnson's embodied metaphor research. Our agents have no grounding — all knowledge is symbolic/textual. Research into whether agents that interact with code, APIs, and infrastructure develop different (better?) memory patterns than purely textual agents.
**Impact:** Could inform how different agent types should store and retrieve differently.

### 7. Bayesian Brain & Probabilistic Reasoning
**Gap:** Knill & Pouget's Bayesian brain hypothesis. Our confidence scores are point estimates, not probability distributions. Research into maintaining uncertainty distributions over memories rather than single confidence values.
**Impact:** Better uncertainty quantification, enabling "I'm 70% sure about X but the distribution is bimodal" rather than "confidence = 0.70."

---

## RECOMMENDED NEXT IMPLEMENTATION
*Top 5 highest-impact items, ordered by dependency.*

### 1. Fix Distillation Pipeline (CRITICAL — unblocks everything)
**What:** Wire reliable event→memory distillation that links source_event_ids and achieves ≥10% ratio.
**Why:** At 0.012, the memory store is starving. Every other improvement — retrieval, reasoning, push, policy — is gated on having memories to work with. This has been the #1 blocker since Wave 3.
**Effort:** 3-5 days | **Impact:** 10× improvement in store utility
**Dependencies:** None — this IS the root dependency.

### 2. Calibrate Confidence & Temporal Promotion
**What:** Fix over-promotion to permanent (27.3% → ≤20%), tune initial confidence values, adjust decay rates so average confidence reaches ≥0.80.
**Why:** Confidence 0.552 means the system doesn't trust its own memories. Over-promotion means memories resist healthy decay. Both corrupt retrieval quality.
**Effort:** 2-3 days | **Impact:** Retrieval precision improvement, healthier lifecycle
**Dependencies:** None (can parallel with #1).

### 3. Increase Consolidation Frequency
**What:** Move from nightly batch to 2-4 hour consolidation cycles. Event→memory lag at 246min needs to drop to ≤60min.
**Why:** Memories that arrive 4 hours after the event are operationally useless for the agents working on that event right now.
**Effort:** 1-2 days | **Impact:** 4× freshness improvement
**Dependencies:** #1 (distillation must work before running it more often matters).

### 4. Wire Adaptive Retrieval Weights
**What:** Implement query-type detection and dynamic weight selection from the COS-201 research.
**Why:** Fixed weights are suboptimal now that the store has real variance in confidence, recency, and importance. Three profiles (temporal, factual, procedural) are designed and ready.
**Effort:** 2-3 days | **Impact:** 15-25% precision improvement
**Dependencies:** #1 (need sufficient memories for weight differentiation to matter).

### 5. Deploy Cognitive Protocol Fast Tier
**What:** Implement the COS-322 recommendation: default agents to the 3-command fast tier (~2K tokens, ~320ms) instead of the broken full protocol (~42K tokens).
**Why:** At 178 agents × 15 heartbeats/day, this saves ~107M tokens/day. The current full protocol has two bugs and wastes 95% of its token budget on unactionable output.
**Effort:** 1 day | **Impact:** 95% reduction in orientation overhead
**Dependencies:** None — can ship immediately.

---

*This digest covers all 47 research documents across 9 waves, plus meta-documents (FRONTIER.md, COMPENDIUM.md, EXECUTIVE_BRIEFING.md, README.md). It reflects the current state of brain.db as of 2026-03-28 with health data from `brainctl health`. The system has made extraordinary research progress — the gap is now implementation velocity, not knowledge.*
