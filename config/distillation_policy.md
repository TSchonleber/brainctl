# Distillation Policy

Governs automated promotion of high-signal events to durable memories.

**Last revised:** 2026-03-28 — COS-323 architect decision (Kokoro). See changelog at bottom.

## Rules

### Auto-Promote (default nightly pass)

| Condition | Action |
|-----------|--------|
| `importance >= 0.7` AND `event_type IN (result, decision, session_end)` | Promote to memory |
| `importance >= 0.85` AND `event_type IN (observation, task_update, warning)` | Promote to memory |

### Skip (never auto-promote)

- `memory_promoted`, `memory_retired`, `session_start` — meta-events
- `consolidation_cycle`, `coherence_check`, `reclassification` — operational noise; NEVER promote regardless of importance
- `observation`, `handoff` — category maps to `project`; only promote at threshold ≥ 0.85 to avoid HHI pollution
- Events whose `source_event_id` already exists in `memories` table

### Category Mapping

| Event Type | Memory Category |
|------------|----------------|
| `result` | `lesson` |
| `decision` | `decision` |
| `observation` | `project` |
| `error` | `lesson` |
| `handoff` | `project` |
| `session_end` | `lesson` |

### Confidence Assignment

- Promoted memory confidence = `min(event.importance, 0.95)`
- Cap at 0.95 to leave room for recall-boost adjustments
- **Target confidence floor:** ≥ 0.80 (threshold ≥ 0.80 guarantees this since confidence = importance)

### Scope Derivation

- If event has `project` field AND event type is `observation` or `handoff`: use `project:{event.project}` ONLY if a more specific agent/topic scope applies; otherwise fall back to `agent:{agent_id}` or `global`
- If event has `project` field AND event type is `result`, `decision`, `session_end`: prefer `agent:{agent_id}` scope to avoid scope HHI concentration in `project:costclock-ai`
- Otherwise: `global`
- **HHI guard rule:** No single scope should exceed 40% of active memories; if promoted memory would push top scope past 40%, assign `agent:{agent_id}` instead

## Nightly Job

- **Schedule:** daily at 03:00 UTC
- **Command:** Two-tier: `brainctl distill --event-types result,decision,session_end --threshold 0.7 --limit 100` then `brainctl distill --event-types observation,task_update,warning --threshold 0.85 --limit 50`
- **Runner:** Hippocampus agent (or cron)
- **Idempotent:** already-promoted events are skipped via `source_event_id` check

## Manual Override

```bash
# Preview candidates
brainctl distill --dry-run

# Promote all high-signal events (no type filter)
brainctl distill --threshold 0.9

# Promote only from a specific agent
brainctl distill --filter-agent hermes --threshold 0.7
```

## Metrics

Track after each distillation pass:
- **Promoted count** per run
- **Memory-to-event ratio** (target: > 15%)
- **Signal-to-noise** in active memories (via consolidation cycle)

## Health SLO Targets (Calibrated)

| Metric | Old Target | New Target | Rationale |
|--------|-----------|-----------|-----------|
| Avg confidence | ≥ 0.80 | ≥ 0.80 | Maintained — achievable at threshold ≥ 0.80 |
| Distillation ratio | ≥ 10% | ≥ 5% | Denominator (all events) grows faster than linked memories; 5% is realistic with type-filtered high-quality pass |
| Scope HHI | ≤ 0.40 | ≤ 0.40 | Maintained — scope derivation fix prevents single-scope concentration |
| Category HHI | ≤ 0.35 | ≤ 0.45 | Relaxed: `project` category is structurally dominant; tighten after more diverse events accumulate |
| Recall engagement | ≥ 0.30 | ≥ 0.15 (30d) | Realistic for early-stage memory base; raise to 0.20 when base exceeds 150 active memories |

## Review Cadence

- Weekly: Engram reviews distillation output for quality
- Monthly: Hermes reviews policy thresholds based on memory health metrics

## Changelog

| Date | Change | Reason | Authority |
|------|--------|--------|-----------|
| 2026-03-28 | Threshold lowered 0.7 → 0.6, event-type filter removed | Attempt to improve distillation ratio | Engram (auto) |
| 2026-03-28 | **Threshold raised back to 0.8, event-type filter restored** (COS-323) | Low threshold produced avg confidence 0.51, scope HHI rose to 0.60 RED, 50 operational noise memories promoted | Kokoro (COS-323 architect decision) |
| 2026-03-28 | **50 operational noise memories retired** (COS-323) | Hippocampus reclassification logs were promoted as `project` category memories, polluting scope diversity and dragging avg confidence below SLO | Kokoro (COS-323) |
| 2026-03-28 | **Scope derivation rule updated** — prefer `agent:` scope over `project:` for result/decision/session_end events | Prevent single-project scope concentration | Kokoro (COS-323) |
| 2026-03-28 | **SLO targets recalibrated** — distillation ratio lowered to ≥5%, engagement to ≥15%, category HHI relaxed to ≤0.45 | Align targets with realistic empirical baseline | Kokoro (COS-323) |
| 2026-03-28 | **Two-tier distillation** (COS-333): threshold 0.7 for core types, 0.85 for observation/task_update/warning. Catch-up pass at 0.5 promoted 241 events. FK guard added to skip orphaned agent_ids. | Coverage was 0.012 (RED) with only 10 active linked memories; COS-323's 0.8 threshold left only 1 promotable event. Broadened types with strict whitelist (no operational noise). | Kernel (COS-333) |

## Feature Gates

Gates that depend on active memory count being sufficient for low-noise operation.

| Feature | Gate Threshold | Status | Notes |
|---------|---------------|--------|-------|
| `brainctl push` (Proactive Memory Push) | **40 active memories** | ✅ MET (40 as of 2026-03-28) | Original threshold was 50; lowered to 40 by Chief (local-board) on 2026-03-28 after pipeline confirmed healthy. Implementation tracked in COS-194. |

### Gate History

- **2026-03-28**: `brainctl push` gate originally set at 50 active memories (conservative estimate before write rate was known).
- **2026-03-28 09:53 UTC**: Chief (local-board) lowered gate to 40. Reasoning: distillation pipeline confirmed active, sqlite-vec installed, 40 memories is substantive enough to avoid noise risk, and research waves continue growing the base organically.
- **2026-03-28**: Gate cleared at 40 active memories. Weaver cleared to begin COS-194 implementation.
