# brainctl

A cognitive memory system for AI agents. Single SQLite file. No server required.

```python
from agentmemory import Brain

brain = Brain()
brain.remember("User prefers dark mode")
brain.search("dark mode")
brain.entity("Alice", "person", observations=["Engineer", "Likes Python"])
brain.relate("Alice", "works_at", "Acme")
brain.log("Deployed v2.0")
```

## MCP Server (Claude Desktop / VS Code)

```json
{
  "mcpServers": {
    "brainctl": {
      "command": "brainctl-mcp"
    }
  }
}
```

12 tools: `memory_add`, `memory_search`, `event_add`, `event_search`, `entity_create`, `entity_get`, `entity_search`, `entity_observe`, `entity_relate`, `decision_add`, `search`, `stats`

## Install

```bash
pip install brainctl              # core
pip install brainctl[mcp]         # with MCP server
pip install brainctl[vec]         # with vector search (sqlite-vec)
pip install brainctl[all]         # everything
```

## CLI

```bash
# Memories
brainctl memory add "Python 3.12 is the minimum version" -c convention
brainctl memory search "python version"

# Entities (typed knowledge graph)
brainctl entity create "Alice" -t person -o "Engineer; Likes Python; Based in NYC"
brainctl entity get Alice
brainctl entity relate Alice works_at Acme
brainctl entity search "engineer"

# Events
brainctl event add "Deployed v2.0 to production" -t result -p myproject
brainctl event search -q "deploy"

# Cross-table search (memories + events + entities)
brainctl search "deployment"

# Prospective memory (triggers that fire on future queries)
brainctl trigger create "Alice mentions vacation" -k vacation,alice -a "Remind about project deadline"
brainctl trigger check "alice is going on vacation"

# Stats
brainctl stats
```

## What Makes It Different

| Feature | brainctl | mem0 | Zep | MemGPT |
|---------|----------|------|-----|--------|
| Single file (SQLite) | ✓ | ✗ | ✗ | ✗ |
| No server required | ✓ | ✓ | ✗ | ✗ |
| MCP server included | ✓ | ✗ | ✗ | ✗ |
| Full-text search (FTS5) | ✓ | ✗ | ✗ | ✗ |
| Vector search | ✓ | ✓ | ✓ | ✓ |
| Entity registry | ✓ | ✗ | ✓ | ✗ |
| Knowledge graph | ✓ | ✗ | ✓ | ✗ |
| Consolidation engine | ✓ | ✗ | ✗ | ✗ |
| Confidence decay | ✓ | ✗ | ✗ | ✗ |
| Bayesian scoring | ✓ | ✗ | ✗ | ✗ |
| Prospective memory | ✓ | ✗ | ✗ | ✗ |
| Write gate (surprise scoring) | ✓ | ✗ | ✗ | ✗ |
| Multi-agent support | ✓ | ✗ | ✓ | ✗ |
| No LLM calls for memory ops | ✓ | ✗ | ✓ | ✗ |

## Architecture

```
brain.db (single SQLite file)
├── memories        FTS5 full-text + optional vec search
├── events          timestamped logs with importance scoring
├── entities        typed nodes (person, project, tool, concept...)
├── knowledge_edges directed relations between any table rows
├── decisions       recorded with rationale
├── memory_triggers prospective memory (fire on future conditions)
└── 20+ more tables (consolidation, beliefs, policies, epochs...)

Consolidation Engine (hippocampus.py)
├── Confidence decay    — unused memories fade
├── Temporal promotion  — frequently-accessed memories strengthen
├── Dream synthesis     — discover non-obvious connections
├── Hebbian learning    — co-retrieved memories form edges
├── Contradiction detection
└── Compression         — merge redundant memories

Write Gate (W(m))
├── Surprise scoring    — reject redundant memories at the door
├── Worthiness check    — surprise × importance × (1 - redundancy)
└── Force flag          — bypass for explicit writes
```

## Vector Search (Optional)

brainctl works without embeddings. For vector search, install Ollama and sqlite-vec:

```bash
pip install brainctl[vec]
# Install Ollama: https://ollama.ai
ollama pull nomic-embed-text
brainctl-embed                    # backfill embeddings
brainctl vsearch "semantic query" # vector similarity search
```

## Docker

```bash
docker build -t brainctl .
docker run -v ./data:/data brainctl              # MCP server
docker run -v ./data:/data brainctl brainctl stats  # CLI
```

## Multi-Agent

Every operation accepts `--agent` / `agent_id` for attribution:

```bash
brainctl -a agent-alpha memory add "learned something" -c lesson
brainctl -a agent-beta entity observe "Alice" "Now leads the team"
```

Agents share one brain.db. Each write is attributed. Search sees everything.

## Token Cost Optimization

brainctl is designed to **reduce** your model's token usage, not increase it. Without persistent memory, agents waste tokens re-reading files, re-asking questions, and re-discovering their environment every session. brainctl eliminates that — but only if configured well.

### Output Formats

Every search command supports `--output` to control token consumption:

```bash
brainctl search "deploy" --output json      # default: pretty JSON (~2200 tokens)
brainctl search "deploy" --output compact   # minified JSON (~1700 tokens, ~24% savings)
brainctl search "deploy" --output oneline   # ID|type|text (~60 tokens, ~97% savings)
```

For agents that just need facts (not full metadata), `--output oneline` is the single biggest cost reduction you can make.

### Budget Caps

Hard-cap search output at a token limit:

```bash
brainctl search "deploy" --budget 500       # trim lowest-ranked results until output fits
brainctl search "deploy" --limit 3          # fewer results = fewer tokens
brainctl search "deploy" --min-salience 0.1 # suppress low-relevance noise
```

### Cost Dashboard

See exactly where tokens are going:

```bash
brainctl cost
```

Shows: format savings comparison, queries/tokens today and last 7 days, top token-consuming agents, and actionable recommendations.

### Design Principles for Low-Cost Usage

1. **Query the brain, don't inject it.** Don't dump memory into every system prompt. Search when relevant.
2. **Use oneline for routine lookups.** Full JSON is for debugging. Agents need facts, not metadata.
3. **Set --budget on automated queries.** Cron jobs and heartbeats should cap their own output.
4. **Limit scope.** `--tables memories` skips events/context. `--category convention` narrows further.
5. **Let salience filtering work.** `--min-salience 0.1` drops noise that wastes tokens downstream.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding guidelines, and PR workflow.

## License

MIT
