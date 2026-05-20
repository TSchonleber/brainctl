# Proposal: The Ascending Reticular Activating System (ARAS) for brainctl

**Status:** Phase 1 design + implementation, branch `brain-regions-aras-phase-1`.
**Author:** Claude Opus 4.7 (overnight continuation after LC+NB Phase 1 ship)
**Date:** 2026-05-20
**Scope:** New subsystem. Sits **above** LC + NB — the brainstem-level global arousal broadcast that gates whether the rest of the neuromod surface is even responsive. Additive — no breaking changes. Phase 1 is inspection-only.

---

## TL;DR

ARAS is the reticular formation broadcaster that decides whether the brain is *available for processing at all*. It precedes (and modulates the responsiveness of) LC, NB, and every other cortical / subcortical structure. Anesthesia is functionally an ARAS shutdown. Waking, drowsiness, hyperarousal, sleep — all are ARAS states.

brainctl's `neuromodulation_state` and `bg_modulators` carry per-dial knobs (`tonic_da`, `lc_ne`, `serotonin`, now `acetylcholine`) but have **no global arousal axis** that gates whether the system is in a processing-receptive mode at all. The May 15 coverage audit explicitly flagged this: *"`neuromodulation_state` table holds org-level arousal/focus. Not wired into retrieval/admission."*

This proposal codifies ARAS as a thin first-class subsystem:

- `aras_state` (single row) — current sleep/wake mode + arousal level + phasic alertness
- `aras_transitions` — log of mode changes with cause
- `aras_triggers` — catalog of event classes that nudge arousal
- 5 MCP tools (`aras_status`, `aras_transition`, `aras_drive`, `aras_register_trigger`, `aras_history`)

Phase 1 ships schema + tools + tests, **no behavior change** to retrieval, write gates, LC, NB, or anything else. Phase 2 wires ARAS into the dispatch shadow consult to log "would-be" mode transitions from event patterns. Phase 3 lets ARAS actually modulate the downstream neuromodulator response (e.g., low arousal damps LC phasic firings; high arousal amplifies NB attention bursts). Phase 4 enforces.

## Architectural placement

```
┌─────────────────────────┐
│   ARAS (this PR)        │  global arousal / sleep-wake gate
│   sleep_wake_mode       │
│   arousal_level         │
│   phasic_alertness      │
└────────┬────────────────┘
         │ (Phase 3: gates the response of)
         ▼
   ┌─────────────────────┐    ┌─────────────────────┐
   │  LC (PR #121)       │    │  NB (PR #122)       │
   │  surprise → NE      │    │  attention → ACh    │
   └──────┬──────────────┘    └──────┬──────────────┘
          │                          │
          ▼                          ▼
   ┌─────────────────────────────────────┐
   │   bg_modulators                     │
   │   tonic_da, lc_ne,                  │
   │   serotonin, acetylcholine          │
   └─────────────────────────────────────┘
```

In two-speed-motif terms (issue #116 §4): ARAS is the *very* slow, system-wide background driver; LC/NB sit one layer down as the medium-speed phasic broadcasters; the per-dial state in `bg_modulators` is the substrate.

## Biological invariants encoded

1. **Tonic vs phasic separation.** ARAS firing has two distinct modes — sustained tonic drive that sets the global arousal baseline, and phasic pulses from external stimuli (novelty, threat, explicit alerts). brainctl's `aras_state` separates these as columns.

2. **Discrete sleep/wake regimes.** Arousal is not just a scalar — biology partitions it into qualitatively different regimes (NREM sleep, REM, drowsy, awake-relaxed, awake-focused, hyperalert). Each has different gating semantics. CHECK constraint on `aras_state.sleep_wake_mode`.

3. **Recovery from suppression takes time.** Going from low arousal back to high arousal is not instantaneous (this is the post-anesthesia recovery curve). Tracked via `aras_state.last_transition_at` so callers can compute a recency-weighted responsiveness.

4. **Specific event classes drive specific arousal deltas.** The seed `aras_triggers` catalog mirrors the LC `lc_triggers` and NB `nb_attention_targets` pattern.

## Phase 1 schema (migration 069)

```sql
CREATE TABLE aras_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  sleep_wake_mode TEXT NOT NULL DEFAULT 'awake_relaxed' CHECK(sleep_wake_mode IN (
    'nrem_sleep', 'rem_sleep', 'drowsy', 'awake_relaxed', 'awake_focused', 'hyperalert'
  )),
  arousal_level REAL NOT NULL DEFAULT 0.5 CHECK(arousal_level BETWEEN 0.0 AND 1.0),
  tonic_drive REAL NOT NULL DEFAULT 0.5 CHECK(tonic_drive BETWEEN 0.0 AND 1.0),
  phasic_alertness REAL NOT NULL DEFAULT 0.0 CHECK(phasic_alertness BETWEEN 0.0 AND 1.0),
  last_transition_at TEXT,
  last_drive_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (...)
);

CREATE TABLE aras_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transitioned_at TEXT NOT NULL DEFAULT (...),
  agent_id TEXT,
  from_mode TEXT NOT NULL,
  to_mode TEXT NOT NULL,
  reason TEXT,
  trigger_id INTEGER,
  arousal_before REAL,
  arousal_after REAL,
  notes TEXT
);

CREATE TABLE aras_triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  trigger_kind TEXT NOT NULL CHECK(trigger_kind IN (
    'novelty', 'threat', 'explicit_alert', 'consolidation_signal', 'idle_decay', 'other'
  )),
  default_arousal_delta REAL NOT NULL DEFAULT 0.05,
  default_target_mode TEXT,
  description TEXT,
  created_at TEXT NOT NULL DEFAULT (...)
);
```

Seeded triggers: `novel_query`, `high_pe_event`, `consolidation_complete`, `idle_30min`, `explicit_user_alert`.

## Phase 1 MCP tool surface

- `aras_status` — current state + last 5 transitions + recent trigger summary
- `aras_transition(to_mode, reason, agent_id)` — explicit mode change; writes `aras_transitions` row
- `aras_drive(trigger_name, magnitude, agent_id)` — phasic arousal pulse; updates `phasic_alertness` + may trigger automatic mode change above threshold
- `aras_register_trigger(name, trigger_kind, default_arousal_delta, ...)` — idempotent UPSERT
- `aras_history(limit, since, agent_id, from_mode, to_mode)` — paginated transitions

Phase 1 does **not** modulate LC/NB/retrieval. That's Phase 3.

## DoD

- Migration 069 applies cleanly to /tmp copy + live (with backup)
- 5 seed triggers + 1 aras_state row after migration
- 5 MCP tools registered + discoverable
- ≥6 tests passing
- Branch pushed, PR open, NOT merged to main
