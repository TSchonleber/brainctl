"""
Knowledge Graph — Relational Structure on Flat Memory Tables
============================================================
Concept: brain.db has a `knowledge_edges` table that connects memories, events,
and context nodes. This module provides algorithms for:
  1. Building edges automatically from co-occurrence and semantic similarity
  2. Graph traversal for context expansion (fetch related memories)
  3. Pagerank-style importance scoring to surface high-value nodes
  4. Subgraph extraction for targeted context injection

Schema used:
  knowledge_edges(source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
  memories(id, content, category, confidence, temporal_class, ...)
"""

import sqlite3
from collections import defaultdict

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"

RELATION_TYPES = {
    "supports":     0.8,   # one memory supports/reinforces another
    "contradicts":  0.9,   # conflict — high weight for detection priority
    "derived_from": 0.7,   # episodic → semantic consolidation
    "co_referenced": 0.5,  # appeared together in same session
    "supersedes":   1.0,   # explicit version chain
}


# ── Edge Management ──────────────────────────────────────────────────────────

def add_edge(
    conn: sqlite3.Connection,
    source_table: str, source_id: int,
    target_table: str, target_id: int,
    relation_type: str,
    weight: float = 1.0,
    agent_id: str = None,
) -> None:
    """Insert or replace an edge. Uses upsert via ON CONFLICT."""
    conn.execute("""
        INSERT INTO knowledge_edges
            (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_table, source_id, target_table, target_id, relation_type)
        DO UPDATE SET weight = excluded.weight
    """, (source_table, source_id, target_table, target_id, relation_type, weight, agent_id))


def get_neighbors(
    conn: sqlite3.Connection,
    source_table: str,
    source_id: int,
    relation_types: list[str] = None,
    min_weight: float = 0.0,
    limit: int = 20,
) -> list[dict]:
    """Return all nodes connected to source, optionally filtered by relation type."""
    query = """
        SELECT target_table, target_id, relation_type, weight
        FROM knowledge_edges
        WHERE source_table = ? AND source_id = ?
          AND weight >= ?
    """
    params = [source_table, source_id, min_weight]

    if relation_types:
        placeholders = ",".join("?" * len(relation_types))
        query += f" AND relation_type IN ({placeholders})"
        params.extend(relation_types)

    query += " ORDER BY weight DESC LIMIT ?"
    params.append(limit)

    cur = conn.execute(query, params)
    return [dict(row) for row in cur.fetchall()]


# ── Graph Traversal ───────────────────────────────────────────────────────────

def expand_context(
    conn: sqlite3.Connection,
    seed_memory_ids: list[int],
    max_hops: int = 2,
    max_nodes: int = 50,
    min_confidence: float = 0.4,
) -> list[dict]:
    """
    BFS expansion from seed memories through knowledge_edges.
    Returns unique memory nodes reachable within max_hops.
    """
    conn.row_factory = sqlite3.Row
    visited = set()
    queue = [("memories", mid, 0) for mid in seed_memory_ids]
    result_ids = []

    while queue and len(result_ids) < max_nodes:
        table, node_id, hop = queue.pop(0)
        key = (table, node_id)
        if key in visited:
            continue
        visited.add(key)

        if table == "memories" and node_id not in seed_memory_ids:
            result_ids.append(node_id)

        if hop < max_hops:
            neighbors = get_neighbors(conn, table, node_id, min_weight=0.3)
            for n in neighbors:
                nkey = (n["target_table"], n["target_id"])
                if nkey not in visited:
                    queue.append((n["target_table"], n["target_id"], hop + 1))

    if not result_ids:
        return []

    placeholders = ",".join("?" * len(result_ids))
    cur = conn.execute(f"""
        SELECT id, content, category, confidence, temporal_class
        FROM memories
        WHERE id IN ({placeholders})
          AND retired_at IS NULL
          AND confidence >= ?
        ORDER BY confidence DESC
    """, result_ids + [min_confidence])

    return [dict(r) for r in cur.fetchall()]


# ── Importance Scoring (PageRank-lite) ────────────────────────────────────────

def compute_memory_pagerank(
    conn: sqlite3.Connection,
    damping: float = 0.85,
    iterations: int = 20,
) -> dict[int, float]:
    """
    Simplified PageRank over memory nodes in knowledge_edges.
    Returns {memory_id: score}.
    """
    cur = conn.execute("""
        SELECT source_id, target_id, weight
        FROM knowledge_edges
        WHERE source_table = 'memories' AND target_table = 'memories'
    """)
    edges = cur.fetchall()

    # Build adjacency
    out_links = defaultdict(list)
    nodes = set()
    for src, tgt, w in edges:
        out_links[src].append((tgt, w))
        nodes.add(src)
        nodes.add(tgt)

    n = len(nodes)
    if n == 0:
        return {}

    node_list = list(nodes)
    scores = {nid: 1.0 / n for nid in node_list}

    for _ in range(iterations):
        new_scores = {}
        for node in node_list:
            inbound_sum = 0.0
            for src in node_list:
                links = out_links.get(src, [])
                total_weight = sum(w for _, w in links)
                if total_weight == 0:
                    continue
                for tgt, w in links:
                    if tgt == node:
                        inbound_sum += scores[src] * (w / total_weight)
            new_scores[node] = (1 - damping) / n + damping * inbound_sum
        scores = new_scores

    return scores


# ── Auto-Edge from Co-Reference ───────────────────────────────────────────────

def build_co_reference_edges(
    conn: sqlite3.Connection,
    session_id: str,
    agent_id: str = None,
) -> int:
    """
    Find all memories recalled during a session and add co_referenced edges
    between them. Returns number of edges created.
    """
    cur = conn.execute("""
        SELECT target_id FROM access_log
        WHERE action = 'read' AND target_table = 'memories' AND agent_id = ?
        ORDER BY created_at
    """, (agent_id or "%",))

    # Fallback: use events referencing memories in this session
    cur2 = conn.execute("""
        SELECT DISTINCT json_each.value as mem_id
        FROM events, json_each(events.refs)
        WHERE events.session_id = ?
          AND json_each.value GLOB 'memories:*'
    """, (session_id,))

    memory_ids = [row[0] for row in cur.fetchall()]
    for row in cur2.fetchall():
        try:
            mid = int(row[0].split(":")[1])
            if mid not in memory_ids:
                memory_ids.append(mid)
        except (ValueError, IndexError):
            pass

    count = 0
    for i, a in enumerate(memory_ids):
        for b in memory_ids[i + 1:]:
            if a != b:
                add_edge(conn, "memories", a, "memories", b, "co_referenced", weight=0.4, agent_id=agent_id)
                add_edge(conn, "memories", b, "memories", a, "co_referenced", weight=0.4, agent_id=agent_id)
                count += 2

    conn.commit()
    return count


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    scores = compute_memory_pagerank(conn)
    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:10]
    print(f"Top 10 memory nodes by PageRank: {top}")
    conn.close()
