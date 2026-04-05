"""
Contradiction Detection — Conflicting Memory Identification
===========================================================
Concept: With 178+ agent memories from multiple agents, contradictions
accumulate. A contradiction is when two memories assert mutually exclusive
facts. We detect them via:

  1. Exact-scope conflicts: same (agent_id, category, scope) with opposing signals
  2. FTS negation patterns: memory A contains "X is Y", memory B contains "X is not Y"
  3. Embedding distance contradiction: semantically close memories from same scope
     that were written by different agents or at different times — flag for review
  4. Supersession chains: if memory A supersedes B but B is still active, flag it
  5. Cross-scope conflicts: memories in DIFFERENT scopes that reference the same
     real-world entity but make incompatible claims (COS-233 / wave6)

Output: A list of (memory_id_a, memory_id_b, conflict_type, confidence_delta)
tuples written to events table with event_type='contradiction_detected'.
"""

import sqlite3
import json
import re
from datetime import datetime, timezone

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
CYCLE_AGENT_ID = "paperclip-engram"

NEGATION_PATTERNS = [
    (r"\bis\b", r"\bis not\b|\bisn'?t\b"),
    (r"\bcan\b", r"\bcannot\b|\bcan'?t\b"),
    (r"\bwill\b", r"\bwill not\b|\bwon'?t\b"),
    (r"\bshould\b", r"\bshould not\b|\bshouldn'?t\b"),
    (r"\bhas\b", r"\bhas not\b|\bhasn'?t\b"),
    (r"\benabled\b", r"\bdisabled\b"),
    (r"\bactive\b", r"\binactive\b"),
    (r"\btrue\b", r"\bfalse\b"),
]


