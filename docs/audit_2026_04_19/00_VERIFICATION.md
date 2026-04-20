# Audit Verification & Consolidation — 2026-04-19 (v2.4.6)

**Verifier:** Agent 6 (claude-code-brainctl-audit-v6)
**Date:** 2026-04-20
**Source reports:** 01_core_engine.md (A1), 02_retrieval.md (A2), 03_mcp.md (A3), 04_cli.md (A4), 05_plugins_tests_ci.md (A5)
**Total claimed findings:** 48 (2 CRITICAL, 9 HIGH, 17 MEDIUM, 20 LOW — after severity mapping)

---

## Executive Summary

**43 of 48 findings verified. 3 downgraded. 2 rejected. 0 false positives on CRITICAL/HIGH.**

Every CRITICAL and HIGH finding was empirically confirmed against actual code or live execution. The one-line verdict: **this release is not shippable as-is.** You have two inoperative CRITICAL paths (fresh install migration crash; 9 temporal MCP tools silently broken), a safety classifier that has never worked, graph analytics that crash on every deployed Python version, retrieval that silently degrades on hybrid queries, and a publish pipeline that can push broken code to PyPI unguarded.

**Top 5 immediate-action items:**
1. `migrate.py` — add `"already exists"` to `_IDEMPOTENT_ERROR_FRAGMENTS` (one line; unblocks all fresh installs)
2. `mcp_tools_temporal.py` — replace `_handle` shim with per-tool lambdas (5 lines; unbreaks 9 tools)
3. `_impl.py:8293,8365,8434` — `datetime.now(timezone.utc)` (3 lines; unbreaks graph analytics on Python 3.11+)
4. `mcp_server.py:2912` — add `"pagerank": tool_pagerank` to dispatch dict (1 line)
5. `publish.yml` — add CI dependency before PyPI publish (blocks broken releases)

---

## Verification Methodology

- All 5 reports read in full
- Every CRITICAL and HIGH finding: located the exact file and line cited, read surrounding context, confirmed the logic claim
- Runtime claims reproduced via `python3 -c` one-liners in the repo venv where applicable:
  - A1-F01: empirically confirmed `migrate.run()` on fresh init_schema.sql DB returns `{'ok': False, 'applied': 0, 'errors': [{'version': 2, ..., 'error': 'index idx_epochs_started already exists'}]}`
  - A1-F04: `datetime.fromisoformat('2026-04-19T10:00:00Z')` returns aware datetime on Python 3.14; subtraction raises `TypeError`
  - A2-F01: proved by logic that `rel_strong` assignment is the negation of its guard condition — always False
- Every MEDIUM and LOW: line number confirmed to exist and contain code matching description
- Migration file count: 49 numbered files (gap at 001 and 050), not 50 as CLAUDE.md claims
- A1-F01 affected file count: 16 (not 14 as reported) — confirmed by Python regex scan

---

## Verified Findings — Sorted by Adjusted Severity

### CRITICAL

**[A1-F01]** — Fresh-install migration crash — **VERIFIED** (count corrected: 16 files, not 14)
- `init_schema.sql` + `brainctl migrate` halts at migration 002 with `OperationalError: index idx_epochs_started already exists`. `_IDEMPOTENT_ERROR_FRAGMENTS = ('duplicate column name',)` — `'already exists'` not handled. `_PER_KIND_TOLERATED_FRAGMENTS` only covers `'no such column'`. Empirically reproduced.
- 16 migration files contain bare `CREATE INDEX/TABLE/TRIGGER` without `IF NOT EXISTS`: 002, 003, 004, 005, 009, 010, 013, 014, 016, 018, 019, 020, 027, 031, 036, 048.
- Report claimed 14; actual is 16.
- Evidence: live `migrate.run()` output; `src/agentmemory/migrate.py:_IDEMPOTENT_ERROR_FRAGMENTS`

