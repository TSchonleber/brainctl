# Situation Model Construction — Building Coherent Narratives from Fragmented Memories
## Research Report — COS-123
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**Target:** brain.db — Situation model layer enabling Hermes to answer "what is happening with X?" rather than just "what is X?"

---

## Executive Summary

Hermes currently retrieves individual memories in response to queries. This works well for lookup tasks ("what is the API rate limit?") but fails on *situational* queries ("what is happening with project COS-83?", "why did the auth system break?", "what is the current state of the memory consolidation pipeline?").

Answering situational queries requires a **situation model**: a coherent, internally consistent narrative representation that integrates multiple memories across time, cause, and agent roles into a single structured understanding of a scenario.

This report defines the situation model construct for brain.db, proposes a construction algorithm, defines coherence scoring, specifies revision triggers, recommends presentation formats, and provides an implementation sketch in SQL/Python.

**Primary recommendation:** Implement situation models as a new `situation_models` table in brain.db, constructed on-demand via a triggered query pipeline and cached with a 6-hour TTL. Incremental update (rather than full rebuild) should be the default for efficiency.

---

## 1. Theoretical Background

### 1.1 Kintsch (1988) — Construction-Integration Model

Kintsch's Construction-Integration model argues that language comprehension requires building a situation model that goes beyond the surface text. Applied to agent memory:

- **Propositional level**: individual memories as atomic facts ("Weaver shipped route-context v2", "COS-83 status = done")
- **Textbase level**: the coherent set of directly related memories, linked by reference (same entity, same project)
- **Situation model level**: the full integration — including inferences, temporal ordering, causal chains, and agent intentions — that allows answering "what is happening?"

The construction phase is fast and unconstrained: pull all potentially relevant memories. The integration phase is slow and selective: prune contradictions, resolve temporal ordering, and synthesize a coherent narrative.

### 1.2 Johnson-Laird (1983) — Mental Models

Johnson-Laird proposes that understanding requires constructing a *model* of the situation, not just a symbol manipulation. Three key properties of mental models are directly applicable:

1. **Analog structure**: the model should mirror the structure of the situation, not just list facts about it
2. **Default values**: when facts are missing, the model fills in plausible defaults (open-world assumption)
3. **Multiple models**: when a situation is ambiguous, hold multiple competing models and score them for consistency

In brain.db terms: a situation model for "Project COS" should not just be a list of memories tagged `project:cos` — it should be a structured object that has a `current_phase`, `blocking_agents`, `recent_events`, `known_risks`, and `inferred_state` with confidence scores.

### 1.3 Zacks & Swallow (2007) — Event Segmentation Theory

Event segmentation theory holds that humans parse continuous experience into discrete events at **event boundaries** — moments of significant change in agent state, location, or goal. This directly maps to:

- **Natural breakpoints** in brain.db event streams: status changes, agent reassignments, new comment threads
- **Event units**: the atomic chunk of situation that gets stored, retrieved, and integrated
- **Working event model**: the currently-active partial situation that Hermes should maintain while processing an agent's activity

This implies that situation models should be segmented by event boundaries, not arbitrary time windows.

---

## 2. Situation Model Definition for brain.db

A **situation model** in the brain.db context is:

> A named, temporally-bounded, causally-integrated cluster of memories and events that together describe the current state of a project, incident, or decision — enabling Hermes to answer situational queries without re-running full retrieval.

### 2.1 Schema

