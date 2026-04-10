# brainctl MCP Server

MCP (Model Context Protocol) server exposing brain.db to AI assistants and editors.

## Setup

After installing (`pip install brainctl[mcp]`), the `brainctl-mcp` command is available.

### Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "brainctl": {
      "command": "brainctl-mcp"
    }
  }
}
```

### VS Code

Add to `.vscode/mcp.json` or User Settings:

```json
{
  "mcp": {
    "servers": {
      "brainctl": {
        "command": "brainctl-mcp"
      }
    }
  }
}
```

### Docker

```bash
docker run -v ~/.agentmemory:/data -e BRAIN_DB=/data/brain.db ghcr.io/yourorg/brainctl-mcp
```

## Available Tools (192)

| Tool | Description |
|------|-------------|
| `memory_add` | Add a durable memory with W(m) worthiness gate |
| `memory_search` | Full-text search across memories |
| `event_add` | Log a timestamped event |
| `event_search` | Search events by text, type, or project |
| `entity_create` | Create a typed entity (person, project, tool, concept) |
| `entity_get` | Get an entity by name or ID with all relations |
| `entity_search` | Full-text search across entities |
| `entity_observe` | Add atomic observations to an entity |
| `entity_relate` | Create a directed relation between two entities |
| `trigger_create` | Create a prospective memory trigger |
| `trigger_list` | List triggers, optionally filtered by status |
| `trigger_check` | Check if triggers match a query |
| `trigger_update` | Update fields on an existing trigger |
| `trigger_delete` | Cancel/delete a trigger by ID |
| `decision_add` | Record a decision with rationale |
| `handoff_add` | Create a structured handoff packet |
| `handoff_latest` | Fetch the latest matching handoff packet |
| `handoff_consume` | Mark a handoff packet consumed |
| `handoff_pin` | Pin a handoff packet for preservation |
| `handoff_expire` | Mark a handoff packet expired |
| `search` | Cross-table search (memories + events + entities) |
| `pagerank` | Compute PageRank centrality over knowledge graph |
| `stats` | Database statistics and health summary |
| `resolve_conflict` | AGM credibility-weighted belief conflict resolution |
| `belief_collapse` | Belief collapse mechanics and coherence checking |
| `access_log_annotate` | Annotate access log with task outcomes |
| `affect_classify` | Classify affect from text (zero LLM cost) |
| `affect_log` | Classify affect and store in affect_log |
| `affect_check` | Check current affect state for an agent |
| `affect_monitor` | Fleet-wide affect scan across all agents |
| `replay_boost` | Manually boost a memory's replay_priority for consolidation scheduling |
| `replay_queue` | List top consolidation candidates sorted by replay_priority |
| `reconsolidation_check` | Check if a memory is in its lability window (opened by high-PE retrieval) |
| `reconsolidate` | Merge new content into a labile memory (agent-scoped write window) |
| `consolidation_stats` | Replay queue depth, labile count, ripple event totals |
| `memory_calibration` | Per-category Brier-score calibration, staleness, coverage gaps (metacognition) |
| `attention_snapshot` | Synthesize agent attention state from recent searches and events |
| `consolidation_run` | Run SWR-driven consolidation pass: promote episodic→semantic, mine causal chains |
| `free_energy_check` | Epistemic drive and knowledge gap summary from agent_uncertainty_log |
| `quarantine_list` | List memories under immunity review with reason and contradiction evidence |
| `quarantine_review` | Mark a quarantined memory safe, malicious, or uncertain |
| `quarantine_purge` | Permanently delete a malicious memory and retract derived beliefs |
| `consolidation_schedule` | Predict memories likely to be needed soon and store forecasts |
| `allostatic_prime` | Boost replay_priority for pending forecasts before demand arrives |
| `demand_forecast` | Show consolidation forecasts with signal_source and confidence |
| `memory_promote` | Promote a CONSTRUCT_ONLY memory to FULL_EVOLUTION (embed + FTS index) |
| `tier_stats` | Show write-tier distribution (full/construct) for an agent |
| `abstract_summarize` | Create an extractive summary memory at session/day/week/month/quarter level |
| `zoom_out` | Given a memory, return its parent summaries in the temporal hierarchy |
| `zoom_in` | Given a summary memory, return its constituent child memories |
| `temporal_map` | Count breakdown of memories at each temporal level for an agent |

## Which Tools Do I Need?

192 tools is overwhelming. Most agents need ~15 on a daily basis. Here's how to find what you need.

### Tier 1: Essential (daily use)

**Store information:**
- Durable fact/lesson/convention: `memory_add` (enforces W(m) write gate)
- What just happened: `event_add` (timestamped, no gate)
- Why a choice was made: `decision_add` (with rationale)
- Working state for next session: `handoff_add`

**Find information:**
- Everything about a topic: `search` (memories + events + entities)
- Just memories: `memory_search` (supports category, scope, pagerank_boost)
- Just events: `event_search` (supports event_type, project)
- A specific entity: `entity_get`
- Entities matching a query: `entity_search`

**Track entities:**
- New entity: `entity_create`
- New fact about entity: `entity_observe`
- Link two entities: `entity_relate`

**Session continuity:**
- Set a future reminder: `trigger_create`
- Check reminders: `trigger_check`
- Resume prior work: `handoff_latest` / `handoff_consume`

**Health:**
- Database overview: `stats`
- Schema integrity: `validate`
- Quality lint: `lint`

### Tier 2: Advanced (weekly/as-needed)

| Category | Tools | When to use |
|----------|-------|-------------|
| Consolidation | `consolidation_run`, `replay_boost`, `replay_queue` | Memory maintenance |
| Reconsolidation | `reconsolidation_check`, `reconsolidate` | Lability window mechanics |
| Beliefs & Conflicts | `resolve_conflict`, `belief_collapse` | When memories contradict |
| Temporal Abstraction | `abstract_summarize`, `zoom_out`, `zoom_in`, `temporal_map` | Hierarchical summarization |
| Allostatic Scheduling | `consolidation_schedule`, `allostatic_prime`, `demand_forecast` | Predictive memory pre-loading |
| Immunity | `quarantine_list`, `quarantine_review`, `quarantine_purge` | Poisoned memory handling |
| D-MEM | `memory_promote`, `tier_stats` | Write-tier management |
| Metacognition | `memory_calibration`, `attention_snapshot`, `free_energy_check` | Self-monitoring |
| Affect | `affect_classify`, `affect_log`, `affect_check`, `affect_monitor` | Emotional state tracking |

### Tier 3: Specialist (~150 tools)

The remaining tools cover specialized subsystems: Theory of Mind, Trust scoring, Neuromodulation, MEB (Memory Event Buffer), Expertise routing, Federation, Policy memory, Reasoning chains, Reflexion loops, Workspace management, World models, Analytics, Telemetry, and Usage tracking. These are documented in the individual `mcp_tools_*.py` source modules.

### Decision Tree

```
What do you need?
|
+-- Store something?
|   +-- Durable fact ----------> memory_add
|   +-- What just happened ----> event_add
|   +-- Why a choice was made -> decision_add
|   +-- State for next session > handoff_add
|
+-- Find something?
|   +-- Broad topic search ----> search
|   +-- Memories only ---------> memory_search
|   +-- Events only -----------> event_search
|   +-- Entity by name --------> entity_get
|
+-- Track an entity?
|   +-- New entity ------------> entity_create
|   +-- New fact about it -----> entity_observe
|   +-- Link two entities -----> entity_relate
|
+-- Set a reminder? -----------> trigger_create
+-- Check reminders? ----------> trigger_check
+-- Resume prior work? --------> handoff_latest
+-- Check system health? ------> stats / health / lint
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BRAIN_DB` | `~/agentmemory/db/brain.db` | Path to brain.db |
| `BRAINCTL_OLLAMA_URL` | `http://localhost:11434/api/embed` | Ollama embedding endpoint |
| `BRAINCTL_EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `BRAINCTL_EMBED_DIMENSIONS` | `768` | Embedding vector dimensions |

## Agent Attribution

All tools accept an optional `agent_id` parameter. If omitted, defaults to
`"mcp-client"`. Use this to distinguish which agent or user wrote each record.

## Shared Database

The MCP server and the `brainctl` CLI read and write the same `brain.db`.
Use whichever interface fits your workflow — they are fully interchangeable.

```bash
# These are equivalent:
# MCP tool:  memory_add(content="fact", category="convention", agent_id="myagent")
# CLI:       brainctl -a myagent memory add "fact" -c convention
```
