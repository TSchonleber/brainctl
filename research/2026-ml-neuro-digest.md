# 2026 ML + Comp-Neuro Research Digest for brainctl

Compiled 2026-04-19. All papers arxiv 2025-10 through 2026-04. Grouped by relevance to brainctl's current design surface.

---

## Tier 1 — Surveys / maps of the field (read first, orient wide)

### [AI Meets Brain: A Unified Survey on Memory Systems from Cognitive Neuroscience to Autonomous Agents](https://arxiv.org/abs/2512.23343)
Big bridge paper. Explicitly maps biological memory → LLM agent memory along a progressive trajectory. Covers storage, management lifecycle, evaluation, and — notably — memory *security* (attack/defense). brainctl already has PII scanning and trust scoring; this paper is the closest thing to a reference taxonomy for the "memory as critical infrastructure" framing.

**For brainctl**: cite in whitepaper / Hermes submission; use their lifecycle vocabulary when writing docs so brainctl lines up with the emerging field taxonomy instead of inventing parallel terms.

### [Memory in the Age of AI Agents](https://arxiv.org/abs/2512.13564) (Liu et al.)
The survey paired with the [Agent-Memory-Paper-List](https://github.com/Shichun-Liu/Agent-Memory-Paper-List) GitHub repo — likely the most comprehensive curated reading list in the field right now. Use the repo as a living bibliography.

### [Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers](https://arxiv.org/abs/2603.07670)
Five mechanism families: **context-resident compression**, **retrieval-augmented stores**, **reflective self-improvement**, **hierarchical virtual context**, **policy-learned management**. Three-axis taxonomy: temporal scope × representational substrate × control policy.

Open frontiers they flag — all directly relevant to brainctl:
- **Continual consolidation** (brainctl v2 hippocampus work)
- **Causally grounded retrieval** (→ brainctl's temporal_causes / temporal_chain already points this way)
- **Trustworthy reflection** (→ trust_audit / trust_calibrate / reflexion tools)
- **Learned forgetting** (→ decay / retirement — currently rule-based, not learned)
- **Multimodal embodied memory** (gap in brainctl — text-only today)

**For brainctl**: "learned forgetting" is the clearest architectural gap. Today decay is hand-tuned constants in `hippocampus.py`. A learned policy over retirement could be the next consolidation-engine milestone.

### [Anatomy of Agentic Memory: Taxonomy and Empirical Analysis of Evaluation and System Limitations](https://arxiv.org/abs/2602.19320)
Four pain points in current evaluations:
1. Benchmark saturation (too easy / underscaled)
2. Metric validity / judge sensitivity
3. Backbone-dependent accuracy (same memory system, swap LLM, results swing)
4. System cost oversight — latency/throughput of memory ops rarely measured

**For brainctl**: we should publish our own `memory_calibration` numbers *with* latency overhead per tool call, not just hit-rate. This is a cheap differentiator when the field is ignoring cost.

---

## Tier 2 — Architectures that map onto brainctl primitives

### [Continuum Memory Architectures (CMA) for Long-Horizon LLM Agents](https://arxiv.org/abs/2601.09913)
"RAG treats memory as a stateless lookup table." CMA defines five architectural primitives:
1. **Persistent storage**
2. **Selective retention**
3. **Associative routing**
4. **Temporal chaining**
5. **Consolidation** (into higher-order abstractions)

Explicitly invokes dreaming/REM analogy for consolidation.

**For brainctl**: brainctl already implements all five — persistent SQLite, W(m) worthiness gate for retention, vsearch/entity_relate for associative routing, temporal_chain/temporal_causes/temporal_map for chaining, consolidation_run/dream_cycle for consolidation. We can claim CMA-compliance and point at tool names as evidence. Good positioning language.

### [GAM: Hierarchical Graph-based Agentic Memory for LLM Agents](https://arxiv.org/abs/2604.12285)
Two-phase architecture, sleep-inspired:
- **Episodic Buffering Phase** — ongoing dialogue isolated in an *event progression graph* (sequential, local)
- **Semantic Consolidation Phase** — integrates into a *topic associative network* (global, semantic)

Transition is **Semantic-Event-Triggered**: consolidation fires on detected semantic shift, not on a timer. Noise stays in the buffer; only real shifts propagate.

**For brainctl**: this is the clearest architectural prescription for what brainctl's consolidation trigger *should* be. Today brainctl uses homeostatic pressure + learning load (`compute_homeostatic_pressure`, `compute_learning_load`, `should_trigger_consolidation`). Adding a *semantic-shift detector* as a third OR-trigger is a natural next hippocampus.py addition. Detecting "the conversation topic just moved" is cheap (embedding distance between rolling windows) and aligns with how biological SWRs fire at behavioral boundaries.

### [Multi-Layered Memory Architectures for LLM Agents](https://arxiv.org/abs/2603.29194)
Decomposes dialogue into working / episodic / semantic with **adaptive gating** and **retention regularization**. Controls cross-session drift while bounding context growth.

**For brainctl**: brainctl has memory_type (episodic, semantic) but no explicit working-memory tier. The attention_snapshot / workspace_phi tools approximate a working layer — worth formalizing it as a first-class tier with explicit promotion rules to episodic.

### [HiCL: Hippocampal-Inspired Continual Learning](https://arxiv.org/abs/2508.16651)
Closest thing to a literal hippocampus circuit in ML: **grid-cell layer → DG (sparse pattern separation, top-k) → CA3 autoassociative → DG-gated MoE → EWC** with prioritized replay. Gating uses cosine similarity between sparse DG representations and learned prototypes — no separate gating network.

**For brainctl**: the DG-style sparse pattern separation is interesting. brainctl's entity system already enforces separation by unique names. But *memory* deduplication today is soft (the W(m) gate). A DG-analog "is this embedding sparsely distinguishable from the last N?" gate would sharpen pre-write dedup. The CA3 autoassociative analog in brainctl is vsearch — already there.

---

## Tier 3 — Concrete replay / consolidation algorithms

### [SuRe: Surprise-Driven Prioritised Replay](https://arxiv.org/abs/2511.22367)
Rank replay candidates by **negative log-likelihood** — high-NLL sequences are "surprising" and get prioritized. Combined with dual LoRA adapters, +5 acc points on large-number-of-tasks benchmarks.

**For brainctl**: brainctl already has a replay_queue / replay_boost. The current prioritization is importance-based. **Surprise (NLL) is a better signal than importance** because importance is declared, surprise is measured. Concrete implementation: when a memory is written, compute NLL of the summary under a small reference model (or use LLM-returned logprobs). Store as a `surprise_score` column. Replay queue orders by `surprise_score * importance * recency_weight`.

### [MSSR: Memory-Aware Adaptive Replay for Continual LLM Fine-Tuning](https://arxiv.org/abs/2603.09892) (March 2026)
Estimates **per-sample memory strength** and schedules rehearsal at adaptive intervals — weak memories rehearsed often, strong memories left alone. Mirrors spacing effect / Ebbinghaus directly.

**For brainctl**: this is essentially the spaced-repetition algorithm, adapted. brainctl's decay model is already proximate to this, but rehearsal scheduling is passive (decay lowers confidence, recall boosts it). Active *scheduling* of replay for weak-but-tagged memories during consolidation_run would close the loop. Aligns with Frey & Morris synaptic tagging already in hippocampus.py.

### [Neuroscience-Inspired Memory Replay: Predictive Coding vs Backprop](https://arxiv.org/abs/2512.00619)
Predictive-coding-based generative replay beats backprop-based replay by **+15.3% average retention** with competitive transfer. Small paper (9pp) but a clean empirical result.

**For brainctl**: brainctl doesn't do generative replay (it replays stored text, not synthesized patterns). If/when we add a "dream" step that *generates* plausible memory reconstructions (rather than just reactivating stored ones), predictive-coding-style reconstruction is the empirically-better path. This lines up with the user's existing `dream_cycle` tool — currently light, could be the home for this.

### [Modular Memory is the Key to Continual Learning Agents](https://arxiv.org/abs/2603.01761)
Core-model consolidation via long-term memory leverage. Argues modular (not monolithic) memory is required for CL.

**For brainctl**: validates brainctl's per-agent scoping and per-project scoping. Modularity is already there via `scope="project:<name>"` and `scope="agent:<id>"`.

---

## Tier 4 — Cross-cutting (ToM, associative memory, externalization)

### [Evaluating Theory of Mind and Internal Beliefs in LLM-Based Multi-Agent Systems](https://arxiv.org/abs/2603.00142) (Feb 2026)
Architecture integrates **ToM + BDI-style internal beliefs + symbolic solver** for logical verification. Evaluated on resource allocation across several LLMs.

**For brainctl**: brainctl has extensive ToM tooling (`tom_belief_set`, `tom_conflicts_resolve`, `tom_perspective_get`, `tom_gap_scan`, etc.) and belief tooling (`belief_set`, `belief_conflicts`, `belief_merge`). Adding a *symbolic solver* layer for belief consistency checking (SMT/Datalog over the belief store) is the next step. This paper is the strongest 2026 validation of brainctl's existing ToM direction — cite it.

### [Hopfield-Fenchel-Young Networks: A Unified Framework for Associative Memory Retrieval](https://arxiv.org/abs/2411.08590)
Theoretical umbrella unifying modern Hopfield / sparse Hopfield / attention — all become instances of Fenchel-Young optimization. Rigorous capacity results.

**For brainctl**: theoretical backing for why vsearch (attention-like retrieval over a memory matrix) is architecturally equivalent to an associative memory. Useful for the brainctl technical spec when claiming capacity bounds.

### [Externalization in LLM Agents: Memory, Skills, Protocols, Harness Engineering](https://arxiv.org/abs/2604.08224)
Argues the right frontier is *externalization* of agent cognition — not better LLMs, but richer external state (memory + skills + protocols + harness).

**For brainctl**: direct philosophical alignment with brainctl's thesis. This paper is worth quoting verbatim when pitching brainctl — it's a peer-reviewed argument that memory-as-external-substrate is the right direction.

### [Agentic Memory (AgeMem): Unified Long- and Short-Term Memory as Tool Actions](https://arxiv.org/abs/2601.01885)
Memory operations are exposed as **tool-based actions** — the agent decides when to store/retrieve/update/summarize/discard. This is *exactly* brainctl's model. Independent convergence on the same architecture.

**For brainctl**: reinforces that the MCP-tool-based interface is not incidental — it's the right interface. Worth referencing in product messaging.

### [Hindsight is 20/20: Building Agent Memory that Retains, Recalls, and Reflects](https://arxiv.org/abs/2512.12818)
Focus on the **reflect** step — the retrospective pass that converts raw episodes into summarized, utility-weighted memories.

**For brainctl**: maps to `reflexion_write` / `reflexion_success` / `reflexion_failure_recurrence`. Reflexion tools already exist; this paper gives a cleaner evaluation methodology to benchmark them against.

---

## Synthesis — 5 concrete brainctl deltas this research justifies

1. **Add a semantic-shift trigger to `should_trigger_consolidation`** (from GAM). Third OR-clause alongside homeostatic pressure and learning load. Embedding distance between rolling windows crosses threshold → consolidation fires at topic boundaries, the way SWRs fire at behavioral boundaries.

2. **Replace importance-only replay ordering with surprise-weighted replay** (from SuRe). Store per-memory `surprise_score` (NLL under a reference model or LLM logprobs at write time). Replay queue orders by `surprise * importance * recency`.

3. **Add active rehearsal scheduling** (from MSSR). During `consolidation_run`, actively rehearse weak-but-tagged memories on an adaptive-interval schedule — not just passively decay and hope recalls catch them.

4. **Formalize working-memory tier** (from Multi-Layered Memory Architectures). Promote attention_snapshot / workspace_phi into a first-class pre-episodic tier with explicit promotion rules. Reduces episodic clutter.

5. **Add symbolic-solver layer over beliefs** (from ToM + BDI + Symbolic paper). SMT or Datalog pass over the belief store to detect logical inconsistencies `belief_conflicts_scan` can't catch structurally. brainctl's ToM is already the most elaborate in the field; symbolic verification makes it provably consistent.

---

## Reading order if short on time

1. Memory for Autonomous LLM Agents (survey) — 45 min
2. Continuum Memory Architectures — 30 min (positioning)
3. GAM — 30 min (trigger design)
4. SuRe — 20 min (replay design)
5. HiCL — 30 min (neuro architecture sanity check)
6. ToM+BDI+Symbolic — 30 min (belief-layer roadmap)
