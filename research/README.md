# Cognitive Enhancement Research — Engram

**Project:** Cognitive Architecture & Enhancement
**Author:** Engram (Memory Systems Lead)
**Deliverable for:** [COS-79](/COS/issues/COS-79)
**Date:** 2026-03-28

---

## Overview

Eight concrete algorithms targeting `brain.db` (SQLite with FTS5 + sqlite-vec).
Each module is runnable standalone and designed for integration into the consolidation cycle.

| # | Module | Algorithm | Key Metric |
|---|--------|-----------|------------|
| 01 | `01_spaced_repetition.py` | Exponential decay + recall boost | Confidence 0–1 |
| 02 | `02_semantic_forgetting.py` | Temporal class demotion/promotion | Class transitions |
| 03 | `03_knowledge_graph.py` | PageRank + BFS expansion | Edge weights |
| 04 | `04_attention_salience_routing.py` | Weighted salience scoring (FTS + vec) | Salience 0–1 |
| 05 | `05_consolidation_cycle.py` | Full sleep-cycle orchestrator | Cycle report |
| 06 | `06_contradiction_detection.py` | Negation patterns + supersession breaks | Conflict pairs |
| 07 | `07_emergence_detection.py` | Topic trending, agent drift, store health | Signal-to-noise |
| 08 | `08_context_compression.py` | Token-budget selection + redundancy pruning | Tokens used |

---

## Key Design Decisions

- **All SQL-first.** Every algorithm has a pure-SQL version for direct `brainctl` use and a Python wrapper for complex logic.
- **Dry-run support** on all mutation functions — safe to test without touching live data.
- **FTS5 primary, vec secondary.** Routing uses FTS BM25 when no query embedding is available; falls back to sqlite-vec for precision when embeddings exist.
- **Decay rates by temporal class.** Ephemeral: λ=0.5 (half-life 1.4d), short: λ=0.2, medium: λ=0.05, long: λ=0.01, permanent: no decay.
- **Salience formula:** 0.45×similarity + 0.25×recency + 0.20×confidence + 0.10×importance.

---

## Integration Path

```
consolidation_cycle.py (COS-82)
  ├── calls 01_spaced_repetition.run_decay_pass()
  ├── calls 02_semantic_forgetting.run_demotion_pass()
  ├── calls 06_contradiction_detection.find_contradictions()
  └── logs via events table (event_type='consolidation_cycle')

brainctl memory retrieve
  └── calls 04_attention_salience_routing.route_memories_fts()
      └── optionally calls 08_context_compression.compress_memories_for_context()
```

---

## Next Steps (COS-82)

1. Wire `05_consolidation_cycle.py` into a cron job (daily 03:00 UTC)
2. Integrate LLM summarizer into cluster consolidation (replace naive join)
3. Install sqlite-vec extension and enable vector routing
4. Connect `07_emergence_detection` to Hermes daily briefing
