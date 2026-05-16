# brainctl Brain-Region Coverage Audit

**Date:** 2026-05-15
**Triggered by:** user asking what's under-represented after the thalamus research swarm
**Method:** cross-reference subagent-8 codebase recon against canonical functional neuroanatomy

Verdict per region: ✅ well-modelled · 🟡 partial / under-wired · 🟥 missing · ⚫ N/A for a memory-system

## ✅ Well-modelled

| Region | What it does (bio) | brainctl analog |
|---|---|---|
| **Hippocampus (gross)** | episodic-to-semantic consolidation, SWR replay | `hippocampus.py`, `consolidation_run`, replay queue, lability windows |
| **Theory-of-Mind / TPJ-mPFC** | model other minds | `mcp_tools_tom.py`, perspective tables, belief_conflicts |
| **Belief system / dlPFC inference** | hold and revise probabilistic beliefs | `belief_*` tools, AGM-style credibility-weighted merge |
| **Reflexion / posterior parietal lesson-learning** | failure-driven self-correction | `reflexion_*` tools, CONTEXT_LOSS / HALLUCINATION / TOOL_MISUSE categories |

## 🟡 Partial — substrate exists but not wired or only one half present

| Region | What it does (bio) | brainctl state | Gap |
|---|---|---|---|
| **Brainstem / Ascending Reticular Activating System** | global arousal broadcast via diffuse fan-out | `neuromodulation_state` table holds org-level arousal/focus | Not wired into retrieval/admission. The proposed thalamus mode-broadcast layer is the missing fan-out. |
| **Locus Coeruleus (NE)** | global surprise / reset signal | `neurostate.norepinephrine` in proposed schema | No concrete LC-analog signal currently *fires* on prediction-error to reset attention. |
| **Nucleus Basalis (ACh)** | broaden receptive fields, raise responsiveness | Same — `neurostate.acetylcholine` proposed, not yet emitting | No actual cholinergic-mode admission-loosening tied to attended sectors. |
| **Hypothalamus / allostasis** | homeostatic set-points + drives | `mcp_tools_allostatic.py` — demand_forecast, allostatic_prime | Has *prediction*; has no set-points (need-states) that *generate drives*. The system can't "feel hungry for data" or "need consolidation." |
| **Amygdala** | rapid valence tagging, fear conditioning, one-shot threat learning | `affect_*` tools classify valence/arousal lexically | Affect is a classifier, not a memory modulator. No "this kind of input previously caused a problem → preemptively bias suppression on that channel" loop. No fast-track fear learning that bypasses W(m). |
| **Hippocampal subfields (DG / CA3 / CA1)** | DG = pattern separation, CA3 = pattern completion, CA1 = output | One flat hippocampus abstraction | `memory_search` is implicitly pattern-completion. There's no explicit **pattern-separation step at write time** deciding "store as distinct" vs "merge into existing." Memory dedup happens at the embedding-cosine level, which is the *wrong* end of the loop. |
| **Entorhinal cortex** | conceptual/temporal grid cells, the index between cortex and hippocampus | Temporal: `epochs`, `temporal_*` tools cover this side well | No **conceptual grid** — no learned coordinate space the way grid cells tile concept-space. Closest analog would be `vsearch` over embeddings, but that's not an indexing structure. |
| **Workspace / Global Neuronal Workspace** | bandwidth-limited shared broadcast | `mcp_tools_workspace.py`, `workspace_broadcasts` | Fixed salience threshold, no org_state coupling, no enforced bandwidth limit (any module can write). Proposed thalamus mode-broadcast fixes the coupling; the bandwidth limit (top-K per epoch) is still missing. |

## 🟥 Missing — no analog in the codebase

These are the gaps that matter, ranked by likely engineering value.

### 1. Basal Ganglia / Striatum — the action-selection gate

**Bio:** A learned, RL-driven gate that decides *which action / which working-memory update / which tool* to release through the thalamus. The canonical BG→thalamus→cortex loop is the architecture of working-memory updating (O'Reilly's PBWM). Dopamine from VTA/SNc provides reward prediction error that trains the gating policy.

**Missing in brainctl:** there is no "should I fire this tool / delegate to this agent / write this memory" *policy* that learns from outcome. `policy_*` tools exist but are static rules, not learned. Outcome annotations exist but no closed loop trains gating from them.

**Why it pairs with the thalamus proposal:** BG and thalamus are co-evolved. The thalamus subsystem decides what *can* surface; the BG decides what *will* surface from competing options. Without BG, the thalamus gate has no learned controller — every bias has to come from a hand-crafted source.

**Proposal sketch:** `basal_ganglia` subsystem — outcome-trained gating policy over (action, context) → release-or-suppress, with reward signal sourced from `outcome_annotate` and downstream task success. Maps cleanly to a multi-armed-bandit / contextual-bandit implementation.

### 2. Cerebellum — forward models and timing

**Bio:** Internal models that *predict* sensory consequences of actions, compare to actuals, and emit error signals that fine-tune motor and (per recent work) cognitive sequences. Anatomically isolated from cerebrum, computationally about prediction-vs-reality at sub-second timescale.

**Missing in brainctl:** there is no module that says "given that the agent just called tool X with args Y, here is what I expect the result to look like" and *compares* to the actual result to emit a discrepancy signal. The free-energy and reconsolidation systems do something similar for *memories*, but not for *actions*. Cerebellum-class prediction would be the layer that detects "the world isn't behaving like the model predicts" earlier and cheaper than full belief revision.

**Why it matters:** without a cheap forward model, every surprise has to climb the full perceptual-belief-update stack before it can affect behavior. Cerebellar predictions are fast, dumb, and shape downstream attention.

