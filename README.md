# brainctl

A cognitive memory system for AI agents. Single SQLite file. Zero LLM dependencies. Model-agnostic.

brainctl gives AI agents persistent, structured memory that compounds over time. Instead of re-discovering context every session, agents remember what they've learned, who they've worked with, what decisions were made, and why. One `brain.db` file holds everything — memories, events, entities, a knowledge graph, decisions, affect states, and 80+ supporting tables.

No vendor lock-in. No API keys required. No LLM calls for any memory operation. Pure SQLite + Python stdlib.

```python
from agentmemory import Brain

brain = Brain()
brain.remember("User prefers dark mode")
brain.search("dark mode")
brain.entity("Alice", "person", observations=["Engineer", "Likes Python"])
brain.relate("Alice", "works_at", "Acme")
brain.log("Deployed v2.0")
```

By default, Brain and the CLI point at the same database:
- $BRAIN_DB, if set
- otherwise $BRAINCTL_HOME/db/brain.db
- otherwise ~/agentmemory/db/brain.db

Default agent behavior:
- Brain and the CLI default to agent id `default` when you do not pass `-a/--agent`
- use `-a my-agent` for explicit attribution and multi-agent separation

## Install

```bash
pip install brainctl              # core
pip install brainctl[mcp]         # with MCP server
pip install brainctl[vec]         # with vector search (sqlite-vec)
pip install brainctl[all]         # everything
```

## Quick Start

```bash
pip install brainctl
brainctl init                     # create brain.db
brainctl memory add 'learned something useful' -c lesson -a my-agent
brainctl search 'something useful'
brainctl index                    # browsable catalog of all knowledge
brainctl affect classify 'deployment failed, team is panicking'
brainctl stats
```

## MCP Server

brainctl ships an MCP server for Claude Desktop, VS Code, Cursor, OpenClaw, and any MCP-compatible agent:

```json
{
  "mcpServers": {
    "brainctl": {
      "command": "brainctl-mcp"
    }
  }
}
```

MCP tools include memory, event, entity, decision, affect, trigger, conflict-resolution, and handoff operations.

The CLI and MCP server read and write the same `brain.db` — use whichever fits your workflow.

## Handoff Migration

Older `brain.db` files may not have the `handoff_packets` table yet.

Current guidance:
- fresh databases created with `brainctl init` include the handoff table
- existing databases should be migrated before using handoff commands
- if you want the simplest safe path, initialize a fresh database and import or re-seed what you need

## Core Concepts

### Memories
Durable facts the agent has learned. Each memory has a category, confidence score, and optional file anchor. Memories decay over time based on their category (identity memories last a year, integration details fade in a month) but get reinforced every time they're recalled.

```bash
brainctl memory add "Auth uses JWT with 24h expiry" -c convention -a my-agent --file src/auth/jwt.ts --line 42
brainctl memory search "auth" --file src/auth/jwt.ts   # boosts file-anchored results
brainctl memory list --limit 10
```

Categories: `identity`, `user`, `environment`, `convention`, `project`, `decision`, `lesson`, `preference`, `integration`

### Events
Timestamped logs of what happened. Searchable, attributable, typed.

```bash
brainctl event add "Deployed v2.0 to production" -t result -p myproject -a my-agent
brainctl event search -q "deploy"
brainctl event tail -n 20
```

Types: `observation`, `result`, `decision`, `error`, `handoff`, `task_update`, `artifact`, `session_start`, `session_end`, `warning`, `stale_context`

### Entities & Knowledge Graph
Typed nodes (people, projects, tools, concepts) with observations and directed relations. The knowledge graph is self-building — when you add a memory that mentions a known entity, brainctl automatically creates a `mentions` edge linking them.

```bash
brainctl entity create "Alice" -t person -o "Engineer; Likes Python; Based in NYC"
brainctl entity observe "Alice" "Now leads the infrastructure team"
brainctl entity relate Alice works_at Acme
brainctl entity get Alice                  # shows entity + all relations
brainctl entity search "engineer"
```

Auto-linking example:
```bash
brainctl memory add "Alice deployed CostClock to production" -c project
# → auto_linked_entities: ["Alice", "CostClock"]
# Knowledge edges created automatically
```

### Decisions
Recorded with rationale so agents (and humans) can understand why choices were made.

```bash
brainctl decision add "Switch to local inference" -r "Cloud API costs unsustainable at scale"
```

### Cross-Table Search
Search across memories, events, and entities at once:

```bash
brainctl search "deployment"
```

