# CLI + Integrations + Merge + Federation — Audit 2026-04-19 (v2.4.6)

## Executive summary

Seven findings across the audited scope. Two are HIGH severity — both are correctness bugs in the doctor/status subsystem that undermine CI and monitoring. Four are MEDIUM or LOW, covering federation query safety, merge self-attach error handling, and a file-handle leak. One LOW-severity finding is an operational reliability issue in the quiet-hours cron scripts.

**No critical security issues found.** The wallet implementation is correct: `O_EXCL|O_CREAT 0600` atomic write, no private key bytes emitted to stdout, symlinks explicitly excluded by `code_ingest`. Integration secret handling (CrewAI, LangChain) uses parameterized queries throughout; no injection surfaces identified.

---

## Methodology

Reviewed the following files in full or in relevant depth:

- `src/agentmemory/cli.py` (thin wrapper — no findings)
- `src/agentmemory/_impl.py` — `cmd_doctor` (lines 8874–9121), `cmd_status` (lines 8647–8871), `cmd_backup` (lines 8550–8572), `cmd_merge` (lines 17846–17862), `cmd_update` (lines 17600–17737)
- `src/agentmemory/federation.py` (all 426 lines)
- `src/agentmemory/merge.py` (all 681 lines)
- `src/agentmemory/integrations/crewai.py` (all lines)
- `src/agentmemory/integrations/langchain.py` (all lines)
- `src/agentmemory/lib/mcp_helpers.py` (all lines)
- `src/agentmemory/lib/write_decision.py` (all lines)
- `src/agentmemory/commands/wallet.py` (all lines)
- `src/agentmemory/commands/ingest.py` (all lines)
- `src/agentmemory/code_ingest.py` (symlink/walk section)
- `src/agentmemory/mcp_tools_health.py` (backup section)
- `src/agentmemory/update.py` (all lines)
- `bin/brainctl`, `bin/quiet-hours-start.sh`, `bin/quiet-hours-end.sh`, `bin/consolidation-cycle.sh`

F-05 (merge source==target) was verified empirically: ATTACH on an already-open file succeeds, reads work, `BEGIN IMMEDIATE` raises `database is locked` — not silent corruption.

---

## Findings

### [HIGH] F-01: `cmd_doctor` always exits 0 and hardcodes `"ok": True` in JSON mode

**File(s):** `src/agentmemory/_impl.py:9101–9121`

**Claim:** `cmd_doctor` collects an `issues` list throughout its body (appended at lines 8921, 8937, 8975, 9000, etc.), prints them in human-readable mode, but then falls through to `sys.exit(0)` unconditionally — there is no `sys.exit(1)` when `len(issues) > 0`. In JSON mode (lines 9114–9121), the returned payload hardcodes `"ok": True` regardless of the `healthy` variable's actual value.

**Evidence:**
```python
# _impl.py ~9114
print(json.dumps({
    "ok": True,          # <-- hardcoded, ignores `healthy`
    "issues": issues,
    ...
}))
sys.exit(0)             # always 0
```

**Impact:** Any CI job, monitoring script, or shell pipeline that relies on `brainctl doctor` to gate on DB health will silently pass even when there are pending migrations, broken FTS indexes, or missing schema tables. `brainctl update` internally calls `run_doctor_json()` but reads `migrations.state` directly (not `ok`), so the update flow is not affected by this bug — that's the only caller confirmed safe.

**Recommended fix:**
```python
healthy = len(issues) == 0
if args.json:
    print(json.dumps({"ok": healthy, "issues": issues, ...}))
    sys.exit(0 if healthy else 1)
# human mode:
if issues:
    sys.exit(1)
```

---

### [MEDIUM] F-02: `cmd_status` exits 1 when optional dependencies (Ollama, sqlite-vec) are absent

**File(s):** `src/agentmemory/_impl.py:8805–8811`

**Claim:** `payload["ok"]` is set `False` and the command exits 1 for any of: `pending_migrations > 0`, `not services["ollama"]["reachable"]`, or `not services["sqlite_vec"]["installed"]`. Ollama and sqlite-vec are optional extras — their absence is expected in base installs.

