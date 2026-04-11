# Claude Code — brainctl / agentmemory

## What This Is
Unified agent memory system. SQLite-backed (brain.db) with FTS5, vector embeddings (sqlite-vec + Ollama nomic-embed-text), knowledge graph, affect tracking, belief collapse mechanics, and AGM conflict resolution.

Published as `brainctl` on PyPI (v1.1.2).

## Key Paths
- **DB:** `db/brain.db` (WAL mode, foreign keys ON)
- **CLI:** `bin/brainctl` — main CLI entry
- **MCP server:** `bin/brainctl-mcp` — stdio MCP server (192 tools). Run with `python3`
- **Source:** `src/agentmemory/` — Python package
- **Config:** `config/` — quiet hours, consolidation schedules
- **Agents:** `agents/` — per-agent config (pipeline, engram, etc.)

## Build & Test
```bash
pip install -e .          # dev install
brainctl stats            # verify DB
brainctl search "test"    # test search
python3 bin/brainctl-mcp --list-tools  # verify MCP (needs mcp module)
```

## Architecture
- Tables: memories, events, entities, decisions, context, knowledge_edges, affect_log, access_log, agent_state, agent_beliefs
- FTS5 indexes on memories, events, entities
- Vector embeddings via sqlite-vec extension
- W(m) worthiness gate on memory writes (surprise scoring + semantic dedup)
- PII recency gate (Proactive Interference Index) on supersedes
- Bayesian alpha/beta tracking on memory recall

## Conventions
- Agent IDs: use descriptive names like `my-agent`, `research-bot`, `code-reviewer`
- Memory categories: convention, decision, environment, identity, integration, lesson, preference, project, user
- Event types: artifact, decision, error, handoff, result, session_start/end, task_update, warning, observation
- Entity types: agent, concept, document, event, location, organization, person, project, service, tool

## Don't Touch
- Migration files in `db/migrations/` — append-only
- The W(m) gate logic without understanding surprise scoring
- Quiet hours scripts — they're cron-scheduled
