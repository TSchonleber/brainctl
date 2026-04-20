# MCP Server + Tools — Audit 2026-04-19 (v2.4.6)

**Scope:** `src/agentmemory/mcp_server.py` (3105 lines) + all 29 `mcp_tools_*.py` modules  
**Auditor:** Agent 3 (claude-code-brainctl-audit-mcp)  
**Date:** 2026-04-19  
**Findings:** 11 total — 1 CRITICAL, 2 HIGH, 5 MEDIUM, 2 LOW

---

## Summary

The MCP surface is broadly well-structured. The `lib/mcp_helpers.py` unification (Option C, prior audit) is complete — 26 of 29 extension modules import `now_iso()`, `tool_ok()`, `tool_error()`, and `open_db()`. The W(m) write gate, theta-gamma slot cap, and schema-resonance paths are correctly wired.

Three categories of defects dominate:

1. **Dispatch incompatibility** — the temporal module registered a single `_handle(name, args)` shim against the main dispatcher, which calls `fn(agent_id=agent_id, **arguments)`. Every temporal tool is broken at runtime.
2. **Datetime regressions** — three separate sites still use `datetime.utcnow()` or `datetime.now()` bare, creating tz-mixed string comparisons against UTC-Z-suffixed DB timestamps.
3. **Return shape inconsistency** — several tools return `{"error": "..."}` without the `"ok"` key, violating the contract the rest of the surface depends on.

---

## Findings

### [CRITICAL] F-01: All 9 temporal MCP tools broken by dispatch signature mismatch

**File(s):** `src/agentmemory/mcp_tools_temporal.py:1106,1317`

**Claim:** Every temporal tool registered in this module raises `TypeError` on every call because `_handle`'s positional signature is incompatible with `call_tool`'s calling convention.

**Evidence:**

`call_tool` (mcp_server.py:2907) pops `agent_id` from the input arguments dict and then calls:
```python
result = fn(agent_id=agent_id, **arguments)
```

The temporal module routes ALL 9 tools through a single shim:
```python
# mcp_tools_temporal.py:1106
def _handle(name: str, args: dict) -> dict[str, Any]:
    ...

# mcp_tools_temporal.py:1317
DISPATCH: dict = {tool.name: _handle for tool in TOOLS}
```

When `call_tool` calls `_handle(agent_id='X', temporal_chain=..., ...)`, Python raises:
```
TypeError: _handle() got an unexpected keyword argument 'agent_id'
```

The `call_tool` exception handler catches this and returns `{"error": "_handle() got an unexpected keyword argument 'agent_id'"}` — silently, not as a crash.

Affected tools: `temporal_causes`, `temporal_effects`, `temporal_chain`, `temporal_auto_detect`, `temporal_context`, `event_link`, `epoch_detect`, `epoch_create`, `epoch_list`.

No test in `tests/test_mcp_integration.py` exercises any temporal tool via the dispatch path.

Bug introduced in commit `ca1a8a3` (Phase 2 MCP expansion).

**Impact:** 9 temporal tools are completely non-functional via MCP. Any agent calling `temporal_chain`, `epoch_create`, or `event_link` gets a silent error response, not a useful result or a clear failure signal.

**Recommended fix:** Replace the shared shim with per-tool wrapper lambdas (same pattern used correctly in `mcp_tools_consolidation.py`):

```python
# Replace the bottom of mcp_tools_temporal.py

def _make_handler(name: str):
    def _handler(agent_id: str = "mcp-client", **kwargs) -> dict:
        return _handle(name, {"agent_id": agent_id, **kwargs})
    return _handler

DISPATCH: dict = {tool.name: _make_handler(tool.name) for tool in TOOLS}
```

Then add at least one temporal tool call to `tests/test_mcp_integration.py` to guard against regression.

---

### [HIGH] F-02: `pagerank` tool registered in TOOLS list but missing from dispatch

**File(s):** `src/agentmemory/mcp_server.py:1923,2571`

**Claim:** `tool_pagerank()` is implemented and the tool is registered in the TOOLS list, but "pagerank" is not present in the `dispatch` dict and no extension module registers it. Every call returns `{"error": "Unknown tool: pagerank"}`.

**Evidence:**

The `call_tool` dispatch dict (lines 2911–2946) has no "pagerank" entry. The TOOLS list at line 2571 includes the pagerank `Tool(...)` definition. `tool_pagerank` at line 1923 is fully implemented (191 lines). The ext module sweep in `call_tool` merges `_m.DISPATCH` for each extension, and no extension owns pagerank.

**Impact:** Any agent that calls `pagerank` (e.g., to rank entities by graph centrality) gets a useless error. The tool appears in the MCP schema negotiation so agents believe it is callable.

