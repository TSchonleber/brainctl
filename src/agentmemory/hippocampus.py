#!/usr/bin/env python3
"""Hippocampus maintenance commands for the shared memory spine."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agentmemory.paths import get_db_path

DB_PATH = get_db_path()

DECAY_RATES = {
    "long": 0.01,
    "medium": 0.03,
    "short": 0.07,
    "ephemeral": 0.2,
}

COMPRESS_PROMPT = (
    "You are an expert knowledge compressor. Given these N memories about [scope], "
    "reorganize them into the minimum number of dense, well-structured memories "
    "that capture all the important information. Each output memory should be a "
    "coherent paragraph, not a list of fragments. Output as JSON array of strings."
)


def get_db() -> sqlite3.Connection:
    global DB_PATH
    if os.environ.get("BRAIN_DB") or os.environ.get("BRAINCTL_HOME"):
        DB_PATH = get_db_path()
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def parse_ts(value: str) -> datetime:
    if value is None:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    return datetime.fromisoformat(normalized)


def days_since(now: datetime, timestamp: str) -> float:
    dt = parse_ts(timestamp)
    if dt is None:
        return 0.0
    seconds = (now - dt).total_seconds()
    return max(0.0, seconds / 86400.0)


def resolve_event_agent(db: sqlite3.Connection, requested_agent: str) -> str:
    row = db.execute("SELECT id FROM agents WHERE id = ?", (requested_agent,)).fetchone()
    if row:
        return requested_agent

    for fallback in ("hippocampus", "consolidator"):
        row = db.execute("SELECT id FROM agents WHERE id = ?", (fallback,)).fetchone()
        if row:
            return fallback

    row = db.execute("SELECT id FROM agents ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row["id"]

    raise RuntimeError("No agent rows found in agents table; cannot write warning events.")


def cmd_decay(args):
    db = get_db()
    now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")
    event_agent_id = resolve_event_agent(db, args.agent)

    rows = db.execute(
        """
        SELECT id, confidence, temporal_class, memory_type, created_at, last_recalled_at
        FROM memories
        WHERE retired_at IS NULL
        """
    ).fetchall()

    stats = {
        "scanned": len(rows),
        "skipped_permanent": 0,
        "updated": 0,
        "warnings_logged": 0,
        "retired": 0,
    }

    for row in rows:
        mem_id = row["id"]
        temporal_class = row["temporal_class"]
        confidence = float(row["confidence"])
        memory_type = row["memory_type"] or "episodic"

        if temporal_class == "permanent":
            stats["skipped_permanent"] += 1
            continue

        rate = DECAY_RATES.get(temporal_class)
        if rate is None:
            continue

        # Semantic memories decay 3x slower
        if memory_type == "semantic":
            rate = rate / SEMANTIC_DECAY_MULTIPLIER

        baseline_ts = row["last_recalled_at"] or row["created_at"]
        elapsed_days = days_since(now, baseline_ts)
        new_confidence = confidence * math.exp(-rate * elapsed_days)

        should_retire = new_confidence < 0.1
        should_warn = new_confidence < 0.3

        if not args.dry_run:
            db.execute(
                """
                UPDATE memories
                SET confidence = ?,
                    updated_at = ?,
                    retired_at = CASE WHEN ? THEN ? ELSE retired_at END
                WHERE id = ?
                """,
                (new_confidence, now_sql, 1 if should_retire else 0, now_sql, mem_id),
            )

            if should_warn:
                db.execute(
                    """
                    INSERT INTO events (agent_id, event_type, summary, detail, metadata, project, importance, created_at)
                    VALUES (?, 'warning', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_agent_id,
                        f"Memory #{mem_id} decaying — consider recalling or retiring",
                        f"Memory {mem_id} confidence dropped to {new_confidence:.4f} after {elapsed_days:.2f} days (temporal_class={temporal_class}).",
                        json.dumps(
                            {
                                "memory_id": mem_id,
                                "temporal_class": temporal_class,
                                "old_confidence": confidence,
                                "new_confidence": new_confidence,
                                "days_since_recall": elapsed_days,
                            }
                        ),
                        args.project,
                        0.5,
                        now_sql,
                    ),
                )

        stats["updated"] += 1
        if should_warn:
            stats["warnings_logged"] += 1
        if should_retire:
            stats["retired"] += 1

    if args.dry_run:
        db.rollback()
    else:
        db.commit()

    print(json.dumps(stats, indent=2))


def ensure_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, last_seen_at, updated_at)
        VALUES (?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        (agent_id, agent_id, "agent"),
    )



# parse_llm_json_array and run_llm_compression removed — brainctl is model-agnostic.
# The calling agent should handle LLM reasoning and feed results back via brainctl memory add.


def compress_scope_group(
    conn: sqlite3.Connection,
    *,
    scope: str,
    rows: list[sqlite3.Row],
    agent_id: str,
    dry_run: bool,
) -> dict:
    # LLM compression removed — brainctl is model-agnostic.
    # Uses pure string dedup via _fallback_compress.
    original_count = len(rows)
    max_output = math.ceil(original_count / 3)
    group = [
        {
            "id": row["id"],
            "category": row["category"],
            "scope": row["scope"],
            "content": row["content"],
            "confidence": row["confidence"],
            "temporal_class": row["temporal_class"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        }
        for row in rows
    ]

    compressed = _fallback_compress(group, max_output)

    source_ids = [row["id"] for row in rows]
    compressed_ids = []
    category = Counter(row["category"] for row in rows).most_common(1)[0][0]

    if not dry_run:
        placeholders = ",".join("?" for _ in source_ids)
        conn.execute(
            f"UPDATE memories SET retired_at = datetime('now'), updated_at = datetime('now') WHERE id IN ({placeholders})",
            source_ids,
        )

        tags = json.dumps(["compressed", f"from_{original_count}_originals"])
        for content in compressed:
            cur = conn.execute(
                """
                INSERT INTO memories (agent_id, category, scope, content, confidence, supersedes_id, tags, temporal_class)
                VALUES (?, ?, ?, ?, ?, NULL, ?, 'medium')
                """,
                (agent_id, category, scope, content, 0.95, tags),
            )
            compressed_ids.append(cur.lastrowid)

        project = scope.split(":", 1)[1] if scope.startswith("project:") and ":" in scope else None
        conn.execute(
            """
            INSERT INTO events (agent_id, event_type, summary, metadata, project, importance, created_at)
            VALUES (?, 'result', ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                f"Compressed {original_count} memories in scope {scope} into {len(compressed)} memories",
                json.dumps(
                    {
                        "scope": scope,
                        "original_count": original_count,
                        "compressed_count": len(compressed),
                        "original_memory_ids": source_ids,
                        "compressed_memory_ids": compressed_ids,
                    }
                ),
                project,
                0.9,
                datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )

    return {
        "scope": scope,
        "original_count": original_count,
        "compressed_count": len(compressed),
        "max_allowed": max_output,
        "original_memory_ids": source_ids,
        "compressed_memory_ids": compressed_ids,
        "dry_run": dry_run,
    }


def cmd_compress(args):
    # LLM compression removed — brainctl is model-agnostic.
    # Uses pure string dedup via _fallback_compress.
    db = get_db()
    ensure_agent(db, args.agent)

    sql = (
        "SELECT id, category, scope, content, confidence, tags, temporal_class "
        "FROM memories WHERE retired_at IS NULL AND temporal_class != 'permanent'"
    )
    params = []
    if args.scope:
        sql += " AND scope = ?"
        params.append(args.scope)
    sql += " ORDER BY scope, id"

    rows = db.execute(sql, params).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["scope"]].append(row)

    compressed_scopes = []
    skipped_scopes = []

    for scope, group in grouped.items():
        if len(group) < args.min_group_size:
            skipped_scopes.append(
                {"scope": scope, "reason": f"group_size<{args.min_group_size}", "count": len(group)}
            )
            continue

        compressed_scopes.append(
            compress_scope_group(
                db,
                scope=scope,
                rows=group,
                agent_id=args.agent,
                dry_run=args.dry_run,
            )
        )

    if args.dry_run:
        db.rollback()
    else:
        db.commit()

    print(
        json.dumps(
            {
                "ok": True,
                "agent": args.agent,
                "dry_run": args.dry_run,
                "compressed_scopes": compressed_scopes,
                "skipped_scopes": skipped_scopes,
            },
            indent=2,
        )
    )


CONSOLIDATION_PROMPT = (
    "You are consolidating agent memories. Given these {n} memories about the same topic, "
    "produce a single concise memory that captures all the important information. "
    "Drop redundancy. Keep specifics. Output ONLY the consolidated memory text, nothing else."
)

DEFAULT_CONSOLIDATE_AGENT = "hippocampus"


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


# ---------------------------------------------------------------------------
# Inline embedding helper
# Embeds a newly-written memory row immediately so consolidation and episodic
# promotion paths don't require a subsequent backfill run.
# Non-fatal: any failure (Ollama down, vec extension missing) is silently ignored.
# ---------------------------------------------------------------------------

def _find_vec_dylib():
    """Auto-discover the sqlite-vec loadable extension path."""
    try:
        import sqlite_vec
        return sqlite_vec.loadable_path()
    except (ImportError, AttributeError):
        pass
    import glob
    for pattern in ['/opt/homebrew/lib/python*/site-packages/sqlite_vec/vec0.*',
                    '/usr/lib/python*/site-packages/sqlite_vec/vec0.*']:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None

_VEC_DYLIB_HIPPO = _find_vec_dylib()
_OLLAMA_EMBED_URL_HIPPO = "http://localhost:11434/api/embed"
_EMBED_MODEL_HIPPO = "nomic-embed-text:latest"
_EMBED_DIM_HIPPO = 768
_MAX_EMBED_CHARS_HIPPO = 6000


def _try_embed_new_memory(memory_id: int, content: str) -> bool:
    """Attempt to embed a newly created memory row inline.

    Returns True on success, False on any failure (Ollama unavailable, etc.).
    Never raises — designed to be called in consolidation/promotion paths.
    """
    try:
        import urllib.request, urllib.error
        text = (content or "")[:_MAX_EMBED_CHARS_HIPPO]
        if not text.strip():
            return False
        payload = json.dumps({"model": _EMBED_MODEL_HIPPO, "input": text}).encode()
        req = urllib.request.Request(
            _OLLAMA_EMBED_URL_HIPPO,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
        if len(vec) != _EMBED_DIM_HIPPO:
            return False
        blob = struct.pack(f"{len(vec)}f", *vec)

        # Open a separate connection with vec extension for the vec_memories insert
        vec_conn = sqlite3.connect(str(DB_PATH), timeout=10)
        vec_conn.row_factory = sqlite3.Row
        vec_conn.execute("PRAGMA journal_mode = WAL")
        try:
            vec_conn.enable_load_extension(True)
            vec_conn.load_extension(_VEC_DYLIB_HIPPO)
            vec_conn.enable_load_extension(False)
        except Exception:
            vec_conn.close()
            return False
        vec_conn.execute(
            "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?,?)",
            (memory_id, blob),
        )
        vec_conn.execute(
            "INSERT OR IGNORE INTO embeddings (source_table, source_id, model, dimensions, vector) VALUES (?,?,?,?,?)",
            ("memories", memory_id, _EMBED_MODEL_HIPPO, _EMBED_DIM_HIPPO, blob),
        )
        vec_conn.commit()
        vec_conn.close()
        return True
    except Exception:
        return False


def fts5_or_query(content: str, max_terms: int = 8) -> str:
    """Extract meaningful terms and build an FTS5 OR query."""
    terms = []
    for word in content.split():
        clean = "".join(c for c in word if c.isalnum())
        if len(clean) >= 4:
            terms.append(clean)
        if len(terms) >= max_terms:
            break
    return " OR ".join(terms) if terms else ""


def find_fts5_similar(
    conn: sqlite3.Connection,
    memory_id: int,
    content: str,
    category: str,
    scope: Optional[str] = None,
) -> list[int]:
    """Return IDs of active memories that share key terms with the given memory.

    When scope is provided, restricts results to the same (category, scope).
    When scope is None, searches across all scopes within the same category
    (used for cross-scope contradiction scanning).
    """
    query = fts5_or_query(content)
    if not query:
        return []
    try:
        if scope is not None:
            rows = conn.execute(
                """
                SELECT m.id
                FROM memories m
                JOIN memories_fts ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                  AND m.retired_at IS NULL
                  AND m.category = ?
                  AND m.scope = ?
                  AND m.id != ?
                ORDER BY rank
                LIMIT 50
                """,
                (query, category, scope, memory_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.id
                FROM memories m
                JOIN memories_fts ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                  AND m.retired_at IS NULL
                  AND m.category = ?
                  AND m.id != ?
                ORDER BY rank
                LIMIT 50
                """,
                (query, category, memory_id),
            ).fetchall()
        return [r["id"] for r in rows]
    except sqlite3.OperationalError:
        return []


def build_similarity_clusters(
    conn: sqlite3.Connection,
    memories: list[dict],
    min_size: int,
) -> list[list[dict]]:
    """Union-find clustering via FTS5 pairwise similarity."""
    id_to_mem = {m["id"]: m for m in memories}
    ids = list(id_to_mem)

    # Build adjacency graph
    adjacency: dict[int, set[int]] = defaultdict(set)
    for mem in memories:
        matches = find_fts5_similar(conn, mem["id"], mem["content"], mem["category"], mem["scope"])
        valid = [m_id for m_id in matches if m_id in id_to_mem]
        for m_id in valid:
            adjacency[mem["id"]].add(m_id)
            adjacency[m_id].add(mem["id"])

    # Union-Find
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for node, neighbors in adjacency.items():
        for neighbor in neighbors:
            union(node, neighbor)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)

    return [
        [id_to_mem[i] for i in group]
        for group in groups.values()
        if len(group) >= min_size
    ]


def call_llm_consolidate(memories_text: str, n: int):
    # LLM consolidation removed — brainctl is model-agnostic.
    # The calling agent should handle LLM reasoning and feed results back via brainctl memory add.
    return None