**Evidence:**
```python
payload["ok"] = (
    pending_migrations == 0
    and services["ollama"]["reachable"]       # optional dep
    and services["sqlite_vec"]["installed"]   # optional dep
)
```

**Impact:** Users running `brainctl` without the `[vec]` or `[ollama]` optional extras get a non-zero exit code from `brainctl status` in every invocation, making it unsuitable as a health check in environments that don't need vector search. This is an inconsistency: `cmd_doctor` reports optional-dep issues as informational warnings and exits 0, while `cmd_status` treats them as hard failures.

**Recommended fix:** Gate exit-1 only on `pending_migrations > 0`. Demote optional service states to warnings or a separate `services_degraded` boolean:
```python
payload["ok"] = pending_migrations == 0
payload["services_degraded"] = not (
    services["ollama"]["reachable"] and services["sqlite_vec"]["installed"]
)
```

---

### [LOW] F-03: File handle leak in `cmd_backup` and `mcp_tools_health.py`

**File(s):**
- `src/agentmemory/_impl.py:8562`
- `src/agentmemory/mcp_tools_health.py:618`

**Claim:** Both sites pass `stdout=open(str(sql_path), "w")` directly to `subprocess.run()`. The `open()` call returns a file handle that is never explicitly closed; when `check=True` fires on a non-zero exit code, the handle is neither flushed nor closed before the exception propagates.

**Evidence:**
```python
# Both sites:
subprocess.run(
    ["sqlite3", str(DB_PATH), ".dump"],
    stdout=open(str(sql_path), "w"),   # leaked on exception
    check=True,
)
```

**Impact:** On modern CPython with reference counting this is usually harmless — the GC closes the fd on collection. Under PyPy or under heavy load with many backup calls the leaked handles could exhaust the process fd limit. The bigger risk is an incomplete backup file that looks complete (no exception raised) because the buffer was not flushed.

**Recommended fix:**
```python
with open(sql_path, "w") as fh:
    subprocess.run(["sqlite3", str(DB_PATH), ".dump"], stdout=fh, check=True)
```

---

### [MEDIUM] F-04: Federation LIKE fallback does not escape `%` and `_` wildcards in query

**File(s):**
- `src/agentmemory/federation.py:187–200` (`federated_memory_search` fallback)
- `src/agentmemory/federation.py:334–346` (`federated_search` memories fallback)
- `src/agentmemory/federation.py:372–386` (`federated_search` events fallback)
- `src/agentmemory/federation.py:394–408` (`federated_search` entities fallback)

**Claim:** All four LIKE fallback paths build the pattern as `f"%{query}%"` with no escaping. A query containing `%` or `_` expands as LIKE wildcards rather than being treated as literal characters.

**Evidence:**
```python
# federation.py ~190 (all four sites are structurally identical):
pattern = f"%{query}%"
rows = conn.execute(
    "SELECT ... FROM memories WHERE content LIKE ?", (pattern,)
).fetchall()
```

A query string like `"50%"` becomes `"%50%%"`, matching any content containing `50` followed by any suffix, rather than matching the literal string `50%`.

**Impact:** Unexpected over-broad matches when users search for literal percent or underscore characters. This is most relevant for code-related notes (e.g., searching for `coverage 80%` or `_private_` entity names). No injection risk (query is parameterized) but semantics are wrong.

**Recommended fix:**
```python
def _escape_like(s: str, escape_char: str = "\\") -> str:
    return s.replace(escape_char, escape_char * 2).replace("%", f"{escape_char}%").replace("_", f"{escape_char}_")

pattern = f"%{_escape_like(query)}%"
rows = conn.execute(
    "SELECT ... FROM memories WHERE content LIKE ? ESCAPE '\\'", (pattern,)
).fetchall()
```

---

### [MEDIUM] F-05: `merge()` allows source == target; errors with "database is locked" rather than a clear message (UNCERTAIN-resolved)

**File(s):** `src/agentmemory/merge.py:573–589`

**Claim:** `merge()` performs no guard against `source_path == target_path`. When the same path is passed for both arguments, SQLite's ATTACH succeeds (an already-open file can be attached as a second alias), reads across both aliases work, but the first write attempt (`BEGIN IMMEDIATE`) raises `sqlite3.OperationalError: database is locked`.

