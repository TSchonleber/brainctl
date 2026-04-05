# Wave 6 Research: Neuromodulation & Dynamic Learning Rates

**COS-244 — Dopamine, urgency, and knowing when to learn fast vs slow**
Author: Epoch (Temporal Cognition Engineer)
Date: 2026-03-28

---

## Summary

The brain.db memory spine learns at one speed. Every write gets `confidence = 1.0`,
every read gets the same similarity threshold, every retrieval uses the same temporal
decay constant. Real brains don't work this way. Neuromodulators — dopamine,
norepinephrine, acetylcholine, serotonin — dynamically gate *how* learning happens
based on context. This report designs a neuromodulation layer for brain.db that
modulates learning rate, retrieval breadth, and consolidation aggressiveness based
on organizational state.

**Status of the gap:** brain.db already has the hooks (confidence, temporal_class,
trust_score, temporal lambda in TEMPORAL_DESIGN.md). It lacks a runtime parameter
context that adjusts these knobs based on what the organization is currently doing.

---

## 1. The Four Modulators — Biological → Computational Mapping

### 1.1 Dopamine → Confidence Reinforcement (Reward Signal)

**Biological mechanism:** Schultz (1997). Dopamine neurons fire when outcomes are
*better* than predicted (prediction error positive) and depress when *worse* than
predicted. This gates synaptic plasticity — high dopamine = strengthen the pathway
that led to the outcome; low dopamine = weaken it.

**Computational mapping:** When an agent's task completes with a positive outcome
(done without escalation, praised, fast), boost `confidence` on memories accessed
during that task. When a task fails or escalates, mark those memories for review.

**Formal signal:**
```
dopamine_signal ∈ [-1.0, +1.0]
= (actual_outcome_quality - expected_outcome_quality) / expected_outcome_quality

Where outcome_quality proxies:
  +1.0  task done with praise / resolved without escalation
  +0.5  task done normally
   0.0  task handed back for revision
  -0.5  task blocked / escalated
  -1.0  task failed / cancelled with failure note
```

**Effect on brain.db:**
```sql
-- On positive dopamine signal: boost recently-accessed memories in scope
UPDATE memories
SET confidence = MIN(1.0, confidence + (0.1 * :dopamine_magnitude))
WHERE scope = :task_scope
  AND last_recalled_at >= :task_started_at
  AND retired_at IS NULL;

-- On negative dopamine signal: flag for review (don't delete — Hebbian anti-strengthening)
UPDATE memories
SET confidence = MAX(0.1, confidence - (0.08 * ABS(:dopamine_magnitude))),
    tags = json_insert(COALESCE(tags, '[]'), '$[#]', 'needs_review')
WHERE scope = :task_scope
  AND last_recalled_at >= :task_started_at
  AND retired_at IS NULL;
```

**Implementation path:** `brainctl neuro signal --dopamine <value> --scope <scope>`.
Called by the task completion hook in Paperclip heartbeats. Dopamine magnitude
attenuates at 1/3 per day (short-lived signal, consistent with biological reuptake).

---

### 1.2 Norepinephrine → Arousal Mode (Urgency Signal)

**Biological mechanism:** Locus coeruleus → cortex. High NE = heightened alertness,
faster synaptic learning (lower LTP threshold), broader attentional spotlight.
In biological terms: during threat or urgent novelty, you learn more, faster, from
a wider field. The cost is precision — you also capture more noise.

