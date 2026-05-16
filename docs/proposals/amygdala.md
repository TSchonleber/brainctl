# Proposal: The Amygdala Subsystem for brainctl

**Status:** Design proposal. Fourth brain-inspired subsystem after thalamus, basal ganglia, and cerebellum (all shipped 2026-05-15 evening).
**Authors:** Claude Opus 4.7 (synthesis) over an 8-agent amygdala research swarm
**Date:** 2026-05-15
**Scope:** New subsystem. Additive — no breaking changes.

---

## TL;DR

brainctl has affect classification (`affect_classify` returns valence/arousal/dominance from text in ~1ms via VADER-style lexicons) but **the signal goes nowhere**. Per the recon agent's finding: `consolidation_priority()` in `affect.py:495` is dead code — never called from hippocampus. There is no per-entity valence accumulation, no extinction over time, no per-agent threat tracking, no learning from affect context.

The amygdala subsystem closes those loops. It is the **rapid one-shot valence/threat tagging layer** that turns ephemeral affect classifications into durable per-entity / per-agent valence scores, boosts emotional memory consolidation, fires reconsolidation windows on retrieval, and provides extinction overlays (not erasure) when previously-aversive contexts turn safe.

**The four key novel-vs-existing properties:**
1. **One-shot learning** — single high-arousal event commits a durable trace. Mirrors LA Hebbian CS-US convergence. brainctl currently has no one-shot path: BG learning is gradient-based with eligibility traces; cerebellum is supervised LTD; this is different.
2. **Extinction as overlay, not erasure** — when a previously-aversive context turns safe, install a context-keyed inhibitory gate over the original tag rather than overwriting it. Mirrors ITC biology and prevents the over-erasure failure mode.
3. **Two-path architecture** — fast "low road" (raw features → coarse provisional tag) + slow "high road" (full context → refined tag) running in parallel. The fast path can commit before the slow path resolves.
4. **Modulator, not storer** — per McGaugh: the amygdala does NOT store memories, it MODULATES storage elsewhere. So our amygdala outputs are *modulation signals* fed into hippocampus replay_priority, BG TD bus, thalamus salience, and workspace ignition — never a memory store of its own.

---

## Convergent principles from the 8-agent swarm

1. **Pallial/subpallial split** (BLA = associative/learning, cortex-like glutamatergic; CeA = expression/output, striatum-like GABAergic). Two-stage architecture is mandatory — input nucleus vs output nucleus.
2. **Coincidence-gated single-shot learning** at LA. NMDA-dependent LTP at CS-US convergence. AMPA receptor trafficking is the proximate strength change. CREB-driven transcription locks it in.
3. **Reconsolidation labile windows** — every retrieval reopens the trace for writing within ~1h. Anisomycin during the window abolishes memory; outside the window has no effect.
4. **Extinction = competing inhibitory association, not erasure** — IL/vmPFC → ITC → CeA gating. Spontaneous recovery, renewal, reinstatement all prove the original trace persists.
5. **Bidirectional valence** — not just fear. BLA→NAc projection drives appetitive; BLA→CeA drives defensive. The functional primitive is "is this important and what to do?"
6. **Two-path input** — low road (thalamus → LA, ~12-40ms) vs high road (thalamus → cortex → LA, slower but precise). Evolution's accuracy tax for latency.
7. **McGaugh modulation hypothesis** — amygdala doesn't store, it modulates consolidation elsewhere via NE/cortisol. Output → hippocampus replay_priority, cortex LTP gain, striatum DA modulation.
8. **Co-activation retroactive tagging** — high-arousal events retroactively boost memory for neutral items co-active in a ~2h window. Behavioral tag-and-capture (Moncada & Viola).
9. **Per-agent trust/threat** — Adolphs, Phelps work. Amygdala carries population-level "consensus untrustworthiness" priors over faces, updated by direct experience (intent-driven, not coincidence-driven).
10. **vmPFC → ITC inhibitory override** — top-down regulation works through dedicated gating cells, not direct subtraction. Override authority is earned per context.
11. **Inverted-U on arousal** — saturating nonlinearity prevents single extreme events from monopolizing replay. Trauma is the failure mode of unbounded gain.
12. **Failure modes**: PTSD (over-consolidation + extinction-recall failure), anxiety (over-generalization), Urbach-Wiethe (absent valence → indiscriminate trust), Klüver-Bucy (collapsed valuation → indiscriminate approach), psychopathy (domain-selective blunting), depression (mood-congruent retrieval bias).