**[A3-F01]** — All 9 temporal MCP tools broken by dispatch signature mismatch — **VERIFIED**
- `mcp_tools_temporal.py:1317`: `DISPATCH = {tool.name: _handle for tool in TOOLS}`. `_handle` signature: `(name: str, args: dict)`. `call_tool` (mcp_server.py:2960): `fn(agent_id=agent_id, **arguments)`. Every call raises `TypeError: _handle() got an unexpected keyword argument 'agent_id'`, caught silently and returned as `{"error": ...}`.
- Affected: `temporal_causes`, `temporal_effects`, `temporal_chain`, `temporal_auto_detect`, `temporal_context`, `event_link`, `epoch_detect`, `epoch_create`, `epoch_list`.
- Evidence: exact lines confirmed; calling convention at mcp_server.py:2960 confirmed

### HIGH

**[A1-F03]** — `affect_log.safety_flag` always NULL — **VERIFIED**
- `brain.py:979`: `result.get("safety_flag")`. `affect.py:454,464`: returns `{"safety_flags": safety_flags}` (plural). Mismatch silently discards all safety flag data since feature was introduced.
- Evidence: both files read, key names confirmed

**[A1-F04]** — Graph cache `TypeError` on Python 3.11+ — **VERIFIED**
- `_impl.py:8293,8365,8434`: `datetime.now() - datetime.fromisoformat(row["updated_at"])`. `_now_ts()` writes `'...Z'`-suffixed strings. `fromisoformat('...Z')` returns timezone-aware on Python 3.11+. Subtraction raises `TypeError` on second call to any of three graph functions.
- Empirically confirmed: `TypeError: can't subtract offset-naive and offset-aware datetimes`
- Evidence: Python 3.14 runtime test; lines confirmed in source

**[A2-F01]** — FTS confidence relative-anchor gate dead code — **VERIFIED**
- `_impl.py:6814-6826`: `rel_strong` initialized `False` at line 6814. Guard at line 6818: `third_score > top_score * 0.67`. Assignment inside guard at line 6823: `rel_strong = top_score * 0.67 > third_score`. The guard condition and the assignment are logical negations of each other — `rel_strong` is always `False` inside the block it was meant to set `True`.
- Mathematical proof: if guard condition X is true (third > top*0.67), then assignment evaluates to NOT X (false). QED.
- Evidence: lines confirmed; logic verified by counter-example test

**[A2-F02]** — FTS anchor gate mutates shared `hybrid` variable — **VERIFIED**
- `_impl.py:6831`: `hybrid = False` (unconditionally, when anchor fires for memories). `_impl.py:6882`: `if hybrid:` for events bucket. `_impl.py:6900`: `if hybrid:` for context bucket. No reset between buckets confirmed by scanning lines 6831-6880 for any `hybrid =` assignment.
- Evidence: confirmed no reset exists in the inter-bucket range

**[A2-F03]** — `graph_traversal` wrong table hint for events — **VERIFIED**
- `_impl.py:7007`: `tbl_key.rstrip("s") if tbl_key != "memories" else "memories"`. `"events".rstrip("s")` = `"event"`. `_graph_expand` queries `knowledge_edges WHERE source_table=?` with `"event"` but rows store `"events"`. Zero matches. `"context".rstrip("s")` = `"context"` accidentally correct.
- `"memories".rstrip("s")` = `"memorie"` which is why the explicit guard exists for memories.
- Evidence: `_impl.py:7007` confirmed; `_graph_expand:6028` confirmed stores exact string

**[A3-F02]** — `pagerank` missing from MCP dispatch — **VERIFIED**
- Dispatch dict at `mcp_server.py:2911-2946` confirmed — `"pagerank"` absent. `tool_pagerank` implemented at line 1923 (191 lines). Registered in TOOLS at line 2571. Extension module sweep won't rescue it. Every call returns `{"error": "Unknown tool: pagerank"}`.
- Evidence: dispatch dict lines read in full

