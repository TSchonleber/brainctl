# Proposal: The Nucleus Basalis Subsystem for brainctl

**Status:** Phase 1 design (this document) + Phase 1 implementation (parallel commits on branch `brain-regions-nb-phase-1`). Pairs with the Locus Coeruleus subsystem (branch `brain-regions-lc-phase-1`, codex parallel track) — NE + ACh are the dual gain/attention control axes and should be reviewed together.
**Authors:** Claude Opus 4.7 (overnight autonomous chain), reading off the May 15 brain-region coverage audit
**Date:** 2026-05-20
**Scope:** New subsystem. Sits alongside thalamus, BG, cerebellum, and the just-shipped LC. Additive — no breaking changes. Phase 1 is inspection-only.

---

## TL;DR

brainctl's `bg_modulators` table holds three neuromod dials — `tonic_da` (BG actor's exploration knob), `lc_ne` (LC-analog broadcaster, now also written by the LC subsystem in PR `brain-regions-lc-phase-1`), and `serotonin` (raphe-analog time-horizon dial). The coverage audit (`docs/proposals/brain_region_coverage.md`) explicitly flagged a fourth that's missing: **acetylcholine**, the basal-forebrain cholinergic broadcast that raises cortical gain on **attended** channels.

Locus coeruleus broadcasts NE on *surprise* — broadly, indiscriminately, "something just changed." Nucleus basalis broadcasts ACh on *attention* — narrowly, target-locked, "this is the channel I'm working on now." NE widens the aperture; ACh sharpens the focus inside it. Both are necessary, neither is sufficient, and they antagonize as much as they compose. NE without ACh produces stimulus-locked alarm; ACh without NE produces tunnel-vision exploitation. brainctl needs both.

This proposal codifies NB as a first-class subsystem with:

- A `nb_attention_targets` catalog that maps brainctl thalamic sectors (and other channel-like surfaces — agents, scopes, intent classes) onto per-target ACh gain multipliers.
- A `nb_firings` log of cholinergic broadcasts (which target, magnitude, who fired it).
- A `nb_state` single-row reservoir tracking the current global ACh level + last attended target.
- An `acetylcholine` column added to `bg_modulators` (the 4th dial) so the existing cascade infrastructure (commit `32c466e`) can extend to ACh later.
- 5 MCP tools for Phase 1 inspection: `nb_status`, `nb_fire`, `nb_attend_sector`, `nb_register_target`, `nb_signal_history`.

Phase 1 lands the data tables, MCP surface, and seed catalog. **No behavior change** to retrieval, write gates, or any existing subsystem. Phase 2 (separate PR, daytime work) wires NB into the shadow consult pipeline at `mcp_server.py:3265` to fire on thalamic sector activations above threshold and broadcast ACh delta. Phase 3 closes the loop (ACh modulates retrieval admission). Phase 4 enforces.

## Architectural placement

```
                                                 ┌──── bg_modulators ────┐
                                                 │ tonic_da              │
                  ┌── LC (PR sibling) ──── lc_ne ─┤ lc_ne                 │
                  │                              │ serotonin             │
   cerebellum ────┤                              │ acetylcholine (new) ──┘
   bg_td_events ──┤                                          ▲
                  │                                          │  (Phase 2: cascade)
                  │                                          │
                  └──────────── NB (this PR) ────────────────┘
                                          ▲
                                          │  (Phase 2: shadow consult)
                                          │
                                  thalamus_salience above threshold
                                  attention-grabbing entity sightings
                                  explicit task focus changes
```

In two-speed-motif terms (the recurring memo §4 pattern across thalamus / BG / cerebellum): LC is the fast feedforward surprise broadcaster; NB is the slower modulatory attention-locker. LC fires reflexively; NB commits.

## Convergent principles from the neuroscience

