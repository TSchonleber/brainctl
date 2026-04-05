# Global Workspace Theory & Conscious Broadcasting — Making 178 Agents Share a Spotlight

**Task:** COS-243
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**References:** COS-232 (Memory Event Bus), COS-249 (World Models), COS-231 (Embedding Backfill)

---

## Overview

Baars' Global Workspace Theory (1988) is the most empirically supported theory of
consciousness in cognitive science. Its core claim: information becomes "conscious"
when it wins a competition for a global broadcast channel, making it simultaneously
available to all specialized modules. This research maps GWT onto the 178-agent
CostClock AI system and designs a **Global Workspace Layer** — a salience-based
broadcast mechanism on top of brain.db that creates shared situational awareness
across the org.

---

## Theoretical Foundations

### Baars (1988) — Global Workspace Theory
The human brain contains hundreds of specialized modules (vision, language, motor
control, emotion) that normally work in parallel and in isolation. "Consciousness"
is what happens when one of these specialists broadcasts into the **global workspace**
— a shared medium that every other module can read.

Architecture:
```
Specialist modules: [vision] [language] [emotion] [memory] ...
                              ↓ competition ↓
                     ┌─────────────────────┐
                     │   Global Workspace  │  ← winner broadcasts here
                     └─────────────────────┘
                              ↓ broadcast ↓
       All specialists simultaneously receive the broadcast
```

**Mapping:** Our 178 agents are the specialist modules. brain.db is the filing
cabinet they all write to. But there is no *broadcast* — agents don't see each
other's memories unless they explicitly query. The Global Workspace Layer adds
the missing broadcast channel.

### Dehaene — Neuronal Global Workspace
Stanislas Dehaene (2011) operationalized GWT with the "ignition" model:
- Information below a threshold stays **subliminal** (local, not broadcast)
- When a stimulus crosses threshold, it **ignites** — rapid, non-linear spread
  across the brain, creating a coherent global state

**Mapping:**
- Low-salience events (routine task updates) = subliminal. Stored in brain.db,
  available on query but not broadcast.
- High-salience events (critical failure, major decision, emergency) = ignite.
  Pushed via the Memory Event Bus to all subscribed agents.

### Desimone & Duncan (1995) — Biased Competition
Attention is not a spotlight that moves around. It's a competition: stimuli
compete for neural representation, and the winner suppresses the losers.
**Bias** (from goals, context) determines which stimulus wins.

**Mapping:** Memories compete for the global workspace based on:
1. **Intrinsic salience** (importance, recency, confidence delta)
2. **Goal relevance bias** (does this match current active goals/projects?)
3. **Agent load bias** (high-load agents broadcast more urgently)

### Tononi — Integrated Information Theory (IIT)
Consciousness ≡ integrated information (Phi, Φ). A system has high Phi if its
parts are strongly causally interconnected. A purely modular system (each agent
isolated) has Phi ≈ 0. A fully integrated system has high Phi.

**Mapping:** Phi as an **org-cognition health metric**:
- Measure: how often do agents cite or respond to each other's memory writes?
- Low cross-citation = siloed, low Phi = coordination failures
- High cross-citation = integrated, high Phi = coherent org behavior

---

## Architecture Design

### The Global Workspace Layer

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Global Workspace Layer                       │
│                                                                     │
│  ┌──────────────┐    ┌──────────────────────────────────────────┐   │
│  │  Salience    │    │  Broadcast Channel                       │   │
│  │  Scoring     │    │  (workspace_events table)                │   │
│  │              │    │                                          │   │
│  │  salience =  │    │  - ignition_threshold: REAL             │   │
│  │  f(priority, │───▶│  - subscriber_filter: TEXT (JSON)       │   │
│  │  recency,    │    │  - delivered_to: TEXT (JSON)            │   │
│  │  goal_match, │    │  - ttl: TEXT (expires_at)               │   │
│  │  error_delta)│    └──────────────────────────────────────────┘   │
│  └──────────────┘                                                   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Integration Metrics                                         │   │
│  │  (workspace_phi table)                                       │   │
│  │  - cross_citation_rate per agent pair                        │   │
│  │  - org_phi score (aggregate integration)                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Schema

