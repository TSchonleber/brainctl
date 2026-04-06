# Memory Store Health SLOs
## Research Report — COS-202
**Author:** Prune (Memory Hygiene Specialist)
**Date:** 2026-03-28
**Target:** brain.db — Defining what "healthy" organizational memory looks like and how to measure it operationally

---

## Executive Summary

The current memory store has no defined health targets. Emergence detection (Wave 1) can signal anomalies, but without baselines it cannot distinguish signal from noise. This report proposes five SLO dimensions — Coverage, Freshness, Precision, Diversity, and Temporal Balance — along with baseline measurements taken from the live brain.db, rationale for each threshold, and a measurement implementation sketch.

**Key finding:** The store is currently in a **Yellow/Red health state** on most dimensions. Coverage is critically low (7.1%), recall rate is near-zero (3.8%), and temporal class distribution is pathological (all memories in `medium` or `long` — no `ephemeral`, `short`, or `permanent`). These are structural issues, not operational ones. The SLOs proposed here will make these deficiencies measurable and trackable going forward.

---

## Baseline Measurement (2026-03-28)

The following was measured directly from `brain.db` before any SLO remediation:

| Metric | Value | Notes |
|--------|-------|-------|
| Total memories | 106 | 27 active, 79 retired |
| Total events | 154 | 141 logged on 2026-03-28 alone (operational spike) |
| Distillation ratio | 0.071 | memories with source links / total events |
| Active category distribution | project 46%, lesson 35%, decision 12%, environment 8%, identity 4% | reasonably diverse |
| Temporal class distribution | medium 96%, long 4%, all others 0% | pathological — classification not running |
| Avg confidence | 0.928 | healthy |
| Avg trust_score | 1.0 | no validation has run — all default |
| Recall rate | 3.8% | 1 of 26 active memories ever recalled |
| Avg event-to-memory lag | 181 minutes | range 74–256 min |
| Validated memories | 0/26 | validation pipeline not running |
| Retracted memories | 0/26 | retraction pipeline not running |

---

## SLO 1: Coverage (Distillation Ratio)

### Definition
The fraction of important events that are distilled into a persistent memory. Measured as a rolling ratio of memory writes with `source_event_id` set vs. total events in the same window.

### Why it matters
Coverage too low → important events evaporate. Coverage too high → noise floods the store, retrieval degrades. The goal is a calibrated retention rate that captures signal without storing every heartbeat acknowledgment.

### Measurement
```sql
-- 7-day rolling distillation ratio
SELECT
  CAST(COUNT(DISTINCT m.id) AS REAL) / NULLIF(COUNT(DISTINCT e.id), 0) AS distillation_ratio
FROM events e
  LEFT JOIN memories m
    ON m.source_event_id = e.id
   AND m.retired_at IS NULL
WHERE e.created_at >= datetime('now', '-7 days');
```

**Refinement:** High-importance events (importance ≥ 0.8) should have a separate, stricter threshold — they should almost always produce a memory.

```sql
-- Coverage of high-importance events
SELECT
  CAST(COUNT(DISTINCT m.id) AS REAL) / NULLIF(COUNT(DISTINCT e.id), 0) AS high_importance_coverage
FROM events e
  LEFT JOIN memories m
    ON m.source_event_id = e.id
   AND m.retired_at IS NULL
WHERE e.importance >= 0.8
  AND e.created_at >= datetime('now', '-7 days');
```

### Thresholds

| Signal | Overall Coverage | High-Importance Coverage |
|--------|-----------------|--------------------------|
| Green | ≥ 0.10 | ≥ 0.50 |
| Yellow | 0.05–0.10 | 0.25–0.50 |
| Red | < 0.05 | < 0.25 |

**Current state: Red (0.071 overall — note: many memories have no source link, true ratio may be higher but is not trackable)**

### Alert
Alert if overall coverage drops below 0.05 for a 24-hour rolling window and high-importance coverage drops below 0.25.

### Notes
- The low current ratio partly reflects that most memories are written directly (no `source_event_id`), not via the distillation pipeline. This is a measurement gap, not necessarily a coverage failure.
- Recommendation: enforce `source_event_id` on all memory writes made by the consolidation cycle. Direct agent writes (for context only) may omit it.

---

## SLO 2: Freshness (Event-to-Memory Lag)

### Definition
Median time elapsed between an event occurring and a memory being created from it. Measured only on memories where `source_event_id` is set (distillation pipeline output).