**Recommended fix:** Add to the dispatch dict alongside related tools:

```python
dispatch = {
    ...
    "pagerank": tool_pagerank,
    ...
}
```

---

### [HIGH] F-03: `_surprise_score_mcp` has the unfixed `fts5_no_matches` bias — claimed fixed in prior audit

**File(s):** `src/agentmemory/mcp_server.py:387-463` vs `src/agentmemory/_impl.py:5766`

**Claim:** `_surprise_score_mcp` (the MCP write path) returns `(1.0, "fts5_no_matches")` when no FTS5 matches are found. This inflates the W(m) worthiness score for novel phrases, allowing near-duplicate first-of-kind memories to pass the gate. The fixed version in `_impl.py` returns `(0.5, "fts5_no_matches_neutral")`.

**Evidence:**

```python
# mcp_server.py:445 — UNFIXED
if not rows:
    return 1.0, "fts5_no_matches"

# _impl.py:5766 — FIXED
if not rows:
    return 0.5, "fts5_no_matches_neutral"
```

Memory #1711 from the 2026-04-18 audit claims this was "promoted/fixed." The fix was applied to `_impl.py` only; `mcp_server.py` retains the old logic. Since all MCP `memory_add` calls go through `_surprise_score_mcp`, the fix has no effect on the dominant write path.

**Impact:** Novel-phrase memories pass the W(m) gate at 1.0 surprise rather than 0.5 neutral, inflating storage of first-of-kind memories on the MCP path. The `_impl.py` fix is effectively unreachable for MCP-originated writes.

**Recommended fix:** Apply the same fix to `mcp_server.py`:

```python
if not rows:
    return 0.5, "fts5_no_matches_neutral"
```

Then delete `_surprise_score_mcp` and replace all callers with `_surprise_score` from `_impl.py` to eliminate the divergence entirely.

---

### [MEDIUM] F-04: Three tools return `{"error": "..."}` without `"ok"` key

**File(s):** `src/agentmemory/mcp_server.py:2044-2105,2108-2125,2128-2180`

**Claim:** `tool_belief_collapse`, `tool_access_log_annotate`, and `tool_resolve_conflict` return `{"error": "..."}` on error paths, omitting the `"ok": false` field that all other tools include. `tool_stats()` also returns a raw dict, though this is handled by special-case logic in `call_tool`.

**Evidence:**

```python
# tool_belief_collapse (line ~2080)
return {"error": f"belief_revision module not available: {e}"}

# tool_access_log_annotate (line ~2118)
return {"error": f"outcome_eval module not available: {e}"}

# tool_resolve_conflict (line ~2150)
return {"error": str(e)}
```

All other tools use `tool_error(msg)` from `lib/mcp_helpers.py` which returns `{"ok": False, "error": msg}`. Callers checking `.get("ok") is False` will get `None` (falsy but not `False`) and silently mishandle the error.

**Impact:** Agent error-handling logic that checks `result["ok"] == False` or `result.get("ok") is False` will not catch these failures. Silent error propagation.

**Recommended fix:** Replace bare `{"error": ...}` returns with `tool_error(msg)` from `lib/mcp_helpers.py`:

```python
from agentmemory.lib.mcp_helpers import tool_error
# ...
return tool_error(f"belief_revision module not available: {e}")
```

---

### [MEDIUM] F-05: Recall boost cooldown uses naive `datetime.now()` vs UTC-Z DB timestamps

**File(s):** `src/agentmemory/mcp_server.py:1065`

**Claim:** The recall_boost cooldown guard computes a cutoff using `datetime.now()` (naive local time), then string-compares it against `last_recalled_at` stored in the DB with a Z-suffix UTC timestamp. On any non-UTC system, the comparison will be tz-mixed.

**Evidence:**

```python
# mcp_server.py:1065
_sixty_secs_ago = (datetime.now() - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
```

DB `last_recalled_at` values are stored as `"2026-04-19T14:32:00Z"` (with Z suffix, written by `now_iso()`). The string comparison `last_recalled_at > _sixty_secs_ago` mixes `"...Z"` against `"..."` (no suffix). On a system 4 hours behind UTC (EST), `_sixty_secs_ago` produces `"2026-04-19T10:31:00"` while `last_recalled_at` is `"2026-04-19T14:31:30Z"`. The `Z` sorts after bare digits, making `last_recalled_at > _sixty_secs_ago` always `True` regardless of actual time — cooldown fires incorrectly, suppressing recall_boost for all recently-recalled memories.