---

## What brainctl has today (recon)

| Component | File:line | Wired? |
|---|---|---|
| `classify_affect()` (valence/arousal/dominance) | `affect.py:303` | yes (returns dict) |
| `arousal_write_boost()` (multiplier on W(m) worthiness) | `affect.py:472` | yes (used in W(m)) |
| `consolidation_priority()` (returns [0.0, 2.0]) | `affect.py:495` | **DEAD CODE** — never called |
| `affect_log` table | `init_schema.sql:1677` | logging only |
| Per-entity valence accumulation | — | **MISSING** |
| Extinction (context-keyed safe-overlay) | — | **MISSING** |
| Per-agent trust/threat dimension on `agent_perspective_models` | — | **MISSING** |
| Reconsolidation lability on retrieval | `mcp_tools_consolidation.py:24` `_LABILITY_THRESHOLD = 0.35` | yes for cosine PE; **MISSING** for affect |
| Workspace ignition driven by amygdala salience | `workspace_broadcasts` | currently fixed threshold |
| `replay_priority` boost from affect | `memories.replay_priority` | **MISSING** — function exists but unused |

**Diagnosis:** the substrate is ~80% built. The amygdala subsystem connects existing components into a learning loop.

---

## Subsystem design

### Two-stage architecture (BLA-analog + CeA-analog)

- **BLA-analog (learning)** — accumulates per-entity / per-agent / per-context valence tags from observations. Owns the durable store.
- **CeA-analog (expression)** — emits modulation signals to other subsystems: replay_priority boost, thalamic salience bias, workspace broadcast salience, BG TD-error supplement.

In code: same two-table split. `amygdala_valence_tags` is the BLA store (entity/agent + valence + arousal + n_updates). The expression layer is a set of *helper functions* that read tags and emit signals — no separate output store, mirroring biology (CeA outputs are inhibitory connections, not storage).

### Five sectors for two-path input

Each input arrives via two paths:

- **Low road (fast)** — at memory_add / event_add, compute affect from text immediately, apply provisional valence tag.
- **High road (slow)** — during consolidation cycles, re-process with full entity context + retrieved memories, refine the tag.

Phase 1 ships only the low road; Phase 2 adds the high road.

### Extinction gates (ITC-analog)

When a previously-aversive entity is encountered repeatedly without aversive context, install an `amygdala_extinction_gate` row: `(entity_id, context_hash, suppression_level, installed_at)`. At read time, the effective valence is `raw_valence × (1 - max(extinction_suppression for matching context))`. Original tag preserved; gate is context-keyed.

### Reconsolidation lability

When `amygdala_query_valence(entity_id)` is called, mark the tag as labile for 1h. Subsequent `amygdala_tag(entity_id, ...)` calls within that window do *additive* updates with elevated learning rate (one-shot regime). Outside the window, updates use the normal incremental rate.

### Bounded gain (inverted-U)

Tag updates pass through `saturating_tanh(arousal × valence_delta)` so a single extreme event can move the tag at most ±0.5. Prevents PTSD-mode lock-in.

---

## Schema additions

Migration 058. Additive.

```sql
-- Per-entity (and per-agent) valence accumulation. BLA-analog associative store.
CREATE TABLE amygdala_valence_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('entity','agent','context')),
    target_id TEXT NOT NULL,
    valence REAL NOT NULL DEFAULT 0.0,
    arousal REAL NOT NULL DEFAULT 0.0,
    n_updates INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    labile_until TEXT,
    UNIQUE (target_kind, target_id)
);

-- Audit trail of valence updates — every learning event logged.
CREATE TABLE amygdala_valence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    valence_delta REAL NOT NULL,
    arousal REAL NOT NULL,
    source_memory_id INTEGER,
    source_event_id INTEGER,
    reason TEXT,
    learning_rate REAL,
    fired_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- Extinction gates: context-keyed inhibitory overlays. ITC-analog.
CREATE TABLE amygdala_extinction_gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    suppression_level REAL NOT NULL DEFAULT 0.5,
    n_safe_exposures INTEGER NOT NULL DEFAULT 1,
    installed_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE (target_kind, target_id, context_hash)
);
```