### Index — Knowledge Catalog
Generate a browsable snapshot of everything in the brain. Inspired by Karpathy's LLM Wiki pattern — an index that lets agents orient fast without searching.

```bash
brainctl index                         # markdown to stdout
brainctl index --format json           # machine-readable
brainctl index --out index.md          # write to file
brainctl index -c convention           # filter by category
```

### Prospective Memory (Triggers)
Set conditions that fire on future queries — "remember to tell me X when Y comes up":

```bash
brainctl trigger create "Alice mentions vacation" -k vacation,alice -a "Remind about project deadline"
brainctl trigger check "alice is going on vacation"
```

## Memory Lifecycle

brainctl doesn't just store memories — it manages their lifecycle like biological memory. Features inspired by [TORMENT](https://github.com/pzychozen/TORMENT) and neuroscience research:

### Query-Time Half-Life Decay
Memories fade naturally based on their category. Identity memories last ~1 year. Integration details fade in ~1 month. But every time a memory is recalled, its decay clock resets — frequently-used knowledge stays strong.

| Category | Half-Life | Rationale |
|----------|-----------|-----------|
| identity, user | 365 days | Who you are changes slowly |
| convention, environment | 180 days | Standards are durable |
| preference | 120 days | Tastes evolve |
| project | 90 days | Projects have seasons |
| decision, lesson | 60 days | Context shifts |
| integration | 30 days | Technical details change fast |

Protected memories and `permanent` temporal class never decay.

### Pre-Ingest Duplicate Suppression
Before writing a new memory, brainctl checks for near-duplicates (FTS5 + Jaccard word similarity ≥ 85%). Duplicates are reinforced instead of duplicated — confidence boosted 30% toward ceiling, recall counter bumped. Prevents unbounded growth from repeated observations.

### Write Gate
New memories pass through a surprise scoring gate before insertion. Low-novelty writes are rejected unless `--force` is used. This prevents agents from filling the brain with redundant observations.

### Hard Memory Cap
Safety net at 10,000 memories per agent. When exceeded, the lowest-confidence unprotected memories are retired down to 8,000. Protected and permanent memories are never touched. Emergency compressions are logged as warning events.

### Consolidation Engine
The hippocampus runs batch maintenance on the memory store:

```bash
brainctl-consolidate decay       # confidence decay on unused memories
brainctl-consolidate compress    # merge redundant memories
brainctl-consolidate promote     # promote important events to memories
brainctl-consolidate sweep       # full maintenance cycle
```

Pure-math operations: Hebbian co-retrieval learning, temporal demotion, EWC importance scoring, experience replay. No LLM calls. Schedule with cron for autonomous maintenance:

```bash
0 */4 * * * BRAIN_DB=~/brain.db brainctl-consolidate sweep
```

## Multi-Agent

Every operation accepts `-a AGENT_NAME` for attribution:

```bash
brainctl -a agent-alpha memory add "learned something" -c lesson
brainctl -a agent-beta entity observe "Alice" "Now leads the team"
```

Agents share one brain.db. Each write is attributed. Search sees everything. The knowledge graph connects insights across agents automatically.

## Affect Tracking

Functional affect states (frustration, urgency, satisfaction, confusion, confidence, curiosity) that influence memory formation and retrieval. Not sentiment analysis — internal operational state tracking.

```bash
brainctl affect classify 'the deploy failed and rollback is stuck'
# → {"state": "frustration", "valence": -0.7, "arousal": 0.8, ...}

brainctl affect log 'finally resolved the outage after 4 hours'
brainctl affect check              # current state + trajectory
brainctl affect monitor --watch    # live-stream changes
```

High-arousal states increase the write gate threshold (harder to write impulsively). Affect-tagged memories get priority during consolidation.

## What Makes It Different

| Feature | brainctl | mem0 | Zep | MemGPT |
|---------|----------|------|-----|--------|
| Single file (SQLite) | ✓ | ✗ | ✗ | ✗ |
| No server required | ✓ | ✓ | ✗ | ✗ |
| No LLM calls | ✓ | ✗ | ✓ | ✗ |
| MCP server included | ✓ | ✗ | ✗ | ✗ |
| Full-text search (FTS5) | ✓ | ✗ | ✗ | ✗ |
| Vector search | ✓ | ✓ | ✓ | ✓ |
| Knowledge graph | ✓ | ✗ | ✓ | ✗ |
| Self-building graph | ✓ | ✗ | ✗ | ✗ |
| File-anchored memories | ✓ | ✗ | ✗ | ✗ |
| Half-life decay | ✓ | ✗ | ✗ | ✗ |
| Duplicate suppression | ✓ | ✗ | ✗ | ✗ |
| Hard memory cap | ✓ | ✗ | ✗ | ✗ |
| Consolidation engine | ✓ | ✗ | ✗ | ✗ |
| Bayesian scoring | ✓ | ✗ | ✗ | ✗ |
| Prospective memory | ✓ | ✗ | ✗ | ✗ |
| Write gate | ✓ | ✗ | ✗ | ✗ |
| Multi-agent | ✓ | ✗ | ✓ | ✗ |
| Affect tracking | ✓ | ✗ | ✗ | ✗ |
| Knowledge index | ✓ | ✗ | ✗ | ✗ |
| Model-agnostic | ✓ | ✗ | ✓ | ✗ |