**Impact:** On non-UTC deployments, recall_boost is permanently suppressed for all memories that have ever been recalled, degrading Bayesian reinforcement tracking.

**Recommended fix:**

```python
from agentmemory.lib.mcp_helpers import now_iso
from datetime import timezone
_sixty_secs_ago = (datetime.now(timezone.utc) - timedelta(seconds=60)) \
    .replace(microsecond=0).isoformat().replace("+00:00", "Z")
```

Or call `now_iso()` and subtract 60 seconds at the datetime level before formatting.

---

### [MEDIUM] F-06: `mcp_tools_health.py` access_log pruning uses `datetime.utcnow()` — wrong window on non-UTC systems

**File(s):** `src/agentmemory/mcp_tools_health.py:542`

**Claim:** The access_log pruning cutoff is computed with `datetime.utcnow()` (deprecated, naive, no Z suffix), then string-compared against Z-suffixed DB timestamps. Double regression: wrong on non-UTC systems AND uses the deprecated API.

**Evidence:**

```python
# mcp_tools_health.py:542
cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
# produces: "2026-03-20T14:32:00" (no Z suffix)
```

DB `accessed_at` values stored with Z suffix. SQL comparison `accessed_at < cutoff` with mixed suffixes produces incorrect ordering — records outside the intended 30-day window may be retained or deleted.

**Impact:** Access_log records are pruned at the wrong boundary. On systems ahead of UTC, records younger than 30 days may be deleted; on systems behind UTC, stale records are retained. Affects memory access tracking quality.

**Recommended fix:**

```python
from agentmemory.lib.mcp_helpers import now_iso
from datetime import datetime, timezone, timedelta
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)) \
    .replace(microsecond=0).isoformat().replace("+00:00", "Z")
```

---

### [MEDIUM] F-07: Labile rescue window uses `datetime.utcnow()` fallback creating mixed-tz arithmetic

**File(s):** `src/agentmemory/mcp_server.py:1120`

**Claim:** When the `now_ts` parameter fails to parse in the labile rescue path, the code falls back to `datetime.utcnow()` (naive UTC), while the SQL query upper bound (`now_ts`) retains the Z suffix. The resulting `window_start` and `labile_until` are computed from a naive datetime while compared against Z-suffixed DB values.

**Evidence:**

```python
# mcp_server.py:1120
try:
    now_dt = datetime.fromisoformat(now_ts)
except Exception:
    now_dt = datetime.utcnow()   # naive UTC — no Z suffix
window_start = (now_dt - timedelta(hours=2)).isoformat()  # no Z suffix
labile_until = (now_dt + timedelta(hours=1)).isoformat()  # no Z suffix
```

The SQL WHERE clause compares `created_at >= window_start` where `created_at` has Z-suffix timestamps. Mixed comparison produces wrong labile window boundaries.

**Impact:** When the fallback path fires (malformed `now_ts` input), the labile memory rescue window is computed incorrectly. Memories that should be retroactively tagged as important may be missed.

**Recommended fix:**

```python
from agentmemory.lib.mcp_helpers import now_iso
try:
    now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
except Exception:
    now_dt = datetime.now(timezone.utc)
window_start = (now_dt - timedelta(hours=2)).replace(microsecond=0) \
    .isoformat().replace("+00:00", "Z")
labile_until = (now_dt + timedelta(hours=1)).replace(microsecond=0) \
    .isoformat().replace("+00:00", "Z")
```

---

### [MEDIUM] F-08: Three tools depend on user-local files outside the pip package

**File(s):** `src/agentmemory/mcp_server.py:104-109,149-153,2054-2063,2114-2120,2138-2148`

**Claim:** `tool_belief_collapse`, `tool_access_log_annotate`, and `tool_resolve_conflict` load `outcome_eval.py`, `belief_revision.py`, and `collapse_mechanics.py` via `sys.path.insert` pointing to `~/bin/lib/` and `~/agentmemory/` root — user-local paths absent on any non-developer deployment.

**Evidence:**

```python
# mcp_server.py:104-109
sys.path.insert(0, str(Path.home() / "bin" / "lib"))
sys.path.insert(0, str(Path.home() / "agentmemory"))

# tool_belief_collapse (~2063)
from collapse_mechanics import ...

# tool_access_log_annotate (~2120)
from outcome_eval import ...

# tool_resolve_conflict (~2148)
from belief_revision import ...
```

On a machine where these files don't exist (pip install from PyPI, CI, Docker), the import fails at call time and the tool returns a module-not-available error.

**Impact:** These three tools are silently non-functional on any non-developer install. The `brainctl[signing]` extra pattern establishes the correct model — optional deps should be declared in `pyproject.toml` or the dependent code should be moved into the package.

