# Proposal: The Habenula Subsystem for brainctl

**Status:** Phase 1 design + implementation, branch `brain-regions-habenula-phase-1`.
**Author:** Claude Opus 4.7 (overnight chain continuation)
**Date:** 2026-05-20
**Scope:** New subsystem. Pairs antisymmetrically with LC + the BG's TD-error bus.

---

## TL;DR

Lateral habenula (LHb) is the brain's **negative-reward-prediction-error** source. It fires when an expected positive outcome **fails to materialize** (omission) or when an aversive outcome arrives. Its primary projection target is the rostromedial tegmental nucleus (RMTg), which then **suppresses** dopaminergic VTA/SNc neurons. The functional consequence: habenula activity damps dopamine, disengages exploration, and drives task switching.

In brainctl, the BG's `bg_td_events` already broadcasts a TD-error signal (`δ = utility + γ·V(s') − V(s)`) that can be either sign. But there's no first-class structure for tracking **systematic negative outcomes** — repeated retrieval failures, repeated aversive valences, expected-good-results that didn't pan out. Issue #116's audit memo noted that brainctl learns from positive outcomes through bg_striatal_weights but doesn't have a dedicated "anti-reward" channel that drives disengagement, task abandonment, or exploration cessation.

This proposal codifies habenula as a thin first-class subsystem that:

- Logs negative outcome events specifically (separately from the general `bg_td_events` stream)
- Tracks running disappointment / aversive-event counters per (agent, context)
- Provides a `tonic_da` damping signal that the existing BG-thalamus modulator cascade can read in Phase 2
- Pairs antisymmetrically with LC: LC fires on positive surprise (high |+δ|), habenula fires on negative surprise (high |−δ|) or expected-positive omission

Phase 1 ships schema + 5 MCP tools + tests. **No behavior change** — does not yet damp `bg_modulators.tonic_da`. That's Phase 3.

## Architectural placement

```
       ┌────── LC (PR #121) ─────────┐    ┌────── Habenula (this PR) ──────┐
       │  fires on +surprise / NE   │    │  fires on −surprise / aversion │
       │  → bg_modulators.lc_ne     │    │  → Phase 3 damps tonic_da      │
       └────────────────────────────┘    └────────────────────────────────┘
                       │                                  │
                       │                                  │
                       └────────────┬─────────────────────┘
                                    ▼
                         ┌────────────────────┐
                         │  bg_td_events bus  │
                         │  (sign-agnostic δ) │
                         └────────────────────┘
```

Habenula is NOT the same as a negative δ in bg_td_events. The TD signal already supports negative δ. What habenula adds is:

1. **Expected-positive omission detection** — δ ≈ 0 isn't enough; we need "the prediction said *positive*, the observation gave *neutral or worse*"
2. **Aggregation across events** — sustained disappointment looks different from one bad TD
3. **A dedicated channel for disengagement triggers** — agents/contexts where the agent should *stop trying that retrieval pattern*
4. **Asymmetric coupling to LC** — habenula and LC together cover the full ±PE space; together they're the candidate signal for the Phase 4 enforcement flip

## Biological invariants encoded

1. **Negative-RPE coding.** LHb neurons phasically activate on negative-RPE events (Matsumoto & Hikosaka 2007). brainctl schema: `habenula_firings.signed_pe` is the source of truth, always ≤ 0.
2. **Reward omission ≠ punishment.** Both fire habenula but with different downstream consequences. `habenula_firings.event_kind` distinguishes `omission` from `aversive`.
3. **Tonic vs phasic.** Like LC and ARAS, habenula has tonic baseline and phasic bursts. Tracked in `habenula_state`.
4. **DA-suppression effect proportional to integrated activity.** A single bad event doesn't kill the whole reward circuit — sustained or extreme activity does. Phase 3 implementation will use an exponentially-decayed running average; Phase 1 just records the events.

## Phase 1 schema (migration 070)

```sql
CREATE TABLE habenula_triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  event_kind TEXT NOT NULL CHECK(event_kind IN ('omission', 'aversive', 'repeated_failure', 'other')),
  default_pe REAL NOT NULL DEFAULT -0.1 CHECK(default_pe <= 0.0),
  description TEXT,
  created_at TEXT NOT NULL DEFAULT (...)
);

CREATE TABLE habenula_firings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fired_at TEXT NOT NULL DEFAULT (...),
  agent_id TEXT,
  trigger_id INTEGER REFERENCES habenula_triggers(id),
  event_kind TEXT NOT NULL CHECK(event_kind IN ('omission', 'aversive', 'repeated_failure', 'other')),
  signed_pe REAL NOT NULL CHECK(signed_pe <= 0.0),
  context_hash TEXT,
  source_event_id INTEGER,
  notes TEXT
);

CREATE TABLE habenula_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  tonic_activity REAL NOT NULL DEFAULT 0.0,
  phasic_burst REAL NOT NULL DEFAULT 0.0,
  rolling_disappointment_24h INTEGER NOT NULL DEFAULT 0,
  last_firing_at TEXT,
  suggested_da_damp REAL NOT NULL DEFAULT 0.0,
  updated_at TEXT NOT NULL DEFAULT (...)
);
```

Seeded triggers: `reward_omission`, `retrieval_failure`, `repeated_low_utility`, `aversive_valence`, `task_abandoned`.

## Phase 1 MCP tools

- `habenula_status` — current state + last 5 firings + 24h aggregate
- `habenula_fire(trigger_name, signed_pe, agent_id, context_hash, ...)` — record a negative event
- `habenula_register_trigger` — idempotent UPSERT
- `habenula_history(limit, since, agent_id, event_kind)` — paginated firings
- `habenula_reset(agent_id)` — manually zero out tonic/phasic for an agent (admin-mode disengagement-cooldown)

## DoD

- Migration 070 applies cleanly to /tmp + live (with backup)
- Schema-version 70 row present
- 5 seed triggers + single state row
- 5 MCP tools registered + discoverable
- ≥7 tests passing
- Branch pushed, PR open, NOT merged to main
