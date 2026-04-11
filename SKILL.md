---
name: brainctl
description: "Unified agent memory CLI — read, write, search, and maintain the shared memory spine (brain.db). Use for persistent cross-session memory, knowledge graph, event logging, decisions, affect tracking, and consolidation."
version: 1.1.2
tags: [memory, agents, sqlite, knowledge-graph, mcp, cli]
trigger: "When you need to store durable facts, log events, track entities/relations, record decisions, search past context, or orient yourself in a multi-agent environment."
---

# brainctl — Agent Memory CLI

## Overview

brainctl is the single interface for agent memory. One SQLite file (`brain.db`), no server required. All agents share it. Every write is attributed to the writing agent.

- **GitHub:** https://github.com/TSchonleber/brainctl
- **Database:** `~/agentmemory/db/brain.db` (override with `BRAIN_DB` env var)
- **CLI binary:** `~/bin/brainctl` or `~/.local/bin/brainctl`
- **MCP server:** `brainctl-mcp` (192 tools, same DB)

Already installed on this machine. If missing, see GitHub README for install instructions.

## Design Principle: Model-Agnostic

brainctl has ZERO LLM dependencies. No Anthropic, no OpenAI, no vendor SDK. It is pure SQLite + Python stdlib. The agent does the thinking, brainctl does the remembering. If an agent wants LLM-powered consolidation, the agent calls its own LLM and feeds results back via `brainctl memory add`. Functions like `consolidate_memories()` and `compress_memories()` accept an optional `llm_fn` parameter — callers can inject their own, but brainctl never calls one itself.

## Agent Attribution

ALWAYS pass `-a AGENT_NAME` to attribute writes. Examples:
- `-a my-agent` — your agent's unique name
- `-a human` — manual/human entries
- `-a unknown` — fallback if unsure

## Commands — Quick Reference

### Memories (durable facts)

```bash
# Add a memory (categories: identity, user, environment, convention, project, decision, lesson, preference, integration)
brainctl -a my-agent memory add "Python 3.12 is the minimum version" -c convention

# Search memories
brainctl -a my-agent memory search "python version"

# List recent memories
brainctl -a my-agent memory list --limit 10
```

### Events (timestamped logs)

```bash
# Log an event (types: observation, result, decision, error, handoff, task_update, artifact, session_start, session_end, warning, stale_context)
brainctl -a my-agent event add "Deployed v2.0 to production" -t result -p myproject

# Search events
brainctl -a my-agent event search -q "deploy"

# Recent events
brainctl -a my-agent event tail -n 20
```

### Entities (typed knowledge graph)

```bash
# Create an entity (types: person, project, tool, concept, org, location, etc.)
brainctl -a my-agent entity create "Alice" -t person -o "Engineer; Likes Python; Based in NYC"

# Get entity details + relations
brainctl -a my-agent entity get Alice

# Add observations to existing entity
brainctl -a my-agent entity observe "Alice" "Now leads the infrastructure team"

# Create directed relation
brainctl -a my-agent entity relate Alice works_at Acme

# Search entities
brainctl -a my-agent entity search "engineer"
```

### Decisions

```bash
brainctl -a my-agent decision add "Switch to local inference" -r "Need local-first fallback"
```

### Cross-Table Search

```bash
# Search across memories + events + entities at once
brainctl -a my-agent search "deployment"
```

### Prospective Memory (Triggers)

```bash
# Create a trigger that fires on future queries
brainctl trigger create "Alice mentions vacation" -k vacation,alice -a "Remind about project deadline"

# Check triggers against a query
brainctl trigger check "alice is going on vacation"
```

### Reports & Lint

```bash
brainctl report                        # full brain report
brainctl report --topic "deploy"       # topic-focused
brainctl report --entity "Alice"       # entity deep-dive

brainctl lint                          # health check (JSON)
brainctl lint --output text            # human-readable
brainctl lint --fix                    # auto-fix safe issues
```

### Stats

```bash
brainctl stats                         # DB overview: table counts, sizes, health
```

### Init (first-time setup)

```bash
brainctl init                          # create brain.db with full schema
```

## Output Formats

Control token consumption with `--output`:

```bash
brainctl search "deploy" --output json      # default: pretty JSON
brainctl search "deploy" --output compact   # minified JSON (~24% fewer tokens)
```

## Affect Tracking

Functional affect states (frustration, urgency, satisfaction, confusion, confidence, curiosity) — not sentiment analysis. Influences memory formation and retrieval.

