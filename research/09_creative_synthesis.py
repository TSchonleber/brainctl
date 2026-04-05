"""
Creative Synthesis — Dream Pass
================================
Implements the bisociation step and incubation queue for the consolidation cycle.

Inspired by REM sleep's role in creative recombination: finds cross-scope memory
pairs with unexpectedly high embedding similarity and synthesizes novel connection
hypotheses. Failed searches are queued and retried with relaxed thresholds.

Pipeline (called as step 9 in 05_consolidation_cycle.py):
  1. Bisociation pass  — cosine similarity across different-scope memory pairs
  2. Incubation pass   — retry deferred zero-result queries with looser thresholds

All synthetic outputs:
  - category = 'insight'
  - trust_score = 0.30, confidence = 0.25
  - tagged ['synthetic', 'dream_bisociation']
  - excluded from normal search unless --include-synthetic is passed
  - cap: 3 new insight memories per bisociation pass

References: research/wave6/24_creative_synthesis_dreams.md (COS-247), COS-303
"""

import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
CYCLE_AGENT_ID = "paperclip-engram"

# Thresholds (per COS-303 spec)
BISOCIATION_SIMILARITY_THRESHOLD = 0.75   # cosine sim > 0.75  →  vec distance < 0.25
BISOCIATION_MAX_PAIRS_SCANNED = 50        # pairs evaluated per pass
BISOCIATION_MAX_INSIGHTS = 3             # hard cap on new insight memories per pass

SYNTHETIC_TRUST_SCORE = 0.30
SYNTHETIC_CONFIDENCE = 0.25
SYNTHETIC_TEMPORAL_CLASS = "short"       # decays in ~7 days unless promoted

DEFERRED_QUERY_MAX_AGE_DAYS = 30
INCUBATION_SIMILARITY_DELTA = 0.10      # threshold relaxed by this amount
INCUBATION_LIMIT_PER_QUERY = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_vec_ext(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension. Returns True on success."""
    dylib_paths = [
        "/opt/homebrew/lib/python3.13/site-packages/sqlite_vec/vec0.dylib",
        "/Users/r4vager/agentmemory/bin/vec0",
        "/Users/r4vager/agentmemory/bin/vec0.dylib",
    ]
    try:
        conn.enable_load_extension(True)
        for p in dylib_paths:
            import os
            if os.path.exists(p):
                conn.load_extension(p)
                return True
    except Exception:
        pass
    return False


# ── 1. Bisociation Pass ───────────────────────────────────────────────────────

def find_bisociation_candidates(
    conn: sqlite3.Connection,
    min_similarity: float = BISOCIATION_SIMILARITY_THRESHOLD,
    limit: int = BISOCIATION_MAX_PAIRS_SCANNED,
) -> list[dict]:
    """
    Find active cross-scope memory pairs with cosine similarity > min_similarity
    that have no existing knowledge_edge between them.

    Requires sqlite-vec extension. Returns empty list if vec is unavailable.
    """
    if not _load_vec_ext(conn):
        return []

    distance_threshold = 1.0 - min_similarity  # vec distance < 0.25 for sim > 0.75

    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT
                a.id           AS id_a,
                b.id           AS id_b,
                a.content      AS content_a,
                b.content      AS content_b,
                a.scope        AS scope_a,
                b.scope        AS scope_b,
                a.category     AS category_a,
                b.category     AS category_b,
                a.agent_id     AS agent_a,
                a.confidence   AS conf_a,
                b.confidence   AS conf_b,
                vec_distance_cosine(va.embedding, vb.embedding) AS distance
            FROM memories a
            JOIN memories b
                ON  a.id < b.id
                AND a.scope != b.scope
                AND a.retired_at IS NULL
                AND b.retired_at IS NULL
                AND a.confidence > 0.3
                AND b.confidence > 0.3
            JOIN vec_memories va ON va.id = a.id
            JOIN vec_memories vb ON vb.id = b.id
            WHERE vec_distance_cosine(va.embedding, vb.embedding) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM knowledge_edges ke
                  WHERE (ke.source_id = a.id AND ke.target_id = b.id)
                     OR (ke.source_id = b.id AND ke.target_id = a.id)
              )
            ORDER BY distance ASC
            LIMIT ?
        """, (distance_threshold, limit)).fetchall()
    except Exception:
        return []

    return [dict(r) for r in rows]