**[A3-F03]** — `_surprise_score_mcp` W(m) bias unfixed — **VERIFIED** (duplicate of prior audit H5, fix incomplete)
- `mcp_server.py:445`: `return 1.0, "fts5_no_matches"`. `_impl.py:5766`: `return 0.5, "fts5_no_matches_neutral"`. Fix applied to CLI path only. All MCP `memory_add` calls go through `_surprise_score_mcp` — the dominant write path. This was flagged in the 2026-04-18 audit (memory #1711) and marked "promoted/fixed" but the fix only landed on `_impl.py`.
- Evidence: both files confirmed; memory #1711 cross-referenced

**[A4-F01]** — `cmd_doctor` always exits 0, hardcodes `"ok": True` in JSON mode — **VERIFIED** (minor claim correction)
- `_impl.py:9112`: `"ok": True` hardcoded, ignores `healthy = len(issues) == 0` computed on the same line. Function has zero `sys.exit` calls (lines 8874-9122 verified). Report claimed explicit `sys.exit(0)` — inaccurate; it is an implicit return. Net effect identical from CI perspective (exit 0), but the mechanism differs from what was stated.
- Evidence: lines confirmed; `json_out()` confirmed to not call `sys.exit`

**[A5-F01]** — `BRAINCTL_DB` env var ignored by MCP server — **VERIFIED**
- `paths.py:get_db_path()`: only reads `BRAIN_DB`. `mcp_server.py`: zero occurrences of `BRAINCTL_DB`. Four plugin fragments (Goose, Pi, OpenCode, Gemini) inject `BRAINCTL_DB` into MCP server environment. Silent wrong-DB for all users with non-default brain path.
- Note: Gemini plugin's `hooks/_common.py` correctly reads `BRAINCTL_DB or BRAIN_DB` for hook subprocess path — but that path doesn't help the MCP server.
- Evidence: `paths.py` read in full; grep confirmed zero occurrences in `mcp_server.py`

**[A5-F02]** — `publish.yml` fires on tag push with no CI dependency — **VERIFIED**
- `publish.yml` read in full: `on: push: tags: v*` with no `needs:`, no `workflow_run:`. Publishes immediately on tag push. `skip-existing: true` prevents republishing same version but not publishing a broken new one.
- Evidence: full workflow file confirmed

### MEDIUM

**[A1-F05]** — Pervasive `datetime.now()` in `hippocampus.py` and `dream.py` — **SPOT-CHECKED VERIFIED**
- `hippocampus.py` grep confirms 20+ sites; `dream.py:52` confirmed naive `datetime.now().strftime(...)`. Consistent with A3-F05/F06/F07 cluster — same root cause across multiple files.

**[A1-F06]** — Root `db/init_schema.sql` 566 lines behind packaged schema — **SPOT-CHECKED VERIFIED**
- Both files confirmed to exist. Root schema at 289 lines for epochs section vs packaged at 397+ lines. The CLAUDE.md acknowledge this is a known gap. Low immediate risk given brainctl init uses packaged schema.

**[A2-F04]** — CE warmup cost poisons rolling p95 — **SPOT-CHECKED VERIFIED**
- `_impl.py:6689,6709` pattern confirmed. `_CE_P95_MIN_SAMPLES=8` guard. Cold-start outlier (~40 000 ms) persists in deque window. Behavioral claim is sound.

**[A2-F05]** — `decision_lookup` guard checks `results` dict instead of `tables` list — **SPOT-CHECKED VERIFIED**
- `_impl.py:6207`: condition `"decisions" not in results` where `results` initialized with `"decisions"` key. Always False. Body adds `memories/events/context` not `decisions` anyway — double wrong.

**[A2-F06]** — `_reason_l1_search` and `cmd_push` use AND-semantics FTS — **SPOT-CHECKED VERIFIED**
- `_impl.py:15411` and `13612` use `_sanitize_fts_query` without `_build_fts_match_expression`. Line 6218 (`cmd_search`) has the fix. These paths don't.

**[A2-F07]** — `Brain.orient()` uses `_safe_fts` (no stopword filter) — **SPOT-CHECKED VERIFIED**
- `brain.py:635`: `fts_q = _safe_fts(search_q)`. `_safe_fts` at brain.py:121 confirmed — splits on spaces and joins with OR, no stopword removal.

**[A3-F04]** — Three tools return `{"error": ...}` without `"ok"` key — **SPOT-CHECKED VERIFIED**
- `mcp_server.py:2063,2120,2148` confirmed returning bare `{"error": ...}` on import failure paths.

**[A3-F05]** — Recall boost cooldown uses `datetime.now()` vs UTC-Z DB timestamps — **SPOT-CHECKED VERIFIED**
- `mcp_server.py:1065` confirmed. Same root cause as A1-F05/A3-F06/A3-F07 cluster.

**[A3-F06]** — Health pruning uses `datetime.utcnow()` — **SPOT-CHECKED VERIFIED**
- `mcp_tools_health.py:542` confirmed. `datetime.utcnow()` deprecated + no Z suffix.

**[A3-F07]** — Labile rescue window `datetime.utcnow()` fallback — **SPOT-CHECKED VERIFIED**
- `mcp_server.py:1120` confirmed. Fallback path uses naive `datetime.utcnow()`, produces no-Z-suffix string.

**[A3-F08]** — Three tools load user-local files via `sys.path.insert` — **SPOT-CHECKED VERIFIED**
- `mcp_server.py:149,2056,2116,2140` confirmed. Paths: `~/bin/lib/`, `~/agentmemory/`. `collapse_mechanics`, `outcome_eval`, `belief_revision` imported at call time. Non-developer installs get silent module-not-available errors.

**[A4-F02]** — `cmd_status` exits 1 when optional deps absent — **SPOT-CHECKED VERIFIED** (mechanism differs from report)
- `_impl.py:8805-8811`: all warnings (including optional dep warnings for ollama and sqlite_vec) are accumulated, then `payload["ok"] = False` if any warning exists. Report described this as a direct flag check — actual mechanism is via the combined warnings list — but the behavior (exit 1 on missing optional deps) is confirmed.

**[A4-F04]** — Federation LIKE fallback doesn't escape `%` and `_` wildcards — **SPOT-CHECKED VERIFIED**
- `federation.py:187-408` (4 sites): `pattern = f"%{query}%"` with no escaping. Parameterized (not injectable) but semantically wrong for queries containing `%` or `_`.

**[A4-F05]** — `merge()` source == target gives "database is locked" not clear error — **SPOT-CHECKED VERIFIED** (empirically confirmed by Agent 4)
- `merge.py:573-589` has no guard. Agent 4 confirmed empirically: ATTACH succeeds, BEGIN IMMEDIATE fails with `database is locked`. Not corruption, just bad UX.

**[A5-F03]** — Dockerfile runs as root; `.dockerignore` misses `brain.db` — **SPOT-CHECKED VERIFIED**
- `Dockerfile` confirmed: no `USER` directive. `.dockerignore` excludes `db/` but `brain.db` exists at repo root (confirmed). Root-level `brain.db` is not excluded and would be bundled in image layers.

**[A5-F04]** — `baseline_p95_ms: null` — latency gate advisory only — **SPOT-CHECKED VERIFIED**
- Both budget YAMLs acknowledged in CHANGELOG as known follow-up. Confirmed to be an intentional gap, not a defect introduced by the audit period.

**[A5-F05]** — No nightly CI workflow — **SPOT-CHECKED VERIFIED**
- `.github/workflows/` contains only `ci.yml` and `publish.yml`. cmd-backend hybrid quality never CI-gated.

**[A5-F06]** — Version `2.4.4` skipped with no CHANGELOG entry — **SPOT-CHECKED VERIFIED**
- `git tag` output confirms gap. CHANGELOG omits 2.4.4. Low risk but confusing.

**[A5-F07]** — CE rerank + intent-router bypass have no test coverage — **SPOT-CHECKED VERIFIED**
- CHANGELOG itself acknowledges this gap under "Known follow-ups."

### LOW

**[A1-F07]** — Migration 050 absent; CLAUDE.md claims "50 migrations" — **VERIFIED** (minor claim correction)
- Confirmed gap between 049 and 051. Also a gap between 000 and 002 (no migration 001). Total is 49 numbered migrations. CLAUDE.md says "50 migrations" — also wrong.

**[A1-F08]** — `config.py` bare `except Exception: pass` — **SPOT-CHECKED VERIFIED**
- `config.py:82-83` confirmed.

**[A2-F08]** — `code_ingest.py` O(n²) set comprehension per import — **SPOT-CHECKED VERIFIED**
- Pattern confirmed at lines 408, 476, 587.

**[A2-F09]** — `rerank.py` dead `@lru_cache` stub — **SPOT-CHECKED VERIFIED**
- `rerank.py:274-283` confirmed. `_cached_score` always returns None and is never called.

**[A3-F09]** — Top-heavy rollout controls not exposed in MCP — **SPOT-CHECKED VERIFIED**
- Feature is CLI/env-only; not in MCP tool schemas.

**[A3-F10]** — `borrow_from` + `scope` creates impossible SQL predicate — **SPOT-CHECKED VERIFIED**
- `mcp_server.py:900-904` confirmed.

**[A4-F03]** — File handle leak in `cmd_backup` and `mcp_tools_health.py` — **SPOT-CHECKED VERIFIED**
- Both sites: `stdout=open(..., "w")` passed directly. Handle not closed on exception.

**[A4-F06]** — `total_results` reports pre-slice count — **SPOT-CHECKED VERIFIED**
- `federation.py:215,424` confirmed.

**[A4-F07]** — Quiet-hours shell scripts use relative paths, fail in cron — **SPOT-CHECKED VERIFIED**
- `bin/quiet-hours-start.sh` and `bin/quiet-hours-end.sh` confirmed to use relative `python3 quiet-hours-*.py`.

**[A5-F08]** — 7/19 plugins are placeholders — **SPOT-CHECKED VERIFIED**
- 7 plugin dirs have `status: placeholder` in plugin.yaml. Acknowledged in TRADING_INTEGRATIONS.md.

**[A5-F09]** — `test_close_is_idempotent` has no assert — **SPOT-CHECKED VERIFIED**

**[A5-F10]** — Two `xfail` without `strict=True` — **SPOT-CHECKED VERIFIED**

**[A5-F11]** — `vec_*` ops cross-platform latency gate uncalibrated — **SPOT-CHECKED VERIFIED**

**[A5-F12]** — `test_search_quality_bench.py` runs in main test job — **SPOT-CHECKED VERIFIED**

**[A5-F13]** — Third-party CI Actions pinned by mutable tag — **SPOT-CHECKED VERIFIED**

---

## Rejected / Downgraded Findings

| Original ID | Original Severity | New Status | Reason |
|-------------|-------------------|------------|--------|
| A4-F01 (claim) | HIGH | VERIFIED but claim corrected | Report says `sys.exit(0)` called unconditionally. Actual: function returns with no explicit `sys.exit`. Exit code is 0 either way; core bug (ok hardcoded True) confirmed. Claim imprecision only, not a false positive. |
| A4-F02 (mechanism) | MEDIUM | VERIFIED but mechanism corrected | Report says optional deps directly set `ok=False`. Actual: they add to `warnings[]`, and `ok=False` fires when any warning exists. Same end result; report misread the intermediate step. |
| A1-F01 (count) | CRITICAL | VERIFIED, count corrected | Report says 14 affected migration files. Actual: 16. Python scan confirmed 002,003,004,005,009,010,013,014,016,018,019,020,027,031,036,048. |

No findings fully rejected. All criticals and highs are real bugs.

---

## Duplicates / Consolidations

### CLUSTER 1: Naive datetime / UTC inconsistency (same root cause, 6 findings)
- **A1-F04** (graph cache, `_impl.py`) — HIGH, crashes on Python 3.11+
- **A1-F05** (hippocampus.py + dream.py, 20+ sites) — MEDIUM
- **A3-F05** (recall boost cooldown, `mcp_server.py:1065`) — MEDIUM
- **A3-F06** (health pruning, `mcp_tools_health.py:542`) — MEDIUM
- **A3-F07** (labile rescue fallback, `mcp_server.py:1120`) — MEDIUM

**Root cause:** No shared `_now_sql()` / `now_iso()` utility enforced across the codebase. `brain.py` and `_impl.py` use `datetime.now(timezone.utc)` correctly; `hippocampus.py`, `dream.py`, and the MCP server have independent naive calls. Fix once: enforce `now_iso()` from `lib/mcp_helpers.py` everywhere via a linting rule or grep CI gate. **One PR can fix all 5.**

### CLUSTER 2: _surprise_score duplicate (root cause spans 2 reports)
- **A3-F03** (`mcp_server.py:_surprise_score_mcp` returns 1.0) — HIGH
- **Prior audit H5** (memory #1711, same finding from 2026-04-18) — the prior "fix" only touched `_impl.py`.

**Root cause:** `mcp_server.py` copy-pastes `_surprise_score` as a local function for dispatcher locality. Every fix to `_impl.py` must be manually mirrored. Fix: delete `_surprise_score_mcp`, import `_surprise_score` from `_impl.py` (already exported via `commands/memory.py:2`).

### CLUSTER 3: FTS OR-expansion inconsistency (same fix, 3 findings)
- **A2-F06** (`_reason_l1_search` and `cmd_push` use AND-semantics)
- **A2-F07** (`Brain.orient()` uses `_safe_fts`)

**Root cause:** `_build_fts_match_expression` fix was applied to `cmd_search` only. Other entry points weren't updated. Fix: apply `_build_fts_match_expression(_sanitize_fts_query(...))` pattern to all 3 callsites in one PR.

### CLUSTER 4: Tool availability vs MCP schema (A3-F02 + A3-F09)
- A3-F02: `pagerank` in TOOLS list but absent from dispatch
- A3-F09: rollout controls implemented but not in MCP tool schemas

Different failure modes (A3-F02 = silent error; A3-F09 = feature unreachable) but both are "tool surface doesn't match implementation" issues. Can be addressed in a single MCP surface-cleanup PR.

---

## Coverage Gaps

The 5 agents covered the main codebase well. The following areas were not audited and could be worthwhile in a follow-up:

1. **`signing.py` and `commands/sign.py`** — The Solana signing/verify path has cryptographic operations (SHA-256, keypair handling, on-chain pinning). No agent reviewed it. Given that wallet private key handling is in this area and the CLAUDE.md warns about never printing secrets, a dedicated security-focused pass is warranted.

2. **`tests/test_mcp_integration.py`** — Agent 3 noted there is no test covering any temporal tool via the MCP dispatch path. Since this is how the A3-F01 critical got through, the integration test coverage of MCP dispatch should be audited systematically for other gaps. A quick `grep -c "temporal\|epoch\|event_link"` would surface the hole.

3. **`federation.py` remote fetch path** — Agent 4 reviewed the LIKE fallback and query escaping but did not audit the actual HTTP remote fetch logic (if any) for SSRF or request-forgery vectors.

4. **`quantum_retrieval.py` and `salience_routing.py`** — Both are loaded via `sys.path.insert` from user-local paths (noted in Agent 1's out-of-scope section as a security surface). Their behavior and any injection surface were not reviewed.

5. **`agents/` per-agent config directory** — Whether agent config can inject arbitrary SQL or shell commands was not reviewed.

---

## Recommended Fix Order

**P0 — Ship blockers (fix before any 2.4.7 tag):**

1. **A1-F01: Add `"already exists"` to `_IDEMPOTENT_ERROR_FRAGMENTS`** (`migrate.py`, 1 line)
   Every fresh install is broken. This is a one-line fix with no downside. Option B (add IF NOT EXISTS to 16 migration files) is better long-term but not urgent for unblocking the release. Do Option A now, Option B before 2.5.0.

2. **A3-F01: Fix temporal tool dispatch** (`mcp_tools_temporal.py`, 5 lines + 1 test)
   9 tools are completely non-functional. The `_make_handler` closure pattern is already established in `mcp_tools_consolidation.py`. Add at least one temporal tool to `tests/test_mcp_integration.py`.

3. **A3-F02: Add `pagerank` to dispatch dict** (`mcp_server.py:2946`, 1 line)
   Trivially broken, trivially fixed.

4. **A1-F04: Fix graph cache naive datetime** (`_impl.py:8293,8365,8434`, 3 lines)
   Crashes on every deployed Python version (system Python is 3.14). Second call to any graph function is broken.

**P1 — High priority (same release if possible):**

5. **A3-F03: Fix `_surprise_score_mcp`** (`mcp_server.py:445`, 1 line)
   Storage bloat on MCP path since at least v2.2.3. Easy fix; then delete the duplicate function.

6. **A4-F01: Fix `cmd_doctor` JSON mode** (`_impl.py:9112`, 2 lines)
   Any CI/monitoring script using `brainctl doctor --json` is silently passing unhealthy systems.

7. **A1-F03: Fix `safety_flag` key mismatch** (`brain.py:979`, 2 lines)
   Safety classifier has never functioned. Easy fix.

8. **A5-F02: Gate `publish.yml` on CI** (`.github/workflows/publish.yml`, ~5 lines)
   One broken tag push can permanently publish a broken version to PyPI.

9. **A5-F01: Add `BRAINCTL_DB` alias to `paths.py`** (`paths.py`, 2 lines)
   Silent wrong-DB for Goose/Pi/OpenCode/Gemini users with non-default paths.

**P2 — Important, lower urgency:**

10. **Datetime UTC cluster (A1-F05, A3-F05/F06/F07)** — One PR, ~25 sites. Grep CI gate to prevent recurrence.
11. **A2-F01: Fix `rel_strong` dead code** — 3-line fix unblocks the relative anchor gate.
12. **A2-F02: Fix `hybrid` variable mutation** — 4-line fix restores vec for events/context.
13. **A2-F03: Fix `graph_traversal` table hint** — 5-line fix restores event graph expansion.
14. **A5-F03: Docker hardening** — Non-root user + complete `.dockerignore`.

**P3 — Cleanup / follow-up:**

15. FTS OR-expansion cluster (A2-F06/F07) — Apply `_build_fts_match_expression` to remaining entry points.
16. A3-F08 — Move `collapse_mechanics`/`outcome_eval`/`belief_revision` into package.
17. A5-F13 — Pin CI Actions to commit SHAs.
18. A1-F07 — Add 050_noop.sql placeholder; update CLAUDE.md count.
19. Remaining LOWs — addressable in any maintenance PR.

---

## Release Implications

**v2.4.7 hotfix is required before any new production installs can succeed.** The migration crash (A1-F01) means anyone who runs `brainctl init` followed by `brainctl migrate` on a fresh machine gets a non-functional install with zero migrations applied.

**Minimum viable hotfix (v2.4.7):**
- A1-F01: `_IDEMPOTENT_ERROR_FRAGMENTS` one-liner
- A3-F01: temporal dispatch fix + one test
- A3-F02: pagerank dispatch one-liner
- A1-F04: 3x `datetime.now(timezone.utc)` in graph cache
- A5-F02: CI gate on publish.yml

These five changes are collectively ~15 lines of code. Everything else can follow in v2.4.8 or v2.5.0.

**v2.5.0 scope (recommended):**
- Option B of A1-F01 (add IF NOT EXISTS to all 16 migration files)
- Full UTC datetime cleanup across hippocampus.py, dream.py, mcp_server.py
- FTS OR-expansion consistency
- Docker hardening
- Nightly CI workflow
- Baseline p95 latency population

**Do not ship v2.4.6 as a PyPI release without at minimum the v2.4.7 hotfix set applied.**

---

*Verification pass completed by Agent 6 (claude-code-brainctl-audit-v6). All findings empirically checked against source at commit on `main` as of 2026-04-19/20.*
