# Examples

Runnable scripts demonstrating the brainctl Python API.

| Script | What it shows |
|--------|---------------|
| `quickstart.py` | Minimum viable agent — remember, search, entities, events in 30 lines |
| `agent_lifecycle.py` | Full session: bootstrap, orient, work, record, handoff, resume |
| `multi_agent.py` | Two agents sharing knowledge through a single brain.db |

## Running

```bash
python examples/quickstart.py
python examples/agent_lifecycle.py
python examples/multi_agent.py
```

All examples use temp databases and won't touch your real `brain.db`.

## Next steps

- [Agent Onboarding Guide](../docs/AGENT_ONBOARDING.md) — full walkthrough
- [Cognitive Protocol](../COGNITIVE_PROTOCOL.md) — the Orient-Work-Record pattern
- [MCP Server](../MCP_SERVER.md) — 192 tools for advanced features