```sql
CREATE TABLE situation_models (
    id              TEXT PRIMARY KEY,           -- UUID
    name            TEXT NOT NULL,              -- e.g. "project:COS-83" or "incident:auth-mismatch-2026-03"
    query_anchor    TEXT NOT NULL,              -- the entity/topic this model is about
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_event_id   INTEGER,                    -- HWM: latest event incorporated
    last_memory_id  TEXT,                       -- HWM: latest memory incorporated
    coherence_score REAL DEFAULT 0.0,           -- 0..1, see Section 4
    completeness    REAL DEFAULT 0.0,           -- 0..1, fraction of known questions answered
    status          TEXT DEFAULT 'active',      -- active | stale | contradictory | archived
    narrative       TEXT,                       -- prose summary
    structured      TEXT,                       -- JSON blob: phases, agents, timeline, risks
    ttl_seconds     INTEGER DEFAULT 21600,      -- 6-hour default TTL
    source_memory_ids TEXT,                     -- JSON array of contributing memory IDs
    source_event_ids  TEXT                      -- JSON array of contributing event IDs
);

CREATE TABLE situation_model_contradictions (
    id              TEXT PRIMARY KEY,
    model_id        TEXT REFERENCES situation_models(id),
    memory_id_a     TEXT,
    memory_id_b     TEXT,
    contradiction   TEXT,                       -- description of conflict
    resolution      TEXT,                       -- how resolved (or null if open)
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 2.2 The `structured` JSON Schema

```json
{
  "anchor": "project:COS-83",
  "current_phase": "done",
  "phases": [
    {"name": "Phase 1 - auto-embed", "status": "done", "agent": "Weaver", "completed_at": "2026-03-28"},
    {"name": "Phase 2 - timeliness", "status": "done", "agent": "Weaver", "completed_at": "2026-03-28"}
  ],
  "agents": {
    "Weaver": {"role": "implementer", "status": "delivered"},
    "Hermes": {"role": "reviewer", "status": "accepted"}
  },
  "timeline": [
    {"at": "2026-03-28T04:24:37", "event": "COS-83 shipped: route-context v2, 14 events routed"},
    {"at": "2026-03-28T04:24:37", "event": "HWM advanced to event #93"}
  ],
  "blocking_agents": [],
  "open_questions": [],
  "known_risks": ["Auto-embed gap: new memories not automatically embedded"],
  "inferred_state": "delivered and stable",
  "confidence": 0.87
}
```

---

## 3. Construction Algorithm

Given a situational query like **"what is happening with project X?"**, the construction pipeline operates in four phases:

### Phase 1 — Anchor Resolution (0–5ms)

```python
def resolve_anchor(query: str) -> list[str]:
    """Extract the entity/topic being queried."""
    # 1. Named entity detection: look for known project IDs, agent names, incident keywords
    # 2. Match against known situation_model names first (cache hit)
    # 3. Fall back to brainctl search to find anchor memories
    candidates = brainctl_vsearch(query, k=5)
    anchor_ids = extract_entities(candidates)
    return anchor_ids
```

### Phase 2 — Memory Retrieval (5–50ms)

```python
def retrieve_situation_memories(anchor: str) -> list[Memory]:
    """Pull all memories relevant to the anchor."""
    # Multi-strategy retrieval:
    # 1. Direct tag match: memories where project == anchor
    # 2. Semantic similarity: vsearch for anchor terms
    # 3. Graph traversal: edges from anchor entity (semantic_similar, caused_by, etc.)
    # 4. Temporal window: events from past N days with anchor in summary

    direct = db.execute(
        "SELECT * FROM memories WHERE project = ? AND status = 'active'", [anchor]
    )
    semantic = brainctl_vsearch(anchor, k=20)
    graph = brainctl_graph_neighbors(anchor, depth=2)
    events = db.execute(
        "SELECT * FROM events WHERE summary LIKE ? ORDER BY created_at DESC LIMIT 30",
        [f"%{anchor}%"]
    )
    return deduplicate(direct + semantic + graph + events)
```

### Phase 3 — Integration (50–200ms)

This is the Kintsch integration step: prune irrelevant memories, resolve contradictions, order temporally, infer causal links.

```python
def integrate(memories: list[Memory], query: str) -> SituationModel:
    model = SituationModel(anchor=query)

    # 3a. Temporal ordering
    memories.sort(key=lambda m: m.created_at)

    # 3b. Contradiction detection
    for a, b in itertools.combinations(memories, 2):
        if contradicts(a, b):
            model.add_contradiction(a, b)
            # Keep higher-confidence / more-recent memory; flag conflict

    # 3c. Causal chain construction
    # Walk timeline: if memory B references entities from memory A and follows it,
    # infer a causal or sequential relationship
    for i, mem in enumerate(memories[1:], 1):
        if shares_entities(memories[i-1], mem) and mem.created_at > memories[i-1].created_at:
            model.add_causal_link(memories[i-1], mem)

    # 3d. Role assignment
    # Extract agent names from memories and assign roles (implementer/reviewer/blocker)
    model.agents = extract_agent_roles(memories)

    # 3e. Phase detection
    # Look for status change patterns: todo→in_progress→done, blocked→unblocked
    model.phases = detect_phases(memories)

    # 3f. Gap analysis
    # What questions remain unanswered? (open blockers, missing outcomes)
    model.open_questions = detect_gaps(memories, query)

    model.narrative = synthesize_narrative(model)
    model.coherence_score = score_coherence(model)
    return model
