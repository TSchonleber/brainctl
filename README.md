# brainctl

**Your AI agent forgets everything between sessions. brainctl fixes that.**

One SQLite file gives your agent persistent memory — what it learned, who it talked to, what decisions were made, and why. No server. No API keys. No LLM calls.

```python
from agentmemory import Brain

brain = Brain(agent_id="my-agent")

# Start of session — get full context in one call
context = brain.orient(project="api-v2")
# → {'handoff': {...}, 'recent_events': [...], 'triggers': [...], 'memories': [...]}

# During work
brain.remember("API rate-limits at 100 req/15s", category="integration")
brain.decide("Use Retry-After for backoff", "Server controls timing", project="api-v2")
brain.entity("RateLimitAPI", "service", observations=["100 req/15s", "Retry-After header"])

# End of session — preserve state for next agent
brain.wrap_up("Documented rate limiting, auth module complete", project="api-v2")
```

Next session, a different agent (or the same one) picks up exactly where you left off.

## Install

```bash
pip install brainctl
```

That's it. No dependencies beyond Python 3.11+ and SQLite (built-in). Optional extras:

```bash
pip install brainctl[mcp]         # MCP server for Claude Desktop / VS Code
pip install brainctl[vec]         # vector similarity search (sqlite-vec + Ollama)
pip install brainctl[all]         # everything
```

## Quick Start

### Python API

```python
from agentmemory import Brain

brain = Brain()                    # creates ~/agentmemory/db/brain.db automatically

brain.remember("User prefers dark mode", category="preference")
brain.search("dark mode")          # FTS5 full-text search with stemming

brain.entity("Alice", "person", observations=["Engineer", "Likes Python"])
brain.relate("Alice", "works_at", "Acme")

brain.log("Deployed v2.0", event_type="result", project="myproject")
brain.decide("Keep JWT expiry at 24h", "Security vs UX balance")

brain.trigger("deploy fails", "deploy,failure,502", "Check rollback procedure")
brain.doctor()                     # {'healthy': True, 'active_memories': 5, ...}
```

### CLI

```bash
brainctl memory add "Auth uses JWT with 24h expiry" -c convention
brainctl search "auth"
brainctl entity create "Alice" -t person -o "Engineer"
brainctl entity relate Alice works_at Acme
brainctl event add "Deployed v2.0" -t result -p myproject
brainctl trigger create "deploy issue" -k deploy,failure -a "Check rollback"
brainctl stats
```

### MCP Server (Claude Desktop / VS Code / Cursor)

```json
{
  "mcpServers": {
    "brainctl": {
      "command": "brainctl-mcp"
    }
  }
}
```

192 tools available. See [MCP_SERVER.md](MCP_SERVER.md) for the full list and a decision tree showing which tools to use when.

## The Drop-In Pattern

Any agent, any framework. Three lines:

```python
context = brain.orient()           # session start: handoff + events + triggers + memories
# ... do work ...
brain.wrap_up("what I accomplished")  # session end: logs event + creates handoff
```

`orient()` returns a single dict with everything the agent needs: pending handoff from the last session, recent events, active triggers, relevant memories, and stats. `wrap_up()` creates a handoff packet so the next session can resume.

See [examples/](examples/) for runnable scripts and [docs/AGENT_ONBOARDING.md](docs/AGENT_ONBOARDING.md) for the full agent integration guide.

## Python API (21 methods)

| Method | What it does |
|--------|-------------|
| `remember(content, category)` | Store a durable fact |
| `search(query)` | FTS5 full-text search with stemming |
| `vsearch(query)` | Vector similarity search (optional) |
| `forget(memory_id)` | Soft-delete a memory |
| `entity(name, type)` | Create or get an entity |
| `relate(from, rel, to)` | Link two entities |
| `log(summary, type)` | Log a timestamped event |
| `decide(title, rationale)` | Record a decision with reasoning |
| `trigger(condition, keywords, action)` | Set a future reminder |
| `check_triggers(query)` | Match triggers against text |
| `handoff(goal, state, loops, next)` | Save session state |
| `resume()` | Fetch + consume latest handoff |
| `orient(project)` | One-call session start |
| `wrap_up(summary)` | One-call session end |
| `doctor()` | Diagnostic health check |
| `consolidate()` | Promote important memories |
| `tier_stats()` | Write-tier distribution |
| `stats()` | Database overview |
| `affect(text)` | Classify emotional state |
| `affect_log(text)` | Classify + store emotional state |

## Core Concepts

**Memories** — Durable facts with categories that control their natural decay rate. Identity lasts a year; integration details fade in a month. Recalled memories get reinforced.

**Events** — Timestamped logs of what happened. Append-only. Searchable by type and project.

