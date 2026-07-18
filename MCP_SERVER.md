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

No prebuilt image is published yet. Build locally from the repo's
`Dockerfile`:

```bash
git clone https://github.com/TSchonleber/brainctl.git && cd brainctl
docker build -t brainctl .
docker run -v ~/.agentmemory:/data -e BRAIN_DB=/data/brain.db brainctl
```

The `CMD` defaults to `brainctl-mcp`, so the container runs the MCP
server over stdio.

## Available Tools (98)

brainctl exposes **100 tools** in v2 — a consolidation that cut the
surface from 370 named tools to ~100 by routing through action-
discriminated dispatchers. The full underlying functionality is
preserved; agents call generic dispatchers like
`subsystem_emit(name='lc', action='fire', payload={...})` instead of
the v1 `lc_fire(...)`.

Why: many MCP harnesses cap at ~100 tools, and ~370 tool descriptions
in every system prompt was burning ~50k tokens before any agent work
began. v2 cuts that to ~12k tokens.

### Tier 1: Primary tools (call directly by name)

These are the daily-use surfaces. Call them by their exact name.

**Store / look up information:**
| Tool | Purpose |
|---|---|
| `memory_add` | Add a durable memory (W(m) worthiness gate) |
| `memory_search` | Hybrid FTS+vector search across memories |
| `vsearch` | Pure vector search (cosine over embeddings) |
| `search` | Cross-table search (memories + events + entities) |
| `search_patterns` | Pattern-based retrieval |
| `event_add` | Log a timestamped event |
| `event_search` | Search events by text / type / project |
| `event_link` | Link two events causally |
| `decision_add` | Record a decision with rationale |
| `entity_create` | Create a typed entity (person / project / tool / concept) |
| `entity_get` | Get an entity by name or ID with relations |
| `entity_search` | Full-text search across entities |
| `entity_observe` | Add atomic observations to an entity |
| `entity_relate` | Create a directed relation between entities |
| `procedure_add` / `procedure_get` / `procedure_list` / `procedure_search` | Procedural memory (workflows / recipes) |
| `push` | Push a memory to another agent's inbox |
| `push_report` | Report on push deliveries |

**Session continuity:**
| Tool | Purpose |
|---|---|
| `agent_orient` | Resume from prior session (loads handoff + recent events + top memories) |
| `agent_wrap_up` | End a session — logs handoff for the next one |
| `agent_register` | Register an agent in brain.db |
| `handoff_add` | Create a structured handoff packet |
| `handoff_latest` | Fetch the latest matching handoff packet |
| `trigger_create` / `trigger_check` | Prospective memory triggers |

**Affect:**
| Tool | Purpose |
|---|---|
| `affect_classify` | Classify affect from text (zero LLM cost) |
| `affect_log` | Classify + store in affect_log |
| `affect_check` | Check current affect for an agent |
| `affect_monitor` | Fleet-wide affect scan |

**Reasoning / inference:**
| Tool | Purpose |
|---|---|
| `reason` | Mid-task structured reasoning |
| `infer` | One-shot inference |
| `infer_pretask` | Pre-task hypothesis generation |
| `infer_gapfill` | Fill knowledge gaps via inference |
| `think` | Long-form deliberation log |

**Reconsolidation + lifecycle:**
| Tool | Purpose |
|---|---|
| `reconsolidate` | Merge new content into a labile memory |
| `reconsolidation_check` | Check if a memory is in its labile window |
| `promote` | Promote a CONSTRUCT_ONLY memory to FULL_EVOLUTION |
| `free_energy_check` | Epistemic drive + knowledge gap summary |
| `retirement_analysis` | Decide which memories are ready to retire |
| `retrieval_effectiveness` | Per-query retrieval quality |
| `allostatic_prime` | Boost replay_priority for pending forecasts |
| `demand_forecast` | Show consolidation forecasts |

**Health / admin:**
| Tool | Purpose |
|---|---|
| `health` | Database overview + integrity |
| `stats` | Database statistics |
| `validate` | Schema integrity check |
| `lint` | Quality lint pass |
| `backup` | Snapshot the brain.db |
| `pagerank` | Compute PageRank centrality over the knowledge graph |
| `weights` | Show memory-weighting state |
| `whosknows` | Find which agents have memories on a topic |
| `dream_cycle` | Run a dream-cycle pass |
| `telemetry` | Server telemetry snapshot |
| `write_gate_stats` | W(m) write-gate metrics |
| `budget_set` / `budget_status` | Per-agent token-budget controls |
| `resolve_conflict` | AGM credibility-weighted belief conflict resolution |
| `merge_status` / `merge_execute` | Cross-store merge |
| `abstract_summarize` / `zoom_in` / `zoom_out` | Temporal abstraction |

### Tier 2: Subsystem dispatchers (`subsystem_*`)

Brain-region subsystems (LC, NB, ARAS, Habenula, VTA, Raphe, septum,
BG, cerebellum, thalamus, amygdala, hippocampus, ACC, DMN, drives,
insula, PFC, entorhinal, CA1, mammillary, claustrum, colliculi,
olfactory, sleep, memory_aging, workspace_bandwidth, connectome) are
accessed through 7 generic dispatchers:

| Tool | Use |
|---|---|
| `subsystem_list` | Discoverability — list all 27 subsystems with layer + summary |
| `subsystem_list_actions` | List valid emit actions / register kinds / configure fields for a subsystem |
| `subsystem_status` | Current state + recent activity (replaces `*_status` × 27) |
| `subsystem_emit` | Fire / record an event (replaces `*_fire`, `*_tag`, `*_transition`, `*_predict`, `*_observe`, etc.) |
| `subsystem_register` | Idempotent UPSERT into a subsystem's catalog (replaces `*_register_*` × 12) |
| `subsystem_history` | Paginated event/firing history (replaces `*_history`, `*_signal_history`) |
| `subsystem_configure` | Update mode / state / config (replaces `*_set`, `*_set_mode`, `*_modulator_set`) |

**Call pattern:**
```jsonc
// Always start with subsystem_list to learn what's available
subsystem_list()
// → { subsystems: [{name: "lc", layer: "neuromod_broadcast", ...}, ...] }

// Then subsystem_list_actions for the one you want
subsystem_list_actions(name="lc")
// → { emit_actions: ["fire"], register_kinds: ["trigger"], configure_fields: ["set_mode"], ... }

// Then call the appropriate dispatcher
subsystem_emit(name="lc", action="fire", payload={
    trigger_name: "cerebellum_high_pe",
    surprise_magnitude: 0.7,
    agent_id: "your-agent",
})
```

### Tier 3: Topic dispatchers (action-discriminated)

| Tool | Replaces | Actions |
|---|---|---|
| `belief` | 12 belief_* / collapse_* | collapse, conflicts, conflicts_scan, consensus, diff, get, merge, propagate, seed, set, collapse_log, collapse_stats |
| `tom` | 10 tom_* | belief_invalidate, belief_set, conflicts_list, conflicts_resolve, gap_scan, inject, perspective_get, perspective_set, status, update |
| `trust` | 6 trust_* | audit, calibrate, decay, process_meb, show, update_contradiction |
| `reflexion` | 6 reflexion_* | failure_recurrence, list, query, retire, success, write |
| `gaps` | 4 gaps_* | list, refresh, resolve, scan |
| `federated` | 4 federated_* | entity_search, memory_search, search, stats |
| `world` | 6 world_* | agent, predict, project, resolve, status, rebuild_caps |
| `workspace` | 6 workspace_* | ack, broadcast, history, ingest, phi, status |
| `temporal` | 6 temporal_* | auto_detect, causes, chain, context, effects, map |
| `consolidation` | 4 consolidation_* | events, run, schedule, stats |
| `expertise` | 4 expertise_* | build, list, show, update |
| `neuro` | 5 neuro_* + neurostate | detect, history, set, signal, status, state |
| `meb` | 3 meb_* | prune, stats, tail |
| `quarantine` | 3 quarantine_* | list, purge, review |
| `epoch` | 3 epoch_* | create, detect, list |
| `usage` | 4 usage_* | check, fleet, log, summary |
| `schedule` | 3 schedule_* | run, set, status |
| `task` | 3 task_* | add, list, update |
| `policy` | 4 policy_* | add, feedback, list, match |
| `knowledge` | 4 (knowledge_*, dreams, distill) | index, report, dreams, distill |
| `context` | 2 context_* | add, search |
| `lifecycle` | 5 (lifecycle_summary, decay_report, outcome_*, access_log_annotate) | summary, decay_report, outcome_annotate, outcome_report, outcome_report_annotate |

### Tier 4: Admin dispatchers

| Tool | Replaces | Actions |
|---|---|---|
| `entity_admin` | 9 entity_* admin tools (alias/merge/compile/etc.) | add_alias, alias, aliases, compile, cross_agent_view, duplicates_scan, merge, reconcile_report, tier |
| `memory_admin` | 13 memory_* admin tools | calibration, attention_snapshot, replay_boost, replay_queue, hot, cold, promote, tier_stats, trust_propagate, utility_rate, suggest_category, pii, pii_scan |
| `agent_admin` | 4 agent_* admin tools | activity, list, model, ping |
| `handoff_admin` | 3 handoff_* admin tools (consume/expire/pin) | consume, expire, pin |
| `trigger_admin` | 3 trigger_* admin tools (delete/list/update) | delete, list, update |
| `procedure_admin` | 4 procedure_* admin tools (backfill/stats/update/feedback) | backfill, stats, update, feedback |

### Migration from v1 named tools

Old call → new call mapping lives in `docs/TOOL_MIGRATION_V2.md`. A few common ones:

| v1 (deprecated) | v2 |
|---|---|
| `lc_status()` | `subsystem_status(name="lc")` |
| `lc_fire(trigger_name="x", surprise_magnitude=0.7)` | `subsystem_emit(name="lc", action="fire", payload={trigger_name: "x", surprise_magnitude: 0.7})` |
| `belief_collapse(...)` | `belief(action="collapse", payload={...})` |
| `gaps_scan(...)` | `gaps(action="scan", payload={...})` |
| `entity_merge(...)` | `entity_admin(action="merge", payload={...})` |

### Rollback

If anything breaks downstream, the consolidation can be reverted by
removing the filter in `mcp_server.py:list_tools` (or simply reverting
the v2 commit). The v1 tool functions are untouched — they remain in
DISPATCH and become visible again the moment the filter is gone.

The 16 new brain-region migrations (067-082) are append-only and have
DROP TABLE rollback DDL in each migration file's header comment.
