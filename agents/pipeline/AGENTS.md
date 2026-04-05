# Pipeline — Agent Instructions

Agent ID: `fa8cf8cf-86ce-4215-968d-7bad0a6d5135`

## Policy Engine — Post-Checkout Step

**After every successful Paperclip checkout** (Step 5 of the heartbeat), run the policy engine before proceeding with task work:

```bash
brainctl policy match "<task title or decision context>" -a "$PAPERCLIP_AGENT_ID"
```

If `neuromodulation_state.org_state` is `incident` or `sprint`, use `--all` to surface every applicable policy:

```bash
brainctl policy match "<context>" --all -a "$PAPERCLIP_AGENT_ID"
```

Inject the results as additional context when planning your approach to the task. Policies are decision directives derived from org experience — treat them as standing orders unless the task explicitly overrides one.

### Feedback Loop

When a policy-guided decision produces a clear outcome, record it:

```bash
# Successful outcome
brainctl policy feedback <policy_name_or_id> --success --notes "<brief note>" -a "$PAPERCLIP_AGENT_ID"

# Failed outcome
brainctl policy feedback <policy_name_or_id> --failure --notes "<brief note>" -a "$PAPERCLIP_AGENT_ID"
```

If a `stale_warning` appears in feedback output, flag the policy to the managing agent (Engram or Hermes) for review.

### Reviewing All Policies

```bash
# List all active policies
brainctl policy list -a "$PAPERCLIP_AGENT_ID"

# List including deprecated/candidate
brainctl policy list --status all -a "$PAPERCLIP_AGENT_ID"
```

## Memory Orientation — Tiered Protocol

**After checkout, run `paperclip-post-checkout` to orient yourself:**

```bash
paperclip-post-checkout "$TASK_TITLE" "$PAPERCLIP_AGENT_ID" "$PROJECT_NAME"
```

This auto-selects FAST or FULL tier based on env context:

| Tier | Tokens | When |
|------|--------|------|
| **FAST** (default) | ~2K | Single-agent task, continuation heartbeat, routine IC work |
| **FULL** | ~12K | `PAPERCLIP_LINKED_ISSUE_IDS` set, `issue_comment_mentioned`, blocked recovery |

Force FULL tier when needed: `PAPERCLIP_FORCE_FULL=1 paperclip-post-checkout ...`

See full tier definitions in `~/agentmemory/COGNITIVE_PROTOCOL.md`.

## brainctl MCP Server (Available)

brain.db is now accessible as an MCP server (brainctl-mcp) with 12 typed tools:
memory_add, memory_search, event_add, event_search, entity_create, entity_get,
entity_search, entity_observe, entity_relate, decision_add, search, stats.

If your adapter supports MCP natively, prefer it over CLI shell-outs.
Via mcporter: `mcporter call brainctl.<tool> key=value`
Via CLI (still works): `brainctl -a paperclip-YOURNAME <command>`

New: `entity` commands for knowledge graph entities (people, projects, tools, concepts)
with typed properties, atomic observations, and directed relations.

## Database

brain.db is at: `~/agentmemory/db/brain.db`
brainctl is at: `~/bin/brainctl` (also available at `~/.local/bin/brainctl`)
