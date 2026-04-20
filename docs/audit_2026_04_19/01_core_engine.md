# brainctl v2.4.6 — Core Engine & Schema Audit

**Audit date:** 2026-04-19  
**Auditor:** Claude Code Agent 1 (claude-code-brainctl-audit-core)  
**Scope:** `brain.py`, `_impl.py`, `_gates.py`, `db.py`, `migrate.py` + `db/migrations/`, `hippocampus.py`, `dream.py`, `affect.py`, `config.py`  
**Version delta focus:** Changes since v2.4.3 (new in 2.4.4/2.4.5: code ingest; 2.4.6: top-heavy retrieval, strict CI gates, migration 048 FTS5 fix)

---

## Executive Summary

Eight findings across four severity levels. Two issues block correctness in production today (F-01 fresh-install migration failure; F-04 graph cache TypeError on Python 3.11+). One finding silently corrupts data that has been accumulating since at least v2.4.3 (F-03: `affect_log.safety_flag` always NULL). One finding (F-02: wrong schema_versions table name in 11 migrations) would compound the fresh-install failure once F-01 is fixed. Two medium findings (F-05 pervasive naive datetime; F-06 root init_schema staleness) are latent regressions. Two low findings (F-07 gap at migration 050; F-08 silent config swallow) are low-risk papercuts.

No findings related to WAL mode, FK enforcement, connection pooling, the W(m) worthiness gate logic, or the code-ingest SHA256 cache — those are sound.

---

## Findings

### [CRITICAL] F-01: Fresh-install migration run halts at migration 002 due to `CREATE INDEX` without `IF NOT EXISTS`

**File(s):** `src/agentmemory/migrate.py` (runner); `db/migrations/002_epochs_and_temporal.sql` and 13 others  
**Claim:** Running `brainctl migrate` on a database that was initialized via the packaged `init_schema.sql` fails immediately at migration 002 with `sqlite3.OperationalError: index idx_epochs_started already exists`.  

**Evidence:**

1. `src/agentmemory/db/init_schema.sql` (canonical, packaged) creates `idx_epochs_started`, `idx_memories_agent`, `idx_events_agent`, and many other indexes.
2. Migration 002 (`002_epochs_and_temporal.sql`) opens with:
   ```sql
   CREATE INDEX idx_epochs_started ON epochs(started_at);
   ```
   No `IF NOT EXISTS` guard.
3. `migrate.py` `_IDEMPOTENT_ERROR_FRAGMENTS = ("duplicate column name",)` — "already exists" is **not** in the tolerated list.
4. `_apply_sql()` re-raises the error; the `except Exception` in `run()` records it as `ok: False` and halts.
5. Empirically confirmed: running `migrate.run()` on a fresh DB returns `{'ok': False, 'applied': 0, 'errors': [{'version': 2, 'name': 'epochs and temporal', 'error': 'index idx_epochs_started already exists'}]}`.

**Affected migration files (14 total):** 002, 003, 005, 009, 010, 013, 014, 016, 018, 019, 020, 031, 036, 048 — all contain `CREATE INDEX`, `CREATE TABLE`, or `CREATE TRIGGER` statements that duplicate objects already present in `init_schema.sql`, without `IF NOT EXISTS`.

**Impact:** Any fresh install that initialises from `init_schema.sql` and then runs `brainctl migrate` gets zero migrations applied. All schema additions from migrations 002 onward are absent. Depending on which tables/columns exist in `init_schema.sql` at a given release, this could leave columns, indexes, and triggers in an inconsistent state. The failure is silent to end users unless they inspect exit codes.

**Recommended fix:**

Option A (minimal, correct): Add `"already exists"` to `_IDEMPOTENT_ERROR_FRAGMENTS`. This swallows the collision and lets migration proceed. Appropriate because objects that already exist in init_schema.sql are by definition already applied. Caveat: also silences genuine "table/index already exists" errors introduced by bugs — though those would surface immediately in other ways.

Option B (thorough): Audit all 14 affected migration files and add `IF NOT EXISTS` to each conflicting `CREATE` statement. This preserves error visibility for genuinely new objects while fixing idempotency for known-good ones.

Option B is preferred for long-term correctness; Option A is a safe one-line patch for an emergency release.

---

### [HIGH] F-02: 11 migration files insert into `schema_version` (missing 's') — wrong tracking table name

**File(s):** `db/migrations/002_epochs_and_temporal.sql`, 007, 008, 012, 017, 043, 044, 045, 046, 047, 048  
**Claim:** These 11 files contain `INSERT INTO schema_version` but the actual tracking table is `schema_versions` (plural, as created by migration 001 and verified in `migrate.py` `_VERSIONS_TABLE = "schema_versions"`).

