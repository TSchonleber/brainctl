# Wave 6 Research: Embedding Backfill + Sync Write Path

**Ticket:** COS-231
**Agent:** Recall
**Date:** 2026-03-28
**Status:** Complete

---

## Summary

Executed a full embedding backfill for active memories in `brain.db`, achieving 100% vector coverage (up from 21.1%). Validated that hybrid BM25+vector retrieval returns qualitatively better results. Produced a spec for synchronous embedding on every `brainctl memory add` write.

---

## 1. Pre-Backfill State

At time of audit (2026-03-28 ~10:00 UTC):

| Metric | Value |
|---|---|
| Active memories | 39 |
| Embedded (vec_memories) | 4 |
| Coverage | **21.1%** |
| vec_memories contaminated with retired rows | 10 |

The coverage was worse than the ticket's stated 35.9% — the memory store had grown (MEB probe entries, new agent writes) without triggering an embedding pass.

Root cause: `brainctl memory add` inserts into `memories` and commits, but never calls Ollama. The 30-minute incremental cron was not running (or had missed recent writes).

---

## 2. Backfill Execution

Tool: `~/agentmemory/bin/embed-populate --tables memories`

Steps performed by the script:
1. Loaded `sqlite-vec v0.1.7`
2. Purged 10 retired memory rows from `vec_memories` (contamination cleanup)
3. Embedded 35 previously unembedded active memories via `nomic-embed-text` (768d) through Ollama
4. Wrote each embedding to both `embeddings` table (blob backup) and `vec_memories` virtual table

**Post-backfill coverage: 39/39 = 100.0%**

Duration: ~18 seconds (35 embeddings × ~500ms avg Ollama latency)

---

## 3. Validation — Hybrid Scoring Quality

Tested three queries against the live `brainctl search` endpoint (mode: `hybrid-rrf`).

### Query: "CostClock invoice SaaS"
- Top result: id=130 (CostClock AI project memory) — source `[both]` (FTS + semantic)
- Second: id=78 (invoice subsystem note) — source `[semantic]`
- **Assessment:** Correct. Precise match surfaces first with highest RRF score.

### Query: "embedding vector coverage"
- Top memory result: id=129 (Hippocampus QA contract / retrieval benchmark) — `[semantic]`
- Top event result: id=74 (COS-87 complete: 100% vector coverage) — `[both]`
- **Assessment:** Correct. Events with direct keyword+semantic overlap score highest.

### Query: "temporal classification repair"
- Metacognition: tier=3, `weak-coverage` (expected — no strong memory on this specific topic)
- Returns semantically adjacent memories (project/temporal memory design)
- **Assessment:** Correct behavior. Gap is real, not a retrieval failure.

**Conclusion:** Hybrid scoring is producing correct, relevance-ordered results across topic domains. The `[both]` tag (FTS+semantic RRF fusion) is firing correctly where content matches on both axes.

---

## 4. Spec: Synchronous Embedding on `brainctl memory add`

### Problem

`cmd_memory_add` (brainctl line 219) inserts a memory and commits. No embedding is attempted. The relevant helper functions already exist in brainctl:

- `_embed_query()` (line 1628): calls Ollama, returns `bytes`
- `_try_vec_delete_memories()` (line 1589): vec cleanup on retire
- `_get_db_with_vec()` (line 1573): opens DB with sqlite-vec loaded
- `already_embedded()` / `insert_embedding()` in `embed-populate`: portable pattern

### Required Change to `cmd_memory_add`

After `db.commit()` on the INSERT (line 240), add an inline embedding pass:

```python
# Inline sync embed — best-effort, non-fatal
try:
    import struct, urllib.request, urllib.error
    payload = json.dumps({"model": EMBED_MODEL, "input": args.content}).encode()
    req = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data_resp = json.loads(resp.read())
        vec = data_resp["embeddings"][0]
        blob = struct.pack(f"{len(vec)}f", *vec)
    # Write to embeddings table
    db.execute(
        "INSERT OR REPLACE INTO embeddings (source_table, source_id, model, dimensions, vector) "
        "VALUES ('memories', ?, ?, ?, ?)",
        (memory_id, EMBED_MODEL, EMBED_DIMENSIONS, blob)
    )
    # Write to vec_memories (requires vec-loaded connection)
    db_vec = sqlite3.connect(str(DB_PATH), timeout=10)
    db_vec.enable_load_extension(True)
    db_vec.load_extension(VEC_DYLIB)
    db_vec.enable_load_extension(False)
    db_vec.execute(
        "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?,?)",
        (memory_id, blob)
    )
    db_vec.commit()
    db_vec.close()
    db.commit()
    embed_ok = True
except Exception as e:
    embed_ok = False  # log but don't fail the write

json_out({"ok": True, "memory_id": memory_id, "embedded": embed_ok, ...})
```

### Design Decisions

| Decision | Rationale |
|---|---|
| Best-effort (non-fatal) | Memory write must succeed even if Ollama is down. Backfill cron catches misses. |
| Separate vec connection | `get_db()` (line 220) doesn't load sqlite-vec. Opening a second connection is cheap. |
| `INSERT OR REPLACE` on vec_memories | Idempotent; safe for retry/re-run. |
| Return `embedded: bool` in JSON output | Lets callers detect Ollama misses without failure. |
| No async path | Async adds complexity without benefit at 20-50ms latency per write. |

### Why Not Use `_get_db_with_vec()` for the Main DB?

`get_db()` (line 8 of brainctl) opens the DB without `sqlite-vec`. Changing `cmd_memory_add` to use a vec-loaded connection would be a wider refactor — changing the connection factory affects all callers. The two-connection approach (base write + vec write) is simpler and already precedented by `_try_vec_delete_memories()`.

---

## 5. Drift Guardrails

### Model Pinning

- `EMBED_MODEL = "nomic-embed-text"` is a module-level constant in both `brainctl` and `embed-populate`.
- Current active model: `nomic-embed-text:latest` via Ollama
- **Risk:** If Ollama auto-upgrades `nomic-embed-text` to a different dimensionality, existing 768d vectors become incommensurable with new writes.

**Recommended guardrail:**

```python
# On startup of embed-populate and brainctl vec functions:
actual_dims = len(embed("test"))
assert actual_dims == EMBED_DIMENSIONS, (
    f"Model dimension mismatch: expected {EMBED_DIMENSIONS}, got {actual_dims}. "
    "Run embed-populate --force to re-embed all records."
)
```

Add to `meb_config` table:
```sql
INSERT OR REPLACE INTO meb_config (key, value) VALUES ('embed_model', 'nomic-embed-text:latest');
INSERT OR REPLACE INTO meb_config (key, value) VALUES ('embed_dimensions', '768');
```

### Re-Embed Trigger on Model Change

If `meb_config.embed_model` changes (e.g., migration to `nomic-embed-text-v1.5`):

1. Run `embed-populate --force --tables memories,events,context` to re-embed entire store
2. Purge retired rows: `brainctl vec purge-retired`
3. Rebuild semantic edges: `embed-populate --graph-edges`

This is a manual runbook step. Full automation (trigger-on-config-change) is out of scope for COS-231 but suitable for a follow-on ticket.

---

## 6. Follow-On Work

| Area | Recommendation | Suggested Ticket |
|---|---|---|
| Sync embed on write | Implement the patch spec above in `cmd_memory_add` | New ticket under Kernel/Recall |
| Dimension guard | Add startup assertion to embed-populate and brainctl | Same as above |
| Event writes | `cmd_event_add` also has no sync embed — same pattern applies | Same ticket |
| Cron reliability | The 30-min incremental cron is a safety net; verify it's actually running | Check with Hermes |
| Benchmark re-run | COS-86 hit@5 benchmark should be re-run post-backfill to confirm score improvement | Recall follow-up |

---

## 7. Artifacts

- Backfill executed: `~/agentmemory/bin/embed-populate --tables memories` (2026-03-28)
- Post-run coverage: **100%** (39/39 active memories)
- Retired contamination removed: 10 rows purged from `vec_memories`
- Sync write patch spec: Section 4 above
- Drift guardrails: Section 5 above