def consolidate_cluster(
    conn: sqlite3.Connection,
    cluster: list[dict],
    agent_id: str,
    dry_run: bool,
) -> bool:
    ids = [m["id"] for m in cluster]
    contents = [m["content"] for m in cluster]
    category = cluster[0]["category"]
    scope = cluster[0]["scope"]
    max_confidence = max(m["confidence"] for m in cluster)

    # Merge tags from originals
    tags_union: set[str] = set()
    for m in cluster:
        if m["tags"]:
            try:
                tags_union.update(json.loads(m["tags"]))
            except (json.JSONDecodeError, TypeError):
                pass

    print(f"\n  Cluster ({len(cluster)}) [{category}/{scope}]:")
    for i, m in enumerate(cluster, 1):
        snippet = m["content"][:90] + ("..." if len(m["content"]) > 90 else "")
        print(f"    [{i}] id={m['id']}: {snippet}")

    memories_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(contents))

    if dry_run:
        print("  [DRY RUN] Would consolidate (skipping LLM call).")
        return True

    print("  Calling LLM...")
    consolidated = call_llm_consolidate(memories_text, len(cluster))
    if not consolidated:
        print("  FAILED to get LLM response — skipping cluster.", file=sys.stderr)
        return False

    print(f"  Result: {consolidated[:120]}{'...' if len(consolidated) > 120 else ''}")

    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    supersedes_tags = [f"supersedes:{i}" for i in ids]
    all_tags = json.dumps(sorted(tags_union | set(supersedes_tags)))

    cur = conn.execute(
        """
        INSERT INTO memories
            (agent_id, category, scope, content, confidence, supersedes_id, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (agent_id, category, scope, consolidated, max_confidence, ids[0], all_tags, now_sql, now_sql),
    )
    new_id = cur.lastrowid

    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE memories SET retired_at = ?, updated_at = ? WHERE id IN ({placeholders})",
        [now_sql, now_sql] + ids,
    )

    conn.execute(
        """
        INSERT INTO events (agent_id, event_type, summary, detail, metadata, importance, created_at)
        VALUES (?, 'memory_promoted', ?, ?, ?, 0.7, ?)
        """,
        (
            agent_id,
            f"Consolidated {len(cluster)} memories into id={new_id} [{category}/{scope}]",
            f"Superseded ids: {ids}\nConsolidated: {consolidated}",
            json.dumps({"consolidated_id": new_id, "superseded_ids": ids, "category": category, "scope": scope}),
            now_sql,
        ),
    )

    conn.commit()
    # Embed the new merged memory inline
    _try_embed_new_memory(new_id, consolidated)
    print(f"  Done: new memory id={new_id}, retired ids={ids}")
    return True


def cmd_consolidate(args):
    conn = get_db()
    ensure_agent(conn, args.agent)

    # Build SELECT — skip temporal_class='permanent' only if column exists
    has_tc = has_column(conn, "memories", "temporal_class")
    tc_filter = " AND (temporal_class IS NULL OR temporal_class != 'permanent')" if has_tc else ""

    scope_filter = ""
    scope_params: list = []
    if args.scope:
        scope_filter = " AND scope = ?"
        scope_params.append(args.scope)

    rows = conn.execute(
        f"SELECT id, agent_id, category, scope, content, confidence, tags "
        f"FROM memories WHERE retired_at IS NULL{tc_filter}{scope_filter} ORDER BY category, scope, id",
        scope_params,
    ).fetchall()

    memories = [dict(r) for r in rows]
    print(f"Loaded {len(memories)} active memories.")

    # Group by (category, scope)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for m in memories:
        groups[(m["category"], m["scope"])].append(m)

    print(f"Groups: {len(groups)}")

    total_clusters = 0
    total_retired = 0

    for (category, scope), group_mems in groups.items():
        if len(group_mems) < args.min_cluster:
            continue

        print(f"\n[{category}/{scope}] — {len(group_mems)} memories")
        clusters = build_similarity_clusters(conn, group_mems, args.min_cluster)

        if not clusters:
            print(f"  No clusters of size >= {args.min_cluster} found.")
            continue

        print(f"  Found {len(clusters)} cluster(s).")
        for cluster in clusters:
            ok = consolidate_cluster(conn, cluster, args.agent, args.dry_run)
            if ok and not args.dry_run:
                total_clusters += 1
                total_retired += len(cluster)

    if args.dry_run:
        print("\n[DRY RUN] No changes written.")
    else:
        print(f"\nDone. {total_clusters} cluster(s) consolidated, {total_retired} memories retired.")


# =============================================================================
# Importable API functions — used by test suite and programmatic callers
# =============================================================================

HALF_LIFE_DAYS = {
    "ephemeral": 3.5,
    "short": 10.0,
    "medium": 23.1,
    "long": 69.3,
}

# Semantic memories decay 3x slower than episodic (Tulving 1972 distinction)
SEMANTIC_DECAY_MULTIPLIER = 3.0

# Recall boost parameters (reconsolidation model)
# alpha = BASE_ALPHA / (1 + ALPHA_DAMPING * recalled_count)
# First recall: alpha ~= 0.3, tenth recall: alpha ~= 0.075
BASE_ALPHA = 0.3
ALPHA_DAMPING = 0.3

# Temporal class demotion thresholds: class -> (next_class, confidence_threshold)
# When confidence drops below threshold, memory is demoted to the next class.
# 'permanent' is never demoted; 'ephemeral' has no further demotion.
DEMOTION_THRESHOLDS: Dict[str, tuple] = {
    "long": ("medium", 0.5),
    "medium": ("short", 0.3),
    "short": ("ephemeral", 0.2),
}

# Lambda decay rates per temporal class (used in classification pass)
# These are single-step decay multipliers applied when reclassifying a memory.
TEMPORAL_LAMBDA: Dict[str, float] = {
    "ephemeral": 0.5,
    "short": 0.2,
    "medium": 0.05,
    "long": 0.01,
    "permanent": 0.0,
}

# Temporal classification rules
# Order matters — first matching rule wins.
#   recalled_count > 20 AND confidence > 0.8  → permanent
#   age < 1 day                                → ephemeral
#   age 1–7 days, recalled_count < 2           → short
#   age 7–30 days                              → medium
#   age > 30 days, recalled_count > 5          → long
TEMPORAL_CLASS_ORDER_LIST = ["ephemeral", "short", "medium", "long", "permanent"]

# ── Continual Learning Protections ────────────────────────────────────────────
# Memories meeting these criteria are importance-locked (protected=1).
# Protected memories are skipped in demotion, retirement, compression, and merge.
PROTECT_RECALL_MIN = 10       # recalled_count threshold for auto-protection
PROTECT_CONFIDENCE_MIN = 0.8  # confidence threshold for auto-protection


def _mark_importance_locks(conn: sqlite3.Connection) -> int:
    """Set protected=1 on memories that meet the importance-locking criteria.

    Criteria (EWC-inspired): recalled_count >= PROTECT_RECALL_MIN AND
    confidence >= PROTECT_CONFIDENCE_MIN.

    Returns the number of newly locked memories.
    """
    if not has_column(conn, "memories", "protected"):
        return 0
    cur = conn.execute(
        """
        UPDATE memories
        SET protected = 1, updated_at = datetime('now')
        WHERE retired_at IS NULL
          AND protected = 0
          AND recalled_count >= ?
          AND confidence >= ?
        """,
        (PROTECT_RECALL_MIN, PROTECT_CONFIDENCE_MIN),
    )
    conn.commit()
    return cur.rowcount


def compute_ewc_importance(conn: sqlite3.Connection, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Compute and persist ewc_importance for all active memories.

    Formula (EWC-inspired):
      ewc_importance = 0.4 * norm_recalled + 0.4 * trust_score + 0.2 * norm_age
        norm_recalled = min(1.0, recalled_count / 100.0)  — proxy for Fisher information
        trust_score   = memories.trust_score (0–1, default 1.0)
        norm_age      = min(1.0, age_days / 365.0)  — longevity through many consolidation passes

    Memories with ewc_importance > 0.7 receive heightened protection in consolidation:
    - consolidate/compress passes exclude them (like protected=1)
    - resolve_contradictions requires similarity > 0.9 before retiring them

    Returns: {"updated": N}
    """
    if not has_column(conn, "memories", "ewc_importance"):
        return {"updated": 0}

    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    rows = conn.execute(
        "SELECT id, recalled_count, trust_score, created_at FROM memories WHERE retired_at IS NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        recalled_count = int(row["recalled_count"] or 0)
        trust_score = float(row["trust_score"] if row["trust_score"] is not None else 1.0)
        age_days = days_since(now, row["created_at"])

        norm_recalled = min(1.0, recalled_count / 100.0)
        norm_age = min(1.0, age_days / 365.0)
        ewc = round(0.4 * norm_recalled + 0.4 * trust_score + 0.2 * norm_age, 4)

        conn.execute(
            "UPDATE memories SET ewc_importance = ?, updated_at = ? WHERE id = ?",
            (ewc, now_sql, row["id"]),
        )
        updated += 1

    conn.commit()
    return {"updated": updated}


def _has_active_dependents(conn: sqlite3.Connection, memory_id: int) -> bool:
    """Return True if any active (non-retired) memory derives from memory_id.

    Uses derived_from_ids JSON column added in migration 009.
    Provenance chain protection: a memory cannot be retired while other
    active memories are derived from it.
    """
    if not has_column(conn, "memories", "derived_from_ids"):
        return False
    rows = conn.execute(
        "SELECT id, derived_from_ids FROM memories WHERE retired_at IS NULL AND derived_from_ids IS NOT NULL"
    ).fetchall()
    target = str(memory_id)
    for row in rows:
        try:
            ids = json.loads(row["derived_from_ids"])
            if target in [str(i) for i in ids]:
                return True
        except (json.JSONDecodeError, TypeError):
            continue
    return False

# Temporal class ordering for comparison
TEMPORAL_CLASS_ORDER = ["ephemeral", "short", "medium", "long", "permanent"]


def _decay_rate_from_half_life(half_life_days: float) -> float:
    """Return the exponential decay rate for a given half-life in days."""
    return math.log(2) / half_life_days


def apply_decay(conn: sqlite3.Connection, now: Optional[datetime] = None) -> Dict[str, int]:
    """Apply exponential confidence decay to all non-permanent active memories.

    Uses half-life based decay:
      ephemeral  ~3.5 days half-life
      short      ~10 days
      medium     ~23 days
      long       ~69 days
    Memories whose confidence drops below 0.1 are retired.
    """
    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    _protected_col = has_column(conn, "memories", "protected")
    _protected_select = ", protected" if _protected_col else ""
    _has_ab = has_column(conn, "memories", "alpha")  # Bayesian alpha/beta columns
    _ab_select = ", alpha, beta" if _has_ab else ""
    _has_labile = has_column(conn, "memories", "labile_until")
    _labile_select = ", labile_until" if _has_labile else ""

    rows = conn.execute(
        f"""
        SELECT id, confidence, temporal_class, memory_type, created_at, last_recalled_at{_protected_select}{_ab_select}{_labile_select}
        FROM memories
        WHERE retired_at IS NULL
        """
    ).fetchall()

    stats = {"scanned": len(rows), "skipped_permanent": 0, "skipped_protected": 0,
             "skipped_labile": 0, "skipped_has_dependents": 0, "updated": 0, "retired": 0}

    for row in rows:
        mem_id = row["id"]
        temporal_class = row["temporal_class"]
        confidence = float(row["confidence"])
        memory_type = row["memory_type"] or "episodic"
        is_protected = bool(_protected_col and row["protected"])

        if temporal_class == "permanent":
            stats["skipped_permanent"] += 1
            continue

        # Behavioral tagging: memories in their labile window are immune to decay
        if _has_labile:
            labile_until = row["labile_until"]
            if labile_until and labile_until > now_sql:
                stats["skipped_labile"] += 1
                continue

        half_life = HALF_LIFE_DAYS.get(temporal_class)
        if half_life is None:
            continue

        # Semantic memories decay slower — multiply half-life by protection factor
        if memory_type == "semantic":
            half_life = half_life * SEMANTIC_DECAY_MULTIPLIER

        rate = _decay_rate_from_half_life(half_life)

        baseline_ts = row["last_recalled_at"] or row["created_at"]
        elapsed_days = days_since(now, baseline_ts)
        new_confidence = confidence * math.exp(-rate * elapsed_days)

        should_retire = new_confidence < 0.1

        # Continual learning: block retirement for importance-locked memories
        if should_retire and is_protected:
            stats["skipped_protected"] += 1
            should_retire = False

        # Provenance chain protection: cannot retire a memory that others derive from
        if should_retire and _has_active_dependents(conn, mem_id):
            stats["skipped_has_dependents"] += 1
            should_retire = False

        if _has_ab:
            # Bayesian decay: increment beta proportional to elapsed time
            # beta_increment = rate * elapsed_days (same rate as confidence decay)
            cur_alpha = float(row["alpha"] or 1.0)
            cur_beta  = float(row["beta"]  or 1.0)
            beta_inc  = rate * elapsed_days
            new_beta  = cur_beta + beta_inc
            # Recompute confidence from updated alpha/beta (override scalar decay)
            new_confidence = cur_alpha / (cur_alpha + new_beta)
            should_retire = new_confidence < 0.1
            if should_retire and is_protected:
                stats["skipped_protected"] += 1
                should_retire = False
            if should_retire and _has_active_dependents(conn, mem_id):
                stats["skipped_has_dependents"] += 1
                should_retire = False
            conn.execute(
                """
                UPDATE memories
                SET confidence = ?, alpha = ?, beta = ?,
                    updated_at = ?,
                    retired_at = CASE WHEN ? THEN ? ELSE retired_at END
                WHERE id = ?
                """,
                (new_confidence, cur_alpha, new_beta, now_sql,
                 1 if should_retire else 0, now_sql, mem_id),
            )
        else:
            conn.execute(
                """
                UPDATE memories
                SET confidence = ?,
                    updated_at = ?,
                    retired_at = CASE WHEN ? THEN ? ELSE retired_at END
                WHERE id = ?
                """,
                (new_confidence, now_sql, 1 if should_retire else 0, now_sql, mem_id),
            )

        stats["updated"] += 1
        if should_retire:
            stats["retired"] += 1

    conn.commit()
    return stats


def apply_recall_boost(
    conn: sqlite3.Connection,
    memory_id: int,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Boost confidence of a recalled memory using the reconsolidation formula.

    confidence += alpha * (1 - confidence)
    alpha = BASE_ALPHA / (1 + ALPHA_DAMPING * recalled_count)

    Diminishing returns: first recall gives the largest boost; subsequent recalls
    give progressively smaller boosts. Confidence is capped at 1.0.
    Permanent memories are boosted too (they just never decay).

    Returns a dict with the update details, or None if the memory is not found.
    """
    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    row = conn.execute(
        "SELECT id, confidence, recalled_count, temporal_class FROM memories WHERE id = ? AND retired_at IS NULL",
        (memory_id,),
    ).fetchone()

    if row is None:
        return None

    confidence = float(row["confidence"])
    recalled_count = int(row["recalled_count"])
    alpha = BASE_ALPHA / (1.0 + ALPHA_DAMPING * recalled_count)
    new_confidence = min(1.0, confidence + alpha * (1.0 - confidence))

    conn.execute(
        """
        UPDATE memories
        SET confidence = ?, recalled_count = recalled_count + 1, last_recalled_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_confidence, now_sql, now_sql, memory_id),
    )
    conn.commit()

    return {
        "memory_id": memory_id,
        "temporal_class": row["temporal_class"],
        "old_confidence": round(confidence, 6),
        "new_confidence": round(new_confidence, 6),
        "alpha": round(alpha, 6),
        "recalled_count": recalled_count + 1,
    }


def apply_temporal_demotion(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Demote temporal_class when confidence drops below class-specific thresholds.

    Demotion chain (one level per call): long -> medium -> short -> ephemeral
    'permanent' is never demoted; 'ephemeral' has no lower class to fall to.

    Logs a 'warning' event for each demotion so the supervisor agent can track the state change.
    Returns stats dict with counts of demotions per transition.
    """
    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    _protected_col = has_column(conn, "memories", "protected")
    _protected_select = ", protected" if _protected_col else ""

    rows = conn.execute(
        f"""
        SELECT id, agent_id, confidence, temporal_class{_protected_select}
        FROM memories
        WHERE retired_at IS NULL AND temporal_class NOT IN ('permanent', 'ephemeral')
        """
    ).fetchall()

    stats: Dict[str, int] = {"scanned": len(rows), "demoted": 0, "skipped_protected": 0}
    for row in rows:
        threshold_entry = DEMOTION_THRESHOLDS.get(row["temporal_class"])
        if threshold_entry is None:
            continue
        next_class, threshold = threshold_entry
        if float(row["confidence"]) < threshold:
            # Continual learning: protected memories are immune to demotion
            if _protected_col and row["protected"]:
                stats["skipped_protected"] += 1
                continue
            conn.execute(
                "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                (next_class, now_sql, row["id"]),
            )
            conn.execute(
                """
                INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                VALUES (?, 'warning', ?, ?, 0.6, ?)
                """,
                (
                    row["agent_id"],
                    f"Memory #{row['id']} demoted {row['temporal_class']} -> {next_class} "
                    f"(confidence={row['confidence']:.4f})",
                    f"Memory {row['id']} confidence {row['confidence']:.4f} crossed threshold "
                    f"{threshold} for class '{row['temporal_class']}'; demoted to '{next_class}'.",
                    now_sql,
                ),
            )
            key = f"{row['temporal_class']}_to_{next_class}"
            stats[key] = stats.get(key, 0) + 1
            stats["demoted"] += 1

    conn.commit()
    return stats


# Access pattern thresholds
# Memories recalled >= PROMOTE_RECALL_COUNT times AND confidence >= PROMOTE_CONFIDENCE_MIN
# within the last PROMOTE_WINDOW_DAYS are eligible for temporal class promotion.
PROMOTE_RECALL_COUNT = 5
PROMOTE_CONFIDENCE_MIN = 0.7
PROMOTE_WINDOW_DAYS = 30
NEVER_RECALLED_DAYS = 14  # memories never recalled after this many days are flagged


# Temporal class promotion chain (opposite of demotion)
PROMOTION_CHAIN: Dict[str, str] = {
    "ephemeral": "short",
    "short": "medium",
    "medium": "long",
    # long -> permanent requires human intent; not auto-promoted
}


def analyze_access_patterns(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Analyze memory access patterns and apply temporal class promotions.

    Frequently recalled memories (>= PROMOTE_RECALL_COUNT recalls, high confidence,
    recalled within PROMOTE_WINDOW_DAYS) are promoted one level up the temporal chain.
    Memories never recalled after NEVER_RECALLED_DAYS are flagged as demotion candidates.

    Returns stats with promotion counts, never-recalled count, and per-memory details.
    """
    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    rows = conn.execute(
        """
        SELECT id, agent_id, confidence, temporal_class, recalled_count,
               last_recalled_at, created_at
        FROM memories
        WHERE retired_at IS NULL AND temporal_class != 'permanent'
        """
    ).fetchall()

    stats: Dict[str, Any] = {
        "scanned": len(rows),
        "promoted": 0,
        "never_recalled_flagged": 0,
        "promotions": [],
        "never_recalled_ids": [],
    }

    for row in rows:
        mem_id = row["id"]
        temporal_class = row["temporal_class"]
        confidence = float(row["confidence"])
        recalled_count = int(row["recalled_count"])
        last_recalled_at = row["last_recalled_at"]
        created_at = row["created_at"]

        # --- Promotion check ---
        next_class = PROMOTION_CHAIN.get(temporal_class)
        if next_class is not None and recalled_count >= PROMOTE_RECALL_COUNT and confidence >= PROMOTE_CONFIDENCE_MIN:
            # Only promote if recalled recently enough
            if last_recalled_at is not None:
                days_since_recall = days_since(now, last_recalled_at)
                if days_since_recall <= PROMOTE_WINDOW_DAYS:
                    conn.execute(
                        "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                        (next_class, now_sql, mem_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                        VALUES (?, 'observation', ?, ?, 0.6, ?)
                        """,
                        (
                            row["agent_id"],
                            f"Memory #{mem_id} promoted {temporal_class} -> {next_class} "
                            f"(recalled={recalled_count}, confidence={confidence:.3f})",
                            f"Memory {mem_id} promoted to '{next_class}' after {recalled_count} recalls "
                            f"with confidence {confidence:.4f} (last recalled {days_since_recall:.1f} days ago).",
                            now_sql,
                        ),
                    )
                    stats["promotions"].append({
                        "memory_id": mem_id,
                        "from": temporal_class,
                        "to": next_class,
                        "recalled_count": recalled_count,
                        "confidence": round(confidence, 4),
                    })
                    stats["promoted"] += 1
                    continue  # skip never-recalled check for this memory

        # --- Never-recalled flag ---
        age_days = days_since(now, created_at)
        if recalled_count == 0 and age_days >= NEVER_RECALLED_DAYS:
            stats["never_recalled_ids"].append(mem_id)
            stats["never_recalled_flagged"] += 1

    conn.commit()
    return stats


def temporal_classification_pass(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Reclassify memory temporal_class based on age, recall count, and confidence.

    Classification rules (first matching rule wins):
      recalled_count > 20 AND confidence > 0.8  → permanent
      age < 1 day                                → ephemeral (λ=0.5)
      age 1–7 days, recalled_count < 2           → short    (λ=0.2)
      age 7–30 days                              → medium   (λ=0.05)
      age > 30 days, recalled_count > 5          → long     (λ=0.01)
      (no rule matches)                          → no change

    After reclassifying, applies one lambda decay tick using the new class rate.
    Protected memories are blocked from demotion-direction reclassifications.

    Returns stats with counts of promotions, demotions, and per-change details.
    """
    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    _protected_col = has_column(conn, "memories", "protected")
    _protected_select = ", protected" if _protected_col else ""

    rows = conn.execute(
        f"""
        SELECT id, agent_id, confidence, temporal_class, recalled_count,
               last_recalled_at, created_at{_protected_select}
        FROM memories
        WHERE retired_at IS NULL
        """
    ).fetchall()

    def _class_index(c: str) -> int:
        try:
            return TEMPORAL_CLASS_ORDER_LIST.index(c)
        except ValueError:
            return 2  # treat unknown as medium

    stats: Dict[str, Any] = {
        "scanned": len(rows),
        "reclassified": 0,
        "skipped_protected": 0,
        "promotions": 0,
        "demotions": 0,
        "to_permanent": 0,
        "changes": [],
    }

    for row in rows:
        mem_id = row["id"]
        current_class = row["temporal_class"] or "medium"
        confidence = float(row["confidence"])
        recalled_count = int(row["recalled_count"])
        age_days = days_since(now, row["created_at"])

        # Determine target class (first matching rule wins)
        if recalled_count > 20 and confidence > 0.8:
            target_class = "permanent"
        elif age_days < 1.0:
            target_class = "ephemeral"
        elif age_days < 7.0 and recalled_count < 2:
            target_class = "short"
        elif age_days < 30.0:
            target_class = "medium"
        elif age_days >= 30.0 and recalled_count > 5:
            target_class = "long"
        else:
            target_class = current_class  # no rule matched — leave unchanged

        if target_class == current_class:
            continue

        # Protected memories cannot be demoted
        is_protected = _protected_col and row["protected"]
        if is_protected and _class_index(target_class) < _class_index(current_class):
            stats["skipped_protected"] += 1
            continue

        # Apply one lambda decay tick using the target class rate
        lam = TEMPORAL_LAMBDA.get(target_class, 0.05)
        new_confidence = max(0.01, round(confidence * (1.0 - lam), 6))

        conn.execute(
            "UPDATE memories SET temporal_class = ?, confidence = ?, updated_at = ? WHERE id = ?",
            (target_class, new_confidence, now_sql, mem_id),
        )

        direction = "promotion" if _class_index(target_class) > _class_index(current_class) else "demotion"
        if direction == "promotion":
            stats["promotions"] += 1
        else:
            stats["demotions"] += 1
        if target_class == "permanent":
            stats["to_permanent"] += 1

        conn.execute(
            """
            INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
            VALUES (?, 'observation', ?, ?, 0.5, ?)
            """,
            (
                row["agent_id"],
                f"Memory #{mem_id} reclassified {current_class} -> {target_class} "
                f"(age={age_days:.1f}d, recalled={recalled_count}, conf={confidence:.3f}->{new_confidence:.3f})",
                (
                    f"Temporal classification pass: memory {mem_id} {direction} from "
                    f"'{current_class}' to '{target_class}'. "
                    f"Age={age_days:.2f} days, recalled_count={recalled_count}, "
                    f"confidence {confidence:.4f} -> {new_confidence:.4f} (λ={lam})."
                ),
                now_sql,
            ),
        )
        stats["changes"].append({
            "memory_id": mem_id,
            "from": current_class,
            "to": target_class,
            "direction": direction,
            "age_days": round(age_days, 2),
            "recalled_count": recalled_count,
        })
        stats["reclassified"] += 1

    conn.commit()
    return stats


def run_hebbian_pass(
    db, now=None, dry_run=False
):
    """Hebbian edge strengthening pass.

    Scans memories last_recalled_at timestamps to find co-retrieval sessions
    (two+ memories recalled within a 60-second window). Strengthens
    co_referenced knowledge_edges for each co-retrieved pair.

    Rules:
    - weight += 0.1 per co-retrieval event (capped at 1.0)
    - Critical period: if either memory is < 7 days old, delta = 0.2 (2x)
    - Edges not reinforced in 30+ days decay by 0.05 per cycle
    - Edge weight reaching 0.0 is deleted
    """
    if now is None:
        now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    stats = {
        "edges_strengthened": 0,
        "edges_created": 0,
        "edges_decayed": 0,
        "sessions_scanned": 0,
        "pairs_found": 0,
    }

    # Schema migrations: add columns to knowledge_edges if missing (always run — safe DDL)
    if not has_column(db, "knowledge_edges", "last_reinforced_at"):
        db.execute("ALTER TABLE knowledge_edges ADD COLUMN last_reinforced_at TEXT")
    if not has_column(db, "knowledge_edges", "co_activation_count"):
        db.execute(
            "ALTER TABLE knowledge_edges ADD COLUMN co_activation_count INTEGER DEFAULT 0"
        )
    if not has_column(db, "knowledge_edges", "weight_updated_at"):
        db.execute("ALTER TABLE knowledge_edges ADD COLUMN weight_updated_at TEXT")

    # 1. Co-retrieval session detection via last_recalled_at
    # Collect memories recalled in the last 30 days, sorted by recall time
    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    recalled_rows = db.execute(
        """
        SELECT id, last_recalled_at, created_at
        FROM memories
        WHERE retired_at IS NULL
          AND last_recalled_at IS NOT NULL
          AND last_recalled_at >= ?
        ORDER BY last_recalled_at
        """,
        (cutoff,),
    ).fetchall()

    # Group into 60-second tumbling windows
    sessions = []
    if recalled_rows:
        current_session = [dict(recalled_rows[0])]
        window_start = parse_ts(recalled_rows[0]["last_recalled_at"])

        for row in recalled_rows[1:]:
            ts = parse_ts(row["last_recalled_at"])
            if (ts - window_start).total_seconds() <= 60:
                current_session.append(dict(row))
            else:
                if len(current_session) >= 2:
                    sessions.append(current_session)
                current_session = [dict(row)]
                window_start = ts

        if len(current_session) >= 2:
            sessions.append(current_session)

    stats["sessions_scanned"] = len(sessions)

    # 2. Strengthen edges for each co-retrieved pair
    for session in sessions:
        ids = [r["id"] for r in session]
        created_at_map = {r["id"]: r["created_at"] for r in session}

        pairs = [
            (ids[i], ids[j])
            for i in range(len(ids))
            for j in range(i + 1, len(ids))
        ]
        stats["pairs_found"] += len(pairs)

        for src_id, tgt_id in pairs:
            src_age = days_since(now, created_at_map.get(src_id, now_sql))
            tgt_age = days_since(now, created_at_map.get(tgt_id, now_sql))
            delta = 0.2 if (src_age < 7 or tgt_age < 7) else 0.1

            if dry_run:
                stats["edges_strengthened"] += 1
                continue

            existing = db.execute(
                """
                SELECT id, weight FROM knowledge_edges
                WHERE source_table = 'memories' AND source_id = ?
                  AND target_table = 'memories' AND target_id = ?
                  AND relation_type = 'co_referenced'
                """,
                (src_id, tgt_id),
            ).fetchone()

            if existing:
                new_weight = min(1.0, existing["weight"] + delta)
                db.execute(
                    """
                    UPDATE knowledge_edges
                    SET weight = ?, last_reinforced_at = ?, weight_updated_at = ?,
                        co_activation_count = COALESCE(co_activation_count, 0) + 1
                    WHERE id = ?
                    """,
                    (new_weight, now_sql, now_sql, existing["id"]),
                )
                stats["edges_strengthened"] += 1
            else:
                db.execute(
                    """
                    INSERT INTO knowledge_edges
                      (source_table, source_id, target_table, target_id,
                       relation_type, weight, agent_id, created_at,
                       last_reinforced_at, weight_updated_at, co_activation_count)
                    VALUES ('memories', ?, 'memories', ?, 'co_referenced', ?, 'hippocampus', ?, ?, ?, 1)
                    """,
                    (src_id, tgt_id, min(1.0, delta), now_sql, now_sql, now_sql),
                )
                stats["edges_created"] += 1

    # 3. Decay stale co_referenced edges (not reinforced in 30+ days)
    stale_edges = db.execute(
        """
        SELECT id, weight
        FROM knowledge_edges
        WHERE relation_type = 'co_referenced'
          AND COALESCE(last_reinforced_at, created_at) < ?
        """,
        (cutoff,),
    ).fetchall()

    for edge in stale_edges:
        new_weight = round(edge["weight"] - 0.05, 4)
        stats["edges_decayed"] += 1
        if dry_run:
            continue
        if new_weight <= 0.0:
            db.execute("DELETE FROM knowledge_edges WHERE id = ?", (edge["id"],))
        else:
            db.execute(
                "UPDATE knowledge_edges SET weight = ?, weight_updated_at = ? WHERE id = ?",
                (new_weight, now_sql, edge["id"]),
            )

    # 4. Neuroplasticity promotion: boost frequently co-activated edges that are still low-weight
    # Edges with co_activation_count > 10 and weight < 0.8 get weight *= 1.1 (capped at 1.0)
    promote_edges = db.execute(
        """
        SELECT id, weight, co_activation_count
        FROM knowledge_edges
        WHERE relation_type = 'co_referenced'
          AND COALESCE(co_activation_count, 0) > 10
          AND weight < 0.8
        """
    ).fetchall()

    stats["edges_promoted"] = 0
    for edge in promote_edges:
        new_weight = min(1.0, round(edge["weight"] * 1.1, 6))
        stats["edges_promoted"] += 1
        if dry_run:
            continue
        db.execute(
            "UPDATE knowledge_edges SET weight = ?, weight_updated_at = ? WHERE id = ?",
            (new_weight, now_sql, edge["id"]),
        )

    return stats

def _store_health(db: sqlite3.Connection) -> dict:
    """Return signal-to-noise ratio, at-risk memory count, and distillation ratio.

    Distillation ratio = active memories linked to source events / total events.
    Counts a memory as covering an event if:
      - it has source_event_id pointing directly to that event, OR
      - it supersedes (directly or transitively) a memory that had such a link.
    This prevents the metric from cratering after compression passes retire the
    original source-linked memories and replace them with merged ones.
    """
    row = db.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN confidence < 0.3 THEN 1 ELSE 0 END) as at_risk
        FROM memories WHERE retired_at IS NULL
    """).fetchone()
    total = row["total"] or 1
    at_risk = row["at_risk"] or 0

    # Distillation ratio: traverse supersedes chain so compressed/merged memories
    # that supersede source-linked originals still count as covering those events.
    total_events_row = db.execute("SELECT COUNT(*) AS cnt FROM events").fetchone()
    total_events = total_events_row["cnt"] or 0

    covered_events = 0
    if total_events > 0:
        # Direct links (active memories with source_event_id)
        direct_row = db.execute(
            "SELECT COUNT(DISTINCT source_event_id) AS cnt FROM memories "
            "WHERE retired_at IS NULL AND source_event_id IS NOT NULL"
        ).fetchone()
        direct_covered: set = set()
        for r in db.execute(
            "SELECT DISTINCT source_event_id FROM memories "
            "WHERE retired_at IS NULL AND source_event_id IS NOT NULL"
        ).fetchall():
            direct_covered.add(r["source_event_id"])

        # Chain-traversal: retired memories with source_event_id that are
        # superseded by an active memory (follow supersedes_id chain up).
        # Build a supersedes map: child_id -> parent_id (supersedes_id)
        supersedes_rows = db.execute(
            "SELECT id, supersedes_id, source_event_id, retired_at FROM memories "
            "WHERE supersedes_id IS NOT NULL"
        ).fetchall()
        # Map: memory_id -> source_event_id (for retired originals)
        retired_source: dict = {}
        # Map: supersedes_id -> [list of memory ids that supersede it]
        superseded_by: dict = defaultdict(list)
        for r in supersedes_rows:
            superseded_by[r["supersedes_id"]].append(r["id"])
            if r["retired_at"] is not None and r["source_event_id"] is not None:
                retired_source[r["id"]] = r["source_event_id"]

        # Active memory ids
        active_ids = set(
            r["id"] for r in db.execute(
                "SELECT id FROM memories WHERE retired_at IS NULL"
            ).fetchall()
        )

        # BFS: find all event ids reachable from active memories via supersedes chain
        chained_covered: set = set(direct_covered)
        # Walk: for each retired memory with a source_event_id, check if any
        # active memory supersedes it (directly or transitively).
        for retired_id, event_id in retired_source.items():
            if event_id in chained_covered:
                continue
            # BFS upward through superseded_by
            visited: set = set()
            frontier = [retired_id]
            found = False
            while frontier and not found:
                node = frontier.pop()
                if node in visited:
                    continue
                visited.add(node)
                if node in active_ids:
                    found = True
                    break
                for child_id in superseded_by.get(node, []):
                    frontier.append(child_id)
            if found:
                chained_covered.add(event_id)

        covered_events = len(chained_covered)

    distillation_ratio = round(covered_events / total_events, 4) if total_events > 0 else 0.0

    return {
        "total": total,
        "at_risk": at_risk,
        "signal_to_noise": round(1.0 - at_risk / total, 3),
        "distillation_ratio": distillation_ratio,
        "covered_events": covered_events,
        "total_events": total_events,
    }


def experience_replay(
    conn: sqlite3.Connection,
    top_k: int = 10,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Re-process the top-K highest-recalled active memories.

    Experience replay (Lin 1992 / Schaul 2015): interleaving old high-value
    memories during consolidation prevents catastrophic forgetting. For each
    replayed memory we apply a small recall boost (reconsolidation) and log a
    replay event so the access is visible to the brainctl search pipeline.

    Returns stats dict: {replayed, ids, skipped_permanent}.
    """
    if now is None:
        now = datetime.now()

    rows = conn.execute(
        """
        SELECT id, recalled_count, confidence, temporal_class
        FROM memories
        WHERE retired_at IS NULL
        ORDER BY recalled_count DESC, confidence DESC
        LIMIT ?
        """,
        (top_k,),
    ).fetchall()

    replayed_ids = []
    skipped = 0
    for row in rows:
        mem_id = row["id"]
        if row["temporal_class"] == "permanent":
            skipped += 1
            # permanent memories don't need boost; still count as replayed
        result = apply_recall_boost(conn, mem_id, now=now)
        if result:
            replayed_ids.append(mem_id)

    conn.commit()
    return {"replayed": len(replayed_ids), "ids": replayed_ids, "skipped_permanent": skipped}


def cmd_consolidation_cycle(args):
    """Full consolidation cycle: decay -> demotion -> contradictions -> merge -> compress -> event log.

    This is the main scheduled job that runs as the memory consolidation cycle.
    Runs all maintenance passes and logs a single summary event with
    event_type='consolidation_cycle'. Logs completion event to brain.db.
    """
    db = get_db()
    now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    # Pass 0 (EWC): compute importance scores before any destructive pass
    ewc_stats = compute_ewc_importance(db, now=now)

    # Pass 0.1 (CLF): mark high-importance memories as protected before any destructive pass
    newly_locked = _mark_importance_locks(db)

    # Pass 0.5: Temporal classification — assign correct temporal_class from age/recall rules
    # Must run before decay so decay uses accurate class half-lives.
    classification_stats = temporal_classification_pass(db, now=now)

    # Pass 1: Confidence decay
    decay_stats = apply_decay(db, now=now)

    # Pass 2: Temporal class demotion
    demotion_stats = apply_temporal_demotion(db, now=now)

    # Pass 3: Access pattern analysis + promotions
    access_stats = analyze_access_patterns(db, now=now)

    # Pass 4: Contradiction detection and resolution
    contradiction_stats = resolve_contradictions(db)

    # Pass 5: Cluster-merge duplicate / overlapping memories
    consolidation_stats = consolidate_memories(db)

    # Pass 6: Compress dense scopes (10+ memories → ceil(n/3))
    compression_stats = compress_memories(db)

    # Pass 7: Episodic-to-semantic promotion
    promotion_stats = promote_episodic_to_semantic(db)

    # Pass 8 (CLF): Experience replay — re-process top-10 highest-recalled memories
    # Prevents catastrophic forgetting by re-anchoring important old knowledge.
    replay_stats = experience_replay(db, top_k=10, now=now)

    # Pass 9: Hebbian edge strengthening — co-retrieval pattern learning
    hebbian_stats = run_hebbian_pass(db, now=now)

    # Pass 10: Dream pass — creative synthesis via cross-scope bisociation
    dream_agent = resolve_event_agent(db, args.agent)
    dream_stats = run_dream_pass(db, agent_id=dream_agent)

    db.commit()

    # Store health snapshot
    health = _store_health(db)

    summary = {
        "cycle_at": now_sql,
        "ewc_importance": ewc_stats,
        "importance_locks": {"newly_locked": newly_locked},
        "temporal_classification": {
            "scanned": classification_stats["scanned"],
            "reclassified": classification_stats["reclassified"],
            "promotions": classification_stats["promotions"],
            "demotions": classification_stats["demotions"],
            "to_permanent": classification_stats["to_permanent"],
            "skipped_protected": classification_stats["skipped_protected"],
        },
        "decay": decay_stats,
        "demotion": demotion_stats,
        "access_patterns": {
            "scanned": access_stats["scanned"],
            "promoted": access_stats["promoted"],
            "never_recalled_flagged": access_stats["never_recalled_flagged"],
        },
        "contradictions": contradiction_stats,
        "consolidation": consolidation_stats,
        "compression": compression_stats,
        "episodic_promotion": {
            "candidates_scanned": promotion_stats.get("candidates_scanned", 0),
            "clusters_found": promotion_stats.get("clusters_found", 0),
            "semantic_memories_created": promotion_stats.get("semantic_memories_created", 0),
            "source_memories_tagged": promotion_stats.get("source_memories_tagged", 0),
        },
        "experience_replay": replay_stats,
        "hebbian": hebbian_stats,
        "dream": dream_stats,
        "store_health": health,
    }

    # Log cycle event with correct event_type
    event_agent = resolve_event_agent(db, args.agent)
    db.execute(
        """
        INSERT INTO events (agent_id, event_type, summary, detail, metadata, project, importance, created_at)
        VALUES (?, 'consolidation_cycle', ?, ?, ?, ?, 0.8, ?)
        """,
        (
            event_agent,
            (
                f"Consolidation cycle — "
                f"ewc_scored={ewc_stats.get('updated', 0)}, "
                f"locked={newly_locked}, "
                f"reclassified={classification_stats.get('reclassified', 0)}, "
                f"decayed={decay_stats.get('updated', 0)}, "
                f"retired={decay_stats.get('retired', 0)}, "
                f"prot_skipped={decay_stats.get('skipped_protected', 0)}, "
                f"demoted={demotion_stats.get('demoted', 0)}, "
                f"promoted={access_stats['promoted']}, "
                f"contradictions={contradiction_stats.get('contradictions_found', 0)}, "
                f"merged={consolidation_stats.get('total_retired', 0)}, "
                f"compressed={compression_stats.get('total_retired', 0)}, "
                f"semantic_synthesized={promotion_stats.get('semantic_memories_created', 0)}, "
                f"replayed={replay_stats.get('replayed', 0)}, "
                f"hebb_created={hebbian_stats.get('edges_created', 0)}, "
                f"hebb_strengthened={hebbian_stats.get('edges_strengthened', 0)}, "
                f"dream_created={dream_stats.get('hypotheses_created', 0)}, "
                f"snr={health['signal_to_noise']}"
            ),
            json.dumps(summary, indent=2),
            json.dumps(summary),
            args.project,
            now_sql,
        ),
    )
    db.commit()
    db.close()

    # Log consolidation completion
    alert_summary = (
        f"Consolidation cycle complete at {now_sql}. "
        f"SNR={health['signal_to_noise']} ({health['at_risk']} at-risk / {health['total']} total). "
        f"Protected={newly_locked} newly locked, replayed={replay_stats.get('replayed', 0)}. "
        f"Contradictions={contradiction_stats.get('contradictions_found', 0)} "
        f"(retired={contradiction_stats.get('retired', 0)}). "
        f"Merged={consolidation_stats.get('total_retired', 0)}, "
        f"Compressed={compression_stats.get('total_retired', 0)}, "
        f"SemanticSynthesized={promotion_stats.get('semantic_memories_created', 0)}."
    )
    try:
        brainctl = Path.home() / "bin" / "brainctl"
        subprocess.run(
            [str(brainctl), "-a", event_agent, "event", "add", alert_summary,
             "-t", "consolidation_cycle", "--importance", "0.8"],
            timeout=10, check=False, capture_output=True,
        )
    except Exception:
        pass  # brainctl notification is best-effort

    if not args.quiet:
        print(json.dumps(summary, indent=2))


def consolidate_memories(conn: sqlite3.Connection, llm_fn: Optional[Any] = None) -> Dict[str, int]:
    """Merge clusters of similar non-permanent memories within each (category, scope).

    Uses FTS5 similarity to find clusters of 5+ overlapping memories,
    then merges each cluster into one memory with max(confidence).
    If llm_fn is provided, calls llm_fn(prompt) to generate consolidated text.
    Otherwise uses a simple concatenation fallback.

    Continual learning protections:
    - protected=1 memories are excluded
    - ewc_importance > 0.7 memories are excluded (high-value memories resist merging)
    """
    has_tc = has_column(conn, "memories", "temporal_class")
    tc_filter = " AND (temporal_class IS NULL OR temporal_class != 'permanent')" if has_tc else ""
    # Continual learning: exclude importance-locked memories from cluster merges
    _has_protected = has_column(conn, "memories", "protected")
    protected_filter = " AND (protected IS NULL OR protected = 0)" if _has_protected else ""
    _protected_select = ", protected" if _has_protected else ""
    # EWC: exclude high-importance memories from consolidation merges
    _has_ewc = has_column(conn, "memories", "ewc_importance")
    ewc_filter = " AND (ewc_importance IS NULL OR ewc_importance <= 0.7)" if _has_ewc else ""

    rows = conn.execute(
        f"SELECT id, agent_id, category, scope, content, confidence, tags, source_event_id{_protected_select} "
        f"FROM memories WHERE retired_at IS NULL{tc_filter}{protected_filter}{ewc_filter} ORDER BY category, scope, id",
    ).fetchall()

    memories = [dict(r) for r in rows]

    groups: Dict[tuple, list] = defaultdict(list)
    for m in memories:
        groups[(m["category"], m["scope"])].append(m)

    total_clusters = 0
    total_retired = 0
    min_cluster = 5

    for (category, scope), group_mems in groups.items():
        if len(group_mems) < min_cluster:
            continue

        clusters = build_similarity_clusters(conn, group_mems, min_cluster)
        if not clusters:
            continue

        for cluster in clusters:
            ids = [m["id"] for m in cluster]
            contents = [m["content"] for m in cluster]
            max_confidence = max(m["confidence"] for m in cluster)
            agent_id = cluster[0]["agent_id"]

            # Preserve source_event_id: use the first non-null link from the cluster
            # so the merged memory stays connected to the event lineage for health metrics.
            inherited_source_event_id = next(
                (m["source_event_id"] for m in cluster if m.get("source_event_id")), None
            )

            # Generate consolidated text
            if llm_fn is not None:
                memories_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(contents))
                prompt = CONSOLIDATION_PROMPT.format(n=len(cluster)) + f"\n\nMemories to consolidate:\n{memories_text}"
                consolidated = llm_fn(prompt)
                if not consolidated:
                    consolidated = "; ".join(contents)
            else:
                consolidated = "; ".join(contents)

            now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

            # Create consolidated memory — propagate source_event_id so distillation
            # ratio health metric is not broken by merges.
            cur = conn.execute(
                """
                INSERT INTO memories
                    (agent_id, category, scope, content, confidence, supersedes_id,
                     source_event_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, category, scope, consolidated, max_confidence, ids[0],
                 inherited_source_event_id, now_sql, now_sql),
            )

            # Retire originals
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE memories SET retired_at = ?, updated_at = ? WHERE id IN ({placeholders})",
                [now_sql, now_sql] + ids,
            )

            total_clusters += 1
            total_retired += len(cluster)

    conn.commit()
    return {"clusters": total_clusters, "retired": total_retired}


def _are_contradictory(a: str, b: str) -> bool:
    """Heuristic check if two memory contents contradict each other.

    Looks for opposing keywords/patterns in otherwise similar statements.
    """
    # Normalize for comparison
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()

    # High text similarity suggests they discuss the same topic
    sim = SequenceMatcher(None, a_lower, b_lower).ratio()
    if sim < 0.4:
        return False

    # Look for negation / opposition patterns
    negation_pairs = [
        ("enabled", "disabled"),
        ("is enabled", "is disabled"),
        ("supports", "does not support"),
        ("only", "also"),
        ("always", "never"),
        ("true", "false"),
        ("yes", "no"),
        ("on", "off"),
        ("active", "inactive"),
        ("allow", "deny"),
        ("allowed", "denied"),
        ("password login only", "oidc-only"),
        ("oidc", "password"),
    ]

    for pos, neg in negation_pairs:
        if (pos in a_lower and neg in b_lower) or (neg in a_lower and pos in b_lower):
            return True

    # Check for "X is Y" vs "X is Z" where Y != Z
    pattern = r"^(.{10,}?)(?:is|are|was|were|supports?)\s+(.+)$"
    ma = re.match(pattern, a_lower)
    mb = re.match(pattern, b_lower)
    if ma and mb:
        subj_sim = SequenceMatcher(None, ma.group(1), mb.group(1)).ratio()
        pred_sim = SequenceMatcher(None, ma.group(2), mb.group(2)).ratio()
        if subj_sim > 0.6 and pred_sim < 0.5:
            return True

    return False


def resolve_contradictions(conn: sqlite3.Connection, llm_fn: Optional[Any] = None) -> Dict[str, int]:
    """Find and resolve contradictory memory pairs.

    For non-permanent memories, retires the lower-confidence one.
    For permanent memories, logs a warning event instead.
    EWC protection: if the loser has ewc_importance > 0.7, requires text similarity > 0.9
    before retiring (high bar for deletion of high-value memories).
    """
    _has_ewc = has_column(conn, "memories", "ewc_importance")
    _ewc_select = ", ewc_importance" if _has_ewc else ""

    rows = conn.execute(
        f"""
        SELECT id, agent_id, category, scope, content, confidence, temporal_class{_ewc_select}
        FROM memories
        WHERE retired_at IS NULL
        ORDER BY category, scope, id
        """
    ).fetchall()

    memories = [dict(r) for r in rows]

    groups: Dict[tuple, list] = defaultdict(list)
    for m in memories:
        groups[(m["category"], m["scope"])].append(m)

    stats = {"contradictions_found": 0, "retired": 0, "warnings": 0, "skipped_ewc_protected": 0}
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    for (category, scope), group_mems in groups.items():
        checked = set()
        for i, a in enumerate(group_mems):
            for j, b in enumerate(group_mems):
                if i >= j:
                    continue
                pair_key = (a["id"], b["id"])
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                if not _are_contradictory(a["content"], b["content"]):
                    continue

                stats["contradictions_found"] += 1

                # Both permanent? Flag but don't resolve.
                tc_a = a.get("temporal_class", "medium")
                tc_b = b.get("temporal_class", "medium")
                if tc_a == "permanent" and tc_b == "permanent":
                    agent_id = a["agent_id"]
                    conn.execute(
                        """
                        INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                        VALUES (?, 'warning', ?, ?, 0.9, ?)
                        """,
                        (
                            agent_id,
                            f"Permanent memory contradiction detected between #{a['id']} and #{b['id']}",
                            f"Memory {a['id']}: {a['content']}\nMemory {b['id']}: {b['content']}",
                            now_sql,
                        ),
                    )
                    stats["warnings"] += 1
                    continue

                # Retire the lower-confidence one (or the older one if tied)
                if a["confidence"] >= b["confidence"]:
                    loser = b
                else:
                    loser = a

                # EWC protection: high-importance memories require a very strong contradiction
                # signal (text similarity > 0.9) before they can be retired.
                if _has_ewc:
                    loser_ewc = float(loser.get("ewc_importance") or 0.0)
                    if loser_ewc > 0.7:
                        sim = SequenceMatcher(
                            None,
                            a["content"].lower().strip(),
                            b["content"].lower().strip(),
                        ).ratio()
                        if sim <= 0.9:
                            stats["skipped_ewc_protected"] += 1
                            continue

                # Bayesian update: increment beta for the loser (contradiction evidence)
                _has_ab = has_column(conn, "memories", "alpha")
                if _has_ab:
                    conn.execute(
                        "UPDATE memories SET beta = COALESCE(beta, 1.0) + 1.0, "
                        "confidence = alpha / (alpha + COALESCE(beta, 1.0) + 1.0), "
                        "updated_at = ? WHERE id = ?",
                        (now_sql, loser["id"]),
                    )
                conn.execute(
                    "UPDATE memories SET retired_at = ?, updated_at = ? WHERE id = ?",
                    (now_sql, now_sql, loser["id"]),
                )
                stats["retired"] += 1

    conn.commit()
    return stats


def build_contradiction_report(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Build a structured coherence report for all active memories.

    Runs four detection passes:

    1. **Intra-scope polarity scan** — same (category, scope) pairs with opposing
       keyword patterns (reuses _are_contradictory heuristic). Auto-retires the
       lower-confidence non-permanent one; flags permanent-pair collisions.

    2. **Cross-scope semantic scan** — memories from different scopes that share
       high keyword overlap (FTS5 OR match) AND pass the polarity check. These are
       flagged in a warning event but never auto-retired (scope boundary matters).

    3. **Temporal conflict scan** — active memories that have been superseded
       (another active memory has supersedes_id pointing to them) without having
       been retired, indicating a stale predecessor still lingering.

    4. **Cross-agent assumption audit** — pairs of active memories from *different*
       agents that discuss the same topic (FTS5 similarity >= threshold) but
       contradict each other. Flagged as warnings, never auto-retired.

    Returns a structured dict suitable for JSON output.
    """
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    rows = conn.execute(
        """
        SELECT id, agent_id, category, scope, content, confidence, temporal_class, supersedes_id
        FROM memories
        WHERE retired_at IS NULL
        ORDER BY category, scope, id
        """
    ).fetchall()
    memories = [dict(r) for r in rows]

    report: Dict[str, Any] = {
        "generated_at": now_sql,
        "total_active_memories": len(memories),
        "intra_scope_contradictions": [],
        "cross_scope_contradictions": [],
        "temporal_conflicts": [],
        "cross_agent_contradictions": [],
        "stats": {
            "intra_scope_found": 0,
            "intra_scope_retired": 0,
            "intra_scope_flagged": 0,
            "cross_scope_found": 0,
            "temporal_conflicts_found": 0,
            "cross_agent_found": 0,
        },
    }

    # --- Pass 1: intra-scope polarity scan (existing resolve_contradictions logic) ---
    id_to_mem = {m["id"]: m for m in memories}
    groups: Dict[tuple, list] = defaultdict(list)
    for m in memories:
        groups[(m["category"], m["scope"])].append(m)

    checked: set = set()
    for (category, scope), group_mems in groups.items():
        for i, a in enumerate(group_mems):
            for j, b in enumerate(group_mems):
                if i >= j:
                    continue
                pair_key = (a["id"], b["id"])
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                if not _are_contradictory(a["content"], b["content"]):
                    continue

                report["stats"]["intra_scope_found"] += 1
                entry = {
                    "memory_a": a["id"],
                    "memory_b": b["id"],
                    "category": category,
                    "scope": scope,
                    "confidence_a": a["confidence"],
                    "confidence_b": b["confidence"],
                    "temporal_class_a": a["temporal_class"],
                    "temporal_class_b": b["temporal_class"],
                }

                tc_a = a.get("temporal_class", "medium")
                tc_b = b.get("temporal_class", "medium")
                if tc_a == "permanent" and tc_b == "permanent":
                    entry["action"] = "flagged_permanent_pair"
                    conn.execute(
                        """
                        INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                        VALUES (?, 'warning', ?, ?, 0.9, ?)
                        """,
                        (
                            a["agent_id"],
                            f"Permanent memory contradiction: #{a['id']} vs #{b['id']}",
                            f"Memory {a['id']}: {a['content']}\nMemory {b['id']}: {b['content']}",
                            now_sql,
                        ),
                    )
                    report["stats"]["intra_scope_flagged"] += 1
                else:
                    loser = b if a["confidence"] >= b["confidence"] else a
                    conn.execute(
                        "UPDATE memories SET retired_at = ?, updated_at = ? WHERE id = ?",
                        (now_sql, now_sql, loser["id"]),
                    )
                    entry["action"] = "retired"
                    entry["retired_id"] = loser["id"]
                    report["stats"]["intra_scope_retired"] += 1

                report["intra_scope_contradictions"].append(entry)

    # --- Pass 2: cross-scope semantic scan ---
    cross_checked: set = set()
    for a in memories:
        candidates = find_fts5_similar(conn, a["id"], a["content"], a["category"], scope=None)
        for cand_id in candidates:
            b = id_to_mem.get(cand_id)
            if b is None or b["scope"] == a["scope"]:
                continue
            pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
            if pair_key in cross_checked:
                continue
            cross_checked.add(pair_key)

            if not _are_contradictory(a["content"], b["content"]):
                continue

            report["stats"]["cross_scope_found"] += 1
            entry = {
                "memory_a": a["id"],
                "scope_a": a["scope"],
                "memory_b": b["id"],
                "scope_b": b["scope"],
                "action": "flagged_cross_scope",
            }
            conn.execute(
                """
                INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                VALUES (?, 'warning', ?, ?, 0.7, ?)
                """,
                (
                    a["agent_id"],
                    f"Cross-scope contradiction: #{a['id']} ({a['scope']}) vs #{b['id']} ({b['scope']})",
                    f"Memory {a['id']}: {a['content']}\nMemory {b['id']}: {b['content']}",
                    now_sql,
                ),
            )
            report["cross_scope_contradictions"].append(entry)

    # --- Pass 3: temporal conflict scan ---
    # An active memory X is a temporal conflict if another active memory Y has
    # supersedes_id = X.id  (Y was meant to replace X, but X was never retired).
    superseded_ids = {m["supersedes_id"] for m in memories if m.get("supersedes_id")}
    for m in memories:
        if m["id"] in superseded_ids:
            report["stats"]["temporal_conflicts_found"] += 1
            superseding_id = next(
                (s["id"] for s in memories if s.get("supersedes_id") == m["id"]), None
            )
            entry = {
                "stale_memory_id": m["id"],
                "scope": m["scope"],
                "superseded_by": superseding_id,
                "action": "flagged_temporal_conflict",
            }
            conn.execute(
                """
                INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                VALUES (?, 'stale_context', ?, ?, 0.7, ?)
                """,
                (
                    m["agent_id"],
                    f"Stale memory #{m['id']} not retired after supersession by #{superseding_id}",
                    f"Memory {m['id']} still active despite being superseded by memory {superseding_id}.",
                    now_sql,
                ),
            )
            report["temporal_conflicts"].append(entry)

    # --- Pass 4: cross-agent assumption audit ---
    # Pairs of memories from different agents, same topic (FTS5 match), contradicting.
    agent_checked: set = set()
    for a in memories:
        candidates = find_fts5_similar(conn, a["id"], a["content"], a["category"], scope=None)
        for cand_id in candidates:
            b = id_to_mem.get(cand_id)
            if b is None or b["agent_id"] == a["agent_id"]:
                continue
            pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
            if pair_key in agent_checked:
                continue
            agent_checked.add(pair_key)

            if not _are_contradictory(a["content"], b["content"]):
                continue

            report["stats"]["cross_agent_found"] += 1
            entry = {
                "memory_a": a["id"],
                "agent_a": a["agent_id"],
                "memory_b": b["id"],
                "agent_b": b["agent_id"],
                "action": "flagged_cross_agent",
            }
            conn.execute(
                """
                INSERT INTO events (agent_id, event_type, summary, detail, importance, created_at)
                VALUES (?, 'warning', ?, ?, 0.8, ?)
                """,
                (
                    a["agent_id"],
                    f"Cross-agent contradiction: #{a['id']} ({a['agent_id']}) vs #{b['id']} ({b['agent_id']})",
                    f"Agent {a['agent_id']} memory {a['id']}: {a['content']}\n"
                    f"Agent {b['agent_id']} memory {b['id']}: {b['content']}",
                    now_sql,
                ),
            )
            report["cross_agent_contradictions"].append(entry)

    conn.commit()
    return report


def compress_memories(conn: sqlite3.Connection, llm_fn: Optional[Any] = None) -> Dict[str, Any]:
    """Compress groups of 10+ related memories within a scope into fewer memories.

    Permanent memories are never compressed.
    Groups under 10 are left untouched.
    Target is ceil(original_count / 3) output memories.

    Continual learning protections:
    - protected=1 memories are excluded
    - ewc_importance > 0.7 memories are excluded
    """
    # Continual learning: exclude importance-locked memories from compression
    _has_protected_col = has_column(conn, "memories", "protected")
    _prot_filter = " AND (protected IS NULL OR protected = 0)" if _has_protected_col else ""
    # EWC: exclude high-importance memories from compression
    _has_ewc = has_column(conn, "memories", "ewc_importance")
    _ewc_filter = " AND (ewc_importance IS NULL OR ewc_importance <= 0.7)" if _has_ewc else ""

    rows = conn.execute(
        f"""
        SELECT id, agent_id, category, scope, content, confidence, tags, temporal_class, source_event_id
        FROM memories
        WHERE retired_at IS NULL AND temporal_class != 'permanent'{_prot_filter}{_ewc_filter}
        ORDER BY scope, id
        """
    ).fetchall()

    grouped: Dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row["scope"]].append(dict(row))

    min_group_size = 10
    compressed_scopes = []
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    for scope, group in grouped.items():
        if len(group) < min_group_size:
            continue

        original_count = len(group)
        max_output = math.ceil(original_count / 3)
        source_ids = [m["id"] for m in group]
        agent_id = group[0]["agent_id"]
        category = Counter(m["category"] for m in group).most_common(1)[0][0]

        # Preserve source_event_id: pick the first non-null link from the group
        # so compressed memories stay connected to the event lineage.
        inherited_source_event_id = next(
            (m["source_event_id"] for m in group if m.get("source_event_id")), None
        )

        # Generate compressed memories
        if llm_fn is not None:
            payload = {
                "scope": scope,
                "instructions": COMPRESS_PROMPT,
                "memories": [{"content": m["content"], "confidence": m["confidence"]} for m in group],
            }
            try:
                result = llm_fn(json.dumps(payload))
                compressed_texts = parse_llm_json_array(result)
                if len(compressed_texts) > max_output:
                    compressed_texts = compressed_texts[:max_output]
            except Exception:
                compressed_texts = _fallback_compress(group, max_output)
        else:
            compressed_texts = _fallback_compress(group, max_output)

        # Retire originals
        placeholders = ",".join("?" for _ in source_ids)
        conn.execute(
            f"UPDATE memories SET retired_at = ?, updated_at = ? WHERE id IN ({placeholders})",
            [now_sql, now_sql] + source_ids,
        )

        # Insert compressed — propagate source_event_id so the distillation ratio
        # health metric doesn't crater after compression passes.
        for content in compressed_texts:
            conn.execute(
                """
                INSERT INTO memories
                    (agent_id, category, scope, content, confidence, tags, temporal_class,
                     source_event_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0.95, ?, 'medium', ?, ?, ?)
                """,
                (agent_id, category, scope, content, json.dumps(["compressed"]),
                 inherited_source_event_id, now_sql, now_sql),
            )

        compressed_scopes.append({"scope": scope, "original": original_count, "compressed": len(compressed_texts)})

    conn.commit()
    return {"compressed_scopes": compressed_scopes}


def _fallback_compress(group: List[dict], max_output: int) -> List[str]:
    """Simple non-LLM compression: merge adjacent memories into paragraphs."""
    chunk_size = math.ceil(len(group) / max_output)
    result = []
    for i in range(0, len(group), chunk_size):
        chunk = group[i:i + chunk_size]
        merged = " ".join(m["content"] for m in chunk)
        result.append(merged)
    return result[:max_output]


# ---------------------------------------------------------------------------
# Episodic-to-Semantic Promotion
# ---------------------------------------------------------------------------

PROMOTION_SYNTHESIS_PROMPT = (
    "You are abstracting episodic memories into a stable semantic fact. "
    "Given these {n} related episodic observations, synthesize ONE concise semantic memory: "
    "a durable, decontextualized fact that captures the core pattern or truth they share. "
    "Strip timestamps, agent names, and task-specific detail. "
    "Output ONLY the semantic memory text, nothing else."
)

# Minimum cluster size before episodic→semantic promotion triggers
PROMOTION_MIN_CLUSTER = 3
# Confidence floor for episodic memories to qualify for promotion
PROMOTION_CONFIDENCE_MIN = 0.5
# Tag added to source episodic memories once they've contributed to a synthesis
_PROMOTED_TAG = "promoted_to_semantic"


def _has_tag(tags_json: Optional[str], tag: str) -> bool:
    if not tags_json:
        return False
    try:
        return tag in json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        return False


def _add_tag(tags_json: Optional[str], tag: str) -> str:
    try:
        tags: list = json.loads(tags_json) if tags_json else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    if tag not in tags:
        tags.append(tag)
    return json.dumps(tags)


def _synthesize_semantic(memories_text: str, n: int) -> Optional[str]:
    # LLM synthesis removed — brainctl is model-agnostic.
    # The calling agent should handle LLM reasoning and feed results back via brainctl memory add.
    return None


def promote_episodic_to_semantic(
    conn: sqlite3.Connection,
    llm_fn: Optional[Any] = None,
    min_cluster: int = PROMOTION_MIN_CLUSTER,
    confidence_min: float = PROMOTION_CONFIDENCE_MIN,
) -> Dict[str, Any]:
    """Synthesize semantic memories from clusters of related episodic memories.

    For each (category, scope) group, finds FTS5 clusters of `min_cluster`+
    episodic memories (confidence >= confidence_min, not already promoted).
    Calls an LLM to synthesize a single semantic memory per cluster.

    The synthesized memory is written with:
      memory_type = 'semantic'
      temporal_class = 'long'
      confidence = max(cluster confidences)
      derived_from_ids = JSON list of source IDs

    Source episodic memories are tagged with 'promoted_to_semantic' so they
    are skipped on subsequent promotion passes (they are NOT retired — episodic
    records are time-bound history and remain accessible).

    Returns a stats dict suitable for inclusion in the consolidation cycle summary.
    """
    rows = conn.execute(
        """
        SELECT id, agent_id, category, scope, content, confidence, tags, temporal_class
        FROM memories
        WHERE retired_at IS NULL
          AND memory_type = 'episodic'
          AND temporal_class != 'permanent'
          AND confidence >= ?
        ORDER BY category, scope, id
        """,
        (confidence_min,),
    ).fetchall()

    # Filter out memories already contributed to a promotion
    candidates = [
        dict(r) for r in rows
        if not _has_tag(r["tags"], _PROMOTED_TAG)
    ]

    # Group by (category, scope)
    groups: Dict[tuple, list] = defaultdict(list)
    for m in candidates:
        groups[(m["category"], m["scope"])].append(m)

    stats: Dict[str, Any] = {
        "candidates_scanned": len(candidates),
        "clusters_found": 0,
        "semantic_memories_created": 0,
        "source_memories_tagged": 0,
        "promotions": [],
    }

    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    for (category, scope), group_mems in groups.items():
        if len(group_mems) < min_cluster:
            continue

        clusters = build_similarity_clusters(conn, group_mems, min_cluster)
        if not clusters:
            continue

        stats["clusters_found"] += len(clusters)

        for cluster in clusters:
            ids = [m["id"] for m in cluster]
            contents = [m["content"] for m in cluster]
            max_confidence = max(float(m["confidence"]) for m in cluster)
            agent_id = cluster[0]["agent_id"]

            memories_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(contents))

            # Synthesize via LLM or fallback
            if llm_fn is not None:
                prompt = PROMOTION_SYNTHESIS_PROMPT.format(n=len(cluster)) + f"\n\nEpisodic observations:\n{memories_text}"
                synthesized = llm_fn(prompt)
            else:
                synthesized = _synthesize_semantic(memories_text, len(cluster))

            if not synthesized:
                # Fallback: distill the cluster into a single sentence
                synthesized = f"Pattern observed in {len(cluster)} related events [{category}/{scope}]: " + "; ".join(contents[:2])

            # Write the new semantic memory
            cur = conn.execute(
                """
                INSERT INTO memories
                    (agent_id, category, scope, content, confidence,
                     memory_type, temporal_class, derived_from_ids,
                     tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'semantic', 'long', ?, ?, ?, ?)
                """,
                (
                    agent_id, category, scope, synthesized, max_confidence,
                    json.dumps(ids),
                    json.dumps(["synthesized_from_episodic"]),
                    now_sql, now_sql,
                ),
            )
            new_id = cur.lastrowid
            # Embed the synthesized semantic memory inline
            _try_embed_new_memory(new_id, synthesized)

            # Tag source episodic memories as promoted (do NOT retire them)
            placeholders = ",".join("?" for _ in ids)
            source_rows = conn.execute(
                f"SELECT id, tags FROM memories WHERE id IN ({placeholders})", ids
            ).fetchall()
            for src in source_rows:
                new_tags = _add_tag(src["tags"], _PROMOTED_TAG)
                conn.execute(
                    "UPDATE memories SET tags = ?, updated_at = ? WHERE id = ?",
                    (new_tags, now_sql, src["id"]),
                )

            # Log promotion event
            project = scope.split(":", 1)[1] if scope.startswith("project:") and ":" in scope else None
            conn.execute(
                """
                INSERT INTO events (agent_id, event_type, summary, detail, metadata, project, importance, created_at)
                VALUES (?, 'memory_promoted', ?, ?, ?, ?, 0.75, ?)
                """,
                (
                    agent_id,
                    f"Synthesized semantic memory id={new_id} from {len(cluster)} episodic memories [{category}/{scope}]",
                    f"Source IDs: {ids}\nSynthesized: {synthesized}",
                    json.dumps({
                        "semantic_memory_id": new_id,
                        "source_episodic_ids": ids,
                        "category": category,
                        "scope": scope,
                        "cluster_size": len(cluster),
                        "max_confidence": max_confidence,
                    }),
                    project,
                    now_sql,
                ),
            )

            stats["semantic_memories_created"] += 1
            stats["source_memories_tagged"] += len(ids)
            stats["promotions"].append({
                "semantic_memory_id": new_id,
                "source_ids": ids,
                "category": category,
                "scope": scope,
                "synthesized": synthesized[:120] + ("..." if len(synthesized) > 120 else ""),
            })

    conn.commit()
    return stats