```bash
brainctl affect classify 'the deploy failed and rollback is stuck'
# → {"state": "frustration", "valence": -0.7, "arousal": 0.8, ...}

brainctl affect log 'finally resolved the outage after 4 hours'
brainctl affect check                  # current state + trajectory
brainctl affect monitor --watch        # live-stream changes
```

## Consolidation Engine

Pure-math memory maintenance — no LLM required. Run periodically or via cron:

```bash
brainctl-consolidate decay             # confidence decay on unused memories
brainctl-consolidate compress          # merge redundant memories (pure string dedup, no LLM)
brainctl-consolidate promote           # promote important events to memories
brainctl-consolidate sweep             # full maintenance cycle
```

Also includes: Hebbian co-retrieval strengthening, temporal demotion, EWC importance locking, recall boost, experience replay. All pure SQLite math — no external calls.

## MCP Server (192 tools)

If your adapter supports MCP, configure `brainctl-mcp` as an MCP server:

```json
{"mcpServers": {"brainctl": {"command": "brainctl-mcp"}}}
```

Core tools: `memory_add`, `memory_search`, `event_add`, `event_search`, `entity_create`, `entity_get`, `entity_search`, `entity_observe`, `entity_relate`, `decision_add`, `search`, `stats`, `handoff_add`, `handoff_latest`, `trigger_check`

192 tools total covering memory, events, entities, decisions, triggers, handoffs, consolidation, affect, beliefs, temporal reasoning, and more. See [MCP_SERVER.md](MCP_SERVER.md) for the full list and decision tree.

All tools accept optional `agent_id` and `profile` (task-scoped search presets). CLI and MCP are fully interchangeable — same DB.

## Post-Checkout Orientation (Task-Based Agents)

After checking out a task, orient yourself:

```bash
# Search for relevant context
brainctl -a YOUR_AGENT search "keywords about your task"
brainctl -a YOUR_AGENT memory search "relevant topic"
brainctl event tail -n 10

# After completing work, log it
brainctl -a YOUR_AGENT event add "what you did and the outcome" -t result -p PROJECT_NAME

# If you learned something durable
brainctl -a YOUR_AGENT memory add "the fact or lesson" -c lesson

# If you made a decision
brainctl -a YOUR_AGENT decision add "what was decided" -r "why"
```

## Architecture

```
brain.db (single SQLite file, 80+ tables)
├── memories          FTS5 full-text + optional vector search
├── events            timestamped logs with importance scoring
├── entities          typed nodes (person, project, tool, concept...)
├── knowledge_edges   directed relations between any table rows
├── decisions         recorded with rationale
├── memory_triggers   prospective memory (fire on future conditions)
├── affect_log        per-agent functional affect state tracking
└── 60+ more          consolidation, beliefs, policies, epochs...
```

## Valid Categories & Types

- **Memory categories:** identity, user, environment, convention, project, decision, lesson, preference, integration
- **Event types:** observation, result, decision, error, handoff, task_update, artifact, session_start, session_end, warning, stale_context
- **Task statuses:** pending, in_progress, blocked, completed, cancelled
- **Task priorities:** critical, high, medium, low

## Pitfalls

1. **Namespace collision:** If `~/agentmemory/` exists as a directory, Python may pick it up as a namespace package instead of the installed `agentmemory`. Set `PYTHONPATH` explicitly if needed.
2. **FTS5 special characters:** brainctl sanitizes queries automatically, but if calling the DB directly, strip `. & | * " ( ) \ - @ ^` from search terms.
3. **Agent attribution:** Always use `-a`. Writes without attribution default to "unknown" and are harder to trace.
4. **Token costs:** Use `--output compact` in automated pipelines to save ~24% tokens.
5. **Vector search optional:** brainctl works fine without embeddings. Only needed for semantic similarity search.
6. **Broken local install:** If `~/bin/brainctl` throws import errors, the wrapper may have a circular import. Run from the repo directly: `cd ~/agentmemory && python3 -m agentmemory.cli` or use `~/.local/bin/brainctl`.
7. **When debugging brainctl itself:** Don't chase broken local wrappers — check the GitHub repo (`https://github.com/TSchonleber/brainctl`) for the canonical source. The local install has namespace collisions with `~/agentmemory/` directory.
8. **No batch command:** The `brainctl batch` subcommand was removed (vendor lock-in). If you need bulk LLM operations, do them in your own agent code and write results back via `brainctl memory add`.
