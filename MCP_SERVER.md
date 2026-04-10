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

## Available Tools (186)

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