**Computational mapping:** During `org_state = 'incident'`, switch to fast-learning
mode. Lower the retrieval similarity threshold (capture more, wider), trigger
immediate consolidation (don't wait for scheduled cycle), and slow confidence decay
(incident memories need to persist for post-mortems).

**Arousal level → parameter table:**

| Condition          | arousal_level | retrieval_breadth | consolidation        | confidence_decay/day |
|--------------------|---------------|-------------------|----------------------|----------------------|
| Normal ops         | 0.3           | 1.0× (baseline)   | scheduled (4h cycle) | 0.02                 |
| Sprint             | 0.5           | 1.2×              | scheduled (2h cycle) | 0.015                |
| Incident           | 0.9           | 1.6×              | immediate            | 0.005                |
| Strategic planning | 0.2           | 0.9×              | scheduled (8h cycle) | 0.01                 |

**Incident detection heuristic (for auto-detection):**
```python
def detect_incident(db) -> bool:
    # Condition 1: ≥3 critical-priority tasks open in same project in last 2h
    critical_burst = db.execute("""
        SELECT project_scope, COUNT(*) as cnt FROM events
        WHERE event_type = 'task_opened'
          AND metadata LIKE '%"priority":"critical"%'
          AND created_at >= datetime('now', '-2 hours')
        GROUP BY project_scope HAVING cnt >= 3
    """).fetchone()

    # Condition 2: explicit incident epoch active
    incident_epoch = db.execute("""
        SELECT id FROM epochs
        WHERE name LIKE '%incident%' AND started_at <= datetime('now')
          AND (ended_at IS NULL OR ended_at >= datetime('now'))
    """).fetchone()

    return bool(critical_burst or incident_epoch)
```

---

### 1.3 Acetylcholine → Focused Attention (Signal-to-Noise)

**Biological mechanism:** ACh enhances signal-to-noise in sensory cortex. During
focused attention, ACh suppresses lateral connections (reduces interference from
neighboring representations) and strengthens thalamocortical drive (the task-relevant
signal). Net effect: higher fidelity on the thing you're focused on, lower receptivity
to tangential inputs.

**Computational mapping:** When an agent is in deep focused work (single project,
narrow task sequence), tighten the similarity threshold and scope to that project.
Exploitation over exploration — prioritize memories that have been recalled before.

**Focus detection heuristic:**
```python
def detect_focused_work(db, agent_id: str) -> tuple[bool, str | None]:
    """Return (is_focused, project_scope) if agent worked on one project in last 30min."""
    recent = db.execute("""
        SELECT DISTINCT scope FROM events
        WHERE agent_id = ? AND scope LIKE 'project:%'
          AND created_at >= datetime('now', '-30 minutes')
    """, (agent_id,)).fetchall()
    if len(recent) == 1:
        return True, recent[0]["scope"]
    return False, None
```

**Effect on retrieval:**
- similarity_threshold += 0.15 (only high-confidence matches)
- Scope locked to `project:<current>` first; fall back to global only if < 2 results
- Result ranking: `score = base_score * (1 + 0.3 * log1p(recalled_count))` —
  previously-recalled memories get a bonus (exploitation bias)

---

### 1.4 Serotonin → Time Horizon (Patience Signal)

**Biological mechanism:** Serotonin modulates how far into the future behavior is
planned. High serotonin = patient, long-horizon decision-making. Low serotonin =
reactive, immediate-payoff bias. In temporal difference learning terms, serotonin
regulates the discount factor γ.

**Computational mapping:** The temporal decay constant `λ` in retrieval weighting
(`temporal_weight = relevance * exp(-λ * days_since)`) should vary by org state
and by agent role. Strategic agents need long-horizon context; execution agents need
immediate context.

**Lambda schedule:**

| Mode               | λ        | Effective half-life | Use case                              |
|--------------------|----------|---------------------|---------------------------------------|
| Strategic planning | 0.005    | ~138 days           | Architecture decisions, org strategy  |
| Normal             | 0.03     | ~23 days            | Standard agent operation              |
| Sprint             | 0.06     | ~12 days            | Current sprint context dominates      |
| Incident           | 0.10     | ~7 days             | Recent events heavily weighted        |
| Focused work       | 0.08     | ~9 days             | Task-local recency matters most       |

**Per-temporal_class overrides** (λ is IGNORED for these classes — they use fixed
weights regardless of org state):
- `permanent` → weight 1.0 always
- `long` → weight 0.85 always (architecture decisions shouldn't decay in an incident)

**Context window depth** (number of recent events to inject into agent context):

| Mode               | context_window_depth |
|--------------------|----------------------|
| Strategic planning | 200 events           |
| Normal             | 50 events            |
| Sprint             | 30 events            |
| Incident           | 75 events (recent)   |

---

## 2. The Org State Machine

Four states. Transitions are heuristic-driven but can be manually overridden.

```
                    ┌──────────────────────────────────────┐
                    │                                      │
              ┌─────▼──────┐                              │
         ┌───►│   NORMAL   │◄────────────────────────┐   │
         │    └─────┬──────┘                         │   │
         │          │                                 │   │
    resolved   ┌────▼─────┐  ≥3 critical tasks   ┌──┴───▼──────────────┐
    /cleared   │ INCIDENT │◄─ in same project,  ─►│ STRATEGIC_PLANNING  │
               │          │  or incident epoch     │                     │
               └──────────┘  active               └─────────────────────┘
                    ▲                                        ▲
                    │    sprint policy or                    │
                    │    high task density                   │
               ┌────┴─────┐                                 │
               │  SPRINT   │─────────────────────────────────┘
               └──────────┘   sprint ends / planning phase begins
```

**Transition logic** (run by `brainctl neuro detect`):
1. If incident conditions → `incident` (highest priority override)
2. Elif ≥50% of active issues are `planning` category → `strategic_planning`
3. Elif sprint policy is active OR task-open rate > 8/hour in last 2 hours → `sprint`
4. Else → `normal`

State is stored in `neuromodulation_state` (see §3 schema). `detected_at` is updated
on each auto-detect pass. Manual overrides set `detection_method = 'manual'` and
`expires_at` to prevent stale locks.

---

## 3. Schema: `neuromodulation_state`

**Migration 012** — single-row config table (upsert on id=1):

```sql
-- Migration 012: Neuromodulation State (COS-244)
-- Stores the current runtime neuromodulation context for brain.db operations.

CREATE TABLE IF NOT EXISTS neuromodulation_state (
    id INTEGER PRIMARY KEY DEFAULT 1,  -- single-row table, always id=1

    -- Current organizational mode
    org_state TEXT NOT NULL DEFAULT 'normal'
        CHECK(org_state IN ('normal', 'incident', 'sprint', 'strategic_planning', 'focused_work')),

    -- ─── Dopamine (confidence reinforcement) ───────────────────────────────
    dopamine_signal        REAL NOT NULL DEFAULT 0.0,   -- -1.0 to 1.0, decays 1/3 per day
    confidence_boost_rate  REAL NOT NULL DEFAULT 0.10,  -- delta per successful recall context
    confidence_decay_rate  REAL NOT NULL DEFAULT 0.02,  -- delta per day (normal baseline)
    dopamine_last_fired_at TEXT,

    -- ─── Norepinephrine (arousal / retrieval breadth) ──────────────────────
    arousal_level                REAL NOT NULL DEFAULT 0.3,  -- 0.0-1.0
    retrieval_breadth_multiplier REAL NOT NULL DEFAULT 1.0,  -- applied to result limit
    consolidation_immediacy      TEXT NOT NULL DEFAULT 'scheduled'
                                     CHECK(consolidation_immediacy IN ('immediate', 'scheduled')),
    consolidation_interval_mins  INTEGER NOT NULL DEFAULT 240,  -- 4h default

    -- ─── Acetylcholine (focus / signal-to-noise) ───────────────────────────
    focus_level              REAL NOT NULL DEFAULT 0.3,  -- 0.0-1.0
    similarity_threshold_delta REAL NOT NULL DEFAULT 0.0, -- added to base threshold (0.7)
    scope_restriction        TEXT,  -- NULL = global; 'project:X' = locked scope
    exploitation_bias        REAL NOT NULL DEFAULT 0.0,  -- 0.0-1.0 weight on recalled_count bonus

    -- ─── Serotonin (time horizon / patience) ───────────────────────────────
    temporal_lambda       REAL NOT NULL DEFAULT 0.030,   -- decay constant for retrieval
    context_window_depth  INTEGER NOT NULL DEFAULT 50,   -- recent event injection depth

    -- ─── Metadata ──────────────────────────────────────────────────────────
    detected_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    detection_method TEXT NOT NULL DEFAULT 'auto'  -- 'auto' | 'manual' | 'policy'
                         CHECK(detection_method IN ('auto', 'manual', 'policy')),
    expires_at       TEXT,  -- for manual overrides
    triggered_by     TEXT,  -- agent_id that set this state
    notes            TEXT
);

-- Ensure single-row invariant
CREATE UNIQUE INDEX IF NOT EXISTS idx_neuromod_singleton ON neuromodulation_state(id);

-- Seed default normal-ops state
INSERT OR IGNORE INTO neuromodulation_state (id) VALUES (1);

-- State-change audit log
CREATE TABLE IF NOT EXISTS neuromodulation_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    triggered_by TEXT,
    transitioned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT INTO schema_version (version, description)
VALUES (12, 'Neuromodulation state table — dynamic learning rate context (COS-244)');
```

**Preset parameter packs** (applied atomically by `brainctl neuro set`):

```python
NEUROMOD_PRESETS = {
    "normal": {
        "org_state": "normal",
        "arousal_level": 0.3,
        "retrieval_breadth_multiplier": 1.0,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 240,
        "focus_level": 0.3,
        "similarity_threshold_delta": 0.0,
        "scope_restriction": None,
        "exploitation_bias": 0.0,
        "temporal_lambda": 0.030,
        "context_window_depth": 50,
        "confidence_decay_rate": 0.020,
    },
    "incident": {
        "org_state": "incident",
        "arousal_level": 0.9,
        "retrieval_breadth_multiplier": 1.6,
        "consolidation_immediacy": "immediate",
        "consolidation_interval_mins": 15,
        "focus_level": 0.7,
        "similarity_threshold_delta": -0.10,  # lower threshold = more results
        "scope_restriction": None,
        "exploitation_bias": 0.0,
        "temporal_lambda": 0.100,             # recent events dominate
        "context_window_depth": 75,
        "confidence_decay_rate": 0.005,       # slow decay — need post-mortem data
    },
    "sprint": {
        "org_state": "sprint",
        "arousal_level": 0.5,
        "retrieval_breadth_multiplier": 1.2,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 120,
        "focus_level": 0.5,
        "similarity_threshold_delta": 0.0,
        "scope_restriction": None,
        "exploitation_bias": 0.2,
        "temporal_lambda": 0.060,
        "context_window_depth": 30,
        "confidence_decay_rate": 0.015,
    },
    "strategic_planning": {
        "org_state": "strategic_planning",
        "arousal_level": 0.2,
        "retrieval_breadth_multiplier": 0.9,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 480,
        "focus_level": 0.2,
        "similarity_threshold_delta": 0.05,  # slightly higher threshold — quality over quantity
        "scope_restriction": None,
        "exploitation_bias": 0.3,            # prefer proven patterns
        "temporal_lambda": 0.005,            # 138-day half-life
        "context_window_depth": 200,
        "confidence_decay_rate": 0.010,
    },
    "focused_work": {
        # Set dynamically — scope_restriction filled in by detect_focused_work()
        "org_state": "focused_work",
        "arousal_level": 0.4,
        "retrieval_breadth_multiplier": 0.8,
        "consolidation_immediacy": "scheduled",
        "consolidation_interval_mins": 240,
        "focus_level": 0.9,
        "similarity_threshold_delta": 0.15,
        "scope_restriction": None,           # filled in at runtime
        "exploitation_bias": 0.4,
        "temporal_lambda": 0.080,
        "context_window_depth": 25,
        "confidence_decay_rate": 0.020,
    },
}
```

---

## 4. brainctl Integration — `neuro` Subcommand

```bash
# Status — show current state and parameter values
brainctl neuro status

# Auto-detect org_state from current conditions
brainctl neuro detect [--apply]
# Without --apply: print what it would set. With --apply: update neuromodulation_state.

# Manual override (for testing or emergency use)
brainctl neuro set --state incident [--expires 4h] [--note "prod outage"]
brainctl neuro set --state normal

# Inject a dopamine signal (called by task completion hooks)
brainctl neuro signal \
  --dopamine 0.8 \
  --scope project:costclock \
  --since "2026-03-28T09:00:00"
# Boosts confidence on memories in that scope accessed since the given time.

# View transition history
brainctl neuro history [--limit 20]
```

**Auto-detect frequency:** Run `brainctl neuro detect --apply` every 15 minutes via the
consolidation cycle or as a pre-step in `brainctl search`. Overhead: ~2ms (single-table
read + heuristic queries).

---

## 5. Integration with Existing brainctl Commands

### 5.1 `brainctl search` / `brainctl vsearch`

Before executing the search, read `neuromodulation_state` (cached in-process for
30s to avoid repeated reads):

```python
def get_neuromod_params(db) -> dict:
    row = db.execute("SELECT * FROM neuromodulation_state WHERE id = 1").fetchone()
    return dict(row) if row else NEUROMOD_PRESETS["normal"]

def apply_neuromod_to_search(params, base_limit, base_threshold, query_scope):
    # Norepinephrine: expand result count
    limit = int(base_limit * params["retrieval_breadth_multiplier"])

    # Acetylcholine: tighten/loosen threshold
    threshold = base_threshold + params["similarity_threshold_delta"]
    threshold = max(0.3, min(0.95, threshold))  # clamp

    # Acetylcholine: scope restriction
    scope = params["scope_restriction"] or query_scope

    # Serotonin: temporal weighting lambda
    lambda_ = params["temporal_lambda"]

    return limit, threshold, scope, lambda_
```

After search returns results, apply serotonin temporal weighting:
```python
def apply_temporal_weight(results, lambda_):
    now = datetime.utcnow()
    for r in results:
        days = (now - parse_ts(r["created_at"])).days
        # permanent and long classes are immune to temporal decay
        if r.get("temporal_class") in ("permanent", "long"):
            r["_score"] = r.get("_score", 1.0)
        else:
            r["_score"] = r.get("_score", 1.0) * math.exp(-lambda_ * days)
    return sorted(results, key=lambda r: r["_score"], reverse=True)
```

Apply exploitation bias (acetylcholine recall weighting):
```python
def apply_exploitation_bias(results, exploitation_bias):
    if exploitation_bias <= 0:
        return results
    for r in results:
        recall_bonus = math.log1p(r.get("recalled_count", 0)) * exploitation_bias * 0.3
        r["_score"] = r.get("_score", 1.0) * (1 + recall_bonus)
    return sorted(results, key=lambda r: r["_score"], reverse=True)
```

### 5.2 `brainctl memory add`

In incident mode, default new memories to `temporal_class = 'short'` unless explicitly
overridden (incident context is urgent but transient):
```python
if neuromod["org_state"] == "incident" and not args.temporal_class:
    temporal_class = "short"
```

### 5.3 Consolidation cycle

Read `consolidation_immediacy` and `consolidation_interval_mins` to set scheduling:
```python
if neuromod["consolidation_immediacy"] == "immediate":
    # Run full consolidation pass now
    run_consolidation_cycle(db)
elif time_since_last_consolidation_mins > neuromod["consolidation_interval_mins"]:
    run_consolidation_cycle(db)
```

### 5.4 Confidence decay job (new — runs at consolidation time)

Currently confidence never decays. The neuromodulation layer adds scheduled decay:
```sql
-- Apply decay to non-permanent memories based on neuromod confidence_decay_rate
UPDATE memories
SET confidence = MAX(0.05, confidence - :decay_rate),
    updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
WHERE retired_at IS NULL
  AND temporal_class NOT IN ('permanent')
  AND confidence > 0.05;
```

Decay rate is taken from `neuromodulation_state.confidence_decay_rate`. This runs
once per consolidation cycle. Memories that fall below `confidence = 0.1` are flagged
for review (tagged `low_confidence`). Below `0.05` they are candidates for retirement.

---

## 6. Dopamine Hook — Task Completion Integration

The dopamine signal requires an integration point with task completion events. Proposed
hook in the Paperclip heartbeat skill (`brainctl neuro signal` call after task PATCH):

```python
# In Paperclip heartbeat, after patching task to 'done':
def fire_dopamine_on_completion(task, outcome_quality: float, started_at: str):
    """
    outcome_quality: +1.0 = excellent, +0.5 = normal, 0.0 = revision, -0.5 = blocked/escalated
    """
    scope = f"project:{task.get('projectId', 'unknown')}"
    subprocess.run([
        "brainctl", "neuro", "signal",
        "--dopamine", str(outcome_quality),
        "--scope", scope,
        "--since", started_at
    ])
```

Dopamine magnitude decays: the signal is multiplied by `exp(-0.33 * days_elapsed)`
before being applied. A task that finished 3 days ago triggers at ~37% strength.
This matches biological dopamine's short half-life — the learning signal is strongest
when applied immediately.

---

## 7. Phase Detection Timeline

Epoch-awareness is central to neuromodulation. The org_state auto-detector should
also read the active epochs to pick up signals that task heuristics might miss:

```python
def detect_from_epochs(db) -> str | None:
    """Return org_state if an epoch name implies a mode, else None."""
    active = db.execute("""
        SELECT name FROM epochs
        WHERE started_at <= strftime('%Y-%m-%dT%H:%M:%S', 'now')
          AND (ended_at IS NULL OR ended_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now'))
    """).fetchall()

    for row in active:
        name_lower = row["name"].lower()
        if any(kw in name_lower for kw in ("incident", "outage", "emergency", "hotfix")):
            return "incident"
        if any(kw in name_lower for kw in ("sprint", "push", "crunch")):
            return "sprint"
        if any(kw in name_lower for kw in ("planning", "strategy", "roadmap", "offsite")):
            return "strategic_planning"
    return None
```

This means that simply creating an epoch named `"Q2 Planning Offsite"` will
automatically shift the org to `strategic_planning` mode — long temporal horizon,
slow confidence decay, broad historical context injection.

---

## 8. Open Questions and Risks

### 8.1 State conflicts between agents

Multiple agents may disagree on org_state if they each run `detect --apply`
independently. Mitigation: treat `neuromodulation_state` as a shared singleton.
The last writer wins. Add an advisory lock: any write to `neuromodulation_state`
should include `triggered_by = agent_id` and log a transition. If two agents
disagree within 1 minute, escalate to Hermes.

A cleaner fix: move `detect --apply` to a dedicated scheduler (cron via brainctl)
so only one process drives state transitions. Other agents read but do not write.

### 8.2 Dopamine signal contamination

If an agent completes many tasks quickly (batch processing), the dopamine signal
aggregation might over-boost a set of unrelated memories that happened to be in
the same scope at the same time. Mitigation: require task scope to be specific
(`project:X:task:Y` not just `project:X`) when memory tagging allows it.

### 8.3 Incident mode exit criteria

Auto-detect may flip in/out of incident mode on each pass if conditions are
borderline. Add hysteresis: require the incident condition to be *absent* for 30
consecutive minutes before auto-transitioning back to normal.

```python
# Only exit incident if it's been clear for 30+ minutes
if current_state == "incident" and not incident_detected:
    last_incident_trigger = db.execute(
        "SELECT MAX(transitioned_at) FROM neuromodulation_transitions WHERE to_state = 'incident'"
    ).fetchone()[0]
    if last_incident_trigger:
        clear_minutes = (datetime.utcnow() - parse_ts(last_incident_trigger)).seconds / 60
        if clear_minutes < 30:
            return "incident"  # stay in incident
```

### 8.4 Missing outcome quality signal

The dopamine hook needs `outcome_quality` — a measure of how well the task went.
brain.db doesn't currently track this. Interim proxy: use task metadata from
Paperclip (priority delta, time-to-completion vs estimate, presence of revision
comments). A richer signal requires Paperclip to emit task-quality events. Filed
as a follow-up dependency.

---

## 9. Implementation Roadmap

| Step | Deliverable                                  | Migration | Complexity |
|------|----------------------------------------------|-----------|------------|
| 1    | `neuromodulation_state` table + seed          | 012       | Low        |
| 2    | `neuromodulation_transitions` audit log       | 012       | Low        |
| 3    | `brainctl neuro status / set / detect`        | —         | Medium     |
| 4    | Apply neuro params in `brainctl search`       | —         | Medium     |
| 5    | Temporal lambda weighting in search results   | —         | Medium     |
| 6    | Confidence decay job in consolidation cycle   | —         | Low        |
| 7    | `brainctl neuro signal --dopamine`            | —         | Medium     |
| 8    | Dopamine hook in Paperclip heartbeat skill    | —         | Medium     |
| 9    | Epoch-name-based state detection              | —         | Low        |
| 10   | Hysteresis / state debounce                   | —         | Low        |

Steps 1–6 can be implemented by Cortex or any IC in a single session. Steps 7–8
require coordination with the Paperclip skill layer (Hermes or manager approval).

---

## 10. Expected Impact

| Metric                          | Current       | Expected post-implementation        |
|---------------------------------|---------------|-------------------------------------|
| Memories with recalled_count > 0 | 2.4%         | 15–30% (temporal weighting surfaces more relevant memories) |
| Time-to-relevant-memory in incident | Baseline  | −40% (broader retrieval + recency bias) |
| Post-incident memory retention  | ~50% decay in 23 days | <10% decay in 23 days (0.005 rate) |
| Strategic context depth         | 50 events     | 200 events in planning mode         |
| False-positive memory boosts    | N/A (no signal) | ~15% expected; mitigated by scope specificity |

---

## References

- Schultz, W. (1997). A neural substrate of prediction and reward. *Science*, 275(5306).
- Dayan, P. & Balleine, B.W. (2002). Reward, motivation, and reinforcement learning. *Neuron*, 36(2).
- Yu, A.J. & Dayan, P. (2005). Uncertainty, neuromodulation, and attention. *Neuron*, 46(4).
- Hasselmo, M.E. (2006). The role of acetylcholine in learning and memory. *Current Opinion in Neurobiology*, 16(6).
- TEMPORAL_DESIGN.md — brain.db temporal cognition architecture (Epoch, 2026-03-28)
- Wave 6 Report 22: Trust Score Calibration (COS-233)
- Wave 6 Report 23: Policy Memory Schema (COS-235)

---

*Epoch — Temporal Cognition Engineer*
*Cognitive Architecture & Enhancement Project — Wave 6*
*2026-03-28*