### 3. Insula — interoception (self-state awareness)

**Bio:** Maps the body's internal state into a conscious "felt" signal. Anterior insula does this at the cognitive level: subjective certainty, urgency, fatigue.

**Missing in brainctl:** the system has *no model of itself*. It can report stats (`stats`, `health`, `telemetry`), but those are external readouts. There is no internal signal that says "write pressure is high → bias toward consolidation" or "retrieval latency is climbing → broaden similarity threshold to reduce reranking cost." The thalamus mode proposal touches this at the agent level but not the system level.

**Proposal sketch:** `insula` subsystem — continuously summarizes brainctl's own internal state (queue depths, error rates, gate saturation, recent failure-mode count) into a low-dimensional "felt-state" vector that other subsystems can subscribe to.

### 4. Anterior Cingulate Cortex (ACC) — conflict / error detection

**Bio:** Real-time conflict and error monitor. Fires when expected ≠ actual, when two responses compete, when effort is high. Modulates control allocation downstream.

**Missing in brainctl:** belief_conflicts and ToM conflict tools detect *belief-level* conflict, but there's no in-the-moment monitor for *operational* conflict — e.g., "two agents are about to write contradictory memories in the same scope," "this retrieval contradicts the most recent belief on this entity," "the same query just returned different rankings on two consecutive calls." Reflexion catches these after they cause failure; ACC would catch them in-flight.

**Proposal sketch:** `acc` subsystem — a thin monitor that subscribes to memory_events, watches for contradiction patterns, and emits a conflict-event that the thalamus gate uses as a top-down bias source.

### 5. Default Mode Network (DMN) — self-referential simulation / mind-wandering

**Bio:** Active during rest; runs internal simulation, autobiographical memory, future projection, mind-wandering. Functions as offline imagination.

**Missing in brainctl:** `dream_cycle` and `dreams` exist as primitives but are shallow. There is no sustained "when idle, simulate plausible future scenarios using current beliefs and stored entities, and stash the useful ones." This is the layer that would do `world_predict` continuously instead of on-demand.

**Proposal sketch:** `dmn` subsystem — scheduled offline run that takes the top-N entities and recent decisions, generates counterfactual continuations, and writes the high-value ones as low-confidence speculative memories tagged for later validation.

### 6. PAG / Hypothalamic drives — homeostatic set-points

**Bio:** Hard-coded "needs" — hunger, thirst, threat, comfort. Generate drives that bias all downstream selection.

**Missing in brainctl:** allostatic_prime forecasts demand but doesn't define *needs*. The system has no equivalent of "I need fresh ingest from this project" or "I need to consolidate the last 48h before more writes arrive" expressed as a drive that biases everything else. Currently those decisions are scheduled externally (cron, user prompts).

**Proposal sketch:** `drives` subsystem — a small set of named needs with set-points and current levels (e.g., `consolidation_debt`, `belief_coverage`, `staleness`, `pii_pressure`). Each tick the system reads its own state and updates drive levels; high-drive states bias the thalamus mode and gate.

### 7. PFC architecture (sub-regions, not "the cortex" undifferentiated)

**Bio:** dlPFC = working memory and rule maintenance. vmPFC = value computation. OFC = outcome representation. ACC (above). Frontopolar = meta-cognitive monitoring.

**Missing in brainctl:** brainctl treats "the agent" (Claude/Hermes/OpenClaw) as a monolithic cortex. The PFC subdivisions correspond to real engineering distinctions the substrate could expose: dlPFC ≈ active task slot, vmPFC ≈ outcome-utility table, OFC ≈ realized-outcome log, frontopolar ≈ meta layer over an agent's own performance. brainctl could provide *named slots* for each that an agent fills and rereads.

This is less urgent than 1–3 because the LLM itself is doing PFC-ish work in-context. Worth flagging as a long-tail organization opportunity.

### 8. Reticular Formation (sleep architecture) — beyond mode switching

**Bio:** Controls wake/NREM/REM transitions, spindle generation, slow-wave coordination.

**Partial in brainctl:** the proposed thalamus mode controller has `consolidate` and `offline` modes; what's missing is a *staged* sleep architecture — NREM3 (slow waves, consolidation) → REM (cross-domain recombination) → wake. `dream_cycle` hints at REM but doesn't model the staging.

## ⚫ Not applicable for a memory system

- Primary sensory cortices (V1/A1/S1) — the LLM is the sensory front-end
- Motor cortex / spinal cord — tool-call layer plays this role
- Vagus nerve / autonomic — no physical body
- Olfactory bulb / piriform cortex — no relevant modality
- Brainstem cranial nuclei — no physical control surfaces

---

## Priority ranking for next investments

If I had to pick the next 3 brain-region subsystems to research and build, after the thalamus:

1. **Basal Ganglia** — single biggest gap. Pairs natively with the proposed thalamus. Turns brainctl from a *reactive* memory store into a *learned-policy* controller.
2. **Drives / hypothalamic homeostasis** — small, cheap, surprisingly high-leverage. Gives the system named needs that bias everything else without requiring more research per surface.
3. **Cerebellum (forward models)** — turns surprise from a slow belief-system event into a fast cheap signal that biases attention. Particularly useful as tool-call volume grows.

A 4th-tier wishlist (Insula, ACC, DMN, hippocampal subfields, EC grid coordinates) would benefit from another research swarm of the same shape as the thalamus one.

---

## Pointer back to the thalamus doc

This audit assumes the thalamus proposal at `docs/proposals/thalamus.md` is the next implementation. The BG proposal naturally follows it — BG and thalamus are not separable subsystems in biology and shouldn't be in brainctl either.
