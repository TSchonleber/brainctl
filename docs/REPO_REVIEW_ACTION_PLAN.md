# brainctl — Repo Review & Action Plan

_Review date: 2026-08-06 · against `main` @ `c634808` · 18 open PRs, 9 open issues_

This is a triage-and-correction plan from a full sweep of open PRs and issues.
Every code claim below was verified against the tree at `c634808`.

---

## TL;DR

- **Three subsystems each have a documented command that is currently 100%
  non-functional** on a real database: `brainctl vec reindex` (#160),
  `brainctl vsearch` (#161), and `brainctl dream-cycle` / `dream-daemon`
  (#168). None of these three has a PR yet. **These are the P0 correction
  work.**
- **Three more bugs already have high-quality, tested PRs ready to land**
  (#151→#167, #152→#166, #159→#170). The FTS pair has a merge-order
  dependency: **#166 must land before/with #167.**
- **CI has not run on the three fork PRs** (first-time-contributor gating) —
  approve the workflow runs so they're validated before merge.
- **10 Dependabot PRs** are stale and safe to batch-merge, with a quick eye on
  two major GitHub Action bumps.
- Several **stale draft PRs** (#106, #96, #95, #93) need a resume-or-close call.

---

## Part 1 — Open Pull Requests (18)

### 1a. Ready to merge — substantive, tested fixes

| PR | Fixes | Summary | Assessment |
|----|-------|---------|------------|
| **#166** `fix(fts): scope memories_fts update triggers` | #152 | Scopes the `AFTER UPDATE` triggers to `content,category,tags,indexed,retired_at` so metadata-only writes (`_retrieval_practice_boost` on every search hit) stop eroding the external-content FTS5 index. Adds migration `083`, an idempotent MCP-side self-heal, and a real regression suite. | **Strong. Merge first.** Migration number `083` is clear (highest on main is `082`). Includes retire/delete/content-edit regression guards. |
| **#167** `fix(fts): populate memories_fts on init` | #151 | Seeds the external-content FTS index on `cmd_init` and adds a docsize-vs-active under-population auto-heal + repair migration `084`. | **Strong, but depends on #166.** The PR's own test note + the flipped `xfail` in `test_init.py` state the CLI `add→search` path only goes green once #166's scoped triggers also land. **Merge #166 → then #167.** |
| **#170** `fix(mcp): explain missing server extra` | #159 | Adds `mcp_entrypoint.py` as the console-script boundary; prints an actionable `pip install 'brainctl[mcp]'` hint instead of a raw `ModuleNotFoundError`, while re-raising unrelated import errors. README install section clarified. | **Clean, low-risk.** Good test coverage (missing-extra, unrelated-missing, happy-path). |

> **Action:** Approve CI for all three (see 1d), confirm green, then merge in
> order **#166 → #167 → #170**. #170 is independent and can go anytime.

### 1b. Dependabot (10) — batch triage

`#158` actions/cache 5.0.5→6.1.0 · `#157` actions/checkout 4→7 · `#154` zep-cloud ·
`#153` openai 2.34→2.41 · `#150` cognee · `#148` mem0ai · `#147` letta-client ·
`#143` softprops/action-gh-release 2→3 · `#142` actions/setup-python 5→6 ·
`#141` actions/upload-artifact 4→7

- The `zep-cloud`, `openai`, `cognee`, `mem0ai`, `letta-client` bumps are all
  **dev-dependencies** (comparison/bench harness only) → low blast radius.
- **Give two a quick look before merging:** `#157` (checkout 4→7) and `#141`
  (upload-artifact 4→7) are **major** version jumps — glance at the release
  workflow (`v4` upload-artifact deprecation changed artifact-download
  semantics) to be sure nothing breaks the `brainctl-mcp` bundle job.
- **Action:** merge the rest as a batch; sequence #141/#157 after a 60-second
  workflow compatibility check.

### 1c. Stale drafts — resume-or-close call needed

| PR | Age | Notes |
|----|-----|-------|
| **#106** per-agent cognitive profiles (owner draft) | 2026-05-08 | Your own draft, untouched ~3 months. Decide: resume, or close and re-file as an issue to keep the PR list clean. |
| **#96** retrieval executive + listwise reranker (fork) | Apr | Overlaps the existing rerank-profile path in `cmd_search`. Needs a rebase + benchmark check against `tests/bench/` before it's mergeable. |
| **#95** legacy comparison benchmark harness (fork) | Apr | Overlaps `bin/brainctl-bench` / `tests/bench/`. Likely superseded. |
| **#93** improve legacy benchmark retrieval flow (fork) | Apr | Same cluster as #95/#96; from the fork's `main` branch (messy base). |

> **Action:** Close #93/#95 if superseded by the shipped bench harness; ask
> #96's author to rebase and prove no `tests/bench/` regression, else close.
> Make a call on your own #106.

### 1d. CI gating

`get_status` on #166/#167/#170 returns `total_count: 0` — **no checks have
run** because they're from first-time external contributors and the workflow
needs maintainer approval. **Action:** approve the runs so the PRs are actually
validated before merge (don't merge on unrun CI).

---

## Part 2 — Open Issues (9)

### 2a. Bugs already covered by a PR (land the PR, close the issue)

- **#151** init doesn't populate FTS index → **PR #167**
- **#152** FTS index corruption on `memory_search` → **PR #166**
- **#159** `brainctl-mcp` raw `ModuleNotFoundError` without `[mcp]` → **PR #170**

### 2b. P0 bugs with NO PR — confirmed in code, each kills a documented command

> These are the real correction work. All three were reproduced against the
> source at `c634808`.

**#160 — `brainctl vec reindex` crashes with `ImportError` (dead command).**
`_impl.py:7855` imports `sample_db_embedding_widths` from
`agentmemory.embeddings` and calls it at `_impl.py:7873`, but **the function is
never defined anywhere** (`grep 'def sample_db_embedding_widths'` → nothing).
The import is at the top of `cmd_vec_reindex`, so the command fails before doing
any work. `vec reindex` is the documented model-migration path (`[vec]` extra
docstring), so that path is entirely dead.
_Fix:_ implement `sample_db_embedding_widths(conn, sample_size=8)` next to
`get_db_embedding_dim` returning `{ok, sample_count, consistent, declared_dim,
observed_dims, message}` (blob width = `len(blob)//4` for float32). Reporter
(`J0nd1sk`) says they have an implementation + tests ready to PR.

**#161 — `brainctl vsearch` aborts on missing vec tables (default invocation
broken).** `cmd_vsearch` defaults to `tables=["memories","events","context"]`
(`_impl.py:8081`), but `vec_events` / `vec_context` are **never created** —
grep confirms only *read* sites (`_impl.py:6449–6489`, `8197–8204`), no
`CREATE VIRTUAL TABLE`. A single missing table raises `OperationalError`, which
the CLI turns into "Database table missing", discarding results from
`vec_memories` which *is* present. So the default `vsearch` fails on essentially
every real DB, and the error's hint ("run `brainctl init`") is misleading —
`init` doesn't create those tables either.
_Fix (two parts):_ (1) make `_vsearch_table` catch `OperationalError` for a
missing table and return `[]` (immediate resilience fix); (2) decide the
intended behavior for events/context vector search — either implement the
indexing or drop them from the default `tables` set and document as
unsupported. Reporter has the resilience fix + regression test ready.

**#168 — `dream-cycle` / `dream-daemon` crash in NREM (non-functional on any DB
with recall history).** `hippocampus.run_hebbian_pass` (`~:1638`) uses naive
`now = datetime.now()`, then `days_since(now, created_at)`. `parse_ts`
(`:87–93`) turns a `Z`-suffixed timestamp into an **aware** datetime, and
`days_since` (`:96–101`) subtracts → `TypeError: can't subtract offset-naive and
offset-aware datetimes`. Since `memory_add` writes **aware** UTC (`_utc_now_iso`)
and `apply_recall_boost` writes **naive** local time, this fires on the first
Hebbian pass for any agent with real recall history. There's also **no per-phase
error isolation** in `run_dream_cycle`, so NREM aborting takes REM/Insight down
with it.
_Fix:_ normalize inside `parse_ts`/`days_since` (make both aware, or strip
tzinfo consistently). **Broader:** `datetime.now()` (naive) appears ~15× in
`hippocampus.py` alone — several of those silently compute **wrong ages** (off
by the local UTC offset) rather than crashing. Worth a repo-wide
naive-vs-aware audit and standardizing on aware UTC. Add per-phase try/except in
`run_dream_cycle` so one phase failing doesn't abort the rest.

> **Root-cause theme:** the FTS **and** vec/dream subsystems shipped code paths
> that were never exercised end-to-end. The FTS side is being cleaned up by
> #166/#167; the vec (`vec reindex`, `vsearch`) and consolidation (`dream-cycle`)
> sides are the remaining untested-and-broken surface.

### 2c. Feature gaps / design — not crashes, triage separately

- **#156** ToM tools work, but the Phase-4 auto-staleness hook is unimplemented.
  Feature gap; label and schedule, not a crash.
- **#155** No lifecycle/suppression controls for accidental event-log noise
  (labeled `bug`, but really a feature request). Relates to the `lifecycle`
  dispatcher surface — scope as an enhancement.
- **#116** Thalamus / Basal Ganglia / Cerebellum biologically-grounded
  architecture proposal. Design discussion — keep open as a tracking/RFC issue.

---

## Part 3 — Recommended execution order

1. **Approve CI** on #166, #167, #170; confirm green.
2. **Merge #166 → #167** (FTS pair, in that order), then **#170**. Closes
   #151, #152, #159.
3. **Fix the P0 vec/dream bugs** (#160, #161, #168) — accept the reporters'
   offered PRs for #160/#161 (review + require tests), and take #168 in-house.
   Each restores a documented command to working order.
4. **Do the naive-vs-aware datetime audit** flagged by #168 — the crash is one
   symptom; the silent-wrong-age variants are the more dangerous cousins.
5. **Batch-merge Dependabot** (#142–#158), after a quick compatibility glance at
   #141 (upload-artifact 4→7) and #157 (checkout 4→7).
6. **Resolve stale drafts** #106 / #96 / #95 / #93 (resume or close).
7. **Triage** #156 / #155 / #116 into the feature/RFC backlog.

### Suggested priority labels

- **P0 (broken commands):** #160, #161, #168
- **P1 (ready fixes, just land them):** #166, #167, #170
- **P2 (hygiene):** Dependabot batch, stale drafts
- **P3 (backlog):** #156, #155, #116

---

_Generated by Claude Code as a repo-review pass. All file/line references
verified against `main` @ `c634808`._
