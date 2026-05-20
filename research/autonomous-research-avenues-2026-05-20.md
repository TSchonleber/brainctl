# Autonomous Research Avenues — 2026-05-20

**Author:** Claude Opus 4.7 (overnight chain, ~01:00 EDT)
**Trigger:** Terrance asked for "autonomous avenues of research" alongside region codification. This memo answers that part.

---

## Why this memo

Tonight's chain has shipped Phase 1 for four new brain subsystems (LC, NB, ARAS, Habenula — PRs #121-#124). That's *codification of canonical neuroanatomy* — pulling neuroscience consensus into brainctl tables. The other half of "autonomous research" is **generative** — staking out directions brainctl could explore that don't already have a canonical neuroanatomical answer. This is that half.

Each avenue below is a candidate seed for Phase 1 work, a thinking memo, or an experimental probe. None are committed to. They're starting points. They are the speculative-end of brainctl's roadmap.

---

## Avenue 1: Sleep architecture as a first-class state machine

**The gap.** brainctl has a `dream_cycle` + DMN subsystem (migration 061) and an idle-trigger consolidation path, but they treat sleep as one undifferentiated state ("offline = consolidation runs"). Biology partitions sleep into structured stages — NREM 1/2/3, REM — with **qualitatively different memory operations** in each:

- **NREM 2** — sleep spindles + slow oscillations; declarative memory consolidation
- **NREM 3 (SWS)** — sharp-wave ripples; hippocampal replay → neocortex transfer
- **REM** — procedural / emotional consolidation, creative bisociation

A brainctl that respects this distinction would have:

- `sleep_stage` column on `consolidation_runs` and `dream_cycle_log`
- Stage-gated operation classes — only certain things happen in each stage
- An ARAS-driven stage progression (now possible after PR #123 ships ARAS sleep modes)

**Probe.** Audit the last 30 days of consolidation_run output and bucket it by *what it actually did* (semantic-from-episodic vs. SWR replay vs. emergence detection). If there's already de-facto staging by what gets called in what order, expose it.

**Research question.** Does forcing brainctl through explicit NREM-2→NREM-3→REM cycles overnight (one full ultradian cycle per consolidation pass) outperform the current scheduling? Bench: P@5 + recall@5 on next-morning queries after staged vs. unstaged overnight runs.

**Loose ends:**
- Are sleep spindles a useful organizing event class for the workspace_broadcasts bus?
- Does REM-analog operation imply slack in the W(m) write gate (allowing more "creative" memory creation during REM-mode)?
- How does the new ARAS `sleep_wake_mode` interact with the dream_cycle? They should compose.

---

## Avenue 2: Memory aging as synaptic tagging-and-capture

**The bio.** Memory's late-LTP (long-term potentiation) phase requires both **a tag** at the synapse during initial encoding AND **plasticity-related proteins (PRPs)** showing up within a time window (~1 hour). Memories with tags but no PRPs decay. Memories with PRPs but no tag don't form. Frey & Morris's synaptic tagging-and-capture hypothesis.

The brainctl equivalent: memories are admitted by the W(m) gate (the *tag* — "this is plausibly worth keeping"), but there's no separate **capture** step that decides whether the memory actually lasts past short-term. Current behavior: once written, the memory persists until explicit retirement. Biology says it should be conditional on a follow-up "PRP" signal — typically the memory being **recalled within a critical window**.

**Probe.** What's the distribution of (time-to-first-recall) across all memories in the live brain? If most memories that survive 30+ days were recalled within 24 hours of creation, then biology's pattern matches. If not, brainctl is keeping a lot of synaptic tags that never got captured — and we have a candidate cleanup mechanism.

**Possible mechanism.** A new `memory_capture_window` column: timestamp of last "PRP event" (recall, reinforcement, association). Memories whose capture-window expires unrecalled get demoted (not deleted — moved to a `memories_unconsolidated` tier that only surfaces under explicit query). This is **more aggressive forgetting** than the current decay model and likely improves retrieval precision.

**Research question.** What fraction of brainctl's index is unrecalled-since-creation? At what threshold does demoting that fraction improve overall recall quality?

---

## Avenue 3: Cross-modal binding — the claustrum question

**The bio.** The claustrum is a thin sheet of neurons that **everyone projects to** and that **projects to everyone**. Crick and Koch (2005) proposed it as the consciousness-binding integrator. The actual function is contested. What's not contested: it's where multimodal information converges and re-radiates.

**The brainctl gap.** brainctl has *several* parallel retrieval pathways — FTS, vector, hybrid_rrf, pagerank_boost, multi_pass, temporal_expand, entorhinal grid lookup, procedural-memory search. Each is a "modality." Right now they're stitched together by `cmd_search`'s heuristic merge. There's no first-class structure for *cross-modal binding* — recognizing when two modalities are agreeing on the same target.

A claustrum-analog subsystem would:

- Listen to top-K outputs from every retrieval modality
- Detect convergence (same memory IDs appearing in multiple modalities)
- Boost the confidence of cross-modal hits before reranking
- Expose a `claustrum_binding_strength` per result that reranker layers can use

**Research question.** What fraction of "best" search results (per outcome_annotate's success labels) come from modality-convergence vs. single-modality? If high, the binding is doing real work and deserves its own subsystem; if low, the convergence detection is rare enough that a simpler heuristic suffices.

**Caveat.** This may overlap with the existing RRF fusion. The distinction would be: RRF is **rank-level** merge; claustrum-binding is **identity-level** detection that gets attached to memories as a confidence signal that survives across retrievals.

---

## Avenue 4: Multi-agent brain federation

**The current state.** brainctl is single-tenant per `brain.db`. The federation tools (`federated_*` MCP family, `mcp_tools_federation.py`) provide cross-tenant query plumbing but no real cross-brain learning. Each brain learns alone.

**The bio analog.** This isn't really one brain region — it's the social learning literature. Theory of Mind exists in brainctl (`mcp_tools_tom.py`). What doesn't exist: an OS-level coordination layer where two brainctl instances can:

- Share retrieval-policy weights (BG striatal_weights)
- Share calibrated trust scores
- Share dream-cycle outputs (one brain's REM-derived hypothesis is another brain's testable prediction)
- Co-consolidate (two brains attending the same external event develop coupled memories)

**Research question.** What's the minimal viable federation? Probably: shared `bg_striatal_weights` for the `oculomotor` loop (retrieval strategies). Two agents query the same brain.db slice over time; whichever's policy converges fastest "wins" and others adopt. Federated bandit.

**Caveat.** Cross-brain trust is hard. A poisoned weight from one brain contaminates the federation. Would need a per-source trust score on imported weights, which is what `trust_calibrate` infrastructure already exists for.

---

## Avenue 5: Connectome as a first-class graph

**Current state.** brainctl has subsystem boundaries (BG, cerebellum, thalamus, etc.) but no first-class representation of **which subsystems talk to which** — there's no `connectome` table that says "BG outputs feed into thalamus, thalamus outputs feed into cortex-analog, cerebellum modulates thalamic precision," etc. Those connections are encoded *implicitly* in code (e.g., `bg_shadow.py`'s `broadcast_td_error` writes to thalamic dials).

**The gap.** When we add a new subsystem, we add code, and the new connection lives in the code. No structural view. This makes:
- Cycle detection impossible
- "What writes to this dial" queries impossible
- Impact analysis ("if I disable subsystem X, what breaks") manual

**Proposed.** A `connectome_edges` table: `(source_subsystem, target_subsystem, edge_type, weight)`. `edge_type` ∈ {writes_to, reads_from, modulates, gates, depends_on}. Seeded by walking the existing code; updated when new subsystems land. Could be visualized as a force-directed graph showing brainctl's actual architecture.

**Research question.** Once we have a connectome graph, what's the diameter? What's the betweenness centrality of bg_modulators? Is brainctl's architecture small-world like a real brain, or hub-and-spoke?

---

## Avenue 6: Dream-as-hypothesis-testing

**The current dream cycle.** `dream_cycle` and DMN (migration 061) generate "speculative memories" during idle time. These are counterfactual continuations of recent decisions. They get quarantined until validated.

**The gap.** Validation is currently *passive* — speculative memories graduate to `memories` when real events confirm them. But biology suggests dreams aren't just speculation — they're **active hypothesis tests**: the brain spends sleep cycles checking which of its world-model hypotheses can survive ablation by counterfactual events. If a hypothesis is fragile under counterfactual rollout, it gets weakened.

**Proposed.** Dream cycle generates not just speculations but **predictions about future events**, attached to specific memories. When the predicted event arrives (or fails to arrive), the source memory's trust score moves. This is closing the loop between dream output and trust calibration.

**Research question.** Do brainctl agents that use dream-derived predictions for trust calibration have better next-week retrieval precision than agents using only direct-outcome trust updates?

---

## Avenue 7: VTA/SNc as a first-class dopamine source

**The current state.** Dopamine in brainctl exists as a *dial* (`bg_modulators.tonic_da`) and as a *broadcast* (`bg_td_events.delta` represents the phasic δ that updates striatal weights). What's missing: a **nucleus** that sources this signal with its own state.

In biology, VTA and SNc fire phasically on +RPE events and tonically based on motivational state. Their firing **is** the dopamine signal. brainctl currently distributes this across BG bookkeeping, but there's no place that says "the VTA fired right now, here's the burst magnitude, here's what triggered it."

**Why it might matter.** Right now brainctl's "dopamine" is a derived quantity. If it were a first-class signal with its own time series, you could:

- Detect dopamine pathologies (sustained low DA = depression-analog; sustained high DA = mania-analog)
- Couple DA to ARAS arousal directly (low arousal → low VTA firing → low motivation to retrieve)
- Build a Habenula→RMTg→VTA chain (Habenula PR #124 already prepared for this in Phase 3)

**Proposed Phase 1.** `vta_firings` table; auto-populated by Phase 2 from bg_td_events with δ > threshold. Phase 3 reads ARAS + Habenula to gate VTA firing.

---

## Avenue 8: Septum + theta rhythm as a hippocampal pacemaker

**The current state.** brainctl's hippocampus is shipped (migration 059, DG + CA3 subfields). The `memory_search` docstring mentions theta-gamma coupling ("Result count is capped at 7 × agent attention_budget_tier (theta-gamma coupling)") but there's no actual theta-rhythm clock.

**What's missing.** The medial septum is the hippocampal theta pacemaker — it sets the 4-8 Hz rhythm that the hippocampus uses to phase-lock memory operations. Without an explicit septum clock, brainctl can't:

- Cycle-time consolidation operations on a regular cadence
- Phase-encode memories by which theta-cycle they arrived in
- Use phase-locked memory_search (looking only at memories in the current theta phase's bin)

**Proposed Phase 1.** `septum_state` (single row) with `theta_phase`, `theta_bin`, `cycle_count`. Updated by a daemon at a configurable interval. Memories at write time can be stamped with the current `theta_bin` for later phase-locked retrieval.

**Research question.** Does phase-locked retrieval (only memories from the same theta bin) reduce retrieval cost without harming P@k? Biology says yes (gamma-bin within theta-cycle is the canonical attention-binding mechanism).

---

## Avenue 9: Inferior colliculus / superior colliculus — orienting salience

**The current state.** brainctl has thalamus salience (post migration 050) and pulvinar-analog visual salience implicit there, but no first-class "orienting reflex" — the involuntary attention capture on a novel or threatening stimulus.

**Bio.** Superior colliculus = visual orienting; inferior colliculus = auditory orienting. They're SUB-cortical, fire BEFORE cortical processing, and bias attention rapidly.

**brainctl analog.** A `colliculus_orienting` subsystem that watches the input stream for **novel surface patterns** (new entity sightings, unfamiliar query shapes, unusual content types) and fires a fast `aras_drive` pulse + a thalamic mode adjustment before the full retrieval pipeline gets going. Operates at the dispatch-level shadow consult, like BG and cerebellum already do.

**Research question.** Does pre-cortical orienting actually reduce latency on novel-pattern queries? Or is the cortical layer fast enough that orienting is irrelevant in a software system?

---

## Avenue 10: Allostatic load → trust decay correlation

**The bio.** Chronic stress accumulates as "allostatic load" — measured by cortisol, HPA-axis dysregulation, inflammatory markers. High allostatic load correlates with **specific memory deficits**: hippocampal dependent recall degrades faster than habit/striatal recall.

**brainctl analog.** brainctl already has an `mcp_tools_allostatic.py` (demand_forecast, allostatic_prime) and an `mcp_tools_drives.py` (5 drives including consolidation_debt). What's missing is the **degradation pattern** — high allostatic load should asymmetrically damage episodic memory faster than procedural.

**Probe.** Audit the existing `agent_uncertainty_log` for allostatic spikes. Cross-reference against trust decay on episodic memories vs. procedural memories during those windows. If the pattern matches biology — episodic decays faster — there's a real signal to lean into. If not, brainctl's stress model doesn't track biology and we should either fix the model or rip out the analogy.

---

## Operational suggestions for these avenues

1. **Probes first, schema second.** Each avenue has an empirical probe attached. Run the probe against the live brain.db before committing to a Phase 1 schema. If the probe shows the predicted signal, ship the subsystem. If not, the avenue isn't ready.

2. **Avenues 1, 2, 5 are the highest-tractability.** Sleep architecture, memory aging, and connectome graph all sit on top of infrastructure brainctl already has. No new external dependencies, bounded scope.

3. **Avenues 4, 6 are the most ambitious.** Multi-agent federation and dream-as-hypothesis-testing both require new coordination machinery. Schedule for Phase 1 work over a week, not a night.

4. **Avenues 3, 7, 8, 9, 10 are speculative experiments.** Worth running the probe to see if there's signal, but not worth a Phase 1 commitment yet.

5. **Run-the-probe automation.** For each avenue, the probe is a SQL query + a sanity check against live brain. Could be scripted into a `research/probes/` directory and run nightly to track which avenues are accumulating signal over time.

---

## What I'm NOT in this memo

I'm not generating these for the sake of having ideas — each is something where I can see a concrete next step that brainctl's existing infrastructure supports. The criteria I applied:

- Connects to at least one existing brainctl subsystem
- Has a measurable probe against the live brain.db
- Has a Phase 1 schema sketch in mind (even if not fully designed)
- Doesn't conflict with the closed-loop architecture issue #116 audited

The ideas I rejected during writing (kept for reference, not durable):

- *"Glial cells / astrocytes as memory consolidation second layer"* — no clear brainctl analog, the abstraction doesn't map cleanly
- *"Quantum / Penrose-Hameroff microtubule consciousness"* — speculative even in neuroscience, not actionable in software
- *"Mirror neuron system"* — interesting but ToM already covers most of what would matter
- *"Cerebellar pontine nuclei as message-passing bottleneck"* — cerebellum subsystem already exists; pontine layer would be over-decomposition

If any of these become tractable later, the rejection log is in this section.

---

## Next actions

If/when you want to act on these:

1. Read avenues 1, 2, 5 first (highest tractability).
2. Pick one. Run its probe against live brain.db. Decide based on signal.
3. If go: spec a Phase 1 schema + 3-5 MCP tools, ship as one PR, same shape as tonight's chain.
4. If no-go: stash the probe result in `research/probes/<avenue>-<date>.md` for the next pass.

Each of these is ~2-4 hours of focused work for me. Or codex can take any single one with a tight prompt — the Phase 1 pattern is well-established now.
