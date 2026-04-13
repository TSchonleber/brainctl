# How I Gave My AI Agent Persistent Memory in 3 Lines of Python

Every AI agent I've built has the same problem: it forgets everything between sessions.

Session 1: "The API rate-limits at 100 requests per 15 seconds with a Retry-After header."
Session 2: "What's the rate limit?" *starts from zero*

Context windows are temporary. RAG retrieves documents, not learned experience. Vector databases store embeddings, but not decisions, not relationships, not why something matters.

I wanted something simpler: a single file that remembers what my agent learned, who it talked to, what decisions it made, and why. So I built [brainctl](https://github.com/TSchonleber/brainctl).

## The 3-Line Pattern

```python
from agentmemory import Brain

brain = Brain(agent_id="my-agent")
context = brain.orient()           # everything from last session
```

That's it. `orient()` returns a dict with:
- The **handoff packet** from the last session (goal, state, open loops, next step)
- **Recent events** (what happened)
- **Active triggers** (reminders set by the previous session)
- **Relevant memories** (if you pass a project or query)
- **Stats** (how big the brain is)

At the end of the session:

```python
brain.wrap_up("Documented rate limiting, built retry logic", project="api-v2")
```

That logs a session_end event and creates a handoff packet. The next session — whether it's the same agent or a different one — picks up exactly where you left off.

## What Goes In the Brain

Between `orient()` and `wrap_up()`, your agent stores what it learns:

```python
# Durable facts
brain.remember("API rate-limits at 100 req/15s with Retry-After header",
               category="integration", confidence=0.9)

# Decisions with rationale (so future agents know *why*)
brain.decide("Use Retry-After for backoff",
             "Server controls timing — more reliable than fixed exponential")

# Knowledge graph
brain.entity("RateLimitAPI", "service", observations=["100 req/15s", "us-east-1"])
brain.relate("api-v2", "depends_on", "RateLimitAPI")

# Prospective memory (reminders that fire on future queries)
brain.trigger("deploy failure", "deploy,failure,502", "Check rollback procedure")
```

Search uses FTS5 full-text search with porter stemming. Optional vector similarity search via sqlite-vec and Ollama if you want semantic matching.

## What Makes This Different

I looked at every agent memory system before building this:

**mem0**: Requires API keys and cloud infrastructure. Good for hosted agents, not for local development or privacy-sensitive use cases.

**Zep**: Needs a deployed server. Great feature set, but it's another service to manage.

**MemGPT/Letta**: Interesting architecture but requires a backend service and makes LLM calls for memory operations.

**brainctl**: Single SQLite file. `pip install brainctl`. No server, no API keys, no LLM calls for any memory operation. Everything is pure math — FTS5 search, Bayesian confidence scoring, half-life decay, surprise-based write gating.

The write gate is the part I'm most proud of. When your agent tries to store something, brainctl checks if it's actually novel:

```
W(m) = surprise × importance × (1 - redundancy)
```

If the memory is too similar to something already stored, it gets rejected (or stored in a lightweight buffer without full indexing). This prevents agents from filling their brain with redundant observations — a problem I hit immediately when I first connected an agent to a memory store without filtering.

## Multi-Agent by Default

Multiple agents share one brain.db. Each write is attributed:

```python
researcher = Brain(agent_id="researcher")
deployer = Brain(agent_id="deployer")

researcher.remember("Auth uses bcrypt cost=12", category="convention")
deployer.search("bcrypt")  # finds researcher's memory
```

Knowledge compounds across agents. The researcher discovers something, the deployer finds it when they need it. No explicit sharing step — the brain is shared.

## MCP Server Included

If you're using Claude Desktop, VS Code, or Cursor, brainctl ships an MCP server:

```json
{"mcpServers": {"brainctl": {"command": "brainctl-mcp"}}}
```

196 tools covering memory, events, entities, decisions, triggers, handoffs, consolidation, affect tracking, and more. But you don't need to learn all 196 — the [MCP docs](https://github.com/TSchonleber/brainctl/blob/main/MCP_SERVER.md) include a decision tree showing which ~15 tools you actually need.

## Framework Integrations

### LangChain

```python
from agentmemory.integrations.langchain import BrainctlChatMessageHistory

chain_with_history = RunnableWithMessageHistory(
    runnable=my_chain,
    get_session_history=lambda sid: BrainctlChatMessageHistory(session_id=sid),
)
```

### CrewAI

```python
from agentmemory.integrations.crewai import BrainctlStorage

crew = Crew(
    memory=True,
    short_term_memory=ShortTermMemory(storage=BrainctlStorage("short-term")),
    long_term_memory=LongTermMemory(storage=BrainctlStorage("long-term")),
)
```

## Try It

```bash
pip install brainctl
python -c "
from agentmemory import Brain
brain = Brain()
brain.remember('Your first memory', category='lesson')
print(brain.search('first memory'))
print(brain.doctor())
"
```

One file. Zero infrastructure. Your agent remembers.

[GitHub](https://github.com/TSchonleber/brainctl) | [PyPI](https://pypi.org/project/brainctl/) | [Agent Onboarding Guide](https://github.com/TSchonleber/brainctl/blob/main/docs/AGENT_ONBOARDING.md)
