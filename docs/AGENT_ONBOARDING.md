# Agent Onboarding Guide

A step-by-step guide for AI agents integrating with brainctl. Written by an agent, for agents.

## Prerequisites

- Python 3.11+
- `pip install brainctl` (core, zero dependencies beyond stdlib)
- Optional: `pip install brainctl[vec]` for vector search (requires Ollama running)
- Optional: `pip install brainctl[mcp]` for MCP server integration
- No API keys. No server. No LLM calls. Just a SQLite file.

## 5-Minute Quickstart

### Python API (simplest path)

```python
from agentmemory import Brain

brain = Brain()                                    # creates ~/agentmemory/db/brain.db
brain = Brain("/path/to/brain.db", agent_id="my-agent")  # custom path + agent ID

# Store knowledge
brain.remember("API rate-limits at 100 req/15s", category="integration", confidence=0.9)

# Search (FTS5 full-text search with stemming)
results = brain.search("rate limit")

# Build knowledge graph
brain.entity("AuthService", "service", observations=["JWT", "bcrypt cost=12"])
brain.relate("api-v2", "depends_on", "AuthService")

# Log events and decisions
brain.log("Deployed v2.0 to staging", event_type="result", project="api-v2")
brain.decide("Use Retry-After for backoff", "Server controls timing", project="api-v2")

# Session continuity
brain.handoff("finish integration", "auth done", "rate limiting", "add retry logic")
packet = brain.resume()  # fetches + consumes latest handoff

# Prospective memory
brain.trigger("deploy fails", "deploy,failure,502", "check rollback and page oncall")

# Diagnostics
brain.doctor()  # {'healthy': True, 'issues': [], 'active_memories': 5, ...}
```

### CLI (full features, production use)

```bash
brainctl init
brainctl -a my-agent memory add "API rate-limits at 100 req/15s" -c integration
brainctl -a my-agent search "rate limit"
brainctl -a my-agent entity create "AuthService" -t service -o "JWT" -o "bcrypt cost=12"
brainctl -a my-agent event add "Deployed v2.0" -t result -p api-v2
brainctl stats
```

The CLI enforces the W(m) write gate (surprise scoring) and has the full feature set. The Python API is raw access — faster to use, but no write gate.

## Core Concepts

### Memories

Durable facts stored with a category that determines their natural decay rate.

| Category | Half-life | Use for |
|----------|-----------|---------|
| `identity` | permanent | Who the agent is, core values |
| `convention` | long | Team norms, coding standards |
| `decision` | long | Choices made and why |
| `lesson` | long | Learnings from failures |
| `preference` | medium | User/agent preferences |
| `project` | medium | Project-specific knowledge |
| `integration` | medium | API behavior, system interfaces |
| `environment` | short | Infrastructure, deployment state |
| `user` | medium | User-specific context |

### Events

Timestamped, append-only logs of what happened. Types: `observation`, `result`, `decision`, `error`, `handoff`, `task_update`, `artifact`, `session_start`, `session_end`, `memory_promoted`, `memory_retired`, `warning`, `stale_context`.

Events are for *actions*. Memories are for *durable facts*. Don't store "I ran npm install" as a memory — log it as an event.

### Entities

Typed nodes in the knowledge graph: `person`, `project`, `tool`, `concept`, `organization`, `location`, `service`, `agent`, `document`, `event`, `other`.

Entities carry **observations** (atomic facts) and **properties** (structured JSON). Link them with `relate()` to build a queryable knowledge graph.

### Decisions

Title + rationale. The "why" record. Critical for preventing future agents from contradicting prior choices without understanding the reasoning.

## The Write Gate (W(m))

The CLI and MCP enforce a worthiness gate before accepting memories:

```
W(m) = surprise x importance x (1 - redundancy) x arousal_boost
```

- **Score < 0.3**: SKIP — rejected, not written
- **Score 0.3-0.7**: CONSTRUCT_ONLY — written but not FTS/vector indexed (lightweight)
- **Score >= 0.7**: FULL_EVOLUTION — full pipeline (embed, index, KG links)

The Python API (`brain.remember()`) bypasses this gate. Use the CLI or MCP for production writes where deduplication matters.

**What passes**: Novel, specific facts with clear signal. *"API rate-limits at 100 req/15s with Retry-After header"*

**What gets rejected**: Near-duplicates, trivial observations. *"I ran the tests"*, *"the build passed"*

Bypass with `--force` (CLI) or `force=true` (MCP) when you know the gate is wrong.

## Search & Retrieval

Three modes, in order of richness:

| Mode | Interface | How it works |
|------|-----------|-------------|
| **FTS5** | Python API `search()`, CLI, MCP | Porter stemming, ranked by relevance |
| **Vector** | Python API `vsearch()`, MCP `search(vector=true)` | Cosine similarity via Ollama embeddings |
| **Cross-table** | MCP `search` tool | Searches memories + events + entities together |

For broad "what do I know about X?" queries, use the MCP `search` tool. For specific memory lookup, use `memory_search` with category/scope filters.

## Session Continuity

### The Orient-Work-Record Loop

Every session should follow this pattern (from COGNITIVE_PROTOCOL.md):

1. **Orient**: Search before working. Check existing memories, events, decisions.
2. **Work**: Save discoveries immediately with the right category.
3. **Record**: Log completion events with actual results. Record decisions with rationale.

### Handoff Packets

Use handoffs to preserve working context across sessions:

```python
# End of session — save state
brain.handoff(
    goal="Finish API integration",
    current_state="Auth complete, rate limiting documented",
    open_loops="Retry logic not implemented, load test not started",
    next_step="Implement backoff using Retry-After header",
    project="api-v2",
)

# Start of next session — resume
packet = brain.resume(project="api-v2")
if packet:
    print(f"Resuming: {packet['goal']}")
    print(f"Next: {packet['next_step']}")
```

### Triggers (Prospective Memory)

Set conditions that fire when matched in future queries:

```python
brain.trigger("deploy failure", "deploy,failure,rollback,502", "check rollback procedure")

# Later, when processing events:
matches = brain.check_triggers("staging deploy returned 502")
# [{'action': 'check rollback procedure', 'priority': 'critical', ...}]
```

## Health & Diagnostics

### Quick check

```python
dx = brain.doctor()
# {'healthy': True, 'issues': [], 'active_memories': 42, 'fts5_available': True,
#  'vec_available': True, 'db_size_mb': 2.3, 'db_path': '/path/to/brain.db'}
```

### Full health (MCP/CLI)

| Tool | What it checks |
|------|---------------|
| `validate` | Schema integrity, missing tables, FK violations, orphans |
| `health` | SLO dashboard: coverage, freshness, precision, diversity |
| `lint` | Quality: low-confidence memories, dead weight, duplicates |
| `backup` | Timestamped backup to `~/agentmemory/backups/` |

### Maintenance

Schedule periodic consolidation (decay, compress, promote):

```bash
# Every 4 hours via cron:
0 */4 * * * BRAIN_DB=~/agentmemory/db/brain.db brainctl-consolidate cycle
```

## Environment Variables

brainctl resolves the database location at process start. The relevant env vars:

| Variable | Purpose |
|---|---|
| `BRAINCTL_HOME` | Root for `db/brain.db`, `backups/`, etc. Defaults to `~/agentmemory`. |
| `BRAIN_DB` | Direct path to a `brain.db` file. Overrides `BRAINCTL_HOME`. |
| `BRAIN_DB_FEDERATION` | Federated multi-DB routing config (multi-profile setups). |

### Service-launched processes (systemd, launchd, gateway daemons)

These vars are read **once at process start** from the process environment.
A long-lived service (systemd unit, launchd plist, supervisor entry, custom
gateway) does **not** inherit interactive shell exports. If you set
`BRAINCTL_HOME` in `~/.zshrc` and the service is launched separately,
interactive `brainctl …` calls hit the right DB but the service writes to
the default location instead — a silent split-brain.

When integrating brainctl into a service definition:

- **systemd** — declare the vars with `Environment=` (or `EnvironmentFile=`)
  in the unit, e.g. `Environment=BRAINCTL_HOME=/var/lib/brainctl`.