def cmd_promote_episodic(args):
    """CLI handler: run episodic-to-semantic promotion pass."""
    conn = get_db()
    ensure_agent(conn, args.agent)
    stats = promote_episodic_to_semantic(
        conn,
        min_cluster=args.min_cluster,
        confidence_min=args.confidence_min,
    )
    print(json.dumps({"ok": True, "agent": args.agent, **stats}, indent=2))


# Salience / gate-keeping

TRIVIAL_PATTERNS = [
    r"^ran\s+(npm|pip|yarn|cargo|make|go)\b",
    r"^(installed|updated|upgraded)\s+\w+",
    r"^(cd|ls|cat|echo|mkdir|rm|cp|mv|chmod|chown)\b",
    r"^(ran|running|executed)\s+(a\s+)?command",
    r"^(opened|closed|saved)\s+(a\s+)?file",
    r"^git\s+(add|commit|push|pull|fetch|checkout|merge|rebase)\b",
]


def _is_trivial(content: str) -> bool:
    """Return True if the content describes a trivial/mechanical action."""
    content_lower = content.strip().lower()
    if len(content_lower) < 20:
        return True
    for pat in TRIVIAL_PATTERNS:
        if re.match(pat, content_lower):
            return True
    return False


def _is_near_duplicate(content: str, existing_contents: List[str], threshold: float = 0.75) -> bool:
    """Return True if content is too similar to any existing memory."""
    content_lower = content.lower().strip()
    for existing in existing_contents:
        sim = SequenceMatcher(None, content_lower, existing.lower().strip()).ratio()
        if sim >= threshold:
            return True
    return False


