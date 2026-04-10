# brainctl Agent Instructions

Drop this into any agent's system prompt or CLAUDE.md for automatic memory integration.

## For MCP-capable agents (Claude Code, Claude Desktop, VS Code)

```markdown
## Memory System (brainctl)

You have access to a persistent memory system via brainctl MCP tools. Use it.

### On session start
1. Call `search` with the current project or task context to orient yourself
2. Call `handoff_latest` to check for pending handoffs from prior sessions
3. Call `trigger_check` with a summary of your current task to see if any reminders fire

### During work
- When you learn a durable fact: `memory_add` with the right category (convention, lesson, decision, integration, environment, preference, project)
- When you make a choice: `decision_add` with title and rationale
- When you discover an entity: `entity_create` + `entity_observe`
- When entities are related: `entity_relate`
- When something happens: `event_add` with type (observation, result, error, warning)

### Before session ends
- Call `handoff_add` with: goal (what you're working toward), current_state (what's done), open_loops (what's not), next_step (what should happen next)

### What NOT to store as memories
- Transient actions ("I ran npm install") — use event_add instead
- Build output — not durable
- Secrets/credentials — brain.db is plaintext
```

## For CLI-based agents (Codex, Hermes, shell scripts)

```bash
# Session start — orient
brainctl -a $AGENT_ID search "$PROJECT_CONTEXT"
brainctl -a $AGENT_ID handoff latest
brainctl -a $AGENT_ID trigger check "$TASK_SUMMARY"

# During work — store durable facts
brainctl -a $AGENT_ID memory add "fact" -c category
brainctl -a $AGENT_ID decision add "title" -r "rationale" -p project
brainctl -a $AGENT_ID event add "what happened" -t result -p project

# Session end — handoff
brainctl -a $AGENT_ID handoff add \
  --goal "what you're working toward" \
  --current-state "what's done" \
  --open-loops "what's not" \
  --next-step "what should happen next"
```

## For Python-based agents

```python
from agentmemory import Brain

brain = Brain(agent_id="my-agent")

# Session start — one call
context = brain.orient(project="my-project")
if context["handoff"]:
    print(f"Resuming: {context['handoff']['goal']}")
    brain.resume()  # consume the handoff

# During work
brain.remember("API rate-limits at 100/15s", category="integration")
brain.decide("Use retry-after header", "Server controls timing", project="my-project")
brain.log("Deployed to staging", event_type="result", project="my-project")

# Session end — one call
brain.wrap_up("Finished rate limiting implementation", project="my-project")
```

## Category guide

| Category | Use for | Examples |
|----------|---------|---------|
| `convention` | Team norms, coding standards | "Always use UTC timestamps" |
| `lesson` | Learnings from experience | "Retry logic needs jitter to avoid thundering herd" |
| `decision` | Choices and rationale | "Chose PostgreSQL over MongoDB for ACID guarantees" |
| `integration` | API behavior, system interfaces | "Stripe webhook retries 3x with exponential backoff" |
| `environment` | Infrastructure, deployment | "Production runs on 3x t3.xlarge behind ALB" |
| `preference` | User/agent preferences | "User prefers dark mode and compact layout" |
| `project` | Project-specific knowledge | "Sprint goal: ship billing v2 by Friday" |
| `identity` | Core agent identity | "I am a code review specialist" |

## Memory categories to AVOID

- Don't store build output, test results, or CI logs as memories — use events
- Don't store task progress — use your issue tracker or events
- Don't store secrets — brain.db is plaintext SQLite