def _build_insight_content(pair: dict) -> str:
    """
    Build the insight memory content in the format specified by COS-303:
    '[Memory A scope] and [Memory B scope] may share: [similarity explanation]'
    """
    sim = round(1.0 - pair["distance"], 3)
    excerpt_a = pair["content_a"][:100].rstrip()
    excerpt_b = pair["content_b"][:100].rstrip()
    return (
        f"{pair['scope_a']} and {pair['scope_b']} may share: "
        f"high conceptual proximity (similarity={sim}). "
        f'"{excerpt_a}..." ↔ "{excerpt_b}..."'
    )


def _record_dream_hypothesis(
    conn: sqlite3.Connection,
    pair: dict,
    hypothesis_memory_id: int,
) -> None:
    """Insert a row into dream_hypotheses to track this bisociation pair."""
    conn.execute("""
        INSERT OR IGNORE INTO dream_hypotheses
            (memory_a_id, memory_b_id, hypothesis_memory_id, similarity, status, created_at)
        VALUES (?, ?, ?, ?, 'incubating', datetime('now'))
    """, (pair["id_a"], pair["id_b"], hypothesis_memory_id, round(1.0 - pair["distance"], 4)))


def run_bisociation_pass(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    min_similarity: float = BISOCIATION_SIMILARITY_THRESHOLD,
    max_insights: int = BISOCIATION_MAX_INSIGHTS,
) -> dict:
    """
    Find cross-scope memory pairs with sim > min_similarity and no existing
    knowledge_edge. Write up to max_insights new 'insight' category memories.

    Returns stats dict: pairs_evaluated, insights_written.
    """
    stats = {"pairs_evaluated": 0, "insights_written": 0}

    candidates = find_bisociation_candidates(conn, min_similarity)
    stats["pairs_evaluated"] = len(candidates)

    written = 0
    for pair in candidates:
        if written >= max_insights:
            break

        insight_content = _build_insight_content(pair)
        tags = json.dumps(["synthetic", "dream_bisociation",
                           f"scope:{pair['scope_a']}", f"scope:{pair['scope_b']}"])
        derived_from = json.dumps([pair["id_a"], pair["id_b"]])

        if not dry_run:
            cur = conn.execute("""
                INSERT INTO memories (
                    agent_id, category, scope, content, confidence, trust_score,
                    temporal_class, tags, derived_from_ids, created_at, updated_at
                ) VALUES (?, 'insight', 'global', ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """, (
                CYCLE_AGENT_ID,
                insight_content,
                SYNTHETIC_CONFIDENCE,
                SYNTHETIC_TRUST_SCORE,
                SYNTHETIC_TEMPORAL_CLASS,
                tags,
                derived_from,
            ))
            hyp_mem_id = cur.lastrowid
            _record_dream_hypothesis(conn, pair, hyp_mem_id)

        written += 1

    stats["insights_written"] = written
    return stats


# ── 2. Incubation Pass ────────────────────────────────────────────────────────