def should_accept_memory(
    content: str,
    scope: str,
    category: str,
    existing_contents: Optional[List[str]] = None,
) -> bool:
    """Gate-keeping function: decide whether a new memory should be stored.

    Rejects:
      - Trivial/mechanical actions (npm install, git commit, etc.)
      - Near-duplicates of existing memories (>= 75% similarity)
    Accepts:
      - Concrete, specific, informative memories
    """
    if _is_trivial(content):
        return False

    if existing_contents and _is_near_duplicate(content, existing_contents):
        return False

    return True


def _vec_loaded(conn: sqlite3.Connection) -> bool:
    """Return True if sqlite-vec is already loaded in this connection."""
    try:
        conn.execute("SELECT vec_version()")
        return True
    except sqlite3.OperationalError:
        return False


def search_memories(
    conn: sqlite3.Connection,
    query: str,
    now: Optional[datetime] = None,
    no_recency: bool = False,
    limit: int = 20,
) -> List[dict]:
    """Search memories with optional recency weighting.

    When no_recency=False (default), uses hybrid BM25+vector salience routing
    when sqlite-vec is loaded; falls back to FTS-only recency scoring otherwise.
    When no_recency=True, ranking is purely by confidence (FTS path).
    """
    if now is None:
        now = datetime.now()

    # Hybrid vector path: only when recency is active and vec extension is loaded
    if not no_recency and _vec_loaded(conn):
        try:
            # Import here to avoid circular deps and keep hippocampus self-contained
            import importlib.util, os
            _sr_path = os.path.join(os.path.dirname(__file__), "salience_routing.py")
            spec = importlib.util.spec_from_file_location("salience_routing", _sr_path)
            sr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sr)
            results = sr.route_memories_hybrid(
                conn, query, top_k=limit, vec_available=True, min_salience=0.0
            )
            # Normalise output to match the expected dict shape (add _score, agent_id)
            for r in results:
                r.setdefault("agent_id", None)
                r.setdefault("_score", r.get("salience", 0.0))
            if results:
                return results
        except Exception:
            pass  # fall through to FTS path on any error

    # FTS fallback path (also used for no_recency=True)
    fts_query = fts5_or_query(query)
    if not fts_query:
        return []

    try:
        rows = conn.execute(
            """
            SELECT m.id, m.agent_id, m.category, m.scope, m.content, m.confidence,
                   m.temporal_class, m.created_at, m.last_recalled_at, rank
            FROM memories m
            JOIN memories_fts ON memories_fts.rowid = m.id
            WHERE memories_fts MATCH ?
              AND m.retired_at IS NULL
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit * 5),  # fetch extra for re-ranking
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    results = []
    for row in rows:
        d = dict(row)
        confidence = float(d["confidence"])

        if no_recency:
            d["_score"] = confidence
        else:
            baseline_ts = d["last_recalled_at"] or d["created_at"]
            elapsed = days_since(now, baseline_ts) if baseline_ts else 0.0
            recency_boost = math.exp(-0.03 * elapsed)
            d["_score"] = confidence * recency_boost

        results.append(d)

    results.sort(key=lambda x: x["_score"], reverse=True)
    return results[:limit]


def assign_epoch(conn: sqlite3.Connection, ts: Optional[str] = None) -> Optional[int]:
    """Find (or create) the epoch that covers the given timestamp.

    If no epoch covers the timestamp, creates a new auto-generated epoch.
    Returns the epoch id.
    """
    if ts is None:
        ts = datetime.now().isoformat()

    # Normalize timestamp
    normalized = ts.strip().replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    # Strip timezone info for comparison with SQLite datetimes
    dt = datetime.fromisoformat(normalized)
    ts_sql = dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Find matching epoch (started_at <= ts AND (ended_at IS NULL OR ended_at >= ts))
    row = conn.execute(
        """
        SELECT id FROM epochs
        WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (ts_sql, ts_sql),
    ).fetchone()

    if row:
        return row["id"]

    # No matching epoch — create an auto-generated one
    name = f"Auto-epoch ({dt.strftime('%Y-%m-%d')})"
    cur = conn.execute(
        """
        INSERT INTO epochs (name, description, started_at, ended_at)
        VALUES (?, 'Auto-generated epoch', ?, NULL)
        """,
        (name, ts_sql),
    )
    conn.commit()
    return cur.lastrowid