## Architecture

```
brain.db (single SQLite file, 80+ tables)
├── memories          FTS5 full-text + optional vec search + file anchoring
├── events            timestamped logs with importance scoring
├── entities          typed nodes (person, project, tool, concept...)
├── knowledge_edges   directed relations — self-building via entity auto-linking
├── decisions         recorded with rationale
├── memory_triggers   prospective memory (fire on future conditions)
├── affect_log        per-agent functional affect state tracking
└── 60+ more          consolidation, beliefs, policies, epochs, EWC...

Memory Lifecycle
├── Write gate         → surprise scoring rejects redundant writes
├── Dedup suppression  → near-duplicates reinforce existing memories
├── Entity auto-link   → mentioned entities get knowledge_edges automatically
├── Half-life decay    → category-based decay, reinforced on recall
├── Hard cap           → 10k/agent emergency compression
└── Consolidation      → Hebbian, temporal, compression (cron)
```

## Token Cost Optimization

brainctl reduces your model's token usage. Without persistent memory, agents waste tokens re-reading files and re-discovering context every session.

```bash
brainctl search "deploy" --output json      # ~2200 tokens
brainctl search "deploy" --output compact   # ~1700 tokens (~24% savings)
brainctl search "deploy" --output oneline   # ~60 tokens (~97% savings)
brainctl search "deploy" --budget 500       # hard token cap
brainctl search "deploy" --limit 3          # fewer results
```

For automated agents, `--output oneline` is the single biggest cost reduction available.

## Reports & Health

```bash
brainctl report                    # compile knowledge into markdown (via _impl)
brainctl report --topic "deploy"   # topic-focused
brainctl report --entity "Alice"   # entity deep-dive
brainctl lint                      # health check
brainctl lint --fix                # auto-fix safe issues
brainctl stats                     # database overview
brainctl cost                      # token usage dashboard
```

## Obsidian Integration

Bidirectional sync between brain.db and an [Obsidian](https://obsidian.md) vault.
Follows the Karpathy LLM-wiki pattern: brain.db is the source of truth, Obsidian is the
navigable wiki layer, YAML frontmatter is the schema layer for round-tripping.

```bash
# Export brain to vault (memories → md, entities → md with wikilinks, events → daily notes)
brainctl obsidian export ~/Documents/MyVault

# Check drift between brain.db and vault
brainctl obsidian status ~/Documents/MyVault

# Import new notes you wrote in Obsidian (no brainctl_id → ingested through W(m) gate)
brainctl obsidian import ~/Documents/MyVault

# Watch vault for new/modified notes and auto-ingest
pip install watchdog
brainctl obsidian watch ~/Documents/MyVault
```

Vault layout under `<vault>/brainctl/`:
- `memories/<id>-<slug>.md` — one file per active memory with frontmatter
- `entities/<name>.md` — one file per entity, observations listed, `[[wikilinks]]` ready
- `events/YYYY-MM-DD.md` — daily note grouping all events for that date

Export is idempotent (skips existing files unless `--force`). Import skips any file that
already has a `brainctl_id:` frontmatter field (i.e. files we exported).

## Vector Search (Optional)

brainctl works without embeddings. For semantic similarity search:

```bash
pip install brainctl[vec]
brainctl embed populate            # backfill embeddings for existing memories
brainctl vsearch "semantic query"  # vector similarity search
```

Configure the embedding backend with environment variables:

```bash
export BRAINCTL_OLLAMA_URL="http://localhost:11434/api/embed"
export BRAINCTL_EMBED_MODEL="nomic-embed-text"
export BRAINCTL_EMBED_DIMENSIONS=768
```

## Docker

```bash
docker build -t brainctl .
docker run -v ./data:/data brainctl                    # MCP server
docker run -v ./data:/data brainctl brainctl stats     # CLI
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and PR workflow.

## License

MIT