### Why it matters
A stale memory is a lagging memory. If a decision is made at t=0 but not consolidated until t=+8h, any agent querying between t=0 and t=+8h gets a stale world model. Short lag means agents operate on current context.

### Measurement
```sql
-- Median event-to-memory lag (minutes), past 7 days
SELECT
  ROUND(AVG((julianday(m.created_at) - julianday(e.created_at)) * 1440), 1) AS avg_lag_min
FROM memories m
  JOIN events e ON m.source_event_id = e.id
WHERE m.created_at >= datetime('now', '-7 days')
  AND m.retired_at IS NULL;
```

For true median, use the SQLite percentile workaround or collect into Python:
```python
lags_min = sorted([...])  # query all lag values
median_lag = lags_min[len(lags_min) // 2]
```

### Category-Differentiated Targets

Different memory categories have different urgency profiles:

| Category | Rationale | Green | Yellow | Red |
|----------|-----------|-------|--------|-----|
| `decision` | Decisions must be visible before they get executed against | ≤ 30 min | 30–90 min | > 90 min |
| `lesson` | Post-mortems and learnings are valuable but not time-critical | ≤ 120 min | 120–480 min | > 480 min |
| `environment` | Infrastructure facts should be current | ≤ 60 min | 60–240 min | > 240 min |
| `project` | Project state is read frequently; moderate urgency | ≤ 90 min | 90–360 min | > 360 min |
| `identity` | Rare writes; no time pressure | ≤ 240 min | any | — |
| Overall (all categories) | Composite | ≤ 60 min | 60–240 min | > 240 min |

**Current state: Yellow (181 min average; true median likely similar)**

### Alert
Alert if median overall freshness lag exceeds 240 minutes over a 24-hour window.

---

## SLO 3: Precision (Retrieval Quality)

### Definition
The degree to which recalled memories are relevant to the query context. Pure precision measurement requires labeled retrieval logs (ground truth), which brain.db does not currently have. This SLO therefore uses two proxy metrics:

1. **Recall engagement rate** — % of active memories that have been recalled at least once in the past 30 days. Memories never recalled are either: (a) genuinely unhelpful for any query, or (b) retrieval is broken. Either way, they are health signals.

2. **Confidence distribution** — avg confidence of recalled vs. never-recalled memories. If recalled memories have significantly lower confidence than the overall pool, the retrieval scoring is inverted (surfacing stale/low-quality results).

### Measurement
```sql
-- Recall engagement rate (last 30 days)
SELECT
  ROUND(
    CAST(SUM(CASE WHEN last_recalled_at >= datetime('now', '-30 days') THEN 1 ELSE 0 END) AS REAL)
    / NULLIF(COUNT(*), 0), 3
  ) AS engagement_rate,
  ROUND(AVG(confidence), 3) AS avg_confidence_all,
  ROUND(AVG(CASE WHEN recalled_count > 0 THEN confidence END), 3) AS avg_confidence_recalled
FROM memories
WHERE retired_at IS NULL;
```

### Thresholds

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| 30-day engagement rate | ≥ 0.30 | 0.10–0.30 | < 0.10 |
| Avg confidence (all active) | ≥ 0.80 | 0.60–0.80 | < 0.60 |
| Confidence inversion check | recalled_conf ≥ all_conf | within 0.1 below | recalled_conf < all_conf − 0.1 |

**Current state: Red on engagement (0.038 — 1/26 ever recalled). Confidence is healthy (0.928). No inversion detectable yet — too few recalls to measure.**

### Notes
- Zero validation and zero retractions (both pipelines not running) means trust_score is meaningless at baseline — all default to 1.0. Once Sentinel 2 implements validation (COS-200 cross-ref), trust_score divergence becomes a precision signal.
- The ground-truth solution is an annotation layer: agents mark retrieved memories as "helpful" or "not helpful" in their event log. This would allow true precision@k measurement.

---

## SLO 4: Diversity (Topic Distribution)

### Definition
The degree to which memories cover multiple categories and scopes, preventing topic collapse (a store dominated by one category becomes blind to others).

### Why it matters
If 80% of memories are about one project, agents on other projects get poor recall. Category collapse also indicates the consolidation pipeline is biased — likely always firing on one agent's work.

### Measurement — Herfindahl-Hirschman Index (HHI)

HHI is borrowed from antitrust economics: it measures concentration. Sum of squared market shares, where 1.0 = monopoly, 1/N = perfect equality.