def cmd_boost(args):
    """CLI handler: boost confidence of a specific recalled memory."""
    db = get_db()
    result = apply_recall_boost(db, args.memory_id)
    if result is None:
        print(json.dumps({"ok": False, "error": f"Memory {args.memory_id} not found or already retired"}))
        sys.exit(1)
    print(json.dumps({"ok": True, **result}, indent=2))


def cmd_demote(args):
    """CLI handler: apply temporal class demotion pass."""
    db = get_db()
    stats = apply_temporal_demotion(db)
    print(json.dumps({"ok": True, **stats}, indent=2))


def cmd_contradiction_report(args):
    """CLI handler: build and print the full contradiction/coherence report."""
    db = get_db()
    report = build_contradiction_report(db)
    print(json.dumps(report, indent=2))


def cmd_hebb(args):
    """CLI handler: run Hebbian edge strengthening pass."""
    db = get_db()
    now = datetime.now()
    stats = run_hebbian_pass(db, now=now, dry_run=args.dry_run)
    if not args.dry_run:
        db.commit()
    db.close()
    print(json.dumps(stats, indent=2))



# ---------------------------------------------------------------------------
# Dream Pass — Creative synthesis via bisociation during consolidation
# ---------------------------------------------------------------------------

DREAM_SIMILARITY_THRESHOLD = 0.70   # cosine similarity floor for bisociation
DREAM_MAX_HYPOTHESES_PER_RUN = 20   # cap new hypotheses per consolidation cycle
DREAM_INCUBATION_DAYS = 7           # days before an unrecalled hypothesis retires
DREAM_CANDIDATE_LIMIT = 200         # max memories considered for pairwise scan


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _unpack_vector(blob: bytes, dimensions: int = 768) -> List[float]:
    """Unpack a float32 BLOB into a Python list of floats."""
    return list(struct.unpack(f"{dimensions}f", blob))


