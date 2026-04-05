# HERMES COGNITIVE RESEARCH COMPENDIUM
## 5 Waves | 39 Deliverables | Full Synthesis

**Compiled by:** Hermes (CKO, CostClock AI)
**Date:** 2026-03-28
**Scope:** All research across ~/agentmemory/research/ and wave subdirectories
**Target System:** brain.db (SQLite + FTS5 + sqlite-vec), serving 178 agents via brainctl

---

## WAVE 1: FOUNDATIONS
*8 Algorithmic Modules — All Delivered as Research Prototypes*

### 01 — Spaced Repetition (01_spaced_repetition.py)
Implements the Ebbinghaus forgetting curve for agent memory confidence decay and recall-based boosting. Memories decay exponentially based on temporal class: ephemeral (λ=0.5, half-life ~1.4 days), short (λ=0.2, ~3.5 days), medium (λ=0.05, ~14 days), long (λ=0.01, ~69 days), permanent (no decay). On each retrieval, confidence receives an asymptotic boost of 15% of remaining headroom (α=0.15). Memories falling below a 0.15 confidence threshold are candidates for retirement. The module provides both Python functions (`run_decay_pass`, `record_recall`) and pure SQL equivalents for direct brainctl use. Built with dry-run support for safe testing.

### 02 — Semantic Forgetting (02_semantic_forgetting.py)
Implements temporal class promotion and demotion based on access patterns. Memories that nobody recalls slide down: long→medium→short→ephemeral before retirement. Promotion requires meeting a minimum recall count threshold (3 for ephemeral→short, up to 15 for long→permanent) with confidence ≥0.85 and a recall within the last 7 days. Demotion triggers when confidence drops below a class floor (e.g., 0.45 for long, 0.35 for medium) and no recall has occurred within the demotion window (60 days for long, 21 for medium, 7 for short). A pure SQL version of the demotion pass enables direct database execution.

### 03 — Knowledge Graph (03_knowledge_graph.py)
Builds relational structure over the flat memory tables via the `knowledge_edges` table. Provides algorithms for: edge management (upsert with ON CONFLICT), BFS context expansion from seed memories (max 2 hops, 50 nodes), PageRank-style importance scoring over memory nodes (damping=0.85, 20 iterations), and automatic co-reference edge building from session access patterns. Five relation types are supported with distinct weights: supports (0.8), contradicts (0.9), derived_from (0.7), co_referenced (0.5), supersedes (1.0). The knowledge graph grew to 2,675 edges in production (COS-84).

### 03b — AI Memory Systems Survey (03_ai_memory_systems.md)
Comprehensive survey of 8 state-of-the-art memory and cognition systems: MemGPT/Letta (virtual context management with self-directed memory ops), Advanced RAG (hybrid BM25+vector with RRF fusion, showing 10-20% improvement over pure vector), Lost-in-the-Middle (context window utilization bias — never put critical info in the middle), Reflexion (20-40% reasoning boost from stored failure lessons), SOAR/ACT-R cognitive architectures (declarative/procedural memory split, activation scoring formula), embedding strategies (nomic-embed-text recommended for local, text-embedding-3-small for cloud), memory-augmented transformers (RETRO, MEMIT), and multi-agent shared memory patterns (CrewAI, AutoGen, LangGraph). Key recommendation: hybrid BM25+vector with RRF is the highest-impact, lowest-effort improvement.

### 04 — Neuroscience of Memory (04_neuroscience_memory.md)
Maps six neuroscience principles to brain.db architecture: hippocampal consolidation (temporal_class promotion pipeline), reconsolidation (confidence evolves on every retrieval — memories are not static recordings), synaptic pruning (forgetting is cognitively adaptive, not pathological), sleep consolidation (offline batch processing extracts patterns from episodes), emotional tagging/salience (task outcome importance should weight encoding), and engram theory (embeddings are artificial engrams enabling content-addressable, pattern-completion recall). The central biological insight: a nightly "sleep cycle" that replays, prunes, deduplicates, and extracts patterns activates the most neuroscience principles simultaneously.

### 04b — Attention/Salience Routing (04_attention_salience_routing.py)
Computes a weighted salience score for memory retrieval: 0.45×similarity + 0.25×recency + 0.20×confidence + 0.10×importance. Supports three routing modes: FOCUSED (top-K for single agent), BROADCAST (route subsets to different agents by scope), and HIERARCHICAL (escalate memories above 0.85 salience to managers). Uses FTS5 BM25 for similarity when embeddings aren't available, with sqlite-vec cosine similarity as the precision path. Recency decays exponentially (k=0.1, half-life ~7 days). Importance is log-normalized recalled_count.

### 05 — Consolidation Cycle (05_consolidation_cycle.py)
The "sleep cycle" orchestrator inspired by neuroscience sleep consolidation. Pipeline: (1) collect ephemeral/short memories older than 1 day, (2) cluster by category+scope, (3) consolidate clusters of 3+ into merged memories (naive join or LLM summarizer), (4) retire source memories with derived_from edges, (5) run decay pass (spaced repetition), (6) run demotion pass (semantic forgetting), (7) detect near-duplicates via FTS substring matching, (8) run contradiction detection, (9) log cycle report to events table. The orchestrator dynamically imports modules 01, 02, and 06. LLM summarization is a TODO placeholder for production.

### 06 — Contradiction Detection (06_contradiction_detection.py)
Identifies conflicting memory pairs via two strategies: (1) supersession chain breaks — memory B supersedes A but both remain active, and (2) FTS negation pattern matching — pairs in the same scope/category/agent are checked against 8 negation patterns (is/is not, can/cannot, enabled/disabled, true/false, etc.). Detected contradictions are logged to the events table with event_type='contradiction_detected' and bidirectional 'contradicts' edges are added to knowledge_edges. Auto-resolution retires the lower-confidence memory when confidence_delta > 0.3; ambiguous cases are flagged for human review.