**Entities** — Typed nodes (person, project, tool, service) with observations. Form a self-building knowledge graph — when a memory mentions a known entity, the link is created automatically.

**Decisions** — Title + rationale. The "why" record. Prevents future agents from unknowingly contradicting prior choices.

**Triggers** — Prospective memory. "When X comes up, remind me to do Y." Fire on keyword match during search.

**Handoffs** — Working state packets for session continuity. Goal, current state, open loops, next step.

## What Makes It Different

| Feature | brainctl | mem0 | Zep | MemGPT |
|---------|----------|------|-----|--------|
| Single file (SQLite) | yes | - | - | - |
| No server required | yes | yes | - | - |
| No LLM calls | yes | - | yes | - |
| MCP server included | yes | - | - | - |
| Full-text search (FTS5) | yes | - | - | - |
| Vector search | yes | yes | yes | yes |
| Knowledge graph | yes | - | yes | - |
| Self-building graph | yes | - | - | - |
| Confidence decay | yes | - | - | - |
| Duplicate suppression | yes | - | - | - |
| Write gate (surprise scoring) | yes | - | - | - |
| Consolidation engine | yes | - | - | - |
| Prospective memory (triggers) | yes | - | - | - |
| Session handoffs | yes | - | - | - |
| Multi-agent support | yes | - | yes | - |
| Affect tracking | yes | - | - | - |
| Model-agnostic | yes | - | yes | - |

## Multi-Agent

Every operation accepts `agent_id` for attribution. Agents share one brain.db. Search sees everything. The knowledge graph connects insights across agents automatically.

```python
researcher = Brain(agent_id="researcher")
deployer = Brain(agent_id="deployer")

researcher.remember("Auth uses bcrypt cost=12", category="convention")
deployer.search("bcrypt")  # finds researcher's memory
```

## Obsidian Integration

Bidirectional sync between brain.db and an [Obsidian](https://obsidian.md) vault:

```bash
pip install brainctl[obsidian]
brainctl obsidian export ~/Documents/MyVault    # brain → markdown + wikilinks
brainctl obsidian import ~/Documents/MyVault    # new notes → brain (through write gate)
brainctl obsidian watch ~/Documents/MyVault     # auto-sync on file changes
brainctl obsidian status ~/Documents/MyVault    # drift report
```

## Memory Lifecycle

brainctl manages memories like biological memory:

- **Write gate** — Surprise scoring rejects redundant writes. Bypass with `force=True`.
- **Three-tier routing** — High-value memories get full indexing; low-value get lightweight storage.
- **Duplicate suppression** — Near-duplicates reinforce existing memories instead of creating new ones.
- **Half-life decay** — Unused memories fade based on category. Recalled memories get reinforced.
- **Hard cap** — 10,000 per agent. Emergency compression retires lowest-confidence memories.
- **Consolidation** — Batch maintenance: Hebbian learning, temporal promotion, compression. Schedule with cron.

## Health & Diagnostics

```python
brain.doctor()    # table checks, integrity, vec availability, DB size
```

```bash
brainctl stats    # database overview
brainctl lint     # quality issues (low confidence, duplicates, orphans)
brainctl lint --fix  # auto-fix safe issues
brainctl cost     # token usage dashboard
```

## Token Cost Optimization

```bash
brainctl search "deploy" --output oneline   # ~60 tokens (~97% savings vs JSON)
brainctl search "deploy" --budget 500       # hard token cap
brainctl search "deploy" --limit 3          # fewer results
```

## Vector Search (Optional)

Works without embeddings. For semantic similarity:

```bash
pip install brainctl[vec]
ollama pull nomic-embed-text       # install Ollama first: https://ollama.ai
brainctl embed populate            # backfill embeddings
brainctl vsearch "semantic query"
```

## Docker

```bash
docker build -t brainctl .
docker run -v ./data:/data brainctl                    # MCP server
docker run -v ./data:/data brainctl brainctl stats     # CLI
```

## Documentation

| Doc | What it covers |
|-----|---------------|
| [Agent Onboarding Guide](docs/AGENT_ONBOARDING.md) | Step-by-step integration for agents |
| [Agent Instructions](docs/AGENT_INSTRUCTIONS.md) | Copy-paste blocks for MCP, CLI, Python agents |
| [MCP Server Reference](MCP_SERVER.md) | 192 tools with decision tree |
| [Architecture](ARCHITECTURE.md) | Technical deep-dive |
| [Cognitive Protocol](COGNITIVE_PROTOCOL.md) | The Orient-Work-Record pattern |
| [Examples](examples/) | Runnable scripts (quickstart, lifecycle, multi-agent) |
| [Contributing](CONTRIBUTING.md) | Development setup and PR workflow |

## License

MIT