**Recommended fix:** Either move `outcome_eval.py`, `belief_revision.py`, and `collapse_mechanics.py` into `src/agentmemory/` and import them properly, or declare them as optional extras with a graceful `ImportError` guard that surfaces a clear message: `"Install brainctl[advanced] for belief collapse tools"`.

---

### [LOW] F-09: Top-heavy rollout controls (v2.4.6 I6) not exposed in MCP tool schemas

**File(s):** `src/agentmemory/_impl.py:509-565` vs `src/agentmemory/mcp_server.py` (tool_memory_search, tool_search)

**Claim:** The v2.4.6 top-heavy rollout controls (`rollout_mode`, `rollout_canary_agents`, `rollout_canary_percent`, `rollback_top_heavy`) are implemented in `_impl.py:cmd_search` but not exposed in the MCP tool schemas for `memory_search` or `search`. Agents cannot trigger canary mode or initiate rollback via MCP.

**Evidence:** `_impl.py:_resolve_topheavy_rollout` (lines 509–565) reads these from environment/CLI only. The `tool_memory_search` schema in `mcp_server.py` has no corresponding parameters.

**Impact:** The feature is half-wired. For a deployment where the MCP surface is the primary interface (all agents, no direct CLI access), the rollout controls are unreachable. This prevents agents from participating in canary rollouts or triggering rollback.

**Recommended fix:** Add optional parameters to `tool_memory_search` and `tool_search` schemas with the same names and semantics. Pass them through to `cmd_search`. Gate on `agent_id` authorization if rollback should be operator-only.

---

### [LOW] F-10: `borrow_from` + `scope` creates impossible SQL predicate silently

**File(s):** `src/agentmemory/mcp_server.py:900-904`

**Claim:** When `borrow_from=<agent_id>` and `scope=<non-global>` are both provided, the WHERE clause gets both `m.scope = 'global'` (hardcoded for cross-agent borrow) and `m.scope = ?` (from the caller's scope parameter), producing an impossible predicate (`m.scope = 'global' AND m.scope = 'project:foo'`). Returns 0 results with no error or warning.

**Evidence:** The borrow path hardcodes `m.scope = 'global'` as a safety constraint (correct — cross-agent reads should only reach global memories). The scope parameter path adds a second `m.scope = ?` clause without detecting the conflict.

**Impact:** Usability bug — callers who pass both parameters get silent 0 results. Not a data leak (over-constraint, not under-constraint). Discoverable but confusing.

**Recommended fix:** When `borrow_from` is set, ignore or override the `scope` parameter, or return an explicit error:

```python
if borrow_from and scope and scope != "global":
    return tool_error("borrow_from only accesses global-scoped memories; "
                      "scope parameter is ignored when borrow_from is set")
```

---

## Cross-cutting observations

- **`lib/mcp_helpers.py` adoption is near-complete** — 26/29 modules use it. The 3 that don't (federation, merge, scheduler) have reasonable justification (delegation pattern). No action needed.
- **`mcp_tools_consolidation.py` lambda pattern** is the right model for DISPATCH when the underlying function has a different signature. F-01 (temporal) should adopt it.
- **No SQL injection risk** found. All parameterized queries reviewed used `?` placeholders correctly. The FTS5 unescaped input in `mcp_tools_policy.py:171` is wrapped in try/except and degrades silently — not exploitable.
- **`tool_stats()` raw dict return** is correctly handled by special-case in `call_tool` (line 2957). Not a defect.
- **`datetime.now().astimezone()` in `mcp_tools_temporal.py:729`** is intentional — displaying local timezone context for `temporal_context` tool. Not a bug.

---

## Fix priority

| Priority | Finding | Effort |
|----------|---------|--------|
| P0 (ship blocker) | F-01: Temporal dispatch broken | Low — 5-line DISPATCH rewrite + 1 test |
| P0 (ship blocker) | F-02: pagerank missing from dispatch | Trivial — 1 line |
| P1 | F-03: `_surprise_score_mcp` bias unfixed | Low — 1 line + dedup |
| P1 | F-05: Recall boost cooldown tz-mix | Low — 2 lines |
| P1 | F-06: Health pruning tz-mix | Low — 2 lines |
| P1 | F-07: Labile rescue tz-mix | Low — 4 lines |
| P2 | F-04: Missing `ok` key | Low — 3 tool_error() swaps |
| P2 | F-08: User-local imports | Medium — move 3 files or declare extras |
| P3 | F-09: Rollout params not in MCP | Medium — schema + passthrough |
| P3 | F-10: borrow_from + scope conflict | Low — 3 lines |
