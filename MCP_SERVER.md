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

## Available Tools (12)

| Tool | Description |
|------|-------------|
| `memory_add` | Add a durable memory (fact, lesson, convention, preference) |
| `memory_search` | Full-text search across memories |
| `event_add` | Log a timestamped event |
| `event_search` | Search events by text, type, or project |
| `entity_create` | Create a typed entity (person, project, tool, concept) |
| `entity_get` | Get an entity by name or ID with all relations |
| `entity_search` | Full-text search across entities |
| `entity_observe` | Add atomic observations to an entity |
| `entity_relate` | Create a directed relation between two entities |
| `decision_add` | Record a decision with rationale |
| `search` | Cross-table search (memories + events + entities) |
| `stats` | Database statistics and health summary |

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
