# Codex Handoff — brainctl Open-Source Cleanup

## Repo
`https://github.com/TSchonleber/brainctl` — public, MIT licensed.

## What brainctl Is
A cognitive memory system for AI agents. Single SQLite file (brain.db), CLI with 50+ commands (`brainctl`), MCP server with 12+ tools (`brainctl-mcp`), web dashboard with neural graph visualization (`brainctl ui`), Python API (`from brainctl import Brain`). Consolidation engine (hippocampus), entity registry, knowledge graph, Bayesian confidence, prospective memory triggers, W(m) write gate.

## What Needs Doing

### 1. Strip Internal References (PRIORITY)
This was built inside a company called "CostClock AI" using an agent framework called "Paperclip". All internal references need to be generalized so external contributors can understand the code.

**Specific cleanup needed:**

- **COS-### references** (116 in brainctl, 18 in brainctl-mcp, 17 in hippocampus.py, scattered elsewhere): These are internal Paperclip issue tracker IDs. They appear in comments like `# COS-354` or `(COS-221)`. Replace with descriptive comments that explain WHAT the code does, not which ticket created it. Example: `# COS-354` → `# Bayesian Beta distribution confidence scoring`. Don't just delete them — replace with useful context.

- **Paperclip references** (30 in brainctl, 7 in hippocampus.py): References to "Paperclip", "PAPERCLIP_AGENT_ID", "paperclip-post-checkout". The code has agent type checks like `agent_type = 'paperclip'`. Generalize to just "agent" or "external". Remove any Paperclip-specific checkout/heartbeat logic that's not useful to general users.

- **CostClock/OpenClaw/Kokoro references**: Internal product and agent names. Remove or generalize. "CostClock AI" → just remove, it's not relevant. "OpenClaw" → "agent framework". "Kokoro" → just remove.

- **Hardcoded paths** (`/Users/r4vager/...`): There are shebangs pointing to `/Users/r4vager/agentmemory/.venv/bin/python3`. Change to `#!/usr/bin/env python3`. The vec0.dylib path is hardcoded to `/opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib` — make this auto-discoverable with fallbacks.

- **MCP_SERVER.md**: Has hardcoded paths like `/Users/r4vager/agentmemory/bin/brainctl-mcp`. Generalize to just `brainctl-mcp` (assumes pip install).

### 2. Fix sqlite-vec Dependency
The code hardcodes the path to vec0.dylib at `/opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib`. This only works on macOS with Homebrew Python 3.13.

Fix: Auto-discover sqlite-vec at runtime. Try these in order:
1. `import sqlite_vec; sqlite_vec.load(conn)` (if pip-installed)
2. Check common dylib locations
3. Fall back gracefully — vector search disabled, FTS5 still works
4. Print a clear message: "sqlite-vec not found. Vector search disabled. Install with: pip install sqlite-vec"

Apply this pattern in ALL files that reference VEC_DYLIB: `bin/brainctl`, `bin/brainctl-mcp`, `bin/embed-populate`, `bin/hippocampus.py`.

### 3. Fix Shebangs
All files in `bin/` have shebangs pointing to `/Users/r4vager/agentmemory/.venv/bin/python3`. Change to `#!/usr/bin/env python3`.

### 4. Verify Dashboard Works
The UI (`ui/server.py`) should work standalone:
```bash
python3 ui/server.py --port 3939
# Then open http://localhost:3939
```
Test that both Explorer and Neural Map views load correctly.
The `/api/graph` endpoint should return entities as nodes and knowledge_edges as edges.
Recent commits added Paperclip roster sync to the neural map — check if that code still makes sense without Paperclip, or if it should be removed/generalized.

### 5. Update init_schema.sql
The `db/init_schema.sql` file has COS- references in comments and may be incomplete (it was generated before entities table). Verify it creates a working brain.db from scratch. Remove COS- references from comments.

### 6. Update Documentation
- `ARCHITECTURE.md`: Remove COS- references, Paperclip mentions
- `COGNITIVE_PROTOCOL.md`: Remove COS- references, Paperclip/CostClock mentions  
- `MCP_SERVER.md`: Remove hardcoded paths
- `README.md`: Should already be clean (was written for public), verify

### 7. Test pip install
After cleanup, verify this works from a clean state:
```bash
pip install -e .
brainctl --help
brainctl init  # should create a fresh brain.db
python3 -c "from brainctl import Brain; b = Brain('/tmp/test.db'); b.remember('test'); print(b.search('test'))"
```

## File Sizes for Reference
- `bin/brainctl`: ~490KB, 10000+ lines (the main CLI — be careful with edits)
- `bin/brainctl-mcp`: ~42KB (MCP server)
- `bin/hippocampus.py`: ~133KB (consolidation engine)
- `bin/embed-populate`: ~17KB (embedding pipeline)
- Everything else is small

## Rules
- Do NOT change functional behavior — only clean up references, paths, and comments
- Do NOT delete features — just generalize internal names
- Test that `brainctl --help` still works after changes
- Test that `python3 ui/server.py` still serves the dashboard
- Commit with clear messages explaining what was cleaned
