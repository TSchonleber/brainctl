# Proposal: The Locus Coeruleus Subsystem for brainctl

**Status:** Design proposal, Phase 1 implemented as schema + read/CRUD tools.
**Authors:** Codex GPT-5 (implementation synthesis) following the brain-region subsystem pattern.
**Date:** 2026-05-20
**Scope:** New subsystem; additive and inspection-only in Phase 1. No dispatch, retrieval, write-gate, or neuromodulator behavior changes.

---

## TL;DR

brainctl already has two high-quality surprise sources: cerebellum prediction errors (`cerebellum_predictions.delta_forward`) and basal-ganglia TD errors (`bg_td_events.delta`). What it lacked was the small brainstem broadcaster that turns surprise into a global norepinephrine readiness signal. The locus coeruleus (LC) supplies that layer: a trigger catalog, an activation log, and a single current-state row that records whether the system is phasic-ready, tonically high, tonically mid, or tonically low.

Architecturally, LC sits between fast error detectors and the existing `bg_modulators.lc_ne` dial. Phase 1 only observes and records; Phase 2 will wire cerebellum/BG/novelty events into LC firing and then broadcast norepinephrine into `bg_modulators.lc_ne`.

```
cerebellum_predictions.delta_forward
bg_td_events.delta
memory_events novelty / observation
explicit user alert
          |
          v
   locus_coeruleus
   - lc_triggers
   - lc_firings
   - lc_state
          |
          v
bg_modulators.lc_ne  (Phase 2 write target; Phase 1 reads only)
```

---

## Convergent Principles

1. **Two firing modes, two control problems.** LC phasic bursts are brief, stimulus-locked gain increases; tonic level is the slower arousal background. brainctl models both separately: `lc_firings.mode` captures phasic versus tonic-shift events, while `lc_state.mode` captures the current operating regime.

2. **P3-like "something changed" signal.** LC tracks the orienting response to salient, novel, or unexpected stimuli. In brainctl, that maps to prediction errors, TD errors, novel entity observations, and explicit user alerts.

3. **Aston-Jones adaptive gain.** Mid tonic plus phasic bursts supports exploitation and focused task performance; high tonic favors exploration and labile attention; low tonic maps to drowsy disengagement. LC state therefore uses `tonic_low`, `tonic_mid`, `tonic_high`, and `phasic_ready`.

4. **Norepinephrine gates plasticity.** LC-NE does not encode content. It adjusts gain and learning readiness, including LTP/LTD sensitivity in downstream circuits. In brainctl, that downstream dial is already present as `bg_modulators.lc_ne`; LC owns the event semantics that will eventually set it.

5. **Trigger taxonomy matters.** "Surprise" is not one source. Phase 1 separates cerebellar prediction error, BG TD error, novelty/observation events, and explicit user alerts so Phase 2 can tune thresholds and default NE deltas independently.

---

## Architectural Placement

LC is an interrupt-style broadcaster, not a selector. Cerebellum and BG detect error in their own domains; LC decides whether the error is behaviorally salient enough to raise global gain.

```
                  fast prediction / value errors
                             |
          +------------------+------------------+
          |                                     |
          v                                     v
 cerebellum_predictions                 bg_td_events
 delta_forward                          delta
          |                                     |
          +------------------+------------------+
                             |
                             v
                    lc_triggers catalog
                    threshold + NE delta
                             |
                             v
                      lc_firings log
                             |
                             v
                        lc_state row
                             |
                             v
                bg_modulators.lc_ne
        Phase 2 writes; Phase 1 status reads only
```

The LC subsystem deliberately stays out of the current `mcp_server.py` shadow hookpoints. Phase 1 exposes manual firing and inspection tools so operators can verify schema shape and semantics before automatic wiring.

---

## Phase 1 Schema

Migration `067_locus_coeruleus.sql` adds three tables:

- **`lc_triggers`** - seedable trigger catalog. Each row defines an event class, its source table, threshold field/value, default NE delta, and description.
- **`lc_firings`** - timestamped activation log. Each row records agent, trigger, source event id, surprise magnitude, NE delta that would be applied, firing mode, context hash, and notes.
- **`lc_state`** - single-row current LC mode and NE reservoir, seeded as `id=1`, `mode='tonic_mid'`, `ne_reservoir=0.5`.

Seed triggers:

| Trigger | Source | Field | Threshold | Default NE delta |
|---|---|---|---:|---:|
| `cerebellum_high_pe` | `cerebellum_predictions` | `delta_forward` | 0.5 | 0.15 |
| `bg_large_td_error` | `bg_td_events` | `delta` | 0.6 | 0.10 |
| `novel_entity_sighting` | `memory_events` | `event_type` | null | 0.05 |
| `explicit_user_alert` | `other` | null | null | 0.20 |

Indexes support recent-status reads, agent-specific history, trigger-specific history, and source-table trigger lookup.

---

## Phase 1 MCP Tool Surface

Five tools ship under the `lc_*` namespace:

- **`lc_status(agent_id=None) -> dict`** - returns `lc_state`, current `bg_modulators.lc_ne` if present, and a last-24h firing summary.
- **`lc_fire(trigger_name, surprise_magnitude, agent_id=None, source_event_id=None, notes=None) -> dict`** - manually logs a phasic LC firing by trigger name. Phase 1 updates LC's own state and log only; it does not write `bg_modulators.lc_ne`.
- **`lc_register_trigger(name, source_table, threshold_field, threshold_value, default_ne_delta, description) -> dict`** - idempotent trigger UPSERT. Source table is validated against the Phase 1 taxonomy.
- **`lc_signal_history(limit=20, since=None, agent_id=None, trigger_id=None) -> list[dict]`** - recent firing history with optional filters and pagination limit.
- **`lc_set_mode(mode, reason=None) -> dict`** - validates and updates `lc_state.mode`. Used by future shadow consults and manual inspection.

---

## Phase 2/3/4 Sketch

**Phase 2 - Shadow wiring.** Listen to cerebellum `delta_forward`, BG `delta`, novel observation events, and explicit alert events. Insert `lc_firings` automatically when trigger thresholds pass. Mirror the computed NE delta into `bg_modulators.lc_ne` in shadow mode with audit records and no behavior change.

**Phase 3 - Gain coupling.** Use LC-NE as a read-path and write-path gain signal: broader retrieval under high tonic NE, lower admission thresholds for surprising sources, and higher salience precision for LC-tagged sectors.

**Phase 4 - Calibration and enforcement.** Learn trigger thresholds and NE deltas from downstream outcomes. Couple LC mode to BG action selection and thalamus salience after enough shadow data accumulates.

---

## DoD for Phase 1

- Migration `067_locus_coeruleus.sql` applies idempotently to a fresh DB, a `/tmp` copy of live `brain.db`, and live `brain.db` after backup.
- Seed triggers and the single `lc_state` row exist after migration.
- The five `lc_*` MCP tools are registered and discoverable from `agentmemory.mcp_server`.
- Focused pytest coverage verifies migration seeds, empty status, idempotent trigger registration, firing round-trip, mode validation, and history filtering.
- `MCP_SERVER.md`, `CHANGELOG.md`, and `brain_region_coverage.md` document LC Phase 1 without touching NB-owned files or Phase 2 hookpoints.
