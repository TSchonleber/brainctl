"""
Consolidation Cycle — Background Sleep Job
==========================================
Concept: Inspired by sleep consolidation in neuroscience. Short-term memories
(episodic events) are reorganized, summarized, and compressed into long-term
semantic memories. Runs as a daily cron job.

Pipeline:
  1. Cluster recent short/ephemeral memories by semantic similarity
  2. Summarize each cluster into a consolidated memory
  3. Retire the source memories (superseded by consolidated)
  4. Demote low-confidence memories (temporal class downgrade)
  5. Apply decay pass
  6. Detect contradictions → flag for human review
  7. Merge near-duplicates
  8. Update knowledge_edges for new consolidated nodes
  9. Log cycle report to events table

This file is the orchestrator. Heavy computation (LLM summarization) is
done via brainctl or external calls and is indicated with TODO markers.
"""

import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
CYCLE_AGENT_ID = "paperclip-engram"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Step 1: Collect candidates ────────────────────────────────────────────────

def collect_consolidation_candidates(
    conn: sqlite3.Connection,
    temporal_classes: tuple = ("ephemeral", "short"),
    min_age_days: float = 1.0,
    limit: int = 500,
) -> list[dict]:
    """
    Fetch recent short-lived memories that haven't been consolidated yet.
    Excludes memories already superseded or retired.
    """
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, agent_id, category, scope, content, confidence, temporal_class,
               recalled_count, last_recalled_at, created_at, tags
        FROM memories
        WHERE retired_at IS NULL
          AND supersedes_id IS NULL
          AND temporal_class IN ({})
          AND (julianday('now') - julianday(created_at)) >= ?
        ORDER BY created_at ASC
        LIMIT ?
    """.format(",".join("?" * len(temporal_classes)))

    rows = conn.execute(sql, list(temporal_classes) + [min_age_days, limit]).fetchall()
    return [dict(r) for r in rows]


# ── Step 2: Cluster by category + scope ──────────────────────────────────────

def cluster_memories(memories: list[dict]) -> dict[str, list[dict]]:
    """
    Simple grouping by (category, scope) as a baseline cluster key.
    In production, replace with embedding-based clustering via vec_memories.
    """
    clusters: dict[str, list[dict]] = {}
    for m in memories:
        key = f"{m['category']}::{m['scope']}"
        clusters.setdefault(key, []).append(m)
    # Only consolidate clusters with >= 3 memories
    return {k: v for k, v in clusters.items() if len(v) >= 3}


# ── Step 3: Consolidate a cluster ─────────────────────────────────────────────

def consolidate_cluster(
    conn: sqlite3.Connection,
    cluster_key: str,
    memories: list[dict],
    summarizer_fn=None,
) -> Optional[int]:
    """
    Merge a cluster of memories into one consolidated memory.
    summarizer_fn(texts: list[str]) -> str  — if None, uses simple concatenation.
    Returns new memory ID.
    """
    texts = [m["content"] for m in memories]
    category = memories[0]["category"]
    scope = memories[0]["scope"]
    agent_id = memories[0]["agent_id"]
    avg_confidence = sum(m["confidence"] for m in memories) / len(memories)
    total_recalls = sum(m["recalled_count"] for m in memories)

    # Summarize — replace with LLM call in production
    if summarizer_fn:
        consolidated_content = summarizer_fn(texts)
    else:
        # Naive: join unique sentences, truncate
        seen = set()
        parts = []
        for t in texts:
            for sent in t.split(". "):
                s = sent.strip()
                if s and s not in seen:
                    seen.add(s)
                    parts.append(s)
        consolidated_content = ". ".join(parts[:10])

    # Write consolidated memory
    cur = conn.execute("""
        INSERT INTO memories (agent_id, category, scope, content, confidence,
                              temporal_class, recalled_count, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'medium', ?, ?, datetime('now'), datetime('now'))
    """, (
        agent_id, category, scope, consolidated_content,
        min(1.0, avg_confidence + 0.05),  # slight boost for consolidation
        total_recalls,
        json.dumps(["consolidated", f"cluster:{cluster_key[:40]}"]),
    ))
    new_id = cur.lastrowid

    # Retire source memories, marking them superseded
    for m in memories:
        conn.execute("""
            UPDATE memories
            SET retired_at = datetime('now'), supersedes_id = NULL, updated_at = datetime('now')
            WHERE id = ?
        """, (m["id"],))
        # Link them to the consolidated memory via knowledge_edges
        conn.execute("""
            INSERT OR REPLACE INTO knowledge_edges
                (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
            VALUES ('memories', ?, 'memories', ?, 'derived_from', 0.9, ?)
        """, (new_id, m["id"], agent_id))

    return new_id


# ── Step 4: Merge near-duplicates ─────────────────────────────────────────────

DUPLICATE_SIMILARITY_SQL = """
-- Find memories within same agent+category that share many FTS tokens
-- as a proxy for near-duplicate detection (no embedding required)
SELECT a.id as id_a, b.id as id_b,
       a.content as content_a, b.content as content_b
FROM memories a
JOIN memories b ON a.id < b.id
    AND a.agent_id = b.agent_id
    AND a.category = b.category
    AND a.scope = b.scope
    AND a.retired_at IS NULL
    AND b.retired_at IS NULL
WHERE EXISTS (
    SELECT 1 FROM memories_fts
    WHERE memories_fts MATCH (
        SELECT group_concat(term, ' OR ')
        FROM (
            SELECT term FROM memories_fts_idx
            WHERE segid IN (SELECT segid FROM memories_fts_idx LIMIT 1)
            LIMIT 5
        )
    )
    AND rowid = a.id
)
LIMIT 100;
"""


def find_near_duplicates_simple(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """
    Use FTS to find potential duplicate memories for review.
    Returns pairs (id_a, id_b) that should be inspected.
    """
    conn.row_factory = sqlite3.Row
    # Simplified: find memories with identical first 50 chars
    rows = conn.execute("""
        SELECT a.id as id_a, b.id as id_b
        FROM memories a
        JOIN memories b ON a.id < b.id
            AND a.agent_id = b.agent_id
            AND a.category = b.category
            AND a.retired_at IS NULL
            AND b.retired_at IS NULL
            AND substr(lower(a.content), 1, 50) = substr(lower(b.content), 1, 50)
        LIMIT 50
    """).fetchall()
    return [(r["id_a"], r["id_b"]) for r in rows]


# ── Step 6b: Trust score update pass (COS-302 / COS-234) ─────────────────────

def trust_update_pass(
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict:
    """
    Apply COS-234 trust delta rules to all active memories.

    Decay events (lower trust):
      - Has 'contradicts' knowledge edge:              -0.20
      - Superseded by newer memory (derived_from target): -0.15
      - Never recalled after 30+ days (per week past 30): -0.05
      - No corroborating agent in same scope/category within 14 days: -0.05

    Boost events (raise trust):
      - Corroborated by ≥2 agents in same scope/category within 14 days: +0.15
      - No contradicts edge (survived contradiction scan):               +0.05
      - recalled_count > 5:                                              +0.10

    All deltas are accumulated and applied as a single bounded update per
    memory, clamped to [0.1, 1.0].

    Returns a report dict with counts.
    """
    conn.row_factory = sqlite3.Row

    # Gather active memories with their current trust scores
    memories = conn.execute("""
        SELECT id, trust_score, recalled_count, created_at, scope, category, agent_id
        FROM memories
        WHERE retired_at IS NULL
    """).fetchall()

    # Precompute: memories with 'contradicts' edges (both directions)
    contradicted_ids = set()
    for row in conn.execute("""
        SELECT DISTINCT source_id FROM knowledge_edges
        WHERE source_table = 'memories' AND target_table = 'memories'
          AND relation_type = 'contradicts'
    """).fetchall():
        contradicted_ids.add(row[0])
    for row in conn.execute("""
        SELECT DISTINCT target_id FROM knowledge_edges
        WHERE source_table = 'memories' AND target_table = 'memories'
          AND relation_type = 'contradicts'
    """).fetchall():
        contradicted_ids.add(row[0])

    # Precompute: memories that are targets of 'derived_from' edges
    # (i.e., a newer memory was derived from them — they've been superseded)
    superseded_ids = set()
    for row in conn.execute("""
        SELECT DISTINCT target_id FROM knowledge_edges
        WHERE source_table = 'memories' AND target_table = 'memories'
          AND relation_type = 'derived_from'
    """).fetchall():
        superseded_ids.add(row[0])

    # Precompute: per (scope, category) — set of distinct agent_ids that wrote
    # a memory in the last 14 days
    scope_cat_agents: dict[tuple, set] = {}
    for row in conn.execute("""
        SELECT scope, category, agent_id FROM memories
        WHERE retired_at IS NULL
          AND created_at >= datetime('now', '-14 days')
    """).fetchall():
        key = (row["scope"], row["category"])
        scope_cat_agents.setdefault(key, set()).add(row["agent_id"])

    updated = 0
    deltas: list[tuple] = []  # (new_trust, memory_id)

    for m in memories:
        mid = m["id"]
        current_trust = m["trust_score"] if m["trust_score"] is not None else 1.0
        delta = 0.0

        # Decay: contradiction found
        if mid in contradicted_ids:
            delta -= 0.20

        # Decay: superseded by newer memory
        if mid in superseded_ids:
            delta -= 0.15

        # Decay: never recalled after 30+ days (−0.05 per additional week)
        if (m["recalled_count"] or 0) == 0 and m["created_at"]:
            age_days_sql = conn.execute(
                "SELECT julianday('now') - julianday(?) AS age_days", (m["created_at"],)
            ).fetchone()
            age_days = age_days_sql[0] if age_days_sql else 0
            if age_days > 30:
                weeks_past_30 = (age_days - 30) / 7.0
                delta -= 0.05 * weeks_past_30

        key = (m["scope"], m["category"])
        agents_in_window = scope_cat_agents.get(key, {m["agent_id"]})

        # Decay: no corroborating agent in scope/category within 14 days
        other_agents = agents_in_window - {m["agent_id"]}
        if not other_agents:
            delta -= 0.05

        # Boost: corroborated by ≥2 other agents
        if len(other_agents) >= 2:
            delta += 0.15
        elif len(other_agents) >= 1:
            delta += 0.15  # any corroboration qualifies per spec

        # Boost: survived contradiction scan (no contradicts edge)
        if mid not in contradicted_ids:
            delta += 0.05

        # Boost: well-recalled
        if (m["recalled_count"] or 0) > 5:
            delta += 0.10

        if abs(delta) < 0.001:
            continue

        new_trust = round(max(0.1, min(1.0, current_trust + delta)), 4)
        if abs(new_trust - current_trust) < 0.001:
            continue

        deltas.append((new_trust, mid))

    if not dry_run and deltas:
        conn.executemany(
            "UPDATE memories SET trust_score = ?, updated_at = datetime('now') WHERE id = ?",
            deltas,
        )
        conn.commit()
        updated = len(deltas)

    return {
        "memories_scanned": len(memories),
        "memories_updated": updated if not dry_run else 0,
        "dry_run_would_update": len(deltas) if dry_run else 0,
        "contradicted_count": len(contradicted_ids),
        "superseded_count": len(superseded_ids),
    }


# ── Step 7: Log cycle report ──────────────────────────────────────────────────

def log_cycle_event(
    conn: sqlite3.Connection,
    report: dict,
    epoch_id: int = None,
) -> int:
    """Log the consolidation cycle results to events table."""
    cur = conn.execute("""
        INSERT INTO events (agent_id, event_type, summary, detail, metadata,
                            importance, epoch_id, created_at)
        VALUES (?, 'consolidation_cycle', ?, ?, ?, 0.8, ?, datetime('now'))
    """, (
        CYCLE_AGENT_ID,
        f"Consolidation cycle: {report.get('consolidated', 0)} merged, "
        f"{report.get('retired', 0)} retired, "
        f"{report.get('contradictions', 0)} contradictions flagged",
        json.dumps(report, indent=2),
        json.dumps(report),
        epoch_id,
    ))
    return cur.lastrowid


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_consolidation_cycle(
    db_path: str = DB_PATH,
    dry_run: bool = False,
    summarizer_fn=None,
    run_cross_scope: bool = False,
    run_dream_pass: bool = False,
) -> dict:
    """
    Full consolidation cycle. Returns a cycle report dict.

    Parameters
    ----------
    run_cross_scope : bool
        If True, runs the cross-scope contradiction detection pass (step 7b).
        Off by default — the pass is O(N²) across related memory pairs and is
        better run explicitly (brainctl or --cross-scope flag) on larger stores.
        See COS-233 / wave6/21_cross_scope_contradiction.md.
    run_dream_pass : bool
        If True, runs the dream pass after standard maintenance (step 9).
        Finds cross-scope bisociation candidates and retries deferred queries.
        Requires sqlite-vec extension for bisociation; gracefully skips if unavailable.
        See COS-247 / COS-303.
    """
    import importlib.util as _ilu, sys as _sys, os as _os
    _base = _os.path.dirname(_os.path.abspath(__file__))
    def _load_mod(alias, fname):
        if alias in _sys.modules:
            return _sys.modules[alias]
        spec = _ilu.spec_from_file_location(alias, _os.path.join(_base, fname))
        mod = _ilu.module_from_spec(spec)
        _sys.modules[alias] = mod
        spec.loader.exec_module(mod)
        return mod
    _sr  = _load_mod("_spaced_rep",   "01_spaced_repetition.py")
    _sf  = _load_mod("_sem_forget",   "02_semantic_forgetting.py")
    _cd  = _load_mod("_contra_detect","06_contradiction_detection.py")
    run_decay_pass       = _sr.run_decay_pass
    run_demotion_pass    = _sf.run_demotion_pass
    find_contradictions         = _cd.find_contradictions
    find_cross_scope_contradictions = _cd.find_cross_scope_contradictions
    flag_contradiction          = _cd.flag_contradiction

    conn = sqlite3.connect(db_path)
    report = {
        "started_at": now_iso(),
        "consolidated": 0,
        "clusters": 0,
        "retired": 0,
        "demoted": 0,
        "duplicates_flagged": 0,
        "contradictions": 0,
        "cross_scope_contradictions": 0,
        "trust_updated": 0,
        "dream_pass": {},
        "dry_run": dry_run,
    }

    # 1. Collect candidates
    candidates = collect_consolidation_candidates(conn)
    report["candidates"] = len(candidates)

    # 2. Cluster
    clusters = cluster_memories(candidates)
    report["clusters"] = len(clusters)

    # 3. Consolidate
    if not dry_run:
        for key, mems in clusters.items():
            new_id = consolidate_cluster(conn, key, mems, summarizer_fn)
            if new_id:
                report["consolidated"] += len(mems)
                report["retired"] += len(mems)
        conn.commit()

    # 4. Decay pass
    decay_result = run_decay_pass(db_path, dry_run=dry_run)
    report["retired"] += decay_result.get("retired", 0)

    # 5. Demotion pass
    demotion_result = run_demotion_pass(db_path, dry_run=dry_run)
    report["demoted"] = demotion_result.get("demoted", 0)

    # 6. Near-duplicate detection
    dupes = find_near_duplicates_simple(conn)
    report["duplicates_flagged"] = len(dupes)

    # 7. Contradiction detection (within-scope)
    contradictions = find_contradictions(conn, limit=50)
    report["contradictions"] = len(contradictions)

    # 7b. Cross-scope contradiction detection (opt-in, COS-233)
    if run_cross_scope:
        cs_conflicts = find_cross_scope_contradictions(conn, limit=25)
        report["cross_scope_contradictions"] = len(cs_conflicts)
        if not dry_run:
            for c in cs_conflicts:
                if c.get("resolution") != "temporal_sequence":
                    flag_contradiction(
                        conn,
                        c["memory_id_a"],
                        c["memory_id_b"],
                        c["type"],
                    )
            conn.commit()

    # 7c. Trust score update pass (COS-302 / COS-234)
    trust_result = trust_update_pass(conn, dry_run=dry_run)
    report["trust_updated"] = trust_result.get("memories_updated", 0)
    report["trust_dry_run_would_update"] = trust_result.get("dry_run_would_update", 0)

    # 9. Dream pass — bisociation + incubation queue (COS-247 / COS-303, opt-in)
    if run_dream_pass:
        _dp = _load_mod("_creative_synthesis", "09_creative_synthesis.py")
        dream_result = _dp.run_dream_pass(db_path=db_path, dry_run=dry_run)
        report["dream_pass"] = dream_result

    # 8. Log cycle event
    report["completed_at"] = now_iso()
    if not dry_run:
        log_cycle_event(conn, report)
        conn.commit()

    conn.close()
    return report


if __name__ == "__main__":
    report = run_consolidation_cycle(dry_run=True)
    print(json.dumps(report, indent=2))