---

## MCP tool surface (Phase 1)

Four tools:

- **`amygdala_status(target_kind?, top_n=10)`** — Snapshot: tag counts, top-N |valence|, extinction gate counts, recent valence events.
- **`amygdala_tag(target_kind, target_id, valence, arousal, reason?, source_memory_id?)`** — One-shot or incremental tag. Applies saturating update. Opens lability window. Logs event.
- **`amygdala_query_valence(target_kind, target_id, context_hash?)`** — Returns effective valence: raw_valence × (1 - max(extinction_gates matching context)). Marks the tag labile for 1h.
- **`amygdala_extinguish(target_kind, target_id, context_hash, suppression_level=0.5)`** — Install a context-keyed extinction gate. Does NOT erase the underlying tag.

---

## Hookpoints (Phase 2+)

Phase 1 is the schema + 4 tools, manual usage only. Phase 2 wires automatic tagging:

1. **`memory_add` hook** — when affect classifier flags high-arousal, auto-tag any entities mentioned in the memory content (entity extraction is in `_impl.py`).
2. **`consolidation_run` hook** — read `amygdala_valence_tags`, boost `memories.replay_priority` by `consolidation_priority(arousal, valence)` for memories about high-valence entities.
3. **`outcome_annotate` hook** — on failure outcomes, auto-tag the agent that produced the failure as slightly threat-positive (with saturating update so one bad outcome doesn't dominate).
4. **`thalamus_salience` hook** — read amygdala valence for the candidate's entity/agent and apply a precision multiplier (high arousal → boost salience; high threat → also boost via separate channel).
5. **`workspace_broadcasts` hook** — when |valence| exceeds threshold, force-fire a workspace broadcast even if normal salience is below ignition threshold.

---

## Failure-mode design

| Pathology | brainctl analog | Mitigation |
|---|---|---|
| PTSD over-consolidation | Single extreme event saturates a tag and never decays | Saturating tanh on updates caps single-event delta at ±0.5; n_safe_exposures triggers extinction gates over time |
| Anxiety over-generalization | Tag spreads to similar entities without bound | Tags are per-target_id, not similarity-clustered; explicit extinction gates per context |
| Urbach-Wiethe (absent) | Subsystem disabled | Phase 1 is opt-in; if not used, brainctl behaves as before |
| Klüver-Bucy (indiscriminate) | No selective valuation | Tags ARE selective per-entity; this isn't the failure mode for our design |
| Psychopathy (domain blunting) | Only some kinds of bad outcomes update tags | `reason` field on every event; can audit valence coverage by category |
| Depression (mood-congruent bias) | Tags affect retrieval but retrieval doesn't feed back | Phase 1 doesn't wire to retrieval; Phase 2 wiring must include the audit `affect_log` to prevent loops |

---

## Rollout plan

**Phase 1** (this proposal): Schema + 4 tools. Manual usage. No behavior change.

**Phase 2:** Auto-tag on memory_add. Wire `consolidation_priority()` (currently dead code) into hippocampus replay_priority. Wire amygdala valence into thalamus_salience.

**Phase 3:** workspace ignition by high |valence|; extinction-gate auto-install when n_safe_exposures > threshold.

**Phase 4:** Per-agent threat tracking wired into `agent_perspective_models` (extends ToM with valence dimension).

---

## Sources

Synthesized from 8 research subagents: anatomy (BLA vs CeA, ITC, low/high road, lateralization), fear conditioning & one-shot learning, amygdala-hippocampus consolidation modulation, social cognition & per-agent trust, computational models (PVLV, John Emotional Gatekeeper, Balkenius), vmPFC regulation, failure modes (PTSD/anxiety/Urbach-Wiethe/Klüver-Bucy/psychopathy/depression), and brainctl architecture recon. Citations preserved in brainctl event log under tonight's task notifications.
