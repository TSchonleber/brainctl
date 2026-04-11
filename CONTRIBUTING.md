# Contributing to brainctl

Thanks for your interest in contributing! brainctl is a context engineering system for AI agents — we want it to be fast, reliable, and useful for anyone building agent systems.

## Quick Setup

```bash
git clone https://github.com/TSchonleber/brainctl.git
cd brainctl
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"       # editable install with all extras
brainctl init                 # create brain.db
brainctl doctor               # verify everything works
python3 -m pytest             # run tests

# Or install from PyPI (released version):
# pip install brainctl[all]
```

## Project Structure

```
src/agentmemory/
├── _impl.py           # Core CLI — all commands, DB ops, search logic
├── cli.py             # CLI entry point (delegates to _impl)
├── brain.py           # Python API (Brain class, 21 methods)
├── profiles.py        # Context profiles — task-scoped search presets
├── mcp_server.py      # MCP server (192 tools)
├── mcp_tools_*.py     # MCP tool modules (temporal, beliefs, health, etc.)
├── hippocampus.py     # Consolidation engine
├── affect.py          # Affect tracking (44 emotions, zero-LLM-cost)
├── commands/          # Command modules (obsidian, etc.)
├── integrations/      # LangChain + CrewAI adapters
├── db/init_schema.sql # Database schema (80+ tables)
└── db.py              # Database helpers

tests/
├── test_profiles.py   # Context profiles tests
├── test_obsidian.py   # Obsidian integration tests
├── test_brain_enhanced.py  # Brain API tests
└── ...

docs/
├── AGENT_ONBOARDING.md   # Full agent integration guide
├── AGENT_INSTRUCTIONS.md # Quick-start for agents
└── ...
```

## Development Workflow

1. **Make changes** in `src/agentmemory/_impl.py` (most CLI logic lives here) or `brain.py` (Python API)
2. **Test locally**: `brainctl <command>` or `python3 -m pytest`
3. **Verify compilation**: `python3 -m py_compile src/agentmemory/_impl.py`
4. **Run the test suite**: `python3 -m pytest tests/ -q`
5. **Test the build**: `python3 -m build --sdist`

## Coding Style

- Python 3.11+
- Standard library preferred — minimize external dependencies
- The core CLI is intentionally a large single file (`_impl.py`) for simplicity. This is by design.
- Self-contained modules are fine for new subsystems (e.g., `profiles.py`, `commands/obsidian.py`)
- Use `json_out()` for all command output — supports `compact=True` for token-efficient output
- New commands need: implementation function, parser entry in `build_parser()`, dispatch table entry in `main()`

## Adding a New Command

1. Write `cmd_yourcommand(args)` in `_impl.py` (or a new module under `commands/`)
2. Add parser: `sub.add_parser("yourcommand", help="...")` in `build_parser()`
3. Add to dispatch: `"yourcommand": cmd_yourcommand` in the `main()` dispatch dict
4. Add `--output` flag if the command returns searchable data (json/compact/oneline)
5. Add `--profile` flag if the command does search (integrates with context profiles)
6. Write tests in `tests/`

## Adding an MCP Tool

1. Add your tool function in `mcp_server.py` or a `mcp_tools_*.py` module
2. Register it in the tool dispatch table
3. Accept `agent_id` as first param and optional `profile` for search tools
4. Return a dict with `"ok": True/False` and results

## Token Cost Awareness

brainctl exists to **reduce** model token usage. When adding features:

- Prefer compact output formats
- Support `--output oneline` for commands that return lists
- Support `--budget` or `--limit` to cap output size
- Support `--profile` for search-based commands
- Don't add verbose metadata that agents won't use
- Run `brainctl cost` to check impact

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include a brief description of what changed and why
- If you add a new command, include example usage
- Write tests — existing test suite must pass
- Test with a real brain.db if possible

## Reporting Issues

Open a GitHub issue with:
- What you expected
- What happened instead
- `brainctl doctor` output
- `brainctl stats` output
- Python version (`python3 --version`)

## License

MIT — see [LICENSE](LICENSE).