**Evidence:**

```bash
grep -l "INSERT INTO schema_version[^s]" db/migrations/*.sql
# Returns: 002, 007, 008, 012, 017, 043, 044, 045, 046, 047, 048
```

The runner does NOT use the in-file `INSERT INTO schema_version` statements to track completion — `migrate.py` executes its own `INSERT INTO schema_versions` after each successful migration block. The in-file inserts are therefore dead code that would fail with `no such table: schema_version` if executed on a DB that lacks a legacy alias. This doesn't currently cause a crash because the runner catches per-statement errors in `_apply_sql()` — but it means 11 migrations contain unreachable tracking statements and emit silent failures.

**Impact:** Medium operational risk. If a future developer relies on the in-file tracking inserts (e.g., running a migration file directly via `sqlite3` CLI), the version won't be recorded. Also clutters the migration files with incorrect statements.

**Recommended fix:** Remove or correct the in-file `INSERT INTO schema_version` statements. If tracking from within the SQL file is desired, update to `schema_versions`. Prefer removing them since `migrate.py` already handles tracking.

---

### [HIGH] F-03: `Brain.affect_log()` always writes NULL to `affect_log.safety_flag` — key name mismatch

**File(s):** `src/agentmemory/brain.py:979`; `src/agentmemory/affect.py:return dict`  
**Claim:** `brain.py` reads `result.get("safety_flag")` (singular) but `classify_affect()` in `affect.py` returns a dict with key `"safety_flags"` (plural, list). The column is always written as NULL.

**Evidence:**

`brain.py` line 979:
```python
safety_flag = result.get("safety_flag")        # <-- singular, wrong key
```

`affect.py` — every return path in `classify_affect()`:
```python
return {
    ...
    "safety_flags": flags,   # <-- plural
}
```

`_neutral_result()` also returns `"safety_flags": []`.

The DB column is `affect_log.safety_flag TEXT` (singular). The intent is to store a joined string of any matched safety labels. Because `result.get("safety_flag")` returns `None`, every `affect_log` row has `safety_flag = NULL` regardless of actual affect content.

**Impact:** Safety flag data has been silently discarded since this code was introduced. Any downstream query on `affect_log.safety_flag IS NOT NULL` or on safety_flag values returns no results. The affect classifier's safety detection is effectively neutered.

**Recommended fix:**

```python
# brain.py line 979 — change from:
safety_flag = result.get("safety_flag")
# to:
safety_flags_list = result.get("safety_flags") or []
safety_flag = ", ".join(safety_flags_list) if safety_flags_list else None
```

This joins the list into a comma-separated string for the TEXT column, consistent with the column's intended single-value-or-null semantics. Alternatively, change the column to store JSON array if multi-flag granularity is needed.

---

### [HIGH] F-04: Graph cache age checks in `_impl.py` raise `TypeError` on Python 3.11+ — naive vs. aware datetime subtraction

**File(s):** `src/agentmemory/_impl.py:8293`, `8365`, `8434`  
**Claim:** The graph cache freshness check subtracts a naive `datetime.now()` from a UTC-aware `datetime` returned by `fromisoformat(row["updated_at"])`. On Python 3.11+, `fromisoformat('...Z')` returns a timezone-aware datetime, making the subtraction a `TypeError`.

**Evidence:**

Three identical patterns in `_graph_pagerank`, `_graph_communities`, `_graph_betweenness`:
```python
age_hours = (datetime.now() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
```

`row["updated_at"]` is written by `_now_ts()` which produces strings like `"2026-04-19T10:00:00Z"`.

On Python 3.11+:
```python
>>> datetime.fromisoformat("2026-04-19T10:00:00Z")
datetime.datetime(2026, 4, 19, 10, 0, tzinfo=datetime.timezone.utc)
>>> datetime.now() - datetime.fromisoformat("2026-04-19T10:00:00Z")
TypeError: can't subtract offset-naive and offset-aware datetimes
```

Confirmed: system Python (3.14) raises `TypeError` as expected. The error occurs on the *second* call to any of these three graph functions — the first call always recomputes (cache miss), so the row doesn't exist yet. On second call the cache row is read, `fromisoformat` returns an aware datetime, and the subtraction crashes.

**Impact:** `pagerank`, graph communities, and betweenness centrality features silently fail on the second invocation on Python 3.11+. The crash propagates as an unhandled exception in the calling MCP tool. This affects any feature that builds on top of graph analytics.