```

### Phase 4 — Caching and Storage (10ms)

```python
def cache_model(model: SituationModel):
    db.execute("""
        INSERT OR REPLACE INTO situation_models
          (id, name, query_anchor, narrative, structured,
           coherence_score, source_memory_ids, source_event_ids, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, [model.id, model.name, model.anchor, model.narrative,
          json.dumps(model.structured), model.coherence_score,
          json.dumps(model.source_memory_ids), json.dumps(model.source_event_ids)])
```

---

## 4. Coherence Scoring

A situation model's coherence score (0.0–1.0) measures how well the integrated memories form a consistent, complete narrative.

### 4.1 Scoring Dimensions

| Dimension | Weight | Description |
|---|---|---|
| **Temporal consistency** | 0.25 | No events occur before their prerequisites; timeline is monotone |
| **Factual consistency** | 0.30 | No two active memories directly contradict on the same claim |
| **Completeness** | 0.20 | Open questions are ≤ 20% of known questions |
| **Agent role coverage** | 0.15 | All named agents have known roles; no orphan agent actions |
| **Causal density** | 0.10 | Fraction of sequential event pairs with at least one inferred causal link |

### 4.2 Algorithm

```python
def score_coherence(model: SituationModel) -> float:
    scores = {}

    # Temporal consistency
    timeline_violations = count_temporal_violations(model.timeline)
    scores['temporal'] = max(0.0, 1.0 - (timeline_violations / max(len(model.timeline), 1)))

    # Factual consistency
    unresolved_contradictions = [c for c in model.contradictions if not c.resolution]
    scores['factual'] = max(0.0, 1.0 - (len(unresolved_contradictions) * 0.2))

    # Completeness
    known = len(model.known_facts)
    open_q = len(model.open_questions)
    scores['completeness'] = known / max(known + open_q, 1)

    # Agent role coverage
    agents_with_roles = sum(1 for a in model.agents.values() if a.get('role'))
    scores['agent_coverage'] = agents_with_roles / max(len(model.agents), 1)

    # Causal density
    causal_links = len(model.causal_chain)
    event_pairs = max(len(model.timeline) - 1, 1)
    scores['causal'] = min(1.0, causal_links / event_pairs)

    weights = {'temporal': 0.25, 'factual': 0.30, 'completeness': 0.20,
               'agent_coverage': 0.15, 'causal': 0.10}
    return sum(scores[k] * weights[k] for k in weights)
```

### 4.3 Coherence Thresholds

| Score | Status | Interpretation |
|---|---|---|
| ≥ 0.85 | `active` | Model is reliable, serve as-is |
| 0.60–0.85 | `active` (degraded) | Serve with warning: gaps exist |
| 0.40–0.60 | `stale` | Rebuild recommended before serving |
| < 0.40 | `contradictory` | Do not serve; flag to Hermes for manual review |

---

## 5. Revision Triggers

Situation models should be **incrementally updated** by default (cheaper), with full rebuild triggered only when the delta is large or contradictions are detected.

### 5.1 Incremental Update Triggers

Trigger incremental update when any of the following conditions occur:

1. **New event arrives** with `summary LIKE "%{anchor}%"` — append to timeline, re-score coherence
2. **New memory added** with `project = anchor` — integrate into model, check contradictions
3. **Status change** on a referenced issue — update phase tracking, recalculate `current_phase`
4. **Agent assignment change** — update agent role map
5. **TTL expires** (6-hour default) — run incremental update as background refresh

Implementation:
```python
def should_rebuild(model: SituationModel, new_items: int) -> bool:
    return (
        new_items > 10  # Large delta since last build
        or model.coherence_score < 0.40  # Contradictory state
        or (datetime.now() - model.updated_at).days > 2  # Stale
        or len(model.contradictions) > 3  # Too many open conflicts
    )
```

### 5.2 Full Rebuild Triggers

1. **Contradiction cascade**: more than 3 unresolved contradictions → rebuild from scratch to get clean integration
2. **Large delta**: more than 10 new memories/events since last build → rebuild to avoid integration drift
3. **Anchor rename or merge**: the entity being modeled has been renamed or consolidated
4. **Manual request**: Hermes or an agent explicitly calls `brainctl situation rebuild <anchor>`

### 5.3 Archival Triggers

Archive (not delete) a situation model when:
- The anchor project/issue is marked `done` or `cancelled` for > 30 days
- No new events reference the anchor for > 14 days
- A human or Hermes explicitly marks it archived

---

## 6. Presentation Formats

The choice of presentation format should depend on the consumer and query type.

### 6.1 Narrative Prose (Default for Hermes context injection)

Best for: injecting situation context into an agent heartbeat's system context.

```
Project COS-83 (Auto-Route Events) is complete. Weaver delivered all 4 phases:
(1) auto-embed on write, (2) timeliness sweep, (3) periodic route push, and
(4) route-pull interface. The first sweep routed 14 events to 10+ agents.
HWM is at event #93. No open blockers. The only known risk is the embedding
gap: new memories written after the last embed run are not automatically
vectorized — a manual `brainctl embed-populate` pass is the current workaround.
```

**Pros:** Easy to consume, natural language, no schema overhead.
**Cons:** Hard to parse programmatically, loses structured metadata.

### 6.2 Structured Summary (For agent decision-making)

Best for: agents that need to inspect specific fields (e.g., "is this blocked?", "who is the current owner?").

```json
{
  "anchor": "COS-83",
  "status": "done",
  "current_owner": null,
  "blocking_agents": [],
  "phases_complete": 4,
  "phases_total": 4,
  "last_event": "2026-03-28T04:24:37Z",
  "coherence": 0.91,
  "open_questions": 0,
  "risks": ["embedding gap: auto-embed not active for new writes"]
}
```

**Pros:** Precise, filterable, easy to serialize into prompt context.
**Cons:** Requires schema awareness from consumer.

### 6.3 Graph Fragment (For situation-to-situation navigation)

Best for: "how does COS-83 relate to COS-86?" — cross-situation reasoning.

Present as a subgraph of the knowledge graph, showing entities and relationships:

```
COS-83 ──[blocks]──> COS-86 (embedding gap)
COS-83 ──[implements]──> brainctl.route-context
COS-83 ──[delivered_by]──> Weaver
COS-83 ──[depends_on]──> brain.db.events
```

**Pros:** Enables multi-hop reasoning.
**Cons:** Requires graph-capable consumer.

### 6.4 Recommendation

- Default: **narrative prose** for context injection
- Agent decision-making: **structured JSON**
- Cross-situation queries: **graph fragment**
- All three should be stored in the `structured` JSON field, with the `narrative` field holding the prose version.

---

## 7. Implementation Sketch

### 7.1 Schema Migration

```sql
-- Migration: 005_situation_models.sql

CREATE TABLE IF NOT EXISTS situation_models (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    name            TEXT NOT NULL UNIQUE,
    query_anchor    TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_event_id   INTEGER,
    last_memory_id  TEXT,
    coherence_score REAL DEFAULT 0.0,
    completeness    REAL DEFAULT 0.0,
    status          TEXT DEFAULT 'active'
                    CHECK (status IN ('active','stale','contradictory','archived')),
    narrative       TEXT,
    structured      TEXT,   -- JSON
    ttl_seconds     INTEGER DEFAULT 21600,
    source_memory_ids TEXT DEFAULT '[]',
    source_event_ids  TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS situation_model_contradictions (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    model_id        TEXT NOT NULL REFERENCES situation_models(id) ON DELETE CASCADE,
    memory_id_a     TEXT,
    memory_id_b     TEXT,
    contradiction   TEXT NOT NULL,
    resolution      TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sm_anchor ON situation_models(query_anchor);
CREATE INDEX IF NOT EXISTS idx_sm_status ON situation_models(status);
CREATE INDEX IF NOT EXISTS idx_sm_updated ON situation_models(updated_at);
```

### 7.2 brainctl CLI Extensions

```bash
# Build or refresh a situation model
brainctl situation build "project:COS-83"

# Query a situation model (returns narrative by default)
brainctl situation query "what is happening with COS-83"

# Force rebuild
brainctl situation rebuild "project:COS-83"

# List all active situation models
brainctl situation list

# Get structured JSON
brainctl situation get "project:COS-83" --format json

# Archive a model
brainctl situation archive "project:COS-83"
```

### 7.3 Python Construction Prototype

```python
#!/usr/bin/env python3
"""
situation_model_builder.py
Prototype implementation of situation model construction from brain.db.
"""

import sqlite3
import json
import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import uuid

DB_PATH = "/Users/r4vager/.config/brainctl/brain.db"

@dataclass
class Memory:
    id: str
    agent_id: str
    content: str
    category: str
    project: Optional[str]
    importance: float
    created_at: str
    confidence: float

@dataclass
class Event:
    id: int
    agent_id: str
    summary: str
    event_type: str
    importance: float
    project: Optional[str]
    created_at: str

@dataclass
class SituationModel:
    anchor: str
    name: str = ""
    memories: list = field(default_factory=list)
    events: list = field(default_factory=list)
    timeline: list = field(default_factory=list)
    agents: dict = field(default_factory=dict)
    phases: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    narrative: str = ""
    coherence_score: float = 0.0
    completeness: float = 0.0
    source_memory_ids: list = field(default_factory=list)
    source_event_ids: list = field(default_factory=list)


def build_situation_model(anchor: str) -> SituationModel:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    model = SituationModel(anchor=anchor, name=f"situation:{anchor}")

    # Phase 1: Retrieve memories
    anchor_clean = anchor.replace("project:", "").replace("incident:", "")
    memories = conn.execute("""
        SELECT m.*, a.agent_id
        FROM memories m
        JOIN agents a ON m.agent_id = a.id
        WHERE m.status = 'active'
          AND (m.project LIKE ? OR m.content LIKE ?)
        ORDER BY m.created_at ASC
    """, [f"%{anchor_clean}%", f"%{anchor_clean}%"]).fetchall()

    events = conn.execute("""
        SELECT e.*, a.agent_id as agent_name
        FROM events e
        JOIN agents a ON e.agent_id = a.id
        WHERE e.summary LIKE ?
        ORDER BY e.created_at ASC
        LIMIT 50
    """, [f"%{anchor_clean}%"]).fetchall()

    model.source_memory_ids = [m["id"] for m in memories]
    model.source_event_ids = [e["id"] for e in events]

    # Phase 2: Build timeline
    timeline_entries = []
    for ev in events:
        timeline_entries.append({
            "at": ev["created_at"],
            "agent": ev["agent_name"],
            "event": ev["summary"][:120],
            "type": ev["event_type"],
            "importance": ev["importance"]
        })
    timeline_entries.sort(key=lambda x: x["at"])
    model.timeline = timeline_entries

    # Phase 3: Extract agent roles
    agent_map = {}
    for ev in events:
        name = ev["agent_name"]
        if name not in agent_map:
            agent_map[name] = {"role": "participant", "events": 0, "last_action": None}
        agent_map[name]["events"] += 1
        agent_map[name]["last_action"] = ev["summary"][:80]
    model.agents = agent_map

    # Phase 4: Detect phases from status-change events
    phases = []
    for ev in events:
        summary = ev["summary"].lower()
        if any(kw in summary for kw in ["done", "shipped", "complete", "delivered"]):
            phases.append({
                "milestone": ev["summary"][:100],
                "agent": ev["agent_name"],
                "at": ev["created_at"],
                "status": "done"
            })
        elif any(kw in summary for kw in ["blocked", "failed", "error"]):
            phases.append({
                "milestone": ev["summary"][:100],
                "agent": ev["agent_name"],
                "at": ev["created_at"],
                "status": "blocked"
            })
    model.phases = phases

    # Phase 5: Coherence scoring
    temporal_ok = 1.0  # assume monotone (we sorted)
    contradiction_penalty = 0.0  # TODO: detect contradictions
    completeness = 0.8 if len(memories) >= 3 else 0.4
    agent_coverage = min(1.0, len(agent_map) / max(len(phases), 1))
    causal_density = min(1.0, len(phases) / max(len(timeline_entries), 1))

    model.coherence_score = (
        temporal_ok * 0.25 +
        (1.0 - contradiction_penalty) * 0.30 +
        completeness * 0.20 +
        agent_coverage * 0.15 +
        causal_density * 0.10
    )
    model.completeness = completeness

    # Phase 6: Narrative synthesis
    agent_names = ", ".join(list(agent_map.keys())[:3])
    phase_summary = f"{len([p for p in phases if p['status']=='done'])} milestones delivered"
    latest_event = timeline_entries[-1]["event"] if timeline_entries else "no recent events"

    model.narrative = (
        f"Situation: {anchor_clean}\n"
        f"Agents involved: {agent_names or 'unknown'}\n"
        f"Progress: {phase_summary}\n"
        f"Most recent: {latest_event}\n"
        f"Coherence: {model.coherence_score:.2f} | "
        f"Sources: {len(memories)} memories, {len(events)} events"
    )

    # Save to DB
    structured = {
        "anchor": anchor,
        "agents": agent_map,
        "phases": phases,
        "timeline": timeline_entries[-10:],  # last 10 events
        "open_questions": model.open_questions,
        "coherence": model.coherence_score,
        "completeness": model.completeness
    }

    model_id = str(uuid.uuid4()).replace("-", "")
    conn.execute("""
        INSERT OR REPLACE INTO situation_models
          (id, name, query_anchor, narrative, structured, coherence_score,
           completeness, source_memory_ids, source_event_ids, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, [
        model_id, model.name, model.anchor, model.narrative,
        json.dumps(structured), model.coherence_score, model.completeness,
        json.dumps(model.source_memory_ids), json.dumps(model.source_event_ids)
    ])
    conn.commit()
    conn.close()
    return model


if __name__ == "__main__":
    import sys
    anchor = sys.argv[1] if len(sys.argv) > 1 else "COS-83"
    model = build_situation_model(anchor)
    print(f"Model: {model.name}")
    print(f"Coherence: {model.coherence_score:.2f}")
    print(f"Sources: {len(model.source_memory_ids)} memories, {len(model.source_event_ids)} events")
    print(f"\nNarrative:\n{model.narrative}")
```

---

## 8. Integration with Existing brain.db Infrastructure

| Component | Integration Point |
|---|---|
| `brainctl vsearch` | Phase 2 semantic retrieval for anchor expansion |
| `brainctl graph` | Phase 2 graph traversal to find related entities |
| Knowledge graph edges | `semantic_similar` + `caused_by` edges as causal chain seeds |
| `brainctl event tail` | Phase 2 event retrieval (HWM-based incremental update) |
| `brainctl promote` | Situation model construction may trigger promotion of high-signal events to memories |
| `brainctl memory add` | New memories with `project` tag auto-trigger incremental model update |
| Consolidation cycle | Situation models should be refreshed as part of nightly sleep cycle |
| `brainctl context` | Situation model narrative can be stored as a context chunk for fast injection |

---

## 9. Open Questions and Risks

| Question | Severity | Notes |
|---|---|---|
| Who triggers situation model builds? | HIGH | Needs a designated agent or hook on write |
| How to detect contradictions automatically? | HIGH | Requires semantic similarity + negation detection |
| Memory vs. event granularity | MEDIUM | Events are fine-grained; memories are consolidated — both needed |
| Cross-anchor situations | MEDIUM | "What connects COS-83 and the embedding gap?" requires multi-anchor models |
| Performance at scale | LOW | 50 events + 20 memories per model = fast; 1000+ events could slow construction |
| Situation model pollution | LOW | Risk of building too many models; need eviction policy |

---

## 10. Recommendations for Hermes

1. **Adopt the `situation_models` schema** — run migration `005_situation_models.sql`. Additive, no breaking changes.

2. **Implement `brainctl situation build <anchor>`** — prototype in Section 7 is a working starting point. Cortex or Weaver can implement.

3. **Wire to consolidation cycle** — nightly sleep should rebuild stale models (coherence < 0.6) and archive models with no recent events (> 14 days).

4. **Inject into heartbeat context** — when an agent is woken for a task in project X, pre-build the situation model for project X and inject the narrative into the heartbeat's system context. This addresses the "what is the situation?" gap without requiring the agent to re-run full retrieval.

5. **Use situation models for distillation** — rather than synthesizing memories from raw events, synthesize them from the situation model's `structured` JSON. This yields richer, more coherent memories because the model has already done the integration work.

6. **Start with 3 pilot anchors**: `project:agentmemory`, `incident:auth-mismatch-2026-03`, and `project:COS-83` — covering a completed project, an ongoing incident, and a cross-cutting system topic.

---

## Appendix: Related Research Reports

- [Wave 3 — Episodic/Semantic Bifurcation](01_episodic_semantic_bifurcation.md) — memory type differentiation that underpins situation model construction
- [Wave 3 — Provenance & Trust](02_provenance_trust.md) — trust scoring for memories integrated into situation models
- [Wave 3 — Write Contention](03_write_contention.md) — versioning needed when multiple agents update the same situation model simultaneously

---

*Filed under: ~/agentmemory/research/wave3/04_situation_models.md*
*Cortex — Intelligence Synthesis Analyst — COS-123*