### 07 — Emergence Detection (07_emergence_detection.py)
Surfaces patterns visible only in aggregate across the memory corpus. Five detection algorithms: (1) topic frequency trending using FTS5 term frequencies comparing recent vs. prior windows with "lift" scoring, (2) agent behavioral drift via KL-like divergence on category distributions flagging agents with divergence > 0.3, (3) confidence distribution health assessment with signal-to-noise ratio and at-risk percentages by temporal class, (4) recall cluster analysis identifying the most-retrieved memories as candidates for permanent promotion, and (5) recurring error chain detection via causal_chain_root grouping. Designed to feed Hermes' daily briefing.

### 08 — Context Compression (08_context_compression.py)
Maximizes information density when injecting memories into agent context windows. Full pipeline: (1) temporal compression — recent memories as full text, older as one-line summaries, (2) redundancy pruning via Jaccard token-set overlap (threshold 0.55), (3) optional cluster summarization for sets >20 memories, (4) token-budget selection — greedy highest-salience fit within budget (default 2000 tokens, max 200 per memory). Renders output as a markdown context block with salience scores and compression indicators. Approximate tokenization: 1 token ≈ 4 characters.

---

## WAVE 2: DEEP COGNITION
*9 Deliverables — Theoretical Frameworks with Implementation Designs*

### 09 — Associative Memory & Analogical Reasoning (COS-111)
Investigates six frameworks for making memory "creative": spreading activation (Collins & Loftus 1975), structure-mapping analogy (Gentner 1983), concept blending (Fauconnier & Turner 2002), episodic-semantic cross-pollination (Tulving 1972), Holographic Reduced Representations (Plate 1995), and Sparse Distributed Memory (Kanerva 1988). Central finding: spreading activation over the existing 1,933 knowledge_edges, combined with lightweight structural analogy fingerprinting, delivers the highest-impact associative capability at lowest cost. A `brainctl recall` unified command is proposed that returns direct matches, activated associations, structural analogies, and epoch context in one query. HRR (requiring custom embeddings) and full Gentner-level structural mapping (requiring predicate parsing) are deferred. SDM is superseded by existing 768d vector search. Vector-blend search (interpolating embeddings between two memories) provides weak concept blending.

### 10a — Predictive Cognition (COS-112)
Designs a predictive routing engine that shifts from reactive retrieval to anticipatory push. Synthesizes five research areas: Friston's free energy principle (agents have implicit generative models; searches signal prediction error), collaborative filtering (agent×memory interaction matrix for non-obvious connections), anticipatory computing (post-checkout window is highest-value prediction moment), proactive information retrieval (surrogate query construction from task + role + project context), and temporal pattern mining (PrefixSpan-style sequential pattern rules). Architecture: surrogate query → content similarity stage → collaborative re-rank → sequential rule injection → diversity-constrained top-K selector → push buffer. Latency target: <100ms. Three prediction horizons: immediate (this heartbeat), session (next 3-5 tool calls), background (pre-loaded organizational context).

### 10b — Temporal Reasoning & Causal Inference (COS-114)
Proposes a temporal reasoning layer enabling "why did this happen?" queries. Six frameworks analyzed: Event Calculus (Kowalski & Sergot 1986 — fluents initiated/terminated by events; HoldsAt query implementable as SQL views), Pearl's causal framework (SCM, do-calculus — deferred; need intervention data), bitemporal modeling (valid_from/valid_until columns on memories for "what did we believe at time T?"), Granger causality (deferred — needs dense regular time series), temporal abstraction/episode segmentation (gap-based + context-switch detection for chunking event streams into meaningful work episodes), and counterfactual reasoning (decision-point logging for "what if we'd chosen differently?"). Proposes `temporal_fluents`, `temporal_rules`, and `temporal_state` tables plus `brainctl temporal` subcommands.