def run_dream_pass(
    db: sqlite3.Connection,
    agent_id: str = "hippocampus",
    similarity_threshold: float = DREAM_SIMILARITY_THRESHOLD,
    max_hypotheses: int = DREAM_MAX_HYPOTHESES_PER_RUN,
    incubation_days: int = DREAM_INCUBATION_DAYS,
) -> Dict[str, int]:
    """Dream pass: bisociation + incubation queue for creative synthesis.

    Steps:
    1. Retire expired incubating hypotheses (older than incubation_days, no recall).
    2. Promote hypotheses whose hypothesis memory has been recalled.
    3. Load embeddings for active memories; compute cross-scope pairwise cosine similarity.
    4. For novel pairs (similarity > threshold, no existing knowledge_edge), generate
       a hypothesis memory and insert into dream_hypotheses.

    Returns stats dict.
    """
    now = datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")
    stats: Dict[str, int] = {
        "expired_retired": 0,
        "promoted": 0,
        "pairs_scanned": 0,
        "novel_pairs": 0,
        "hypotheses_created": 0,
        "skipped_no_embedding": 0,
    }

    # Ensure dream_hypotheses table exists (idempotent guard)
    db.execute("""
        CREATE TABLE IF NOT EXISTS dream_hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_a_id INTEGER NOT NULL REFERENCES memories(id),
            memory_b_id INTEGER NOT NULL REFERENCES memories(id),
            hypothesis_memory_id INTEGER REFERENCES memories(id),
            similarity REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'incubating'
                CHECK(status IN ('incubating', 'promoted', 'retired')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            promoted_at TEXT,
            retired_at TEXT,
            retirement_reason TEXT
        )
    """)

    # Step 1: Retire expired incubating hypotheses (no recall within incubation window)
    cutoff_sql = (now - timedelta(days=incubation_days)).strftime("%Y-%m-%dT%H:%M:%S")
    expired_rows = db.execute(
        """
        SELECT dh.id, dh.hypothesis_memory_id
        FROM dream_hypotheses dh
        LEFT JOIN memories m ON m.id = dh.hypothesis_memory_id
        WHERE dh.status = 'incubating'
          AND dh.created_at < ?
          AND (m.recalled_count IS NULL OR m.recalled_count = 0)
        """,
        (cutoff_sql,),
    ).fetchall()

    for row in expired_rows:
        db.execute(
            "UPDATE dream_hypotheses SET status='retired', retired_at=?, "
            "retirement_reason='incubation_expired' WHERE id=?",
            (now_sql, row["id"]),
        )
        if row["hypothesis_memory_id"]:
            db.execute(
                "UPDATE memories SET retired_at=? WHERE id=? AND retired_at IS NULL",
                (now_sql, row["hypothesis_memory_id"]),
            )
        stats["expired_retired"] += 1

    # Step 2: Promote hypotheses whose memory has been recalled
    recalled_rows = db.execute(
        """
        SELECT dh.id, dh.hypothesis_memory_id
        FROM dream_hypotheses dh
        JOIN memories m ON m.id = dh.hypothesis_memory_id
        WHERE dh.status = 'incubating'
          AND m.recalled_count > 0
          AND m.retired_at IS NULL
        """
    ).fetchall()

    for row in recalled_rows:
        db.execute(
            "UPDATE dream_hypotheses SET status='promoted', promoted_at=? WHERE id=?",
            (now_sql, row["id"]),
        )
        # Upgrade from ephemeral to short temporal class and raise confidence
        db.execute(
            "UPDATE memories SET temporal_class='short', category='lesson', "
            "confidence=0.5 WHERE id=? AND temporal_class='ephemeral'",
            (row["hypothesis_memory_id"],),
        )
        stats["promoted"] += 1

    db.commit()

    # Step 3: Load embeddings for active non-hypothesis memories
    mem_rows = db.execute(
        """
        SELECT m.id, m.scope, m.content, e.vector, e.dimensions
        FROM memories m
        JOIN embeddings e ON e.source_table='memories' AND e.source_id=m.id
        WHERE m.retired_at IS NULL
          AND m.category != 'hypothesis'
        ORDER BY m.confidence DESC
        LIMIT ?
        """,
        (DREAM_CANDIDATE_LIMIT,),
    ).fetchall()

    if len(mem_rows) < 2:
        return stats

    # Decode vectors
    memories_with_vecs: List[Dict] = []
    for row in mem_rows:
        if row["vector"] is None:
            stats["skipped_no_embedding"] += 1
            continue
        dims = row["dimensions"] or 768
        try:
            vec = _unpack_vector(bytes(row["vector"]), dims)
        except struct.error:
            stats["skipped_no_embedding"] += 1
            continue
        memories_with_vecs.append({
            "id": row["id"],
            "scope": row["scope"],
            "content": row["content"],
            "vec": vec,
        })

    # Load existing knowledge_edges pairs as a set for O(1) lookup (table may not exist)
    existing_edges: set = set()
    tbl_check = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_edges'"
    ).fetchone()
    if tbl_check:
        edge_rows = db.execute(
            "SELECT source_id, target_id FROM knowledge_edges "
            "WHERE source_table='memories' AND target_table='memories'"
        ).fetchall()
        for er in edge_rows:
            a_id, b_id = er["source_id"], er["target_id"]
            existing_edges.add((min(a_id, b_id), max(a_id, b_id)))

    # Load already-active or recently-retired pairs to avoid duplicates / immediate re-tries.
    # Promoted pairs are always excluded (they became real memories).
    # Retired pairs are excluded for one incubation window after retirement (cooldown).
    incubating_pairs: set = set()
    inc_rows = db.execute(
        """
        SELECT memory_a_id, memory_b_id FROM dream_hypotheses
        WHERE status = 'incubating'
           OR status = 'promoted'
           OR (status = 'retired' AND retired_at >= ?)
        """,
        (cutoff_sql,),
    ).fetchall()
    for ir in inc_rows:
        a_id, b_id = ir["memory_a_id"], ir["memory_b_id"]
        incubating_pairs.add((min(a_id, b_id), max(a_id, b_id)))

    # Step 4: Pairwise scan for cross-scope bisociations
    n = len(memories_with_vecs)
    new_hypotheses = []

    for i in range(n):
        if len(new_hypotheses) >= max_hypotheses:
            break
        for j in range(i + 1, n):
            if len(new_hypotheses) >= max_hypotheses:
                break
            stats["pairs_scanned"] += 1

            mem_a, mem_b = memories_with_vecs[i], memories_with_vecs[j]
            # Must be different scopes for true bisociation
            if mem_a["scope"] == mem_b["scope"]:
                continue

            pair_key = (min(mem_a["id"], mem_b["id"]), max(mem_a["id"], mem_b["id"]))

            # Skip already-connected or already-incubating pairs
            if pair_key in existing_edges or pair_key in incubating_pairs:
                continue

            sim = _cosine_similarity(mem_a["vec"], mem_b["vec"])
            if sim < similarity_threshold:
                continue

            stats["novel_pairs"] += 1
            new_hypotheses.append((mem_a, mem_b, sim))

    # Step 5: Create hypothesis memories and insert into dream_hypotheses
    for mem_a, mem_b, sim in new_hypotheses:
        a_snippet = (mem_a["content"] or "")[:120].replace("\n", " ")
        b_snippet = (mem_b["content"] or "")[:120].replace("\n", " ")
        hypothesis_content = (
            f"Potential connection: [{mem_a['scope']}] {a_snippet} "
            f"may relate to [{mem_b['scope']}] {b_snippet}"
        )

        cur = db.execute(
            """
            INSERT INTO memories
              (agent_id, category, scope, content, confidence, temporal_class, memory_type,
               tags, created_at, updated_at)
            VALUES (?, 'hypothesis', 'global', ?, 0.3, 'ephemeral', 'episodic',
                    '["dream","bisociation","hypothesis"]', ?, ?)
            """,
            (agent_id, hypothesis_content, now_sql, now_sql),
        )
        hyp_memory_id = cur.lastrowid

        pair_key = (min(mem_a["id"], mem_b["id"]), max(mem_a["id"], mem_b["id"]))
        db.execute(
            """
            INSERT OR IGNORE INTO dream_hypotheses
              (memory_a_id, memory_b_id, hypothesis_memory_id, similarity, status, created_at)
            VALUES (?, ?, ?, ?, 'incubating', ?)
            """,
            (pair_key[0], pair_key[1], hyp_memory_id, round(sim, 4), now_sql),
        )
        incubating_pairs.add(pair_key)
        stats["hypotheses_created"] += 1

    db.commit()
    return stats


