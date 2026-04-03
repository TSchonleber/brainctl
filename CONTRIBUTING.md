# Contributing to brainctl

Thanks for your interest in contributing! brainctl is a cognitive memory system for AI agents — we want it to be fast, reliable, and useful for anyone building agent systems.

## Quick Setup

```bash
git clone https://github.com/TSchonleber/brainctl.git
cd brainctl
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"       # editable install with all extras
brainctl stats                # verify it works

# Or install from PyPI (released version):
# pip install brainctl[all]
```

## Project Structure

```
src/agentmemory/
├── _impl.py          # Core implementation — all commands, DB ops, search logic
├── cli.py            # CLI entry point (delegates to _impl)
├── brain.py          # Python API (Brain class)
├── db.py             # Database helpers
├── mcp_server.py     # MCP server (12 tools for Claude Desktop, VS Code, etc.)
├── commands/          # Command modules (thin wrappers importing from _impl)
└── db/init_schema.sql # Database schema

ui/
├── server.py         # Web dashboard server
└── static/           # Frontend (HTML, CSS, JS)

bin/
└── brainctl          # Standalone CLI wrapper
```

## Development Workflow

1. **Make changes** in `src/agentmemory/_impl.py` (most logic lives here)
2. **Test locally**: `python3 bin/brainctl <command>` or `python3 -m pytest` if tests exist
3. **Verify compilation**: `python3 -m py_compile src/agentmemory/_impl.py`
4. **Test the build**: `python3 -m build --sdist`

## Coding Style

- Python 3.11+
- Standard library preferred — minimize external dependencies
- The core is intentionally a large single file (`_impl.py`) for simplicity. This is by design.
- Use `json_out()` for all command output — supports `compact=True` for token-efficient output
- New commands need: implementation function, parser entry in `build_parser()`, dispatch table entry in `main()`

## Adding a New Command

1. Write `cmd_yourcommand(args)` in `_impl.py`
2. Add parser: `sub.add_parser("yourcommand", help="...")` in `build_parser()`
3. Add to dispatch: `"yourcommand": cmd_yourcommand` in the `main()` dispatch dict
4. Add `--output` flag if the command returns searchable data (json/compact/oneline)
5. Create thin wrapper in `commands/yourcommand.py` (imports from `_impl`)

## Token Cost Awareness

brainctl exists to **reduce** model token usage. When adding features:

- Prefer compact output formats
- Support `--output oneline` for commands that return lists
- Support `--budget` or `--limit` to cap output size
- Don't add verbose metadata that agents won't use
- Run `brainctl cost` to check impact

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include a brief description of what changed and why
- If you add a new command, include example usage
- Test with a real brain.db if possible

## Reporting Issues

Open a GitHub issue with:
- What you expected
- What happened instead
- `brainctl stats` output
- Python version (`python3 --version`)

## License

MIT — see [LICENSE](LICENSE).