def find_contradictions(
    conn: sqlite3.Connection,
    limit: int = 100,
    min_confidence: float = 0.3,
) -> list[dict]:
    """
    Find potentially contradicting memory pairs. Returns list of conflict dicts.
    """
    conn.row_factory = sqlite3.Row
    conflicts = []

    # Strategy 1: Supersession breaks — memory supersedes another but both active
    rows = conn.execute("""
        SELECT a.id as id_a, b.id as id_b,
               a.content as content_a, b.content as content_b,
               a.confidence as conf_a, b.confidence as conf_b,
               a.agent_id, a.category, a.scope
        FROM memories a
        JOIN memories b ON b.supersedes_id = a.id
        WHERE a.retired_at IS NULL
          AND b.retired_at IS NULL
        LIMIT ?
    """, (limit,)).fetchall()

    for row in rows:
        conflicts.append({
            "type": "supersession_conflict",
            "memory_id_a": row["id_a"],
            "memory_id_b": row["id_b"],
            "content_a": row["content_a"][:120],
            "content_b": row["content_b"][:120],
            "confidence_delta": abs(row["conf_a"] - row["conf_b"]),
            "agent_id": row["agent_id"],
            "scope": row["scope"],
        })

    # Strategy 2: Same scope, category, agent — FTS negation pattern matching
    rows2 = conn.execute("""
        SELECT a.id as id_a, b.id as id_b,
               a.content as content_a, b.content as content_b,
               a.confidence as conf_a, b.confidence as conf_b,
               a.agent_id, a.category, a.scope
        FROM memories a
        JOIN memories b ON a.id < b.id
            AND a.category = b.category
            AND a.scope = b.scope
            AND a.agent_id = b.agent_id
            AND a.retired_at IS NULL
            AND b.retired_at IS NULL
            AND a.confidence >= ?
            AND b.confidence >= ?
        LIMIT ?
    """, (min_confidence, min_confidence, limit * 3)).fetchall()

    for row in rows2:
        ca, cb = row["content_a"].lower(), row["content_b"].lower()
        for pos_pattern, neg_pattern in NEGATION_PATTERNS:
            # Check if A has X and B has NOT-X or vice versa
            a_pos = bool(re.search(pos_pattern, ca))
            b_neg = bool(re.search(neg_pattern, cb))
            b_pos = bool(re.search(pos_pattern, cb))
            a_neg = bool(re.search(neg_pattern, ca))

            if (a_pos and b_neg) or (b_pos and a_neg):
                conflicts.append({
                    "type": "negation_conflict",
                    "memory_id_a": row["id_a"],
                    "memory_id_b": row["id_b"],
                    "content_a": row["content_a"][:120],
                    "content_b": row["content_b"][:120],
                    "confidence_delta": abs(row["conf_a"] - row["conf_b"]),
                    "pattern": f"{pos_pattern} / {neg_pattern}",
                    "agent_id": row["agent_id"],
                    "scope": row["scope"],
                })
                break

    # Deduplicate by (id_a, id_b)
    seen = set()
    unique = []
    for c in conflicts:
        key = (c["memory_id_a"], c["memory_id_b"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique[:limit]


def flag_contradiction(
    conn: sqlite3.Connection,
    memory_id_a: int,
    memory_id_b: int,
    conflict_type: str,
    agent_id: str = CYCLE_AGENT_ID,
) -> int:
    """
    Write a contradiction_detected event and add a 'contradicts' edge.
    Returns event ID.
    """
    # Add bidirectional contradiction edge
    conn.execute("""
        INSERT OR REPLACE INTO knowledge_edges
            (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
        VALUES ('memories', ?, 'memories', ?, 'contradicts', 0.9, ?)
    """, (memory_id_a, memory_id_b, agent_id))
    conn.execute("""
        INSERT OR REPLACE INTO knowledge_edges
            (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
        VALUES ('memories', ?, 'memories', ?, 'contradicts', 0.9, ?)
    """, (memory_id_b, memory_id_a, agent_id))

    # Log event
    cur = conn.execute("""
        INSERT INTO events (agent_id, event_type, summary, metadata, importance, created_at)
        VALUES (?, 'contradiction_detected', ?, ?, 0.9, datetime('now'))
    """, (
        agent_id,
        f"Contradiction detected ({conflict_type}): memories {memory_id_a} vs {memory_id_b}",
        json.dumps({
            "memory_id_a": memory_id_a,
            "memory_id_b": memory_id_b,
            "conflict_type": conflict_type,
        }),
    ))
    return cur.lastrowid


def resolve_contradiction(
    conn: sqlite3.Connection,
    keep_id: int,
    retire_id: int,
    agent_id: str = CYCLE_AGENT_ID,
) -> None:
    """
    Resolve a contradiction by retiring the lower-confidence memory.
    Updates the supersedes chain.
    """
    conn.execute("""
        UPDATE memories SET supersedes_id = ?, updated_at = datetime('now')
        WHERE id = ?
    """, (retire_id, keep_id))

    conn.execute("""
        UPDATE memories SET retired_at = datetime('now'), updated_at = datetime('now')
        WHERE id = ?
    """, (retire_id,))

    conn.execute("""
        INSERT INTO events (agent_id, event_type, summary, metadata, importance, created_at)
        VALUES (?, 'contradiction_resolved', ?, ?, 0.7, datetime('now'))
    """, (
        agent_id,
        f"Resolved contradiction: kept {keep_id}, retired {retire_id}",
        json.dumps({"kept": keep_id, "retired": retire_id}),
    ))
    conn.commit()


def auto_resolve_contradictions(
    conn: sqlite3.Connection,
    contradictions: list[dict],
    dry_run: bool = False,
) -> int:
    """
    Auto-resolve contradictions by retiring the lower-confidence memory.
    Only resolves when confidence_delta > 0.3 (clear winner).
    Returns number resolved.
    """
    resolved = 0
    for c in contradictions:
        id_a, id_b = c["memory_id_a"], c["memory_id_b"]
        row_a = conn.execute("SELECT confidence FROM memories WHERE id = ?", (id_a,)).fetchone()
        row_b = conn.execute("SELECT confidence FROM memories WHERE id = ?", (id_b,)).fetchone()
        if not row_a or not row_b:
            continue

        delta = abs(row_a[0] - row_b[0])
        if delta < 0.3:
            # Too close to call — flag for human review
            flag_contradiction(conn, id_a, id_b, c["type"])
            continue

        keep = id_a if row_a[0] > row_b[0] else id_b
        retire = id_b if keep == id_a else id_a

        if not dry_run:
            resolve_contradiction(conn, keep, retire)
        resolved += 1

    return resolved


# ── Cross-scope detection (COS-233 / Wave 6) ─────────────────────────────────

# Entity patterns for cross-scope bridging
ENTITY_PATTERNS = [
    # Named agents
    r'\b(hermes|sentinel-2|hippocampus|engram|prune|recall|cortex|weaver|scribe|'
    r'legion|axiom|kernel|cipher|armor|probe|nexus|codex|tempo|lattice|aegis|'
    r'nara|openclaw|paperclip-\w+)\b',
    # Issue identifiers
    r'\b([A-Z]{2,5}-\d+)\b',
    # System components
    r'\b(brain\.db|brainctl|hippocampus\.py|coherence-check|vec_memories|'
    r'knowledge_edges|memories_fts|memory_trust_scores)\b',
    # Key system concepts
    r'\b(auth system|checkout|trust score|consolidation cycle|decay|'
    r'contradiction detection|embedding|FTS5|semantic search)\b',
    # Numeric claims about counts
    r'\b(\d+)\s+(agents?|memories|events?|issues?)\b',
]


def extract_entities(content: str) -> set:
    """Extract normalized entity tokens from memory content."""
    content_lower = content.lower()
    entities = set()
    for pattern in ENTITY_PATTERNS:
        for match in re.finditer(pattern, content_lower, re.IGNORECASE):
            entities.add(match.group(0).lower().strip())
    return entities


def scopes_are_related(scope_a: str, scope_b: str) -> bool:
    """
    Return True if two scopes are related enough to warrant cross-scope comparison.
    Global scope is related to everything. Same project family scopes are related.
    """
    if scope_a == "global" or scope_b == "global":
        return True
    # Extract top-level project name from 'project:name:sub' format
    parts_a = scope_a.split(":")
    parts_b = scope_b.split(":")
    project_a = parts_a[1] if len(parts_a) > 1 else scope_a
    project_b = parts_b[1] if len(parts_b) > 1 else scope_b
    return (
        project_a == project_b
        or project_a.startswith(project_b)
        or project_b.startswith(project_a)
    )


def find_entity_anchored_negation(
    content_a: str,
    content_b: str,
    shared_entities: set,
    negation_patterns: list,
) -> list:
    """
    Returns matched pattern descriptions if a negation pattern fires in
    entity-bearing sentences from both memories.
    """
    sentences_a = re.split(r"[.;]\s+", content_a.lower())
    sentences_b = re.split(r"[.;]\s+", content_b.lower())

    relevant_a = [s for s in sentences_a if any(e in s for e in shared_entities)]
    relevant_b = [s for s in sentences_b if any(e in s for e in shared_entities)]

    if not relevant_a or not relevant_b:
        return []

    found = []
    for sa in relevant_a:
        for sb in relevant_b:
            for pos_pattern, neg_pattern in negation_patterns:
                a_pos = bool(re.search(pos_pattern, sa))
                b_neg = bool(re.search(neg_pattern, sb))
                b_pos = bool(re.search(pos_pattern, sb))
                a_neg = bool(re.search(neg_pattern, sa))
                if (a_pos and b_neg) or (b_pos and a_neg):
                    found.append(f"{pos_pattern} / {neg_pattern}")
    return found


def classify_resolution(conflict: dict, conn: sqlite3.Connection) -> dict:
    """
    Add resolution and recommended_action fields to a conflict dict.
    Checks for temporal sequencing (supersedes chain) and trust-delta resolution.
    """
    id_a, id_b = conflict["memory_id_a"], conflict["memory_id_b"]

    # Check if one memory supersedes the other — temporal sequence, not contradiction
    supersedes = conn.execute("""
        SELECT 1 FROM memories
        WHERE id IN (?, ?) AND supersedes_id IN (?, ?)
    """, (id_a, id_b, id_a, id_b)).fetchone()
    if supersedes:
        conflict["resolution"] = "temporal_sequence"
        conflict["recommended_action"] = "Linked via supersedes chain — not a real conflict."
        return conflict

    # Check if trust_delta is decisive
    trust_delta = conflict.get("trust_delta", 0.0)
    if trust_delta > 0.15:
        row_a = conn.execute("SELECT trust_score FROM memories WHERE id = ?", (id_a,)).fetchone()
        row_b = conn.execute("SELECT trust_score FROM memories WHERE id = ?", (id_b,)).fetchone()
        ts_a = row_a[0] if row_a else 1.0
        ts_b = row_b[0] if row_b else 1.0
        winner = id_a if ts_a >= ts_b else id_b
        conflict["resolution"] = "auto_resolvable"
        conflict["recommended_action"] = (
            f"Memory {winner} has decisively higher trust (delta={trust_delta:.2f}). "
            "Consider retiring the lower-trust memory."
        )
        return conflict

    # Default: flag for review
    scopes_str = f'{conflict.get("scope_a", "?")} ↔ {conflict.get("scope_b", "?")}'
    entities_str = ", ".join(conflict.get("shared_entities", [])[:3])
    conflict["resolution"] = "flag_for_review"
    conflict["recommended_action"] = (
        f"Cross-scope conflict ({scopes_str}) on entities: [{entities_str}]. "
        "Manual review required."
    )
    return conflict


def find_cross_scope_contradictions(
    conn: sqlite3.Connection,
    limit: int = 50,
    min_confidence: float = 0.3,
) -> list:
    """
    Detect contradictions between memories in DIFFERENT scopes (COS-233).

    Only compares memories whose scopes are related (same project family or global),
    have at least one shared entity, and show a negation pattern anchored to that entity.

    Returns conflict dicts compatible with find_contradictions() output, with additional
    fields: scope_a, scope_b, agent_id_a, agent_id_b, shared_entities, trust_delta, resolution.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, agent_id, category, scope, content, confidence, trust_score
        FROM memories
        WHERE retired_at IS NULL
          AND confidence >= ?
        ORDER BY scope, created_at DESC
    """, (min_confidence,)).fetchall()

    memories = [dict(r) for r in rows]

    # Pre-compute entity sets (cache for O(N) entity extraction)
    entity_cache = {m["id"]: extract_entities(m["content"]) for m in memories}

    conflicts = []
    seen = set()

    for i, m_a in enumerate(memories):
        for m_b in memories[i + 1:]:
            if m_a["scope"] == m_b["scope"]:
                continue  # same-scope — handled by find_contradictions()
            if not scopes_are_related(m_a["scope"], m_b["scope"]):
                continue  # unrelated scopes

            pair_key = (min(m_a["id"], m_b["id"]), max(m_a["id"], m_b["id"]))
            if pair_key in seen:
                continue
            seen.add(pair_key)

            shared = entity_cache[m_a["id"]] & entity_cache[m_b["id"]]
            if not shared:
                continue

            matched = find_entity_anchored_negation(
                m_a["content"], m_b["content"], shared, NEGATION_PATTERNS
            )
            if not matched:
                continue

            ts_a = m_a.get("trust_score") or 1.0
            ts_b = m_b.get("trust_score") or 1.0
            conflict = {
                "type": "cross_scope_conflict",
                "memory_id_a": m_a["id"],
                "memory_id_b": m_b["id"],
                "content_a": m_a["content"][:120],
                "content_b": m_b["content"][:120],
                "confidence_delta": abs(m_a["confidence"] - m_b["confidence"]),
                "trust_delta": abs(ts_a - ts_b),
                "pattern": matched[0],
                "shared_entities": list(shared)[:5],
                "agent_id_a": m_a["agent_id"],
                "agent_id_b": m_b["agent_id"],
                "scope_a": m_a["scope"],
                "scope_b": m_b["scope"],
            }
            classify_resolution(conflict, conn)
            conflicts.append(conflict)

            if len(conflicts) >= limit:
                return conflicts

    return conflicts


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Contradiction detection for brain.db")
    parser.add_argument("--cross-scope", action="store_true",
                        help="Run cross-scope contradiction scan (COS-233)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max conflicts to return per scan type")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.cross_scope:
        print("=== Cross-Scope Contradiction Scan ===")
        cs_conflicts = find_cross_scope_contradictions(conn, limit=args.limit)
        print(f"Found {len(cs_conflicts)} cross-scope contradictions:")
        for c in cs_conflicts[:10]:
            print(
                f"  [{c['type']}] mem {c['memory_id_a']} ({c['scope_a']}) vs "
                f"mem {c['memory_id_b']} ({c['scope_b']})"
            )
            print(f"    entities: {c['shared_entities'][:3]}")
            print(f"    resolution: {c['resolution']}")
            print(f"    action: {c['recommended_action']}")
    else:
        print("=== Within-Scope Contradiction Scan ===")
        conflicts = find_contradictions(conn, limit=args.limit)
        print(f"Found {len(conflicts)} within-scope contradictions:")
        for c in conflicts[:5]:
            print(
                f"  [{c['type']}] {c['memory_id_a']} vs {c['memory_id_b']}: "
                f"delta={c['confidence_delta']:.2f}"
            )

    conn.close()