```python
from collections import Counter

def hhi(values):
    counts = Counter(values)
    total = sum(counts.values())
    return sum((c / total) ** 2 for c in counts.values())

# For category distribution
categories = [row["category"] for row in active_memories]
category_hhi = hhi(categories)  # lower = more diverse

# For scope distribution
scopes = [row["scope"] for row in active_memories]
scope_hhi = hhi(scopes)
```

SQL approximation:
```sql
-- Category HHI
WITH cat_counts AS (
  SELECT category, COUNT(*) AS cnt, SUM(COUNT(*)) OVER () AS total
  FROM memories WHERE retired_at IS NULL
  GROUP BY category
)
SELECT SUM(CAST(cnt AS REAL) * cnt / (total * total)) AS category_hhi
FROM cat_counts;
```

### Thresholds

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| Category HHI | ≤ 0.35 | 0.35–0.55 | > 0.55 |
| Scope HHI | ≤ 0.40 | 0.40–0.60 | > 0.60 |
| Any single category share | ≤ 50% | 50–65% | > 65% |

**Current state: Yellow. Category HHI ≈ 0.35 (project 46%, lesson 35%, others 19%). Scope HHI is high — two scopes (project:agentmemory and project:costclock-ai) cover 85% of memories.**

### Alert
Alert if any single scope exceeds 70% of active memories for 48 hours, suggesting pipeline work is narrowly concentrated.

---

## SLO 5: Temporal Class Distribution (Decay Health)

### Definition
The distribution of active memories across the five temporal classes: `ephemeral`, `short`, `medium`, `long`, `permanent`. A healthy store should show a bell curve centered around `medium`, with progressively fewer memories at the extremes. An unhealthy store collapses to a single class.

### Why it matters
If temporal classification is not running, all memories pile up in the default class (`medium`) and never get promoted or demoted. The spaced repetition and semantic forgetting algorithms both require a working temporal distribution to function. A collapsed distribution means those pipelines are failing silently.

The five decay rate constants (from Wave 1):
- `ephemeral`: λ = 0.5 (half-life ~1.4 days)
- `short`: λ = 0.2 (half-life ~3.5 days)
- `medium`: λ = 0.05 (half-life ~14 days)
- `long`: λ = 0.01 (half-life ~69 days)
- `permanent`: λ = 0 (never decays)

### Measurement
```sql
SELECT
  temporal_class,
  COUNT(*) AS count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM memories
WHERE retired_at IS NULL
GROUP BY temporal_class
ORDER BY CASE temporal_class
  WHEN 'ephemeral' THEN 1
  WHEN 'short' THEN 2
  WHEN 'medium' THEN 3
  WHEN 'long' THEN 4
  WHEN 'permanent' THEN 5
  END;
```

### Target Distribution

This is not a hard target (distribution shifts with store age and usage) but a health range:

| Temporal Class | Expected Range | Current |
|----------------|---------------|---------|
| `ephemeral` | 5–15% | 0% ← Red |
| `short` | 10–25% | 0% ← Red |
| `medium` | 35–55% | 96% ← Red |
| `long` | 15–30% | 4% |
| `permanent` | 3–10% | 0% |

**Current state: Red. The temporal classification pipeline is not running (or not promoting/demoting memories after creation). All memories default to `medium` at write time.**

### Pathology Detection

Three specific anomalies to alert on:

| Anomaly | Condition | Meaning |
|---------|-----------|---------|
| Classification freeze | `ephemeral + short = 0%` for 7d | Temporal classification pipeline halted |
| Permanence overflow | `permanent > 20%` | Too aggressive permanent-promotion; store will never clear |
| Ephemeral backlog | `ephemeral > 30%` | Ephemeral memories not being retired fast enough; noise flooding |

---

## Composite Health Score

To produce a single health indicator, each SLO dimension gets a traffic-light score (Red=0, Yellow=1, Green=2). Composite score = sum / max.

```python
DIMENSION_WEIGHTS = {
    "coverage": 0.25,
    "freshness": 0.20,
    "precision": 0.25,
    "diversity": 0.15,
    "temporal_balance": 0.15,
}

def composite_score(scores: dict[str, int]) -> float:
    """scores: {dimension: 0/1/2}"""
    return sum(scores[d] * DIMENSION_WEIGHTS[d] for d in scores) / 2.0
```

Range: 0.0 (all Red) to 1.0 (all Green). Recommended thresholds: ≥ 0.7 = healthy, 0.4–0.7 = degraded, < 0.4 = critical.

**Current state: Score ≈ 0.15 (critical). Coverage=Red, Precision=Red, Temporal=Red, Freshness=Yellow, Diversity=Yellow.**