**Evidence (empirically verified):**
```
ATTACH succeeded (no error)
src rows: [(1, 'hello')]
main rows: [(1, 'hello')]
Error: database is locked
```

**Impact:** When `source == target` the operation fails with a confusing low-level error rather than a clear validation message. Data is not corrupted (the locked error prevents any write), but the user experience is poor and any caller that doesn't check `source != target` before calling will see an unhandled exception.

**Recommended fix:** Add a guard at the top of `merge()` (or `cmd_merge`):
```python
if Path(source_path).resolve() == Path(target_path).resolve():
    raise ValueError(f"source and target refer to the same file: {source_path}")
```

---

### [LOW] F-06: `total_results` in federation responses reports pre-slice count

**File(s):**
- `src/agentmemory/federation.py:215`
- `src/agentmemory/federation.py:424`

**Claim:** `total_results` is computed as `len(results)` before the `results[:limit]` slice is applied and returned. The returned list may be shorter than `total_results` reports.

**Evidence:**
```python
# Line 215:
total_results = len(results)       # e.g. 47
results = results[:limit]          # returns top 10
return {"total_results": 47, "results": results}  # misleading
```

**Impact:** Callers that rely on `total_results` to know whether there are more results beyond the limit (for pagination or "showing N of M" display) will read an inaccurate count. This could mask poor recall in retrieval evaluation.

**Recommended fix:** Compute `total_results` before slicing to reflect the true match count, and rename or clarify that it is the pre-limit count:
```python
total_count = len(results)
returned = results[:limit]
return {"total_results": total_count, "returned": len(returned), "results": returned}
```
Or alternatively, compute `total_results = len(results[:limit])` to report only what was returned — whichever contract is documented in MCP_SERVER.md.

---

### [LOW] F-07: quiet-hours shell scripts use relative `python3` paths, break silently in cron

**File(s):**
- `bin/quiet-hours-start.sh`
- `bin/quiet-hours-end.sh`

**Claim:** Both scripts use `exec python3 quiet-hours-start.py` / `exec python3 quiet-hours-end.py` without `cd`ing to the script directory first and without an absolute path to the Python script. Cron sets `$HOME` as the working directory, not the script's directory.

**Evidence (bin/quiet-hours-start.sh):**
```bash
exec python3 quiet-hours-start.py   # relative path; fails if CWD != bin/
```

**Impact:** When invoked via cron the script silently fails with `python3: can't open file 'quiet-hours-start.py': [Errno 2] No such file or directory`. The quiet-hours feature becomes a no-op without any log or alert unless cron mail is enabled.

**Recommended fix:**
```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/quiet-hours-start.py"
```

---

## Out of scope / confirmed clean

- **`commands/wallet.py` — secret handling**: `_atomic_write_keystore` uses `O_CREAT|O_WRONLY|O_EXCL, 0o600` (perms set before write). `cmd_wallet_show`, `cmd_wallet_export`, `cmd_wallet_address` output only the public address and metadata — no private key bytes emitted to stdout or stderr in any code path.
- **`commands/ingest.py` — path traversal**: `Path(args.path).expanduser().resolve()` followed by `is_dir()` guard. `code_ingest._walk_with_excludes` uses `os.walk(root, followlinks=False)` and explicitly skips symlinks. No path traversal vector.
- **`federation.py` — SQL injection**: agent_id filter uses parameterized `" AND m.agent_id = ?"` — safe.
- **`merge.py` — SQL injection**: ATTACH uses parameterized query; table names route through a `_DEFAULT_TABLES` dispatch whitelist — unrecognized table names are skipped, not interpolated.
- **`integrations/crewai.py` and `integrations/langchain.py`**: all SQL uses `?` parameters; no injection surfaces found.
- **`lib/write_decision.py`**: W(m) gate arithmetic is correct; `_cosine_similarity` is clamped to `[-1, 1]`.
- **`update.py`**: subprocess invocations use list form (no `shell=True`); no sensitive args interpolated into shell strings.