def cmd_dream_pass(args):
    """CLI handler: run the dream pass (bisociation + incubation queue)."""
    db = get_db()
    stats = run_dream_pass(
        db,
        agent_id=getattr(args, "agent", "hippocampus"),
        similarity_threshold=getattr(args, "threshold", DREAM_SIMILARITY_THRESHOLD),
        max_hypotheses=getattr(args, "max_hypotheses", DREAM_MAX_HYPOTHESES_PER_RUN),
        incubation_days=getattr(args, "incubation_days", DREAM_INCUBATION_DAYS),
    )
    print(json.dumps({"ok": True, **stats}, indent=2))


# ---------------------------------------------------------------------------
# Reflexion Propagation Pass
# ---------------------------------------------------------------------------

_PROPAGATION_MIN_CONFIDENCE = 0.7
_PROPAGATION_CONFIDENCE_DISCOUNT = 0.85


def _resolve_propagation_targets(
    conn: sqlite3.Connection,
    generalizable_to: list,
    source_agent_id: str,
) -> list:
    """Expand generalizable_to scope tokens to a list of target agent_id strings.

    Tokens:
      agent_type:<type>   → all active agents with that agent_type
      scope:global        → all active agents
      capability:<cap>    → agents with that capability (falls back to matching agent_type)
      agent:<agent_id>    → a specific agent (direct targeting)
    """
    all_agents = conn.execute(
        "SELECT id, agent_type FROM agents WHERE status = 'active'"
    ).fetchall()

    targets = set()
    for token in generalizable_to:
        token = token.strip()
        if token.startswith("agent_type:"):
            atype = token[len("agent_type:"):]
            targets.update(r["id"] for r in all_agents if r["agent_type"] == atype)
        elif token == "scope:global" or token.startswith("scope:global"):
            targets.update(r["id"] for r in all_agents)
        elif token.startswith("capability:"):
            # Map capability to agent_type:agent as primary brainctl users
            targets.update(r["id"] for r in all_agents if r["agent_type"] == "agent")
        elif token.startswith("agent:"):
            agent_id = token[len("agent:"):]
            targets.add(agent_id)
        # Unknown tokens are silently skipped

    # Never propagate back to the source agent
    targets.discard(source_agent_id)
    return sorted(targets)