**Recommended fix:** Use `datetime.now(timezone.utc)` in the subtraction:

```python
# All three locations — change:
age_hours = (datetime.now() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
# to:
age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
```

`timezone` is already imported in `_impl.py` (line 726 uses it). No additional import needed.

---

### [MEDIUM] F-05: Pervasive `datetime.now()` (naive, local time) in `hippocampus.py` and `dream.py` — regression from UTC standard

**File(s):** `src/agentmemory/hippocampus.py` (20+ call sites); `src/agentmemory/dream.py:52`  
**Claim:** Multiple functions write naive local-time strings to DB timestamp columns, inconsistent with the UTC-aware standard adopted in `brain.py`, `_impl.py`, and `affect.py`.

**Evidence (selected):**

`hippocampus.py:121` (`cmd_decay`):
```python
now = datetime.now()     # naive — decay calculation uses local wall clock
```

`hippocampus.py:303` (`compress_scope_group`):
```python
datetime.now().strftime("%Y-%m-%dT%H:%M:%S")  # naive, written to events table
```

`dream.py:52` (`_now_sql`):
```python
return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")  # naive, no UTC
```

`hippocampus.py` additional naive `datetime.now()` call sites: lines 123, 303, 627, 877, 946, 1125, 1271, 1320, 1402, 1495, 1621, 1896, 2092, 2248, 2467, 2583, 2710, 2945, 3119, 3311, 3382, 3446, 3499.

**Impact:** On machines not running UTC (practically all developer machines), decay, consolidation, and dream-cycle timestamps are off by the local UTC offset. This causes incorrect decay calculations and misleading event logs. Time-zone transitions (DST) can cause non-monotonic timestamps. Cross-timezone deployments will produce inconsistent ordering of memory events.

**Recommended fix:** Introduce a module-level `_now_sql()` helper in `hippocampus.py` mirroring `dream.py`'s `_now_sql` but UTC-correct:

```python
from datetime import datetime, timezone

def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

Replace all `datetime.now()` call sites with `_now_sql()` or `datetime.now(timezone.utc)`. `dream.py:_now_sql()` needs the same fix.

---

### [MEDIUM] F-06: `db/init_schema.sql` (dev root) is 566 lines behind the packaged canonical schema

**File(s):** `db/init_schema.sql` (root); `src/agentmemory/db/init_schema.sql` (packaged)  
**Claim:** The root dev-checkout init_schema.sql is missing 15+ columns on the `memories` table, several indexes, the correct FTS5 split-pair triggers, and the `code_ingest_cache` table. It still uses the pre-v2.4.5 single `memories_fts_update` trigger.

**Evidence (diff summary, missing from root):**

Columns absent from root `memories`:
- `write_tier`, `indexed`, `promoted_at`, `replay_priority`, `ripple_tags`, `labile_until`, `labile_agent_id`, `retrieval_prediction_error`, `encoding_affect_id`, `tag_cycles_remaining`, `stability`, `encoding_task_context`, `encoding_context_hash`, `temporal_level`, `next_review_at`, `q_value`

Other missing objects:
- All indexes on the new columns (`idx_memories_labile_until`, `idx_memories_write_tier`, etc.)
- The FTS5 split-pair triggers (`memories_fts_update_delete` + `memories_fts_update_insert` with `WHEN new.indexed = 1 AND new.retired_at IS NULL` guard)
- `code_ingest_cache` table (migration 051)

The root file has a developer comment acknowledging partial staleness but does not reflect the full delta.

**Impact:** A developer initializing a test DB from the root schema gets a database with 15+ missing columns and the old FTS5 trigger pattern. Tests against this schema produce false positives for code that works on the packaged schema, and false negatives for code that depends on the new columns. CI that uses root init_schema will mask the FTS5 split-pair regression that migration 048 was designed to fix.

**Recommended fix:** Either regenerate `db/init_schema.sql` from the packaged canonical (`src/agentmemory/db/init_schema.sql`) as part of the release script, or remove the root copy entirely and update developer docs to point to the packaged path. A Makefile/tox target that keeps them in sync would prevent recurrence.

---

### [LOW] F-07: Migration 050 absent — gap in migration sequence; CLAUDE.md claims "50 migrations" but 49 exist

**File(s):** `db/migrations/`; `CLAUDE.md`  
**Claim:** Migration files go from 049 to 051, skipping 050. The `CLAUDE.md` architecture section states "50 migrations" but the actual count is 49 (plus the unnumbered `quantum_schema_migration_sqlite.sql`). The task brief describes "Migration 050 fix for FTS5 batching corruption" but the actual FTS5 batching fix is in migration 048.

**Evidence:**

```bash
ls db/migrations/*.sql | sort | grep -E "04[89]|05[012]"
# 048_fk_integrity_fts_retire_trigger.sql
# 049_...
# 051_code_ingest_cache.sql
# (no 050)
```

Migration 048 header: explicitly describes the FTS5 batching corruption fix.

**Impact:** Low operational risk — the migration runner reads version numbers from filenames and skips gaps. No correctness issue at runtime. However, the gap suggests a file was either never committed or was renumbered; any future migration labeled 050 would run as an out-of-order migration on DBs that already have 051.

**Recommended fix:** Either create a placeholder `050_noop.sql` that inserts a version record with a comment, or renumber 051 → 050 at the next major version bump (requires coordinated DB migration). Update CLAUDE.md count to 49.

---

### [LOW] F-08: `config.py` silently swallows all exceptions on malformed TOML

**File(s):** `src/agentmemory/config.py:82-83`  
**Claim:** The config loader catches all exceptions with `except Exception: pass`, including `FileNotFoundError`, `PermissionError`, and TOML parse errors. A broken config file silently produces default values with no diagnostic.

**Evidence:**

```python
try:
    with open(cfg_path) as f:
        raw = toml.load(f)
    _merge(cfg, raw)
except Exception:
    pass
```

**Impact:** Low. Config errors are generally non-fatal and defaults are reasonable. However, a developer who accidentally writes invalid TOML to a config file will see no error — they'll just get unexpected default behavior.

**Recommended fix:** Log a warning at minimum:

```python
except Exception as exc:
    import logging
    logging.getLogger(__name__).warning("Config load failed (%s): %s", cfg_path, exc)
```

Or raise on `PermissionError` / `OSError` while swallowing parse errors, depending on tolerance preference.

---

## Summary Table

| ID | Severity | File(s) | One-liner |
|----|----------|---------|-----------|
| F-01 | CRITICAL | `migrate.py`, 14 migration files | Fresh-install halts at migration 002 — missing `IF NOT EXISTS` |
| F-02 | HIGH | 11 migration files | `INSERT INTO schema_version` (missing 's') — dead tracking code |
| F-03 | HIGH | `brain.py:979`, `affect.py` | `safety_flag` always NULL — key name mismatch (`safety_flags` vs `safety_flag`) |
| F-04 | HIGH | `_impl.py:8293,8365,8434` | Graph cache raises `TypeError` on Py3.11+ — naive vs. aware datetime subtraction |
| F-05 | MEDIUM | `hippocampus.py` (20+ sites), `dream.py:52` | Pervasive `datetime.now()` writes local-time to UTC timestamp columns |
| F-06 | MEDIUM | `db/init_schema.sql` (root) | Root schema 566 lines behind packaged canonical — 15+ missing columns |
| F-07 | LOW | `db/migrations/` | Migration 050 absent; sequence gap; CLAUDE.md count wrong |
| F-08 | LOW | `config.py:82-83` | Bare `except Exception: pass` on TOML load silences all config errors |

---

## Out-of-scope / Sound

The following areas were inspected and found to be correct or are deferred to other audit agents:

- WAL mode + FK enforcement setup in `brain.py._open_shared_conn()` and `_impl.py.get_db()` — correct
- `_now_ts()` in `brain.py` and `_utc_now_iso()` in `_impl.py` — both correctly use `datetime.now(timezone.utc)`
- `_stdev_seconds()` in `_impl.py:458` — correctly strips 'Z' before `fromisoformat()`
- W(m) worthiness gate logic in `_gates.py` — the `force=False` inner call is reached only on the `not force` branch, so correct
- Code ingest SHA256 cache in `code_ingest.py` — symlink handling and `followlinks=False` are sound
- `get_brain()` factory in `brain.py` — correct per-(db_path, agent_id) caching with module-level lock
- Connection thread safety: `check_same_thread=False` + `threading.RLock` pattern — correct
- `sys.path.insert` in `_impl.py` for salience_routing and quantum_retrieval — noted as a security surface (user-controlled code path) but by-design per `_gates.py` precedent; not filed as a separate finding

*busy_timeout is not set on either connection factory (`brain.py._open_shared_conn`, `_impl.py.get_db`). Under concurrent write load this will surface as `sqlite3.OperationalError: database is locked`. Not filed as a finding since it's a known SQLite deployment pattern, but worth noting for high-concurrency scenarios.*
