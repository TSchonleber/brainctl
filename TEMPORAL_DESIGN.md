# Temporal Cognition — Sense of Time in the Memory Spine

## The Problem

Agents wake up with no felt sense of time. Every memory looks equally "present."
A 3-month-old decision and a 3-hour-old discovery have the same cognitive weight.
This produces flat, ahistorical reasoning — like a person with no sense of how long
ago things happened. You can function, but you can't think narratively, judge
relevance by recency, or feel the rhythm of a project.

## What "Sense of Time" Actually Requires

### 1. Epochs (temporal landmarks)

Humans chunk time into eras: "college," "first job," "after the move."
Agents need the same structure.

```
epochs table:
  id, name, description, started_at, ended_at, parent_epoch_id
```

Examples:
- "Pre-Paperclip era" (before 2026-03-27)
- "CostClock production push" (2026-03-28 to ?)
- "Memory spine buildout" (2026-03-28, sub-epoch of production push)

Every memory and event gets tagged with its epoch. When the hippocampus
processes memories, it understands narrative position: "this was an early
decision" vs "this was a late refinement."

### 2. Recency gradient (temporal weighting on recall)

Search results should be weighted by temporal distance. Not just sorted
by date — exponentially weighted so recent results dominate unless the
query specifically asks for historical context.

```
temporal_weight = base_relevance * exp(-lambda * days_since_event)
```

Lambda should be tunable per scope:
- Global memories: slow decay (lambda = 0.01, half-life ~70 days)
- Project memories: medium decay (lambda = 0.03, half-life ~23 days)
- Integration/environment: very slow decay (lambda = 0.005)
- Task-specific context: fast decay (lambda = 0.1, half-life ~7 days)

### 3. Temporal relevance type

Separate from importance. A memory has an inherent temporal horizon:

```
temporal_class:
  - 'permanent'   — never expires (identity, core decisions, user preferences)
  - 'long'        — relevant for months (architecture decisions, conventions)
  - 'medium'      — relevant for weeks (current sprint context, active project state)
  - 'short'       — relevant for days (in-progress task context, transient state)
  - 'ephemeral'   — relevant for hours (API is down, build is broken, PR is open)
```

The hippocampus uses temporal_class to decide what to prune. An ephemeral
memory that's 3 days old is dead weight. A permanent memory that's 3 months
old is still vital.

### 4. Duration awareness

The hippocampus should compute and surface:
- Project age: "CostClock has been active for 47 days"
- Gap detection: "No events from any agent for 72 hours" (something shifted)
- Burst detection: "14 events in the last 2 hours" (intense sprint)
- Memory age distribution: "80% of memories are from the last 5 days"
  (suggests thin long-term knowledge — consolidation needed)

This metadata should be included in the temporal context summary that
agents receive when they orient at session start.

### 5. Interaction cadence

Track session frequency and density:
- Sessions per day (rolling 7-day average)
- Average session duration (by event count)
- Gaps between sessions
- Which agents are active vs dormant

Surface this as a "rhythm report" — agents should know if the operation
is in an intense daily cadence or a slow weekly rhythm. This affects
how aggressively to consolidate, how much context to load, and how
to weight recent vs historical memories.

### 6. Causal threading

Events should link to their causes:

```
events.caused_by_event_id  — FK to events.id
events.causal_chain_root   — FK to the originating event of a chain
```

This allows the hippocampus to replay causal sequences:
"COS-36 was created -> assigned to Codex -> Codex found the workspace
scoping gap -> fixed it -> PR merged -> invoice security improved"

Without causal threading, events are just a timeline. With it,
they're a narrative.

### 7. Temporal context summary

Before any agent starts work, it should receive a compact temporal
orientation — not raw timestamps, but a narrative sense of where
things stand:

```
TEMPORAL CONTEXT (auto-generated):
- Current epoch: CostClock Production Push (day 3)
- Last activity: 2 hours ago (hermes: built memory spine)
- Cadence: High (8 sessions today, 3 agents active)
- Recent decisions: 5 in last 48h (memory architecture evolving rapidly)
- Stale areas: Harvest integration (no activity in 5 days)
- Active threads: COS-32 through COS-43 (12 open issues)
```

This is what "sense of time" feels like from the inside. Not timestamps.
A felt understanding of where you are in the story.

---

## Implementation Plan

### Phase 1: Schema additions
- Add `epochs` table
- Add `temporal_class` column to memories (default: 'medium')
- Add `epoch_id` FK to memories and events
- Add `caused_by_event_id` to events

### Phase 2: Hippocampus temporal logic
- Recency-weighted search (modify brainctl search to apply temporal gradient)
- Epoch auto-detection (cluster events by time gaps and topic shifts)
- Temporal context summary generator (runs at session start)

### Phase 3: Cadence tracking
- Session frequency tracking in agent_state
- Gap and burst detection
- Rhythm report generation

### Phase 4: Causal reasoning
- Causal link capture (events reference their causes)
- Narrative arc reconstruction from causal chains
- Temporal reasoning in hippocampus consolidation

---

## Open Questions (for Chief + Hermes to resolve iteratively)

1. Should epochs be manually declared or auto-detected from event clustering?
   (Probably both — auto-suggested, human-confirmed)

2. How does temporal weighting interact with confidence weighting?
   (Multiplicative? Separate scores? Blended rank?)

3. Should the temporal context summary be injected into every agent's
   system prompt, or pulled on-demand?
   (Probably injected for Hermes/OpenClaw, on-demand for Paperclip agents)

4. What's the right cadence for the hippocampus cycle?
   (Every 6 hours? Every 24 hours? Triggered by event count threshold?)

5. How do we handle "temporal landmarks" — events that are significant
   enough to define epoch boundaries?
   (Ship to production, major architectural decision, new team member, pivot)