def reflexion_propagation_pass(
    conn: sqlite3.Connection,
    agent_id: str = "hippocampus",
    min_confidence: float = _PROPAGATION_MIN_CONFIDENCE,
    dry_run: bool = False,
) -> dict:
    """Find active reflexion lessons with generalizable_to scope and propagate them.

    For each qualifying lesson, writes a discounted copy to each target agent
    that hasn't received it yet. Tracks propagated_to for idempotency.

    Returns stats: lessons_scanned, lessons_eligible, copies_written, agents_reached.
    """
    # Check that propagated_to column exists (migration 019)
    if not has_column(conn, "reflexion_lessons", "propagated_to"):
        return {
            "ok": False,
            "error": "reflexion_lessons.propagated_to column missing — apply migration 019_reflexion_propagation.sql",
        }

    rows = conn.execute(
        """SELECT * FROM reflexion_lessons
           WHERE status = 'active'
             AND confidence >= ?
             AND generalizable_to IS NOT NULL
             AND generalizable_to != '[]'
        """,
        (min_confidence,),
    ).fetchall()

    stats = {
        "lessons_scanned": len(rows),
        "lessons_eligible": 0,
        "copies_written": 0,
        "agents_reached": set(),
        "skipped_already_propagated": 0,
    }

    now = datetime.utcnow().isoformat()

    for row in rows:
        row = dict(row)
        try:
            generalizable_to = json.loads(row["generalizable_to"] or "[]")
        except (json.JSONDecodeError, TypeError):
            generalizable_to = []

        if not generalizable_to:
            continue

        try:
            already_propagated = set(json.loads(row.get("propagated_to") or "[]"))
        except (json.JSONDecodeError, TypeError):
            already_propagated = set()

        targets = _resolve_propagation_targets(conn, generalizable_to, row["source_agent_id"])
        pending = [t for t in targets if t not in already_propagated]

        if not pending:
            stats["skipped_already_propagated"] += 1
            continue

        stats["lessons_eligible"] += 1
        new_propagated = list(already_propagated)

        for target_agent_id in pending:
            # Ensure target agent exists in agents table
            existing = conn.execute(
                "SELECT id FROM agents WHERE id = ?", (target_agent_id,)
            ).fetchone()
            if not existing:
                continue

            propagated_content = (
                f"Generalized from {row['source_agent_id']}: {row['lesson_content']}"
            )
            discounted_confidence = round(row["confidence"] * _PROPAGATION_CONFIDENCE_DISCOUNT, 4)
            # Propagated copies are scoped to the specific target agent only
            propagated_scope = json.dumps([f"agent:{target_agent_id}"])

            if not dry_run:
                conn.execute(
                    """INSERT INTO reflexion_lessons (
                        source_agent_id, source_event_id, source_run_id,
                        failure_class, failure_subclass,
                        trigger_conditions, lesson_content, generalizable_to,
                        confidence, override_level, status,
                        expiration_policy, expiration_n, expiration_ttl_days,
                        root_cause_ref, propagated_to, propagation_source_lesson_id,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        target_agent_id,
                        row.get("source_event_id"),
                        row.get("source_run_id"),
                        row["failure_class"],
                        row.get("failure_subclass"),
                        row["trigger_conditions"],
                        propagated_content,
                        propagated_scope,
                        discounted_confidence,
                        row["override_level"],
                        "active",
                        row["expiration_policy"],
                        row.get("expiration_n"),
                        row.get("expiration_ttl_days"),
                        row.get("root_cause_ref"),
                        "[]",  # propagated copies don't cascade further
                        row["id"],
                        now,
                        now,
                    ),
                )

            new_propagated.append(target_agent_id)
            stats["copies_written"] += 1
            stats["agents_reached"].add(target_agent_id)

        if not dry_run and new_propagated != list(already_propagated):
            conn.execute(
                "UPDATE reflexion_lessons SET propagated_to = ? WHERE id = ?",
                (json.dumps(sorted(new_propagated)), row["id"]),
            )

    if not dry_run:
        conn.commit()

        if stats["copies_written"] > 0:
            conn.execute(
                """INSERT INTO events (agent_id, event_type, summary, metadata, project, importance, created_at)
                   VALUES (?, 'reflexion_propagation', ?, ?, 'agentmemory', 0.7, ?)""",
                (
                    agent_id,
                    f"Reflexion propagation pass: {stats['copies_written']} copies written to {len(stats['agents_reached'])} agents",
                    json.dumps({
                        "lessons_scanned": stats["lessons_scanned"],
                        "lessons_eligible": stats["lessons_eligible"],
                        "copies_written": stats["copies_written"],
                        "agents_reached": sorted(stats["agents_reached"]),
                    }),
                    now,
                ),
            )
            conn.commit()

    stats["agents_reached"] = sorted(stats["agents_reached"])
    return stats


def cmd_reflexion_propagation_pass(args):
    """CLI handler: run the reflexion propagation pass."""
    db = get_db()
    dry_run = getattr(args, "dry_run", False)
    min_confidence = getattr(args, "min_confidence", _PROPAGATION_MIN_CONFIDENCE)
    agent_id = getattr(args, "agent", "hippocampus")
    stats = reflexion_propagation_pass(db, agent_id=agent_id, min_confidence=min_confidence, dry_run=dry_run)
    print(json.dumps({"ok": True, **stats}, indent=2))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hippocampus background jobs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    decay = sub.add_parser("decay", help="Apply confidence decay to active memories")
    decay.add_argument("--agent", default="hippocampus", help="Agent id for warning events")
    decay.add_argument("--project", default="agentmemory", help="Project tag for warning events")
    decay.add_argument("--dry-run", action="store_true", help="Compute decay without writing changes")
    decay.set_defaults(func=cmd_decay)

    boost = sub.add_parser("boost", help="Boost confidence of a recalled memory (reconsolidation)")
    boost.add_argument("memory_id", type=int, help="ID of the memory to boost")
    boost.set_defaults(func=cmd_boost)

    demote = sub.add_parser("demote", help="Apply temporal class demotion pass to all memories")
    demote.set_defaults(func=cmd_demote)

    contradiction_report = sub.add_parser(
        "contradiction-report",
        help="Build coherence report: intra-scope, cross-scope, temporal conflicts, cross-agent",
    )
    contradiction_report.set_defaults(func=cmd_contradiction_report)

    compress = sub.add_parser("compress", help="Compress active memories by scope")
    compress.add_argument("--agent", default="hippocampus", help="Agent id for synthesized memories/events")
    compress.add_argument("--scope", help="Optional single scope to process")
    compress.add_argument("--min-group-size", type=int, default=10, help="Only compress scopes with at least N memories")
    compress.add_argument("--llm-cmd", help="Shell command: reads JSON from stdin, writes JSON array of strings to stdout")
    compress.add_argument("--dry-run", action="store_true", help="Preview compression without writing changes")
    compress.set_defaults(func=cmd_compress)

    consolidate = sub.add_parser("consolidate", help="Merge duplicate/clustered memories using FTS5 similarity")
    consolidate.add_argument("--agent", default=DEFAULT_CONSOLIDATE_AGENT, help="Agent id to attribute consolidated memories to")
    consolidate.add_argument("--min-cluster", type=int, default=3, help="Minimum cluster size to trigger consolidation (default: 3)")
    consolidate.add_argument("--scope", help="Limit to a specific scope (e.g. project:myproject)")
    consolidate.add_argument("--dry-run", action="store_true", help="Show what would be consolidated without making changes")
    consolidate.set_defaults(func=cmd_consolidate)

    cycle = sub.add_parser(
        "consolidation-cycle",
        help="Run full consolidation cycle: decay -> demotion -> access pattern analysis -> event log",
    )
    cycle.add_argument("--agent", default="hippocampus", help="Agent id for cycle event")
    cycle.add_argument("--project", default="agentmemory", help="Project tag for cycle event")
    cycle.add_argument("--quiet", action="store_true", help="Suppress JSON output")
    cycle.set_defaults(func=cmd_consolidation_cycle)

    promote = sub.add_parser(
        "promote-episodic",
        help="Synthesize semantic memories from clusters of related episodic memories",
    )
    promote.add_argument("--agent", default="hippocampus", help="Agent id for synthesized memories/events")
    promote.add_argument("--min-cluster", type=int, default=PROMOTION_MIN_CLUSTER,
                         help=f"Minimum episodic cluster size to trigger synthesis (default: {PROMOTION_MIN_CLUSTER})")
    promote.add_argument("--confidence-min", type=float, default=PROMOTION_CONFIDENCE_MIN,
                         help=f"Minimum episodic confidence to qualify for promotion (default: {PROMOTION_CONFIDENCE_MIN})")
    promote.set_defaults(func=cmd_promote_episodic)


    hebb = sub.add_parser(
        "hebb",
        help="Run Hebbian edge strengthening pass (co-retrieval pattern learning)",
    )
    hebb.add_argument("--dry-run", action="store_true", help="Compute without writing changes")
    hebb.set_defaults(func=cmd_hebb)

    dream = sub.add_parser(
        "dream-pass",
        help="Run dream pass: bisociation scan + incubation queue (creative synthesis)",
    )
    dream.add_argument("--agent", default="hippocampus", help="Agent id for hypothesis memories")
    dream.add_argument(
        "--threshold", type=float, default=DREAM_SIMILARITY_THRESHOLD,
        help=f"Cosine similarity threshold for bisociation (default: {DREAM_SIMILARITY_THRESHOLD})"
    )
    dream.add_argument(
        "--max-hypotheses", dest="max_hypotheses", type=int, default=DREAM_MAX_HYPOTHESES_PER_RUN,
        help=f"Max new hypotheses per run (default: {DREAM_MAX_HYPOTHESES_PER_RUN})"
    )
    dream.add_argument(
        "--incubation-days", dest="incubation_days", type=int, default=DREAM_INCUBATION_DAYS,
        help=f"Days before unrecalled hypothesis is retired (default: {DREAM_INCUBATION_DAYS})"
    )
    dream.set_defaults(func=cmd_dream_pass)

    propagate = sub.add_parser(
        "reflexion-propagate",
        help="Propagate generalizable reflexion lessons to matching agents",
    )
    propagate.add_argument("--agent", default="hippocampus", help="Agent id for event attribution")
    propagate.add_argument(
        "--min-confidence", dest="min_confidence", type=float,
        default=_PROPAGATION_MIN_CONFIDENCE,
        help=f"Minimum lesson confidence to propagate (default: {_PROPAGATION_MIN_CONFIDENCE})",
    )
    propagate.add_argument("--dry-run", dest="dry_run", action="store_true",
                           help="Preview propagation without writing copies")
    propagate.set_defaults(func=cmd_reflexion_propagation_pass)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
