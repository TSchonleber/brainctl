# Cognitive Protocol — How Agents Use brainctl

A guide for agent developers integrating brainctl as persistent memory.

## The Core Loop

Every agent session follows the same pattern:
1. **Orient** — search before you work
2. **Work** — save discoveries as you go
3. **Record** — log what you did when you finish

## 1. Orient (Before Working)

Before starting any task, check what's already known:

```bash
brainctl -a myagent search "task keywords" --limit 10
brainctl -a myagent procedure search "task keywords" --limit 5
brainctl event tail -n 10
brainctl decision list
```

You're looking for:
- Has someone already tried this? What happened?
- Are there decisions that constrain your approach?
- Is there context you'd be foolish to ignore?

**Don't skip orientation.** The most common failure is redoing work or
contradicting a prior decision because you didn't check first.

## 2. Work (Save Discoveries Immediately)

When you find something non-obvious, save it right away:

```bash
brainctl -a myagent memory add "what you discovered" -c CATEGORY -s SCOPE
```

If what you learned is reusable execution knowledge rather than a plain fact,
store it as a procedure:

```bash
brainctl -a myagent procedure add \
  --title "staging deploy runbook" \
  --goal "deploy to staging safely" \
  --step "run tests" \
  --step "brainctl migrate" \
  --step "deploy and verify health"
```

**Good memories:** "The API rate-limits at 100 req/15s with Retry-After header"
**Bad memories:** "I ran npm install" (trivial) / "The build passed" (transient)

**Good procedures:** rollback plans, troubleshooting sequences, migration
runbooks, validated tool-use recipes.

### Categories

| Category | Use for |
|----------|---------|
| `environment` | System facts, tool versions |
| `convention` | How things are done |
| `project` | Project-specific knowledge |
| `decision` | Something decided |
| `lesson` | Learned the hard way |
| `preference` | User preferences |
| `integration` | Third-party API behavior |

### Scopes

- `global` — everyone needs this
- `project:NAME` — only relevant to that project
- `agent:NAME` — only relevant to one agent

## 3. Record (After Finishing)

Every agent must log a completion event:

```bash
brainctl -a myagent event add "WHAT you did and WHAT happened" -t result -p PROJECT
```

**Good:** "Fixed auth scoping on delete routes. Added 403 for cross-org. Tests pass."
**Bad:** "Done" / "Worked on issue #36"

Event types (most common in day-to-day work): `result`, `observation`, `decision`, `error`, `warning`, `handoff`. The full enum also includes `task_update`, `artifact`, `session_start`, `session_end`, `memory_promoted`, `memory_retired`, and `stale_context` — see `VALID_EVENT_TYPES` in `src/agentmemory/_impl.py` for the authoritative list.

For decisions, also record rationale:

```bash
brainctl -a myagent decision add "what you decided" \
  -r "why — the reasoning, not just the conclusion"
```

## Memory Lifecycle

Memories follow a natural lifecycle: **Write → Decay → Consolidation → Retirement**

1. **Write**: Agent stores a memory with confidence and importance scores
2. **Decay**: Access scores decrease over time if never recalled
3. **Consolidation**: The hippocampus merges related low-value memories,
   strengthens frequently-accessed ones, synthesizes new connections
4. **Retirement**: Memories below threshold are archived

Set low confidence on uncertain facts:
```bash
brainctl -a myagent memory add "might be X" -c project --confidence 0.5
```

## The Write Gate

Not every observation deserves storage. brainctl uses a surprise-based write
gate: W(m) scores how novel a candidate memory is relative to what's stored.
High-surprise memories pass through; low-surprise memories are suppressed.
This keeps the store high-signal without manual curation.

## Prospective Memory Triggers

Set fire-when conditions for future events:

```bash
brainctl trigger add "when deployment fails, check the rollback runbook" \
  --condition "deployment AND (fail OR error)" --agent myagent
```

Triggers surface relevant reminders automatically when conditions match.

## Health Monitoring

```bash
brainctl health
```

Reports on memory coverage, recall quality, temporal balance, and embedding
coverage. Run periodically to catch degradation early.

## Anti-Patterns

1. **Skipping search before work** — you will redo work or violate decisions
2. **Not logging after work** — future agents fly blind
3. **Saving trivial state as memories** — pollutes the signal
4. **Wrong scope** — project facts at `global` clutters every agent
5. **Using `-a unknown`** — attribution matters for audit trails
6. **Task progress in memories** — use events or your issue tracker

## TL;DR

1. **Search first**: `brainctl search`, `brainctl event tail`
2. **Save discoveries**: `brainctl memory add` with right category and scope
3. **Log outcomes**: `brainctl event add` with actual results
4. **Record decisions**: `brainctl decision add` with rationale
5. **Trust the hippocampus**: decay, consolidation, and pruning run automatically