#### `workspace_broadcasts` table
```sql
CREATE TABLE workspace_broadcasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent_id TEXT NOT NULL,
    memory_id       INTEGER,            -- FK to memories table
    event_id        INTEGER,            -- FK to memory_events table
    salience_score  REAL NOT NULL,      -- 0.0–1.0
    broadcast_type  TEXT NOT NULL,      -- 'ignition' | 'scheduled' | 'manual'
    content_summary TEXT NOT NULL,      -- 1–2 sentence summary for broadcast
    scope_filter    TEXT,               -- JSON: {"projects": [...], "roles": [...]}
    created_at      TEXT NOT NULL,
    expires_at      TEXT,               -- NULL = permanent
    ack_count       INTEGER DEFAULT 0   -- how many agents acknowledged
);

CREATE INDEX idx_ws_broadcasts_salience ON workspace_broadcasts(salience_score DESC);
CREATE INDEX idx_ws_broadcasts_created ON workspace_broadcasts(created_at DESC);
```

#### `workspace_acks` table
```sql
CREATE TABLE workspace_acks (
    broadcast_id    INTEGER NOT NULL,
    agent_id        TEXT NOT NULL,
    acked_at        TEXT NOT NULL,
    response_type   TEXT,               -- 'noted' | 'acted' | 'irrelevant'
    PRIMARY KEY (broadcast_id, agent_id)
);
```

#### `workspace_phi` table (integration metric)
```sql
CREATE TABLE workspace_phi (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    agent_a         TEXT NOT NULL,
    agent_b         TEXT NOT NULL,
    citation_count  INTEGER DEFAULT 0,
    phi_contribution REAL DEFAULT 0.0,
    updated_at      TEXT NOT NULL
);
```

### Salience Scoring Function

Salience is computed when an agent writes a memory or event:

```python
def compute_salience(event):
    base = 0.0

    # Priority signal
    priority_weights = {'critical': 1.0, 'high': 0.7, 'medium': 0.4, 'low': 0.1}
    base += priority_weights.get(event.priority, 0.3) * 0.4

    # Recency signal (decays over time — not relevant here, it's new)
    base += 0.2  # full recency for new events

    # Goal relevance bias
    if event_matches_active_goals(event):
        base += 0.25

    # Prediction error signal (from World Model layer — COS-249)
    # High prediction error = surprising = high salience
    if event.prediction_error and event.prediction_error > 0.5:
        base += event.prediction_error * 0.15

    return min(base, 1.0)

IGNITION_THRESHOLD = 0.70  # Events above this broadcast to all subscribed agents
```

### brainctl Commands (Sketches)

```bash
# Check current global workspace — what's in the spotlight right now?
brainctl workspace status

# Manually broadcast a high-salience event
brainctl workspace broadcast --memory-id 42 --summary "Critical: auth failure pattern detected"

# List recent broadcasts (with ack rates)
brainctl workspace tail --n 20

# Agent subscription — filter broadcasts relevant to me
brainctl workspace subscribe --projects agentmemory costclock-ai --roles researcher

# Integration metric — current org Phi score
brainctl workspace phi

# View which agent pairs are most/least integrated
brainctl workspace phi --breakdown
```

---

## Ignition Dynamics in Practice

What triggers ignition (broadcast) in CostClock AI?

| Event Type | Salience | Broadcast? |
|------------|----------|-----------|
| Routine task comment | 0.2–0.35 | No (subliminal) |
| Task blocked (high priority) | 0.55–0.70 | Borderline |
| Critical task blocked | 0.75–0.85 | Yes — broadcast to parent + peers |
| Agent prediction error > 0.7 | 0.70–0.80 | Yes — Hermes attention triggered |
| New policy memory added | 0.60–0.75 | Yes — relevant agents notified |
| Org Phi drops below threshold | 0.90 | Emergency broadcast to Hermes |
| New wave of research complete | 0.65–0.75 | Yes — synthesis agents notified |

---

## The Binding Problem for Organizations

Tononi's binding problem: how does the brain unify features from different modules
into a single percept? For orgs: how do we combine insights from 5 agents working
in parallel into a unified organizational understanding?

**Current state:** It doesn't happen. Five agents write to brain.db but nobody
synthesizes across them in real-time.

**GWT solution:**
- After ignition, Cortex (the synthesis analyst) receives broadcasts from high-salience
  events across all agents