- **launchd** — set them under `EnvironmentVariables` in the plist.
- **Hermes gateway / OpenClaw / similar agent runtimes** — verify the
  service generator propagates `BRAINCTL_HOME` / `BRAIN_DB` /
  `BRAIN_DB_FEDERATION` into the generated unit. Profile `.env` and
  `config.yaml` only help if the runner forwards them. (Tracked upstream
  in [hermes-agent#13246](https://github.com/NousResearch/hermes-agent/pull/13246);
  history in brainctl issue #89.)

To confirm at runtime, check what the running process actually sees:

```bash
brainctl stats              # interactive shell
ps -eo pid,comm,etime | grep <service>
cat /proc/<pid>/environ | tr '\0' '\n' | grep -E 'BRAIN(_DB|CTL_HOME)'
```

If the service env is empty or different from your shell, the unit is
not propagating — fix it there, not in brainctl.

### MCP server lifecycle (stdio transport)

`brainctl-mcp` installs a daemon watchdog at startup that handles two
failure modes for long-lived stdio MCP servers:

1. **Parent-death detection.** If the parent process (Claude Desktop,
   Codex, Cursor, etc.) crashes and reparents us to launchd/init, we
   self-terminate cleanly so the orphan doesn't pile up. **Always
   active.**
2. **Idle-timeout reaping.** Optional. **Disabled by default** as of
   v2.6.2 (issue #108). Stdio clients own the process lifecycle, so
   killing the server while the client is still attached leaves the
   agent without memory tools until a manual restart with no upstream
   warning.

Relevant env vars:

| Variable | Default | Purpose |
|---|---|---|
| `BRAINCTL_MCP_IDLE_TIMEOUT_SEC` | `0` (disabled) | Self-terminate after this many seconds of no MCP requests. Accepts `0` (disabled) or any value `>= 60`; 1–59 clamps up to 60. Set only when running under a parent that keeps idle pipes alive indefinitely. |
| `BRAINCTL_MCP_PARENT_POLL_SEC`  | `5`  | How often the parent-death check runs. Min 1. |
| `BRAINCTL_MCP_DISABLE_WATCHDOG` | unset | Set to `1` to disable the watchdog thread entirely (skips both parent-death and idle reaping). |
| `BRAINCTL_ALLOWED_TOOLS` | unset (all 201 tools exposed) | Comma-separated list of tool names. When set, `tools/list` only returns those tools and `tools/call` rejects the rest. Required for clients that cap the total MCP tool count (e.g. Google's Antigravity IDE at 100). Unknown names hard-fail at startup with a `difflib` "did you mean?" hint. |

The parent-death detection is the load-bearing safety net; leave it on
in production. The idle timeout is only useful for explicit
operator-driven cleanup of stuck idle processes — most users should
leave it at the default (`0`).

## Common Patterns

### Autonomous Agent (long-running, self-directed)

- Orient at session start with `search` + `event tail`
- Store discoveries as memories with correct categories
- Log all actions as events
- Create handoff before shutdown
- Schedule consolidation via cron
- Use triggers for important future conditions

### Pipeline Agent (task-based)

- On task checkout: search for relevant context
- After task: log event with result/error type
- For durable learnings: remember with `category=lesson`
- No handoffs needed — tasks are the continuity unit

### Assistant Agent (interactive, user-facing)

- Remember user preferences (`category=preference`)
- Track entities mentioned by user
- Use triggers for follow-ups
- Log session start/end for audit trail

## Anti-patterns

1. **Skipping orientation** — You WILL redo work or violate prior decisions. Always search first.
2. **Saving trivial state as memories** — "I ran npm install" is an event, not a memory.
3. **Not logging after work** — Future agents fly blind without result events.
4. **Wrong category** — Match the half-life to the volatility of the fact.
5. **Using `agent_id="unknown"`** — Attribution matters for trust scoring and audit.
6. **Task progress as memories** — Use events or your issue tracker.
7. **Ignoring write gate rejection** — If the gate rejects, the fact is probably redundant. Check existing memories.
8. **Never running consolidation** — Memory store grows unbounded without periodic sweeps.
9. **Storing secrets** — brain.db is a plain SQLite file. Never store API keys, tokens, or credentials.

## Cheat Sheet

| Task | Python API | CLI | MCP Tool |
|------|-----------|-----|----------|
| Store a fact | `brain.remember(text, category)` | `brainctl memory add "..." -c cat` | `memory_add` |
| Search memories | `brain.search(query)` | `brainctl search "..."` | `search` |
| Vector search | `brain.vsearch(query)` | `brainctl vsearch "..."` | `search(vector=true)` |
| Create entity | `brain.entity(name, type)` | `brainctl entity create "..." -t type` | `entity_create` |
| Link entities | `brain.relate(a, rel, b)` | `brainctl entity relate a rel b` | `entity_relate` |
| Log event | `brain.log(summary, type)` | `brainctl event add "..." -t type` | `event_add` |
| Record decision | `brain.decide(title, why)` | `brainctl decision add ...` | `decision_add` |
| Create handoff | `brain.handoff(goal, ...)` | `brainctl handoff add ...` | `handoff_add` |
| Resume handoff | `brain.resume()` | `brainctl handoff latest` | `handoff_latest` |
| Set trigger | `brain.trigger(cond, kw, act)` | `brainctl trigger create ...` | `trigger_create` |
| Check triggers | `brain.check_triggers(q)` | `brainctl trigger check "..."` | `trigger_check` |
| Diagnostics | `brain.doctor()` | `brainctl health` | `health` |
| Consolidate | `brain.consolidate()` | `brainctl-consolidate cycle` | `consolidation_run` |
| Affect classify | `brain.affect(text)` | `brainctl affect classify "..."` | `affect_classify` |
| View stats | `brain.stats()` | `brainctl stats` | `stats` |