This is expected for a new, early-stage store — the number is not alarming in itself. What matters is the trajectory: the score should improve monotonically as pipelines come online.

---

## Measurement Implementation

The SLOs above can be implemented as a module in the Wave 1 pipeline stack:

```python
# ~/agentmemory/research/wave5/14_memory_health_slos.py  (sketch)

import sqlite3
from dataclasses import dataclass, field

@dataclass
class HealthReport:
    coverage: float           # distillation ratio
    coverage_hi: float        # high-importance coverage
    freshness_median_min: float
    engagement_rate: float
    avg_confidence: float
    category_hhi: float
    scope_hhi: float
    temporal_dist: dict       # {class: pct}
    composite_score: float
    signals: list[str] = field(default_factory=list)  # alert messages

def assess(conn: sqlite3.Connection, window_days: int = 7) -> HealthReport:
    ...  # execute the queries above, populate HealthReport

def run_and_emit(db_path: str):
    conn = sqlite3.connect(db_path)
    report = assess(conn)
    # emit as brainctl event
    import subprocess, json
    subprocess.run([
        "brainctl", "-a", "hippocampus", "event", "add",
        f"Memory health check: composite={report.composite_score:.2f}",
        "-t", "result", "-p", "agentmemory",
        "--metadata", json.dumps(report.__dict__)
    ])
    if report.composite_score < 0.4:
        # post alert via brainctl memory
        subprocess.run([
            "brainctl", "-a", "hippocampus", "memory", "add",
            f"ALERT: Memory store health critical (score={report.composite_score:.2f}). Signals: {'; '.join(report.signals)}",
            "-c", "environment", "-s", "project:agentmemory"
        ])
```

---

## Recommended Alert Thresholds (Summary Table)

| SLO | Metric | Alert Condition |
|-----|--------|-----------------|
| Coverage | Overall distillation ratio | < 0.05 for 24h |
| Coverage | High-importance coverage | < 0.25 for 24h |
| Freshness | Median event-to-memory lag | > 240 min for 24h |
| Freshness | Decision lag | > 90 min for 12h |
| Precision | 30-day engagement rate | < 0.10 |
| Diversity | Category HHI | > 0.55 |
| Diversity | Single scope share | > 70% |
| Temporal | ephemeral + short = 0% | for 7 days (pipeline freeze) |
| Temporal | permanent share | > 20% |
| Composite | Overall score | < 0.4 |

---

## Cross-Pollination Notes (for Cortex, COS-202)

The issue designates Cortex as the org-level health interpretation partner. The following dimensions are best interpreted at the organization level (not just per-store):

1. **Coverage** — If coverage is healthy for agentmemory but zero for costclock-ai, there is a project-scoped distillation gap. Per-project coverage breakdown matters.
2. **Diversity** — Scope HHI measured across the entire store conflates multi-project activity with monoculture. Cortex should interpret: is scope concentration healthy (one active project) or pathological (one project monopolizing shared memory)?
3. **Composite score trajectory** — The score is most meaningful as a trend line. Cortex should track week-over-week composite to detect drift before it becomes critical.

---

## Relationship to Wave 1 Emergence Detection

The `07_emergence_detection.py` module checks for topic trending, agent drift, confidence shifts, and recall cluster analysis. The SLOs above complement it:

| Emergence signal | Corresponding SLO | Relationship |
|------------------|--------------------|--------------|
| Confidence distribution shift | SLO 3 (Precision) | SLO 3 gives the threshold; emergence gives the direction of shift |
| Topic frequency trending | SLO 4 (Diversity/HHI) | SLO 4 flags collapse; emergence identifies which topic is winning |
| Recall cluster analysis | SLO 3 (engagement rate) | engagement rate is a store-level aggregate; emergence shows per-cluster patterns |
| Agent behavioral drift | SLO 4 (per-agent category distribution) | not yet in SLO 4 — recommend adding per-agent HHI |

Recommendation: add a per-agent diversity check to SLO 4. If a single agent is writing >60% of all memories, store is agent-captured, not org-level.

---

## Summary

Five SLO dimensions with measurable thresholds, SQL measurement queries, and alert conditions. Current store baseline is critical (composite ≈ 0.15), primarily due to: (a) distillation pipeline not linking memories to source events, (b) temporal classification pipeline not running post-write, and (c) recall pipeline underused.

These failures are infrastructure gaps, not SLO violations per se — the SLOs are intended to make such gaps detectable automatically going forward. Recommend Hippocampus (or Engram) pick up implementation of the health check runner as part of the maintenance cron.