def run_incubation_pass(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    base_threshold: float = 0.70,  # normal FTS coverage threshold
    threshold_delta: float = INCUBATION_SIMILARITY_DELTA,
    limit_per_query: int = INCUBATION_LIMIT_PER_QUERY,
) -> dict:
    """
    Retry deferred zero-result queries with expanded matching (threshold lowered
    by threshold_delta). On match, emit a deferred_query_resolved event and
    surface the result as a memory retrieval suggestion.

    Returns stats dict: queries_checked, resolved.
    """
    conn.row_factory = sqlite3.Row
    stats = {"queries_checked": 0, "resolved": 0}

    pending = conn.execute("""
        SELECT id, agent_id, query_text, query_embedding, queried_at
        FROM deferred_queries
        WHERE resolved_at IS NULL
          AND (julianday('now') - julianday(queried_at)) <= ?
        ORDER BY queried_at ASC
        LIMIT 50
    """, (DEFERRED_QUERY_MAX_AGE_DAYS,)).fetchall()

    import re
    _FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^]')

    def _sanitize(q: str) -> str:
        cleaned = _FTS5_SPECIAL.sub(" ", q or "")
        return re.sub(r"\s+", " ", cleaned).strip()

    for q in pending:
        stats["queries_checked"] += 1
        fts_q = _sanitize(q["query_text"])
        if not fts_q:
            continue

        # FTS retry — look for memories created after the original query
        # or that weren't present when the search was first attempted
        try:
            fts_rows = conn.execute("""
                SELECT m.id, m.content, m.scope, m.confidence
                FROM memories m
                JOIN memories_fts mf ON mf.rowid = m.id
                WHERE memories_fts MATCH ?
                  AND m.retired_at IS NULL
                LIMIT ?
            """, (fts_q, limit_per_query)).fetchall()
        except Exception:
            fts_rows = []

        if fts_rows:
            stats["resolved"] += 1
            if not dry_run:
                best = fts_rows[0]
                conn.execute("""
                    UPDATE deferred_queries
                    SET resolved_at = datetime('now'),
                        resolution_memory_id = ?,
                        attempts = attempts + 1
                    WHERE id = ?
                """, (best["id"], q["id"]))
                conn.execute("""
                    INSERT INTO events
                        (agent_id, event_type, summary, detail, importance, created_at)
                    VALUES (?, 'deferred_query_resolved', ?, ?, 0.6, datetime('now'))
                """, (
                    q["agent_id"],
                    f"Incubated query resolved: '{q['query_text'][:60]}'",
                    json.dumps({
                        "query": q["query_text"],
                        "queried_at": q["queried_at"],
                        "resolution_memory_id": best["id"],
                        "resolution_content_preview": best["content"][:120],
                    }),
                ))
        else:
            if not dry_run:
                conn.execute("""
                    UPDATE deferred_queries SET attempts = attempts + 1 WHERE id = ?
                """, (q["id"],))

    return stats


# ── Dream Pass Orchestrator ───────────────────────────────────────────────────

def run_dream_pass(
    db_path: str = DB_PATH,
    dry_run: bool = False,
    run_bisociation: bool = True,
    run_incubation: bool = True,
    bisociation_threshold: float = BISOCIATION_SIMILARITY_THRESHOLD,
    max_insights: int = BISOCIATION_MAX_INSIGHTS,
) -> dict:
    """
    Full dream pass: bisociation + incubation.

    Called by run_consolidation_cycle(run_dream_pass=True) after step 8.
    Returns a report dict logged to the events table.
    """
    conn = sqlite3.connect(db_path)
    report: dict = {
        "started_at": _now_iso(),
        "dry_run": dry_run,
        "bisociation": {},
        "incubation": {},
        "synthetic_memories_written": 0,
    }

    if run_bisociation:
        report["bisociation"] = run_bisociation_pass(
            conn, dry_run, bisociation_threshold, max_insights
        )
        report["synthetic_memories_written"] += report["bisociation"].get("insights_written", 0)

    if run_incubation:
        report["incubation"] = run_incubation_pass(conn, dry_run)

    report["completed_at"] = _now_iso()

    if not dry_run:
        conn.execute("""
            INSERT INTO events
                (agent_id, event_type, summary, detail, importance, created_at)
            VALUES (?, 'dream_pass_complete', ?, ?, 0.7, datetime('now'))
        """, (
            CYCLE_AGENT_ID,
            (
                f"Dream pass: {report['synthetic_memories_written']} insight memories written, "
                f"{report['bisociation'].get('pairs_evaluated', 0)} bisociation pairs scanned, "
                f"{report['incubation'].get('resolved', 0)} deferred queries resolved"
            ),
            json.dumps(report),
        ))
        conn.commit()

    conn.close()
    return report


if __name__ == "__main__":
    import json as _json
    result = run_dream_pass(dry_run=True)
    print(_json.dumps(result, indent=2))