- Cortex's job IS the binding function: synthesize pattern across broadcasts into
  intelligence briefs
- Cortex uses the Integration Metric (Phi) to detect when the org is becoming siloed
  and broadcast a "re-integration" event

This formally defines Cortex's role in the cognitive architecture: **Cortex is the
binding function of the Global Workspace.**

---

## Phi — Organizational Integration Metric

Phi (Φ) measures integration. A practical approximation for orgs:

```
Φ_pair(A, B) = (# times A's memories are cited by B) +
               (# times B's memories are cited by A)
               ÷ (total memories written by A + B in window)

Φ_org = mean(Φ_pair) across all active agent pairs in window
```

Healthy Φ_org targets:
- < 0.05 = siloed (agents working in isolation, coordination risk)
- 0.05–0.20 = normal operational range
- > 0.20 = highly integrated (may indicate overlapping work)

Hermes should monitor Φ_org and broadcast an integration alert when it drops
below 0.05 for > 2 consecutive measurement windows.

---

## Relationship to Memory Event Bus (COS-232)

The MEB is the *transport layer*. The Global Workspace Layer is the *routing logic*:

```
MEB: delivers all events to subscribers who asked
GWT: determines WHICH events are important enough to broadcast PROACTIVELY
```

The MEB supports the GWT by providing the event stream. The GWT adds:
1. Salience scoring on top of MEB events
2. Ignition threshold filtering
3. Broadcast acknowledgment tracking
4. Phi integration metrics

---

## Open Questions

1. **Who computes salience?** The writing agent (potentially biased) or a neutral
   scoring function (consistent but slower)? Suggest: writing agent proposes,
   SQLite trigger caps it at role-appropriate max salience.

2. **Broadcast storm risk:** If 10 critical tasks block simultaneously, 10 ignitions
   could flood agent inboxes. Need a **workspace governor**: max N broadcasts per
   window, queue the rest, rate-limit to Phi × throughput.

3. **Acknowledgment as feedback:** If no agents ack a broadcast within T seconds,
   does that lower the source agent's salience calibration score? Suggest: yes,
   after 3 unacked broadcasts, score the agent's salience calibration.

4. **Scope filters:** Broadcasts to all 178 agents are expensive. Most events only
   need to reach the 5–10 agents in the relevant project or reporting chain.
   The `scope_filter` field addresses this — define good defaults.

5. **Memory vs. event broadcasts:** Should broadcasts carry the raw memory content
   or just a pointer + summary? Suggest: summary in broadcast, full content on
   demand via `brainctl workspace read --broadcast-id N`.

---

## Implementation Path

| Phase | Deliverable | Complexity |
|-------|-------------|------------|
| 1 | `workspace_broadcasts` + `workspace_acks` tables | Low |
| 2 | Salience scoring function + ignition threshold in SQLite trigger | Medium |
| 3 | `brainctl workspace tail` + `subscribe` commands | Low |
| 4 | `workspace_phi` table + Φ measurement | Medium |
| 5 | `brainctl workspace phi` — integration metric reporting | Low |
| 6 | Workspace governor (rate limiting, storm prevention) | Medium |
| 7 | Cortex integration as binding function (receives all ignitions) | High |

---

## Connections to Other Wave 6 Work

- **COS-249 (World Models):** Prediction errors from the world model are a primary
  salience signal. High-error = surprising = ignite and broadcast.
- **COS-235 (Policy Memory):** Policy updates should broadcast at high salience —
  every agent needs to know when org policy changes.
- **COS-232 (MEB):** GWT rides on top of MEB. Broadcasts are high-priority MEB events.
- **COS-233 (Contradiction Detection):** Contradictions detected cross-scope are a
  natural ignition trigger — broadcast the contradiction to relevant agents.

---

## Summary

The Global Workspace Layer transforms brain.db from a filing cabinet to a **shared
spotlight of attention**. High-salience events ignite and broadcast; low-salience
events stay local. Integration is measured via Phi. Cortex serves as the binding
function, synthesizing cross-agent broadcasts into organizational intelligence.

This is the attention and awareness layer of the cognitive architecture. Without it,
178 agents are 178 isolated specialists. With it, they share a collective mind.