### 10c — Cognitive Compression & Abstraction (COS-116)
Answers "how do we keep brain.db useful as it grows by 100×?" Core finding: compression is an epistemology problem, not a storage problem — the most effective strategy is abstraction (replacing many specific records with fewer general ones). Proposes a three-tier hierarchical memory: Raw events (7-30d retention) → Episodes (90-365d, ~10× compression) → Schemas/abstractions (indefinite, ~100-1000× compression). Uses progressive summarization (Forte's 4-pass framework mapped to temporal class demotions), prototype extraction (centroid embeddings from semantic clusters), automatic schema induction from repeated event sequences, power-law forgetting (Anderson & Schooler — more neurologically accurate than exponential decay), and multi-resolution vector search using Matryoshka embeddings (64d fast pass → 768d rerank). Estimated impact: 90-95% reduction in active footprint at 1M records while maintaining retrieval quality.

### 11a — Causal Event Graph (COS-184)
Designs automatic causal chain construction over the events table. Three-tier causal edge system: (1) auto-detected from temporal proximity + shared context heuristics (confidence 0.3-0.7), (2) type-based causal templates with known cause-effect patterns (deploy→error, approval→deploy, etc., confidence 0.5-0.8), (3) explicit reference chains from the refs JSON field (highest confidence 0.85-1.0). Agent-reported causation via `brainctl event link`. Recursive CTEs provide both forward chains ("what did X cause?") and backward traces ("why did X happen?"). Edge confidence decays weekly (0.95 per week, 0.975 for agent-reported). DAG integrity enforced via cycle detection before edge insertion. Reliability: ~60-70% for high-confidence automatic detection; remaining 30-40% requires agent self-reporting.

### 11b — Metacognition & Self-Modeling (COS-110)
Proposes a metacognition layer so Hermes can know what it knows, what it doesn't, and what it's blind to. Implements the Nelson & Narens (1990) monitoring/control framework with four metacognitive judgments: Ease of Learning, Judgment of Learning, Feeling of Knowing, and Confidence Judgment. Central finding: the most valuable capability is gap detection — actively flagging when no memories cover an agent's current task scope. Schema additions: `knowledge_coverage` table (scope-level coverage density with freshness weighting), `knowledge_gaps` table (explicit blind spots: coverage_hole, staleness_hole, confidence_hole, contradiction_hole), and `metacognitive_judgments` audit trail. Dempster-Shafer theory proposed for distinguishing "no evidence for X" from "evidence against X" — the Open-World Assumption over the Closed-World Assumption. Post-retrieval tier annotation (1=high confidence, 2=partial/TOT, 3=weak associative, 4=coverage gap) should replace silent empty results.

### 12a — Collective Intelligence Emergence (COS-113)
Investigates how 178 agents sharing brain.db can be smarter than the sum of parts. Six frameworks: swarm intelligence/ACO (stigmergic memory with collective recall boost for memories retrieved by 3+ distinct agents), wisdom of crowds (diversity + independence + decentralization are present but aggregation is entirely missing), transactive memory systems (Wegner 1987 — need an `agent_expertise` directory mapping who knows what), network topology (small-world structure needed — current star topology creates hub dependency), computational social choice (weighted belief merging, preserving minority insights), and evolutionary epistemology (memetic fitness function: recall_count×0.4 + citation_count×0.3 + confidence×0.2 + coherence_contribution×0.1). Highest-impact recommendation: build an `agent_expertise` table enabling capability-aware query routing — transactive memory completion.

### 12b — Advanced Retrieval & Reasoning (COS-117)
Evaluates seven advanced retrieval paradigms against brain.db. Current baseline: P@5=0.22, R@5=0.925 (high recall, low precision). Core finding: the highest-leverage improvement is not a better algorithm but iterative retrieval with reasoning (IRCoT) over the existing FTS5+vec layer, combined with graph-augmented re-ranking using the 2,675 knowledge_edges. Secondary finding: P@5=0.22 is partly a content problem (9 active memories at time of benchmark) — distillation is prerequisite to precision gains. Recommended sequence: retired vec fix (done) → graph-augmented reranking (1-2 days, no schema change, highest ROI) → IRCoT iterative retrieval → cross-modal late fusion → query decomposition. ColBERT (token-level similarity) deferred as architecturally incompatible with SQLite-first design.

### 12c — Adversarial Robustness & Memory Integrity (COS-115)
Analyzes six threat vectors against brain.db. Embedding poisoning is the highest-risk attack: adversarial insertions can hijack vector search without triggering keyword-based checks. Proposes layered defenses: content-addressable hashing (SHA-256 over canonical fields for tamper detection), embedding anomaly detection (re-embed and compare cosine similarity), per-agent hash chains, Merkle tree for bulk verification, hallucination detection via cross-reference verification, query injection prevention (length limits, term limits, Unicode normalization). Reputation-weighted validation replaces full BFT: agents with trust ≥0.9 auto-accepted, <0.5 require quorum validation. Three-tier self-healing escalation: auto-repair (duplicates/TTL), flag-for-review (trust anomalies), quarantine (integrity violations). Source-of-truth hierarchy: human > Hermes > higher-confidence > more-recent > project-scoped > validated.

---

## WAVE 3: ARCHITECTURE PATTERNS
*6 Deliverables — All Complete, Implementation-Ready Designs*

### Wave 3 Synthesis (00_wave3_synthesis.md)
Cross-report brief identifying that the five Wave 3 proposals form a coherent system with shared infrastructure prerequisites. Critical dependency chain: COS-127 fix (retired vec cleanup) → COS-122 schema (version column + CAS) → COS-120 + COS-121 (memory_type + provenance, parallel) → distillation job → COS-123 + COS-124 (parallel). Root finding: all five improvements are architectural investments that underperform until event-to-memory distillation is working — at only 14 memories from ~123 events, the store is too sparse for higher-order features. The knowledge graph (2,675 edges from COS-84) directly improves proactive push scoring, situation model construction, and trust propagation but was not referenced in any Wave 3 report.

### 01 — Episodic vs. Semantic Bifurcation (COS-120)
Adds `memory_type` column ('episodic'|'semantic') to the memories table to differentiate decay, consolidation, and retrieval paths. Episodic memories (time-stamped events) follow exponential confidence decay; semantic memories (stable facts) use staleness detection instead — they don't become less true with age, only potentially outdated when superseded. Separate decay rates per type and class: episodic short λ=0.10 (tightened), semantic uses no decay but emits stale_context events when age exceeds threshold (21d for short, 60d for medium, 180d for long). Episodic→semantic promotion synthesizes stable facts from clusters of 3+ related episodic memories via LLM. Default classification is 'episodic' for backward compatibility. Estimated impact: 40-60% reduction in stale episodic records within 30 days.

### 02 — Memory Provenance & Source Trust Chains (COS-121)
Adds provenance tracking to brain.db with four new columns on memories (validation_agent_id, trust_score, derived_from_ids, retracted_at/retraction_reason) and a new `memory_trust_scores` table for per-agent per-category rolling trust scores. Trust formula: base_prior(agent,category) × validation_boost × age_survival_factor × retraction_penalty. Retraction is distinct from retirement — retired means lifecycle end, retracted means "this was wrong." Retraction cascade propagates through derived_from_ids and knowledge_edges with three modes: flag-only (trust dropped), flag-for-review (retracted), cascade-retract (provably wrong). Maximum traversal depth of 10 hops with 0.7 decay per hop. `brainctl memory retract`, `validate`, and `trust-report` commands specified.

### 03 — Write Contention & Consistency (COS-122)
Empirical analysis of live brain.db with 22 agents finding confirmed multi-agent write collisions (same-second writes from 2 agents), 7 in-place mutations bypassing supersede chains, and mixed timestamp formats causing sort bugs. Taxonomy of 5 consistency failure types: stale-read-stale-act (HIGH risk), phantom supersede, lost update (no version column), WAL phantom (benign), clock skew ordering. Recommends optimistic locking via `version INTEGER` column with CAS (compare-and-swap) UPDATE pattern: `WHERE id=? AND version=?`. At 178 agents, projected 230 event writes/hr and 160 memory writes/hr. CAS adds <0.1ms overhead (O(1) integer lookup). The `project` category is the primary contention zone at 59% of memories with multi-agent overlap. Timestamp normalization migration also specified.

### 04 — Situation Model Construction (COS-123)
Enables Hermes to answer "what is happening with X?" rather than just "what is X?" by building coherent narrative representations. Grounded in Kintsch's Construction-Integration model and Johnson-Laird's mental models. Four-phase construction pipeline: anchor resolution (0-5ms), multi-strategy memory retrieval (5-50ms: direct tag + semantic + graph + temporal), integration (50-200ms: temporal ordering, contradiction detection, causal chain construction, role assignment, phase detection, gap analysis), and caching (10ms). Coherence scoring across 5 dimensions: temporal consistency (0.25), factual consistency (0.30), completeness (0.20), agent role coverage (0.15), causal density (0.10). Three presentation formats: narrative prose (default), structured JSON, graph fragment. New `situation_models` and `situation_model_contradictions` tables with 6-hour TTL and incremental update support.

### 05 — Proactive Memory Push (COS-124)
Shifts from pull-based to push-based memory delivery. Based on predictive coding theory (Rao & Ballard 1999): the system should predict incoming needs and only require explicit search for the prediction error (residual). Issue checkout is the optimal trigger — agent has committed to work, latency budget is generous (~70ms total push query), task description provides highest-quality semantic signal. Three-layer scoring pipeline: FTS5 keyword gate (top-50) → vector similarity gate (cosine >0.72, top-20) → graph activation bonus (+0.1 per activated neighbor). Hard cap: never push more than 5 memories per checkout. Anti-noise safeguards: topic coherence check, repetition guard. Utility tracking via `push_log` table correlating push IDs to recalled_count deltas. Estimated impact: 30-50% reduction in explicit brainctl search calls for well-scoped tasks.

---

## WAVE 4: ADVANCED CAPABILITIES
*4 Deliverables Filed (of 8 planned)*

### 01a — Agent-to-Agent Knowledge Transfer Protocol (COS-177)
Proposes a Memory Event Bus (MEB): a lightweight SQLite trigger → shared event table → agent polling architecture for real-time knowledge propagation. When Agent A writes to brain.db, a trigger automatically inserts into `memory_events`. Agents poll at heartbeat start via `brainctl events poll` and receive events since their last cursor position. Three subscription models: broadcast (recommended initially), topic-filtered, importance-threshold. Three invalidation strategies: content (specific memory updated), topic (cluster changed), dependency (graph-aware). End-to-end latency: 3ms write overhead + 0-120s poll interval (closing the current 30-minute gap by 15×). No external dependencies — pure SQLite. Protocol spec: at-least-once delivery, strict ordering via AUTOINCREMENT, 24h TTL + rolling 10k window.

### 01b — Memory Granularity Calibration (COS-178)
Empirical analysis revealing three distinct granularity failures: memories are too fine-grained (p50=33 tokens — single-sentence facts without semantic context), context records are catastrophically too coarse (894K avg tokens — entire conversation sessions as monolithic blobs with chunk_index always 0), and events lack importance differentiation (82% at default 0.5). Target granularity: memories 80-250 tokens, context 200-400 tokens with 15% overlap and semantic boundaries. The 150-400 token sweet spot produces coherent semantic units with meaningful embeddings. Auto-chunking rules at write time plus nightly aggregation of micro-memories with cosine sim >0.80. Event importance calibration table by type (error=0.9, contradiction=0.95, heartbeat=0.2). Expected precision improvement: context P@10 from 0.3 → 0.75.

### 03 — Cross-Agent Belief Reconciliation (COS-179)
Addresses the harder problem beyond explicit contradiction detection: implicit world-model divergence where agents operate from incompatible assumptions without either having written them down. Five divergence types: explicit conflicts (handled by Wave 1), cross-scope conflicts (partially handled — needs extension), temporal conflicts (not handled — need temporal ordering pass), implicit behavioral conflicts (not handled — requires event pattern mining), and staleness conflicts (partially via decay). Proposes cross-scope entity extraction and comparison as a new consolidation pass. Resolution strategies: auto-resolve (clear temporal/confidence winner), flag-for-synthesis (similar confidence), escalate-to-human (trusted agent contradicted). Schema additions: `belief_conflicts`, `entity_belief_index`, `worldmodel_reports` tables. Implicit belief detection via behavioral pattern mining over event streams.

### 10 — Memory-to-Goal Feedback Loop (COS-180)
Demonstrates that memory can drive proactive goal formation, not just reactive retrieval. Five-stage SQL-first pipeline: signal extraction (topic surges, error clusters, confidence decay, agent drift, recall dead zones) → Jaccard token-overlap clustering → rule-based proposal generation → composite ranking (35% strength + 25% coverage + 25% urgency + 15% novelty) → deduplication against existing tasks. No LLM in the critical path — fully deterministic and auditable. Thresholds: topic surge lift ≥3.0x with ≥5 mentions, error cluster ≥3 events sharing causal root, confidence decay ≥3 memories below 0.3, drift divergence >0.4, recall dead zone ≥10 memories with 0 recalls. Proposals are suggestions requiring explicit CEO/manager approval, preserving governance.

---

## WAVE 5: PRODUCTION HARDENING
*6 Deliverables — Operational Excellence Focus*

### 11 — Reflexion Failure Taxonomy (COS-199)
Defines the optimal failure classification and lesson lifecycle for 178 agents. Five canonical failure classes: REASONING_ERROR (incorrect inference), CONTEXT_LOSS (stale/missing context — 2nd most common), HALLUCINATION (fabricated facts), COORDINATION_FAILURE (checkout/auth/lock conflicts — dominant class in org data: API key identity mismatch across 6+ agents), and TOOL_MISUSE (wrong flags/tools). Event-driven expiration over time-based TTL: N consecutive successes (3 for coordination, 10 for reasoning), code fix to root cause, supersession by higher-confidence lesson. Three injection levels: HARD_OVERRIDE (confidence ≥0.85, protocol-class), SOFT_HINT (confidence 0.70-0.84), SILENT_LOG (<0.50). Cross-agent generalization via `generalizable_to` JSON field (agent_type, capability, project, scope). Dedicated `reflexion_lessons` table with FTS5 index, separate from generic memories.

### 12 — Memory Access Control & RBAC (COS-200)
Proposes four-tier visibility model for brain.db: public (org commons), project (same project only), agent (private to writing agent), restricted (explicit read_acl allowlist). Enforced at brainctl CLI layer via query-time filtering — not SQL views or OS-level ACL. Schema: `visibility TEXT` column with CHECK constraint + `read_acl TEXT` JSON array on memories. Current risk assessment: LOW-MEDIUM — most memories are operational heartbeat logs, no secrets/PII found, risk is context leakage not credential exposure. FTS5 and vec search use post-join filtering. Knowledge graph traversal is a Phase 2 hardening gap — edges could reveal existence of restricted memories. All 36 existing memories default to public. Write ACL deferred (YAGNI at current scale).

### 13 — Adaptive Retrieval Weights (COS-201)
Weight sensitivity analysis at scale showing current fixed weights (0.45 sim + 0.25 recency + 0.20 confidence + 0.10 importance) are appropriate for the current sparse store but suboptimal at 10×. Current pathologies: confidence compression (36/39 memories ≥0.90 — weight is noise), importance near-zero (97% with 0 recalls — weight is dead), recency bias too mild (all memories <8h old). Proposes adaptive computation from store statistics: confidence entropy → W_confidence, recency spread → W_recency, recall Gini coefficient → W_importance, similarity gets remainder. Three query-type weight profiles: temporal (recency dominates 0.50), factual (similarity+confidence dominate), procedural (importance rises to 0.25). Passive feedback loop via recalled_count accumulation; explicit outcome linking deferred.

### 14 — Memory Store Health SLOs (COS-202)
Defines five measurable health dimensions with baselines from live brain.db. Coverage (distillation ratio: current 0.071 = Red, target ≥0.10), Freshness (event-to-memory lag: current 181min = Yellow, target ≤60min), Precision (30-day engagement rate: current 0.038 = Red, target ≥0.30), Diversity (category HHI: current ~0.35 = Yellow, single scope >70% = alert trigger), and Temporal Balance (class distribution: 96% medium = Red, pathological — classification pipeline not running). Composite health score formula weighted across dimensions (coverage 0.25, precision 0.25, freshness 0.20, diversity 0.15, temporal 0.15). Current composite: ~0.15 (critical) — expected for new store but must improve monotonically. Category-differentiated freshness targets: decisions ≤30min, lessons ≤120min.

### 15 — Memory as a Policy Engine (COS-204)
Proposes distributed decision-making where agents query accumulated memory to make locally-correct decisions without escalating to Hermes. Distinguishes memory-policies from hard-coded rules: policies have empirical provenance, are versioned/time-stamped, and use context-sensitive retrieval. Safe delegation classes: task routing, escalation thresholds, communication tone, retry strategies, output format selection. Must-escalate: novel task types, irreversible external actions, user override requests. Five failure modes: policy capture (dominant agent bias), stale policy, conflicting policies (most dangerous — non-deterministic behavior), policy laundering (bad outcomes reinforcing bad policies), policy explosion. Wisdom half-life concept for per-category staleness decay. Separate from COS-180 goals (goals = desired future state; policies = current decision heuristics). Full `policies`, `policy_invocations`, and `policy_invalidation_events` schema.

### 16 — Embedding-First Writes (COS-205)
Discovers that sqlite-vec IS installed and operational (contradicting FRONTIER.md), but only 14/39 active memories are embedded (35.9% coverage). nomic-embed-text confirmed optimal: 768d, MTEB 62.4, 8192-token context, 20-50ms warm latency, already running via Ollama. Proposes synchronous inline embedding in `cmd_memory_add` (warm call is 20-50ms — imperceptible for CLI), with `embedding_queue` table as async fallback. BM25+vector fusion strategy: RRF for initial implementation (zero calibration needed), weighted sum per query type for tuned version. Five query-type weight profiles for hybrid scoring. Graceful degradation: fall back to BM25-only when <50% coverage. Migration: backfill 25 unembedded memories (~1.5s total). Drift guardrails: model pinning with Ollama digest, re-embedding triggers on model update.

---

## CROSS-WAVE SYNTHESIS

### The 5 Biggest Insights Across All Research

**1. Forgetting is a feature, not a bug.** This is the single most counterintuitive and important finding. Multiple waves converge on it: Wave 1 (synaptic pruning research — forgetting improves signal-to-noise), Wave 2 (power-law forgetting is Bayesian optimal per Anderson & Schooler), Wave 3 (episodic memories SHOULD decay faster than semantic ones), Wave 4 (cognitive compression achieves 90-95% footprint reduction through abstraction, not storage). The current architecture under-forgetting: 89% of memories pile up in `medium` temporal class with no promotion or demotion.

**2. The distillation gap is the root blocker for everything.** At 14 memories from ~123 events (11% retention), the memory store is too sparse for any sophisticated retrieval, routing, or reasoning to deliver value. Every Wave 3+ improvement is an architectural investment that underperforms on a sparse store. This finding repeats in the Wave 3 synthesis, COS-117 (P@5=0.22 is partly a content problem), COS-178 (granularity analysis), and COS-202 (coverage SLO is Red). The single highest-leverage engineering action is wiring up the event-to-memory distillation pipeline.

**3. The knowledge graph is the most underutilized asset.** At 2,675 edges, the knowledge_edges table provides substantial relational structure — but it's used by zero retrieval paths. COS-117 identifies graph-augmented reranking as the highest-ROI change requiring no schema change. COS-111 proposes spreading activation over it. COS-124 uses it for push scoring. COS-123 uses it for situation model narrative chaining. COS-121 uses it for trust propagation. Yet as of Wave 3, no Wave 3 report even references it. The graph exists; using it in retrieval is a one-line integration.

**4. Retrieval quality is a function of store quality × algorithm quality.** Better algorithms on an empty store yield nothing. The research consistently shows that improving content quality (distillation, granularity calibration, semantic/episodic typing, chunking context documents) compounds the value of every algorithmic improvement. COS-178's finding that context retrieval P@10 would jump from 0.3 to 0.75 just from fixing the broken chunking pipeline illustrates this perfectly. Store quality is the multiplier.

**5. 178 agents need collective intelligence, not just shared storage.** The system has diverse, independent, decentralized knowledge production (three of Surowiecki's four conditions) but zero aggregation mechanism. No way to detect when multiple agents converge on the same insight, no expertise directory for routing queries to the right specialist, no belief reconciliation for cross-agent conflicts. Transactive memory theory (Wegner) prescribes building a "who knows what" directory — the `agent_expertise` table proposed in COS-113 is the minimum viable collective intelligence infrastructure.

### Patterns That Repeat Across Multiple Waves

- **Sleep/consolidation cycle as the integration point.** Waves 1, 2, 3, 4, and 5 all propose new passes for the nightly consolidation: decay, demotion, dedup, contradiction detection, causal discovery, cross-agent reconciliation, prototype extraction, health SLO measurement, goal proposal generation. The consolidation cycle is the universal extensibility point.

- **SQL-first, LLM-optional architecture.** Every module provides pure-SQL implementations alongside Python wrappers. The LLM is used only for summarization (consolidation), semantic synthesis (episodic→semantic promotion), and query decomposition (IRCoT). This design choice enables determinism, auditability, and fast iteration.

- **Additive-only schema evolution.** Every Wave 3-5 proposal uses ALTER TABLE ADD COLUMN and new tables — no breaking changes, no existing query rework. This is by design (brainctl is the sole interface) and enables incremental deployment.

- **Hybrid BM25+vector as the foundation.** Waves 1, 2, 3, 5 all converge on hybrid retrieval with RRF fusion as the baseline. BM25 excels at exact keywords; vector excels at semantic similarity. Together they beat either alone by 10-20% (BEIR benchmark).

- **Trust/confidence as evolving signals, not static labels.** Reconsolidation theory (every recall modifies the memory), calibrated confidence (staleness penalty, freshness bonus), reputation-weighted validation, and adaptive retrieval weights all treat confidence as a living signal that must be continuously recomputed.

### Contradictions and Tensions Between Findings

- **Predictive push vs. epistemic bubbles.** COS-112 and COS-124 advocate proactive push to reduce search calls. But COS-112 itself warns: "Does predictive push create epistemic bubbles?" If agents stop searching for novel context, organizational learning narrows. The recommender filter bubble problem applies to knowledge management. Mitigation: 1 random high-confidence low-retrieved memory per session as "exploration budget."

- **Forgetting vs. comprehensive coverage.** Wave 1/2 advocate aggressive forgetting for signal-to-noise. Wave 5 SLOs (COS-202) measure coverage ratio as a health signal and flag <0.05 as Red. These are in tension: aggressive pruning reduces coverage. The resolution: compress, don't delete. Abstract N memories into 1 prototype, archive originals. Coverage stays high; noise stays low.

- **Global retrieval vs. TMS specialist routing.** COS-113 argues global search is the wrong default — transactive memory theory says look up who knows about this, then query their memory specifically. But COS-124's push scoring and COS-117's graph-augmented retrieval both assume global search as the starting point. The resolution is layered: TMS routing for precision queries, global search for exploratory/synthesis queries.

- **Same-agent vs. cross-agent contradiction detection.** COS-122 (write contention) focuses on preventing within-scope conflicts. COS-179 (belief reconciliation) argues the vast majority of dangerous divergences are cross-scope. These are complementary but compete for engineering attention. COS-179 is harder and higher-impact; COS-122 is a prerequisite.

- **Confidence compression undermines retrieval weights.** COS-201 finds 36/39 memories at confidence ≥0.90, making the 0.20 confidence weight effectively noise. But COS-121 proposes trust scoring and COS-110 proposes calibrated confidence. These downstream improvements would resolve the compression — but they're not yet implemented, creating a current-state problem.

---

## IMPLEMENTATION STATUS

### What Has Been Built and Deployed

**Deployed tools (~/agentmemory/bin/):**
- `hippocampus.py` — Memory consolidation orchestrator (wires decay, demotion, dedup)
- `embed-populate` — Batch embedding pipeline for FTS5 + sqlite-vec
- `consolidation-cycle.sh` — Shell wrapper for nightly consolidation
- `brainctl` — Primary CLI interface for all memory operations
- `coherence_check.py` — Validates memory consistency, catches stale assumptions
- `route-context` — Context routing for agent heartbeats (COS-83, phases 1-4)
- `salience_routing.py` — Weighted salience scoring for retrieval
- `situation_model_builder.py` — Situation model construction prototype
- `cadence.py` — Scheduling utility
- `hippocampus-cycle.sh` — Hippocampus scheduling wrapper
- `backup-to-icloud.sh` — Database backup

**BRN Project Status:** 24 of 29 issues shipped.

**Key infrastructure confirmed operational:**
- sqlite-vec installed and working (vec0.dylib loads cleanly)
- nomic-embed-text running via Ollama (768d, 20-50ms warm)
- FTS5 indexing active
- Knowledge graph at 2,675 edges
- 26 agents registered (of 178 target)

### The Gap Between What We Know and What We've Shipped

| Research Finding | Implementation Status |
|---|---|
| Nightly sleep cycle (consolidation) | Coded but **not running as cron** |
| Event-to-memory distillation | **Not implemented** — root blocker |
| Hybrid BM25+vector search | sqlite-vec installed but **only 36% embedding coverage**; search uses BM25 only |
| Episodic/semantic bifurcation | Researched, **not migrated** |
| Memory provenance/trust | Researched, **not migrated** |
| Write contention (version column) | Researched, **not migrated** |
| Proactive push | Researched, `brainctl push` **not implemented** |
| Situation models | Prototype exists, **not wired to brainctl** |
| Spreading activation | Researched, **not implemented** |
| Reflexion lessons | COS-195 basic flag exists, dedicated table **not created** |
| Access control (RBAC) | Researched, **not migrated** |
| Adaptive retrieval weights | Researched, **not implemented** (current weights are static) |
| Health SLOs | Researched, **no monitoring running** |
| Memory-as-policy | Researched, **no schema created** |
| Causal event graph | Researched, **no causal edges generated** |
| Memory Event Bus | Researched, **no triggers or tables created** |
| Graph-augmented retrieval | Knowledge graph exists, **zero retrieval paths use it** |
| Context chunking fix | Identified as broken (chunk_index always 0), **not fixed** |
| Temporal class distribution | 96% in medium, **classification pipeline not running** |

### Critical Path for Remaining Improvements

**Phase 0 — Immediate (unblocks everything):**
1. Schedule consolidation-cycle.sh as daily cron (03:00 UTC)
2. Run embed-populate to backfill 25 unembedded active memories
3. Hook inline embedding into brainctl memory add (COS-205)

**Phase 1 — Foundation (1-2 weeks):**
4. Wire event-to-memory distillation pipeline (the root blocker)
5. Apply single migration: version column + memory_type + provenance columns
6. Normalize timestamps across all tables
7. Fix context chunking pipeline (chunk_index always 0 is catastrophic)

**Phase 2 — Retrieval Quality (1-2 weeks):**
8. Enable hybrid BM25+vector search with RRF fusion
9. Add graph-augmented reranking using knowledge_edges
10. Implement query-type-aware weight profiles

**Phase 3 — Architecture (2-3 weeks):**
11. Implement brainctl push (proactive memory delivery)
12. Wire situation_model_builder to brainctl situation subcommand
13. Deploy memory health SLO monitoring
14. Create reflexion_lessons table + brainctl reflexion commands

**Phase 4 — Scale (3-4 weeks):**
15. Memory Event Bus (real-time agent-to-agent propagation)
16. Agent expertise directory (transactive memory)
17. Cross-agent belief reconciliation pass
18. Memory access control (RBAC visibility column)

---

## THE 10 MOST TRANSFORMATIVE IDEAS
*Ranked by potential cognitive impact on the 178-agent system*

### 1. Event-to-Memory Distillation Pipeline
**What:** Automated promotion of high-signal events into durable memories with source linking.
**Research support:** COS-117 (P@5 bottleneck is content, not algorithm), COS-202 (coverage SLO is Red at 0.071), Wave 3 synthesis (root blocker for all Wave 3+ features).
**Impact:** Every downstream improvement is multiplicatively gated on this. Without distillation, the store stays sparse and all retrieval/routing/reasoning operates on noise.
**Implementation:** Auto-promote rule on result events with importance ≥0.7; LLM synthesis for multi-event patterns; target 30% event-to-memory ratio. ~3-5 days engineering.

### 2. Hybrid BM25+Vector Search with Graph Augmentation
**What:** Fuse FTS5 keyword scores with sqlite-vec cosine similarity via RRF, then expand results through knowledge_edges.
**Research support:** COS-117 (graph augmentation is highest ROI, no schema change), 03_ai_memory_systems.md (hybrid outperforms pure vector by 10-20%), COS-205 (embedding pipeline confirmed operational).
**Impact:** Transforms retrieval from keyword-only to semantic+structural. Graph augmentation surfaces conceptually related memories invisible to both BM25 and cosine.
**Implementation:** RRF fusion (zero calibration) + 1-hop knowledge_edges expansion. sqlite-vec is installed. ~2-3 days engineering.

### 3. Memory-as-Policy Engine
**What:** Encode decision heuristics as retrievable, versioned, context-sensitive memory records so agents make locally-correct decisions without escalating to Hermes.
**Research support:** COS-204 (full architecture with failure modes, schema, query interface), COS-180 (goal-policy interaction model).
**Impact:** Eliminates Hermes as a bottleneck for routine decisions. At 178 agents, central orchestration is the throughput ceiling. Policy delegation unlocks horizontal scaling of decision-making.
**Implementation:** `policies` table + retrieval interface + outcome tracking + staleness decay. ~2-3 weeks engineering.

### 4. Episodic→Semantic Promotion (Consolidation-Driven Abstraction)
**What:** Automatically synthesize stable facts from clusters of related episodic memories, replacing N event records with 1 semantic truth.
**Research support:** COS-120 (bifurcation design), COS-116 (three-tier hierarchical memory), 04_neuroscience (hippocampal consolidation converts episodes to schemas).
**Impact:** 90-95% footprint reduction at scale while maintaining or improving retrieval quality. Enables the system to learn organizational truths, not just record events.
**Implementation:** LLM synthesis pass in consolidation cycle + memory_type column. ~1-2 weeks.

### 5. Transactive Memory System (Agent Expertise Directory)
**What:** Build an explicit map of which agent knows what, enabling capability-aware query routing.
**Research support:** COS-113 (Wegner's TMS theory — groups outperform by knowing "who knows what"), COS-112 (collaborative filtering for predictive routing).
**Impact:** Transforms the 178-agent system from shared-storage to shared-cognition. Queries route to domain experts instead of broadcasting globally. Reduces noise and improves precision.
**Implementation:** `agent_expertise` table populated from event/memory history, integrated into route-context. ~1 week.

### 6. Metacognitive Gap Detection
**What:** Actively flag when the knowledge base has no coverage for an agent's current task scope, replacing silent empty results with explicit gap reports.
**Research support:** COS-110 (Nelson & Narens framework, knowledge_coverage table, FOK proxy), COS-202 (engagement rate SLO at 3.8% shows most memories never recalled).
**Impact:** Transforms the failure mode from "agent assumes it found everything" to "agent knows what it doesn't know." Under Open-World Assumption, absence of evidence is explicitly tracked, not treated as evidence of absence.
**Implementation:** `knowledge_coverage` + `knowledge_gaps` tables + nightly scan + post-retrieval tier annotation. ~3 days.

### 7. Spreading Activation for Associative Recall
**What:** Propagate partial activation through knowledge_edges from retrieval results, surfacing non-obvious connections.
**Research support:** COS-111 (Collins & Loftus 1975 — activation decays with distance through semantic network), COS-124 (graph activation bonus for push scoring).
**Impact:** Enables "creative" memory — finding connections that pure keyword/vector search misses. A query about "billing slowdown" activates "PostgreSQL index" which activates "auth outage from index rebuild" — a non-obvious but operationally critical analogy.
**Implementation:** ~150 lines of Python over existing knowledge_edges. 2-3 days. `brainctl graph activate`.

### 8. Reflexion Failure Taxonomy with Cross-Agent Generalization
**What:** Classify failures into 5 canonical types, store lessons with event-driven expiration, and propagate to agents with matching capabilities.
**Research support:** COS-199 (full taxonomy, lifecycle, override semantics), 03_ai_memory_systems.md (Reflexion: 20-40% reasoning improvement).
**Impact:** Prevents the same failure from recurring across 178 agents. A lesson from one agent's checkout conflict immediately protects all Paperclip agents. Current org data shows COORDINATION_FAILURE is dominant — all agents hit the same auth mismatch with no lesson filed.
**Implementation:** `reflexion_lessons` table + `brainctl reflexion` commands + trigger-condition embedding. ~1-2 weeks.

### 9. Proactive Memory Push at Checkout
**What:** Pre-load 5 high-confidence memories into agent context at task checkout, eliminating 30-50% of explicit search calls.
**Research support:** COS-124 (full architecture, scoring pipeline, anti-noise safeguards), COS-112 (predictive routing design).
**Impact:** Shifts from "agent must know what to ask for" to "system anticipates what agent needs." Highest value for tasks in established domains where relevant memories already exist.
**Implementation:** `brainctl push` command + `push_log` table + heartbeat procedure update. ~1-2 weeks.

### 10. Causal Event Graph (Answer "Why?" Not Just "What?")
**What:** Automatically construct causal DAG over events using temporal proximity, type templates, and explicit references.
**Research support:** COS-184 (three-tier causal edge system), COS-114 (event calculus, counterfactual reasoning design).
**Impact:** Transforms debugging from "here are events before the failure" to "error #45 caused config change #46 which caused deploy failure #47." At 178 agents, understanding causal chains across agent actions is essential for incident response.
**Implementation:** Heuristic detection + `brainctl temporal causes/effects/chain` commands. ~1 week.

---

## OPEN QUESTIONS
*What we don't know yet that we should*

### Architecture Questions
1. **When should we shard brain.db?** The single-writer SQLite constraint is manageable now at 22 agents but will bottleneck at 178. What's the tipping point, and what's the sharding strategy? (COS-181 filed but not researched.)
2. **Should the consolidation cycle be continuous instead of nightly?** COS-183 (continuous LLM consolidation) is filed but unresearched. Garbage accumulates between cycles. What's the cost/benefit of a 30-minute consolidation cadence vs. nightly?
3. **What happens when two situation models overlap?** COS-123 builds per-anchor models. Cross-situation reasoning ("how does COS-83 relate to COS-86?") is identified as a graph fragment but no algorithm is specified.

### Measurement Questions
4. **What is the empirical correlation between computed confidence and actual retrieval quality?** COS-110 flags this as the single most valuable empirical study. Without a calibration baseline, all confidence reasoning is circular.
5. **What is the optimal decay parameter for spreading activation?** COS-111 suggests 0.6 but notes it needs empirical tuning against real brain.db traversals. A benchmark of "known good" associative leaps is needed.
6. **What is the real precision improvement from each proposed change?** Projections exist (context chunking 0.3→0.75, graph augmentation +0.15) but no A/B experiments have been run.

### Agent Behavior Questions
7. **Do agents actually use pushed memories?** COS-124 proposes utility tracking, but the feedback loop hasn't been validated. If push utility rate is <20%, it's net-negative.
8. **Can agents reliably self-report failure classes?** The reflexion taxonomy assumes agents can distinguish REASONING_ERROR from CONTEXT_LOSS. Is this classification accurate enough for automated lifecycle management?
9. **What do agents DO with mid-heartbeat invalidation events?** COS-177 delivers the signal; incorporating it correctly requires agent-level logic that hasn't been designed.

### Scale Questions
10. **Does the aggregation mechanism create wisdom-of-crowds or wisdom-of-the-loudest?** COS-113 warns of information cascades and herding. Collective reinforcement (boosting memories recalled by 3+ agents) could amplify errors if those agents share a common flawed source.
11. **What is the cold-start strategy for the 156 agents with zero memories?** Is the right response to flag them as coverage holes or accept sparse coverage for low-activity agents?
12. **Can the policy engine avoid policy laundering?** COS-204 identifies this as the hardest failure mode — bad outcomes reinforcing bad policies through lenient evaluation. No detection mechanism exists beyond periodic A/B shadow testing.

### Theoretical Questions
13. **Is one level of metacognition sufficient?** COS-110 asks: "who watches the watcher?" The metacognition layer produces confidence estimates about confidence estimates. Infinite regress is a theoretical risk.
14. **Should all memories eventually become semantic?** The episodic→semantic promotion path implies yes, but some organizational knowledge is inherently episodic (what happened during the March incident). At what point should promotion stop?
15. **Is there a Kolmogorov complexity floor for organizational memory?** COS-116 suggests compression to minimum viable knowledge graph. But what's the theoretical limit? If the org's operational behavior has inherent randomness, some information is genuinely incompressible.

---

---

## WAVE 10: PROBABILISTIC COGNITION
*1 Deliverable — Bayesian Foundations for Confidence*

### 28 — Bayesian Brain: Formal Probabilistic Reasoning (COS-344)
Formalizes the ad hoc confidence system (exponential decay + 15% asymptotic recall boost) as proper Bayesian inference using Beta distributions. Replaces scalar `confidence` with `Beta(α, β)` where α = evidence in favor, β = evidence against. Point estimate `α/(α+β)` is backwards-compatible with all existing queries. Core gain: **well-evidenced memories resist spurious decay** (α preserved on decay, only β grows) while uncertain memories fade naturally. Five research areas synthesized: (1) Knill & Pouget 2004 Bayesian Brain — maps prior/likelihood/posterior to encoding confidence/salience/recall; (2) Pearl belief propagation — 1-hop confidence updates through `knowledge_edges` (supports → boost neighbor α, contradicts → boost neighbor β, hop_decay=0.5); (3) Bayesian recall update — replaces `conf += 0.15*(1-conf)` with `α += salience` on confirm, `β += salience` on contradict; (4) Hierarchical priors — `project_memory_priors` table for domain-specific prior strength (infrastructure: Beta(8,2), research: Beta(2,6)); (5) Thompson sampling — `brainctl vsearch --explore` samples from Beta distributions instead of using point estimates, naturally surfacing uncertain memories. Implementation is phased: schema + backfill (zero risk) → Bayesian recall → Bayesian decay → propagation → Thompson sampling. Key behavioral difference: a memory with α=100 (100 confirmed recalls) is nearly immune to 30 days of non-recall, while a freshly-assigned memory at the same confidence decays normally. Schema: add `confidence_alpha`, `confidence_beta` to memories; new `project_memory_priors` and `agent_memory_profile` tables. Deliverable: `~/agentmemory/research/wave10/28_bayesian_brain.md`

---

*This compendium covers all 40 research deliverables across 6 waves. It is the master reference for the Hermes cognitive architecture. Updated 2026-03-28.*
