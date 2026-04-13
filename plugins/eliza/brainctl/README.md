# @brainctl/eliza-plugin

Persistent memory for [Eliza](https://github.com/elizaos/eliza) agents, powered by [brainctl](https://pypi.org/project/brainctl/).

SQLite-backed long-term memory with FTS5 full-text search, optional vector recall, a knowledge graph, affect tracking, and session handoffs. One file, zero servers, zero API keys. MIT licensed.

> Your Eliza agent forgets everything between sessions. This plugin fixes that.

## What it gives you

Six actions your agent can call at any time:

| Action | Purpose |
|---|---|
| `BRAINCTL_REMEMBER` | Store a durable fact (preferences, integrations, observations) |
| `BRAINCTL_SEARCH` | Recall memories via FTS5, vector, or spreading-activation |
| `BRAINCTL_ORIENT` | Pull the full session-start snapshot (handoff + events + triggers + memories) |
| `BRAINCTL_WRAP_UP` | Save a session handoff packet for the next run |
| `BRAINCTL_DECIDE` | Record a decision with its rationale |
| `BRAINCTL_LOG` | Log an event to the structured event stream |

Plus an auto-recall **memory provider** that injects relevant memories + the orient snapshot into the LLM prompt before every message. Runs transparently.

## Prerequisites

Install brainctl and the MCP server (Python 3.11+):

```bash
pip install 'brainctl[mcp]'
```

That puts `brainctl-mcp` on your PATH. The plugin spawns it as a subprocess when the agent starts.

**Optional:** for vector recall, install `brainctl[vec]` and run Ollama locally with `nomic-embed-text`:

```bash
pip install 'brainctl[vec]'
ollama pull nomic-embed-text
```

## Install

```bash
npm install @brainctl/eliza-plugin
# or
pnpm add @brainctl/eliza-plugin
```

## Usage

```ts
import { AgentRuntime } from "@elizaos/core";
import { createBrainctlPlugin } from "@brainctl/eliza-plugin";

const agent = new AgentRuntime({
  character,
  plugins: [
    createBrainctlPlugin({
      agentId: "my-agent",
      project: "api-v2",
      memoryMode: "hybrid",
      recallMethod: "search",
      recallLimit: 8,
      sessionBookends: true,
    }),
  ],
  // ...
});
```

Or reference it from a character file:

```json
{
  "name": "my-agent",
  "plugins": ["@brainctl/eliza-plugin"],
  "settings": {
    "brainctl": {
      "agentId": "my-agent",
      "project": "api-v2",
      "memoryMode": "hybrid"
    }
  }
}
```

## Config

All fields are optional — sensible defaults apply.

| Key | Default | Description |
|---|---|---|
| `mcpPath` | `brainctl-mcp` on PATH | Path to the brainctl-mcp executable. Env: `BRAINCTL_MCP_PATH`. |
| `dbPath` | `~/agentmemory/db/brain.db` | Path to the SQLite brain. Env: `BRAIN_DB`. |
| `agentId` | `eliza` | Recorded on every write for multi-agent scoping. Env: `BRAINCTL_AGENT_ID`. |
| `project` | *(none)* | Optional project scope for events, decisions, and handoffs. |
| `memoryMode` | `hybrid` | `context` (auto-inject only), `tools` (LLM-visible only), or `hybrid`. |
| `recallMethod` | `search` | `search` (FTS5), `vsearch` (vector), or `think` (spreading activation). |
| `recallLimit` | `8` | Max memories returned per auto-recall. |
| `sessionBookends` | `true` | Call `brain.orient()` on first turn and `brain.wrap_up()` at session end. |

## Memory modes explained

- **`context`** — the plugin never surfaces actions to the LLM. It just silently injects recalled memories + the orient snapshot into the prompt. Zero token overhead on tool calls.
- **`tools`** — the plugin exposes all six actions as tool calls the LLM can invoke explicitly. No auto-injection. Best for agents that want full control.
- **`hybrid`** — both. Auto-inject relevant context *and* expose tools for explicit recall/retain. This is the default.

## Why brainctl over Eliza's built-in memory

Eliza's built-in memory layer is FIFO-ish and optimized for recent conversation recall. brainctl adds:

- **Worthiness-gated writes** — surprise scoring + semantic dedup prevent context pollution
- **Knowledge graph** — entities, typed relations, observations — your agent builds a model of people, services, projects
- **AGM belief revision** — contradictions are reconciled, not discarded
- **Session handoff packets** — `orient()` / `wrap_up()` give you zero-loss session continuity
- **Bayesian confidence tracking** — α/β posteriors on every memory's reliability
- **Affect log** — emotional salience drives recall prioritization
- **40+ research notes** in the brainctl repo documenting the cognitive-science grounding

It's built for agents that run for weeks, not minutes.

## Example: Milady-coded trader agent

See [`examples/trader.character.json`](./examples/trader.character.json) for a trading agent that uses brainctl to remember strategies, postmortems, and counterparty behavior across sessions. Run it:

```bash
eliza --character=examples/trader.character.json
```

## Architecture

```
┌──────────────────┐       stdio MCP        ┌──────────────────┐
│   Eliza runtime  │ ◄─────────────────────► │   brainctl-mcp   │
│                  │                         │   (Python)       │
│  ┌─────────────┐ │                         │                  │
│  │ Plugin      │ │                         │  194 tools over  │
│  │  - actions  │ │                         │  SQLite FTS5 +   │
│  │  - provider │ │                         │  sqlite-vec +    │
│  │  - service  │ │                         │  knowledge graph │
│  └─────────────┘ │                         │                  │
└──────────────────┘                         └────────┬─────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │   brain.db      │
                                              │   (SQLite, WAL) │
                                              └─────────────────┘
```

The plugin maintains a long-lived MCP client that speaks the stdio protocol to `brainctl-mcp`. All six action handlers and the provider route through `BrainctlService.callTool()`.

## Graceful degradation

If `brainctl-mcp` is missing, misconfigured, or crashes, the plugin **does not block the agent**. The provider logs a warning and returns empty context. The agent keeps running — it just loses its long-term memory for that turn. Fix the config, restart the agent, no data lost.

## Development

```bash
cd plugins/eliza/brainctl
npm install
npm run build
```

## License

MIT. Contributions welcome — see the [brainctl repo](https://github.com/TSchonleber/brainctl).
