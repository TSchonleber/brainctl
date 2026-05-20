# Proposal: Hippocampal CA1 + Subiculum (completion of the trisynaptic loop)

**Status:** Phase 1 design + implementation, branch `brain-regions-ca1-phase-1`.
**Author:** Claude Opus 4.7 (overnight chain — 5th region tonight)
**Date:** 2026-05-20
**Scope:** Extension of migration 059 (DG+CA3). Completes the hippocampal trisynaptic loop. Phase 1 inspection-only.

---

## TL;DR

Migration 059 shipped Dentate Gyrus (pattern separation) + CA3 (pattern completion). The hippocampal trisynaptic loop is:

```
Entorhinal Cortex L2 → DG → CA3 → CA1 → Subiculum → Entorhinal Cortex L5 → out
                                  ↑
                              missing
```

CA1 is **computationally key** — it sits between CA3's pattern-completion output and Subiculum's cortical-bridge output. Its function: **compare incoming entorhinal input against CA3's recall output**. Match = familiarity confirmation. Mismatch = novelty detection / prediction error.

Subiculum is the hippocampal output structure. Without it, the hippocampal "memory trace" has no clean way to influence the rest of the brain — it's bottled up in CA3.

In brainctl terms, missing CA1+Subiculum means:
- No first-class **match/mismatch detector** for memory writes (would CA3's completion match what the entorhinal grid currently sees?)
- No first-class **hippocampal output channel** (writes just land in `memories` and get picked up by full-text search; biology has the hippocampus actively pushing certain content into cortex via Subiculum→EC deep layers)

Phase 1 ships schema for both subfields + 4 MCP tools. **No behavior change.** Phase 2 hooks CA1 into the existing hippocampus_dg_separate / hippocampus_ca3_complete pipeline. Phase 3 wires Subiculum into workspace_broadcasts. Phase 4 enforces.

## Architectural placement

```
              ┌──────────── existing ────────────┐
              │                                  │
   EC L2 ──→ DG ──→ CA3 (pattern completion)     │
              │      │                           │
              │      └──→ CA1 ◀── EC L3 ─────────┤
              │           ▼      (this PR)       │
              │      compare → match/mismatch    │
              │           │                      │
              │           ▼                      │
              │      Subiculum                   │
              │      (this PR)                   │
              │           │                      │
              │           ▼                      │
              │      EC L5/L6 → cortex            │
              └──────────────────────────────────┘
```

## Phase 1 schema (migration 071)

```sql
CREATE TABLE hippocampus_ca1_comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  compared_at TEXT NOT NULL DEFAULT (...),
  agent_id TEXT,
  memory_id INTEGER,                -- the memory under consideration
  ec_input_hash TEXT,               -- hash of the entorhinal-layer input (what's coming in now)
  ca3_output_hash TEXT,             -- hash of the CA3 pattern-completion output (what we'd recall)
  match_score REAL NOT NULL CHECK(match_score BETWEEN 0.0 AND 1.0),
  novelty_score REAL NOT NULL CHECK(novelty_score BETWEEN 0.0 AND 1.0),
  classification TEXT NOT NULL CHECK(classification IN ('match', 'mismatch', 'partial', 'ambiguous')),
  notes TEXT
);

CREATE TABLE hippocampus_ca1_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  recent_match_rate REAL DEFAULT 0.5,      -- EWMA of recent match_score
  recent_novelty_rate REAL DEFAULT 0.5,    -- EWMA of recent novelty_score
  total_comparisons INTEGER DEFAULT 0,
  last_comparison_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (...)
);

CREATE TABLE hippocampus_subiculum_outputs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  output_at TEXT NOT NULL DEFAULT (...),
  agent_id TEXT,
  memory_id INTEGER,
  ca1_comparison_id INTEGER REFERENCES hippocampus_ca1_comparisons(id),
  target_channel TEXT NOT NULL CHECK(target_channel IN ('cortex_general', 'workspace_broadcast', 'thalamus_relay', 'other')),
  output_strength REAL NOT NULL DEFAULT 0.5 CHECK(output_strength BETWEEN 0.0 AND 1.0),
  notes TEXT
);
```

## Phase 1 MCP tools

- `ca1_compare(memory_id, ec_input_hash, ca3_output_hash, classification)` — record one comparison; computes match_score/novelty_score from hash similarity (Phase 1 uses naive Hamming-distance hash compare; Phase 2 swaps to embedding-cosine)
- `ca1_status` — current state + last 5 comparisons + 24h aggregate
- `subiculum_output` — manually record a subiculum output event with target_channel
- `ca1_subiculum_history(limit, since, agent_id, classification)` — paginated comparison + output history

## DoD

- Migration 071 applies cleanly to /tmp + live (with backup)
- Single state row + tables exist
- 4 MCP tools registered + discoverable
- ≥6 tests passing
- Branch pushed, PR open, NOT merged to main