*(Compressed for proposal-length; full primary-source citations live in the brain-region coverage audit and the issue #116 source memo.)*

1. **Basal-forebrain anatomy is mostly cholinergic projection.** Mesulam's Ch4 nuclei (nucleus basalis of Meynert + diagonal band of Broca + medial septum) project broadly to cortex via myelinated axons. The cortex's cholinergic supply is almost entirely basal-forebrain in origin. Damage produces Alzheimer-class attentional deficits — well before declarative memory deficits.

2. **Phasic vs. tonic ACh is the right cut.** Like LC's two modes, NB has a tonic baseline (sustained low ACh = global cortical readiness) and phasic bursts (target-locked spikes = focused processing). The two modes are dissociable; tonic drives wakefulness, phasic drives attention.

3. **ACh widens what's attended, narrows what's not.** Cholinergic boost on attended channels strengthens feedforward signal (cortex → cortex) and dampens recurrent / top-down expectations. The computational reading: ACh signals "trust the input on this channel more than the prior right now." Yu and Dayan's "uncertainty about cause" framing.

4. **NB fires on attention SHIFTS, not steady attention.** A target that's been attended for a while requires less ACh; the firing is the transition signal. Pairs naturally with brainctl's thalamic mode-switch events.

5. **ACh + NE = the gain matrix.** ACh raises gain on attended channels (selective); NE raises gain everywhere (broad). They overlap on attended-and-surprising input (multiplicative), which is precisely the signal type that should dominate retrieval.

## Phase 1 schema

```sql
-- Migration 068: nucleus basalis Phase 1 — schema + seed catalog
-- Pairs with migration 067 (locus coeruleus). NB-ACh complements LC-NE
-- as the dual gain/attention control axes.

-- Catalog of channels NB can attend to. Pre-seeded with brainctl's
-- thalamic sectors so the read tools have something to return on
-- Day 1; new targets registered idempotently via tool_nb_register_target.
CREATE TABLE IF NOT EXISTS nb_attention_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    channel_kind TEXT NOT NULL CHECK(channel_kind IN (
        'thalamic_sector', 'agent_scope', 'intent_class', 'entity_type', 'other'
    )),
    default_ach_gain REAL NOT NULL DEFAULT 0.10 CHECK(default_ach_gain BETWEEN 0.0 AND 1.0),
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    last_attended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nb_targets_kind ON nb_attention_targets(channel_kind);

-- Log of NB firings (cholinergic broadcasts). Each row = one phasic
-- ACh burst directed at a target.
CREATE TABLE IF NOT EXISTS nb_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    target_id INTEGER NOT NULL,
    target_source_event_id INTEGER,       -- loose, no FK
    attention_magnitude REAL NOT NULL,    -- 0..1, how strongly NB committed
    ach_delta_applied REAL NOT NULL,      -- the actual ACh delta written
    mode TEXT NOT NULL CHECK(mode IN ('phasic', 'tonic_shift')),
    context_hash TEXT,
    notes TEXT,
    FOREIGN KEY (target_id) REFERENCES nb_attention_targets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nb_firings_recent ON nb_firings(fired_at);
CREATE INDEX IF NOT EXISTS idx_nb_firings_agent ON nb_firings(agent_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_nb_firings_target ON nb_firings(target_id, fired_at);

-- Single-row reservoir + current attention focus.
CREATE TABLE IF NOT EXISTS nb_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'tonic_mid' CHECK(mode IN (
        'phasic_locked', 'tonic_high', 'tonic_mid', 'tonic_low'
    )),
    ach_reservoir REAL NOT NULL DEFAULT 0.5 CHECK(ach_reservoir BETWEEN 0.0 AND 1.0),
    last_attended_target_id INTEGER,
    last_phasic_at TEXT,
    last_tonic_shift_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (last_attended_target_id) REFERENCES nb_attention_targets(id)
);
INSERT OR IGNORE INTO nb_state (id, mode, ach_reservoir) VALUES (1, 'tonic_mid', 0.5);

-- Seed: pre-populate the four thalamic sectors brainctl already uses
-- (from migration 050 / thalamic_relays.sector). Other channels
-- (agent_scope, intent_class, entity_type) registered later via
-- nb_register_target as the operator decides what to attend to.
INSERT OR IGNORE INTO nb_attention_targets (name, channel_kind, default_ach_gain, description) VALUES
    ('cognitive', 'thalamic_sector', 0.15, 'planning, reasoning, deliberation'),
    ('episodic', 'thalamic_sector', 0.10, 'event recall and timeline'),
    ('semantic', 'thalamic_sector', 0.08, 'concept / fact retrieval'),
    ('pii_sensitive', 'thalamic_sector', 0.20, 'PII / credential / wallet content — high attention so the W(m) gate sees it');

-- Add the 4th neuromod dial to bg_modulators if not present.
ALTER TABLE bg_modulators ADD COLUMN acetylcholine REAL NOT NULL DEFAULT 0.5;
```

*(That ALTER will fail if the column is already there — guard in the actual migration with the standard ADD-COLUMN-IF-NOT-EXISTS-via-schema-check idiom used elsewhere in brainctl migrations. See migration 024 for the canonical pattern.)*

## Phase 1 MCP tool surface

All Phase 1 tools are inspection / CRUD only. **No behavior change** to existing retrieval or write gates.

- `tool_nb_status(agent_id=None) → dict` — returns the `nb_state` row + last-24h firing summary (count, mean attention_magnitude, mean ach_delta, mode transitions) + the most recent 5 firings.
- `tool_nb_fire(target_name, attention_magnitude, agent_id=None, source_event_id=None, notes=None) → dict` — manually fires NB at the named target. Inserts `nb_firings` row, updates `nb_state.last_attended_target_id` + `last_phasic_at`. Does NOT update `bg_modulators.acetylcholine` in Phase 1 — that's Phase 2.
- `tool_nb_attend_sector(sector_name, attention_magnitude, agent_id=None) → dict` — convenience wrapper that resolves the named thalamic sector to a target_id, then calls `nb_fire`.
- `tool_nb_register_target(name, channel_kind, default_ach_gain, description) → dict` — idempotent UPSERT on `nb_attention_targets`. Validates `channel_kind` against the CHECK constraint.
- `tool_nb_signal_history(limit=20, since=None, agent_id=None, target_id=None) → list[dict]` — paginated firing history with optional filters.

## Phase 1 DoD

- Migration 068 applied to live brain.db with backup at `~/agentmemory/backups/brain.db.pre-nb-*.db`
- `nb_attention_targets` has 4 seeded thalamic sectors after migration
- `nb_state` has the single seed row (id=1, mode='tonic_mid', ach_reservoir=0.5)
- `bg_modulators` has the new `acetylcholine REAL DEFAULT 0.5` column
- `src/agentmemory/mcp_tools_nucleus_basalis.py` registered in `mcp_server.py` dispatch
- `MCP_SERVER.md` has a "Nucleus Basalis" category section
- `tests/test_mcp_tools_nucleus_basalis.py` ≥ 5 tests passing
- Branch `brain-regions-nb-phase-1` pushed; PR open
- `docs/proposals/brain_region_coverage.md` flips NB to ✅ (Phase 1 footnote)
- CHANGELOG [Unreleased] has the NB entry

## Phase 2/3/4 sketch (NOT in this PR)

**Phase 2 — shadow consult.** Hook into `mcp_server.py:3265` so that:
- `thalamic_salience` rows above the per-sector default threshold trigger an automatic `nb_fire` for that sector.
- Each fire writes the proposed ACh delta into a shadow log but does NOT yet update `bg_modulators.acetylcholine`.
- The shadow log records what NB *would* have broadcast, so Terrance can audit the trigger calibration against real workloads before flipping enforcement.

**Phase 3 — closed-loop.** `bg_modulators.acetylcholine` actually moves on NB fires. The thalamus → BG modulator cascade (commit `32c466e`) is extended to include ACh: high tonic ACh narrows thalamic mode toward focused; low tonic ACh broadens toward exploratory. Reciprocal LC/NB coupling lands here too — high LC firing (large NE delta) transiently widens NB's attention bandwidth.

**Phase 4 — enforcement.** ACh-weighted gain actually modulates retrieval admission and the W(m) write gate. Same pattern as the BG enforcement flip will follow: requires 4+ weeks of operational data to calibrate thresholds.

## Coordination notes

- This branch (`brain-regions-nb-phase-1`) and the codex parallel branch (`brain-regions-lc-phase-1`) **share** the following files and must touch them additively:
  - `CHANGELOG.md` — append a separate `### Added — Nucleus Basalis Phase 1` section under `## [Unreleased]`; do not overwrite the LC section codex wrote.
  - `MCP_SERVER.md` — append a "Nucleus Basalis" section after the "Locus Coeruleus" section.
  - `docs/proposals/brain_region_coverage.md` — update the NB row only.
- On merge conflicts in these files: both sides are additive, manual merge.
- Migration numbers are partitioned: 067 = LC, 068 = NB. No collision.
