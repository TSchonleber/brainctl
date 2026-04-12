"""
Three-phase dream cycle (NREM / REM / Insight) + spreading-activation think
+ idle-trigger logic + standalone daemon mode.

Builds on existing primitives in hippocampus.py and _impl.py:
- run_hebbian_pass        (NREM edge strengthening)
- experience_replay       (NREM replay of high-recall memories)
- run_dream_pass          (REM bisociation across scopes)
- spreading_activation    (graph traversal from seed)
- _graph_communities      (label-propagation community detection)
- _graph_betweenness      (Brandes centrality for bridge nodes)

The Insight phase is the new contribution: it identifies high-betweenness nodes
that sit between communities and writes new abstract memories describing those
bridges. The REM phase additionally targets isolated memories (zero edges) and
proposes bridge connections to their nearest semantic neighbors.

CLI:
    brainctl-consolidate dream-cycle [--phase nrem|rem|insight|all]
    brainctl-consolidate dream-daemon [--idle 300] [--memory-threshold 50]
                                      [--poll 60] [--phase all]
"""

from __future__ import annotations

import json
import signal
import sqlite3
import struct
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------

DEFAULT_IDLE_SECONDS = 300          # 5 min idle → trigger
DEFAULT_MEMORY_THRESHOLD = 50       # 50 new memories → trigger
DEFAULT_POLL_SECONDS = 60           # daemon poll interval
DEFAULT_INSIGHT_TOP_N_CANDIDATES = 30
DEFAULT_INSIGHT_MAX_PER_RUN = 5
DEFAULT_REM_ISOLATED_LIMIT = 25
DEFAULT_REM_BRIDGE_THRESHOLD = 0.65
LAST_CYCLE_KEY = "last_dream_cycle_at"
LAST_CYCLE_AGENT = "hippocampus"


def _now_sql() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _ensure_agent(db: sqlite3.Connection, agent_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES (?, ?, 'system', 'active', ?, ?)",
        (agent_id, agent_id, _now_sql(), _now_sql()),
    )


# ============================================================================
# Idle trigger
# ============================================================================

def should_run_dream_cycle(
    db: sqlite3.Connection,
    idle_seconds: int = DEFAULT_IDLE_SECONDS,
    memory_threshold: int = DEFAULT_MEMORY_THRESHOLD,
) -> Dict[str, Any]:
    """Decide whether a dream cycle should fire.

    Returns {
        should_run: bool,
        reason: str,
        idle_seconds: float | None,
        new_memories_since_last: int,
        last_cycle_at: str | None,
    }
    """
    row = db.execute(
        "SELECT value, updated_at FROM agent_state WHERE agent_id=? AND key=?",
        (LAST_CYCLE_AGENT, LAST_CYCLE_KEY),
    ).fetchone()
    last_cycle_at = row["value"].strip('"') if row else None

    last_event_row = db.execute(
        "SELECT max(created_at) AS last FROM events"
    ).fetchone()
    last_event_at = last_event_row["last"] if last_event_row else None

    idle_secs: Optional[float] = None
    if last_event_at:
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(last_event_at[:19], fmt)
                break
            except ValueError:
                continue
        if dt is not None:
            raw = (datetime.now() - dt).total_seconds()
            # Future-dated events (timezone skew) are clamped to 0 idle
            # so we don't underflow into a negative-idle "always block" state.
            idle_secs = max(0.0, raw)

    if last_cycle_at:
        new_mem_row = db.execute(
            "SELECT count(*) AS cnt FROM memories "
            "WHERE retired_at IS NULL AND created_at > ?",
            (last_cycle_at,),
        ).fetchone()
    else:
        new_mem_row = db.execute(
            "SELECT count(*) AS cnt FROM memories WHERE retired_at IS NULL"
        ).fetchone()
    new_memories = int(new_mem_row["cnt"] if new_mem_row else 0)

    should_run = False
    reason = "no_trigger"
    if idle_secs is not None and idle_secs >= idle_seconds:
        should_run = True
        reason = f"idle_{int(idle_secs)}s>={idle_seconds}s"
    elif new_memories >= memory_threshold:
        should_run = True
        reason = f"new_memories_{new_memories}>={memory_threshold}"

    return {
        "should_run": should_run,
        "reason": reason,
        "idle_seconds": idle_secs,
        "new_memories_since_last": new_memories,
        "last_cycle_at": last_cycle_at,
    }


def mark_dream_cycle_complete(db: sqlite3.Connection) -> None:
    """Persist the timestamp of the just-finished dream cycle."""
    _ensure_agent(db, LAST_CYCLE_AGENT)
    now = _now_sql()
    db.execute(
        "INSERT INTO agent_state (agent_id, key, value, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(agent_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (LAST_CYCLE_AGENT, LAST_CYCLE_KEY, json.dumps(now), now),
    )
    db.commit()


# ============================================================================
# Spreading activation — brain.think() backend
# ============================================================================

def think_from_query(
    db: sqlite3.Connection,
    query: str,
    seed_limit: int = 5,
    hops: int = 2,
    decay: float = 0.6,
    top_k: int = 20,
) -> Dict[str, Any]:
    """Run spreading activation from memories matching `query`.

    1. FTS5 search for `query` to get seed memory IDs.
    2. Spread activation across knowledge_edges from those seeds.
    3. Hydrate results with memory content.

    Returns {ok, query, seeds, activated, hops, decay}.
    """
    from agentmemory._impl import spreading_activation  # avoid circular

    if not query or not query.strip():
        return {"ok": False, "error": "empty query"}

    seeds: List[Tuple[str, int]] = []
    seed_meta: List[Dict[str, Any]] = []
    try:
        rows = db.execute(
            "SELECT m.id, m.content, m.category, m.confidence "
            "FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
            "WHERE memories_fts MATCH ? AND m.retired_at IS NULL "
            "ORDER BY fts.rank LIMIT ?",
            (query, seed_limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute(
            "SELECT id, content, category, confidence FROM memories "
            "WHERE content LIKE ? AND retired_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", seed_limit),
        ).fetchall()

    for r in rows:
        seeds.append(("memories", int(r["id"])))
        seed_meta.append({
            "id": int(r["id"]),
            "content": (r["content"] or "")[:160],
            "category": r["category"],
            "confidence": r["confidence"],
        })

    if not seeds:
        return {"ok": True, "query": query, "seeds": [], "activated": [], "note": "no seed memories matched"}

    activated_raw = spreading_activation(db, seeds, hops=hops, decay=decay, top_k=top_k)

    # Hydrate memory rows
    hydrated: List[Dict[str, Any]] = []
    for item in activated_raw:
        if item["table"] != "memories":
            hydrated.append({
                "table": item["table"],
                "id": item["id"],
                "activation": round(item["activation"], 4),
            })
            continue
        row = db.execute(
            "SELECT id, content, category, confidence, scope FROM memories WHERE id = ? AND retired_at IS NULL",
            (item["id"],),
        ).fetchone()
        if not row:
            continue
        hydrated.append({
            "table": "memories",
            "id": int(row["id"]),
            "content": (row["content"] or "")[:240],
            "category": row["category"],
            "confidence": row["confidence"],
            "scope": row["scope"],
            "activation": round(item["activation"], 4),
        })

    return {
        "ok": True,
        "query": query,
        "seeds": seed_meta,
        "activated": hydrated,
        "hops": hops,
        "decay": decay,
    }


# ============================================================================
# NREM phase — replay + edge strengthening + dead-edge pruning
# ============================================================================

def run_nrem_phase(
    db: sqlite3.Connection,
    agent_id: str = "hippocampus",
    prune_weight_threshold: float = 0.05,
) -> Dict[str, Any]:
    """NREM: replay recent high-recall memories, strengthen co-active edges,
    weaken stale ones, prune dead ones.

    Wraps existing experience_replay + run_hebbian_pass, then prunes any
    knowledge_edge whose weight has decayed below `prune_weight_threshold`
    (these are functionally dead and just bloat the graph).
    """
    from agentmemory.hippocampus import experience_replay, run_hebbian_pass

    stats: Dict[str, Any] = {"phase": "nrem"}

    replay_stats = experience_replay(db, top_k=10)
    stats["replay"] = replay_stats

    hebbian_stats = run_hebbian_pass(db)
    stats["hebbian"] = hebbian_stats

    # Prune dead edges (weight < threshold AND no recent reinforcement)
    pruned_row = db.execute(
        "SELECT count(*) AS cnt FROM knowledge_edges WHERE weight < ?",
        (prune_weight_threshold,),
    ).fetchone()
    pruned_count = int(pruned_row["cnt"] if pruned_row else 0)
    if pruned_count > 0:
        db.execute(
            "DELETE FROM knowledge_edges WHERE weight < ?",
            (prune_weight_threshold,),
        )
        db.commit()
    stats["pruned_dead_edges"] = pruned_count
    stats["prune_threshold"] = prune_weight_threshold

    return stats


# ============================================================================
# REM phase — bisociation + isolated-memory bridge discovery
# ============================================================================

def _unpack_vector_safe(blob: bytes, dimensions: int = 768) -> Optional[List[float]]:
    try:
        return list(struct.unpack(f"{dimensions}f", blob))
    except struct.error:
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def run_rem_phase(
    db: sqlite3.Connection,
    agent_id: str = "hippocampus",
    isolated_limit: int = DEFAULT_REM_ISOLATED_LIMIT,
    bridge_threshold: float = DEFAULT_REM_BRIDGE_THRESHOLD,
) -> Dict[str, Any]:
    """REM: cross-scope bisociation (existing dream pass) + isolated-node
    bridge discovery (new).

    Isolated-node bridge discovery:
    1. Find memories with zero knowledge_edges.
    2. For each, embed-search for the nearest connected memory.
    3. If similarity >= bridge_threshold, write a low-weight bridge edge.
    """
    from agentmemory.hippocampus import run_dream_pass

    stats: Dict[str, Any] = {"phase": "rem"}

    dream_stats = run_dream_pass(db, agent_id=agent_id)
    stats["bisociation"] = dream_stats

    # Isolated-node bridge discovery
    bridges_created = 0
    isolated_scanned = 0
    skipped_no_embedding = 0
    now = _now_sql()

    isolated_rows = db.execute(
        """
        SELECT m.id, m.content, m.scope, e.vector, e.dimensions
        FROM memories m
        LEFT JOIN knowledge_edges ke
          ON (ke.source_table='memories' AND ke.source_id=m.id)
          OR (ke.target_table='memories' AND ke.target_id=m.id)
        LEFT JOIN embeddings e
          ON e.source_table='memories' AND e.source_id=m.id
        WHERE m.retired_at IS NULL
          AND m.category != 'hypothesis'
          AND ke.id IS NULL
        GROUP BY m.id
        ORDER BY m.created_at DESC
        LIMIT ?
        """,
        (isolated_limit,),
    ).fetchall()

    if isolated_rows:
        connected_rows = db.execute(
            """
            SELECT DISTINCT m.id, m.content, m.scope, e.vector, e.dimensions
            FROM memories m
            JOIN knowledge_edges ke
              ON (ke.source_table='memories' AND ke.source_id=m.id)
              OR (ke.target_table='memories' AND ke.target_id=m.id)
            JOIN embeddings e
              ON e.source_table='memories' AND e.source_id=m.id
            WHERE m.retired_at IS NULL
            LIMIT 500
            """
        ).fetchall()

        connected_vecs: List[Tuple[int, str, List[float]]] = []
        for cr in connected_rows:
            if cr["vector"] is None:
                continue
            v = _unpack_vector_safe(bytes(cr["vector"]), cr["dimensions"] or 768)
            if v is None:
                continue
            connected_vecs.append((int(cr["id"]), cr["scope"], v))

        for ir in isolated_rows:
            isolated_scanned += 1
            if ir["vector"] is None:
                skipped_no_embedding += 1
                continue
            iso_vec = _unpack_vector_safe(bytes(ir["vector"]), ir["dimensions"] or 768)
            if iso_vec is None:
                skipped_no_embedding += 1
                continue
            iso_id = int(ir["id"])

            best_sim = 0.0
            best_target: Optional[int] = None
            for tgt_id, _scope, tgt_vec in connected_vecs:
                if tgt_id == iso_id:
                    continue
                sim = _cosine(iso_vec, tgt_vec)
                if sim > best_sim:
                    best_sim = sim
                    best_target = tgt_id

            if best_target is None or best_sim < bridge_threshold:
                continue

            a, b = (min(iso_id, best_target), max(iso_id, best_target))
            existing = db.execute(
                "SELECT id FROM knowledge_edges "
                "WHERE source_table='memories' AND target_table='memories' "
                "AND ((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))",
                (a, b, b, a),
            ).fetchone()
            if existing:
                continue

            db.execute(
                "INSERT INTO knowledge_edges "
                "(source_table, source_id, target_table, target_id, relation_type, "
                "weight, source, created_at, updated_at) "
                "VALUES ('memories', ?, 'memories', ?, 'rem_bridge', ?, 'rem_phase', ?, ?)",
                (a, b, round(best_sim, 4), now, now),
            )
            bridges_created += 1

        db.commit()

    stats["isolated_bridge_discovery"] = {
        "isolated_scanned": isolated_scanned,
        "bridges_created": bridges_created,
        "skipped_no_embedding": skipped_no_embedding,
        "bridge_threshold": bridge_threshold,
    }

    return stats


# ============================================================================
# Insight phase — community detection + bridge-node insight memories
# ============================================================================

def run_insight_phase(
    db: sqlite3.Connection,
    agent_id: str = "hippocampus",
    top_n_candidates: int = DEFAULT_INSIGHT_TOP_N_CANDIDATES,
    max_insights: int = DEFAULT_INSIGHT_MAX_PER_RUN,
) -> Dict[str, Any]:
    """Insight: find high-betweenness nodes that bridge communities, write
    them out as new abstract insight memories.

    Insight memories are written with category='lesson' and tagged
    ["dream","insight","community_bridge"] so they parallel the existing
    dream-pass hypothesis pattern but live in a recallable category.
    """
    from agentmemory._impl import _graph_communities, _graph_betweenness

    stats: Dict[str, Any] = {
        "phase": "insight",
        "communities_found": 0,
        "bridge_nodes_examined": 0,
        "insights_created": 0,
        "skipped_existing": 0,
    }

    # _graph_communities and _graph_betweenness cache results in agent_state
    # under agent_id='graph-weaver' — make sure that agent row exists.
    _ensure_agent(db, "graph-weaver")
    db.commit()

    try:
        communities = _graph_communities(db, force=True)
    except Exception as exc:
        stats["error"] = f"community_detection_failed: {exc}"
        return stats

    if not communities:
        return stats

    unique_comms = set(communities.values())
    stats["communities_found"] = len(unique_comms)
    if len(unique_comms) < 2:
        stats["note"] = "fewer than 2 communities — no bridges to find"
        return stats

    try:
        betweenness = _graph_betweenness(db, force=True)
    except Exception as exc:
        stats["error"] = f"betweenness_failed: {exc}"
        return stats

    # Rank candidate bridge nodes by betweenness, restricted to memories.
    # Take the top-N by score (robust across graph sizes — fixed thresholds
    # don't generalize because Brandes normalization scales with graph size).
    candidates = sorted(
        [(node, score) for node, score in betweenness.items() if node[0] == "memories" and score > 0],
        key=lambda x: -x[1],
    )[:top_n_candidates]
    stats["bridge_nodes_examined"] = len(candidates)

    now = _now_sql()
    insights_written = 0

    for (table, mem_id), bscore in candidates:
        if insights_written >= max_insights:
            break

        # Find which communities this node connects via its neighbors
        neighbor_rows = db.execute(
            """
            SELECT target_table, target_id FROM knowledge_edges
              WHERE source_table=? AND source_id=?
            UNION
            SELECT source_table, source_id FROM knowledge_edges
              WHERE target_table=? AND target_id=?
            """,
            (table, mem_id, table, mem_id),
        ).fetchall()

        neighbor_communities: Dict[int, List[Tuple[str, int]]] = {}
        for nr in neighbor_rows:
            key = (nr[0], int(nr[1]))
            comm = communities.get(key)
            if comm is None:
                continue
            neighbor_communities.setdefault(comm, []).append(key)

        if len(neighbor_communities) < 2:
            continue  # not actually bridging

        # Get content of the bridge memory + a sample neighbor from each community
        bridge_row = db.execute(
            "SELECT content, scope FROM memories WHERE id=? AND retired_at IS NULL",
            (mem_id,),
        ).fetchone()
        if not bridge_row:
            continue

        sample_descriptions: List[str] = []
        for comm_id, members in list(neighbor_communities.items())[:3]:
            sample_key = members[0]
            if sample_key[0] == "memories":
                sample_row = db.execute(
                    "SELECT content, scope FROM memories WHERE id=? AND retired_at IS NULL",
                    (sample_key[1],),
                ).fetchone()
                if sample_row:
                    snippet = (sample_row["content"] or "")[:80].replace("\n", " ")
                    sample_descriptions.append(f"community#{comm_id} [{sample_row['scope']}]: {snippet}")
            else:
                sample_descriptions.append(f"community#{comm_id} [{sample_key[0]}#{sample_key[1]}]")

        bridge_snippet = (bridge_row["content"] or "")[:160].replace("\n", " ")
        insight_content = (
            f"Bridge insight: memory#{mem_id} [{bridge_row['scope']}] "
            f'\"{bridge_snippet}\" connects {len(neighbor_communities)} communities. '
            f"Samples: {' | '.join(sample_descriptions)}"
        )

        # Avoid writing duplicate insights for the same bridge node
        existing = db.execute(
            "SELECT id FROM memories WHERE retired_at IS NULL "
            "AND content LIKE ? AND category='lesson'",
            (f"Bridge insight: memory#{mem_id} %",),
        ).fetchone()
        if existing:
            stats["skipped_existing"] += 1
            continue

        _ensure_agent(db, agent_id)
        db.execute(
            """
            INSERT INTO memories
              (agent_id, category, scope, content, confidence, temporal_class, memory_type,
               tags, created_at, updated_at)
            VALUES (?, 'lesson', 'global', ?, ?, 'long', 'semantic',
                    '["dream","insight","community_bridge"]', ?, ?)
            """,
            (agent_id, insight_content, round(min(0.5 + bscore * 10, 0.9), 3), now, now),
        )
        insights_written += 1

    db.commit()
    stats["insights_created"] = insights_written
    return stats


# ============================================================================
# Orchestrator
# ============================================================================

def run_dream_cycle(
    db: sqlite3.Connection,
    agent_id: str = "hippocampus",
    phase: str = "all",
) -> Dict[str, Any]:
    """Run the three-phase dream cycle. `phase` may be 'nrem', 'rem',
    'insight', or 'all' (the default).
    """
    started = _now_sql()
    out: Dict[str, Any] = {"started_at": started, "phase": phase, "phases": {}}

    if phase in ("nrem", "all"):
        out["phases"]["nrem"] = run_nrem_phase(db, agent_id=agent_id)
    if phase in ("rem", "all"):
        out["phases"]["rem"] = run_rem_phase(db, agent_id=agent_id)
    if phase in ("insight", "all"):
        out["phases"]["insight"] = run_insight_phase(db, agent_id=agent_id)

    out["finished_at"] = _now_sql()
    mark_dream_cycle_complete(db)

    # Log a single summary event so the cycle is visible in the timeline
    _ensure_agent(db, agent_id)
    summary_bits: List[str] = []
    if "nrem" in out["phases"]:
        n = out["phases"]["nrem"]
        summary_bits.append(
            f"nrem(replayed={n.get('replay', {}).get('replayed', 0)},"
            f"hebb_strengthened={n.get('hebbian', {}).get('edges_strengthened', 0)},"
            f"pruned={n.get('pruned_dead_edges', 0)})"
        )
    if "rem" in out["phases"]:
        r = out["phases"]["rem"]
        summary_bits.append(
            f"rem(hyp={r.get('bisociation', {}).get('hypotheses_created', 0)},"
            f"bridges={r.get('isolated_bridge_discovery', {}).get('bridges_created', 0)})"
        )
    if "insight" in out["phases"]:
        i = out["phases"]["insight"]
        summary_bits.append(
            f"insight(communities={i.get('communities_found', 0)},"
            f"insights={i.get('insights_created', 0)})"
        )
    summary_text = f"Dream cycle ({phase}): " + " ".join(summary_bits)

    db.execute(
        "INSERT INTO events (agent_id, event_type, summary, detail, metadata, project, importance, created_at) "
        "VALUES (?, 'dream_cycle', ?, ?, ?, 'agentmemory', 0.7, ?)",
        (agent_id, summary_text, json.dumps(out, indent=2), json.dumps(out), _now_sql()),
    )
    db.commit()
    return out


# ============================================================================
# CLI handlers (registered in hippocampus.build_parser)
# ============================================================================

def cmd_dream_cycle(args) -> None:
    from agentmemory.hippocampus import get_db
    db = get_db()
    result = run_dream_cycle(db, agent_id=getattr(args, "agent", "hippocampus"),
                             phase=getattr(args, "phase", "all"))
    if not getattr(args, "quiet", False):
        print(json.dumps(result, indent=2))
    db.close()


def cmd_dream_daemon(args) -> None:
    """Long-lived background process that polls for trigger conditions
    and runs a dream cycle when they fire. Cleanly handles SIGTERM/SIGINT.
    """
    from agentmemory.hippocampus import get_db

    idle_seconds = int(getattr(args, "idle", DEFAULT_IDLE_SECONDS))
    memory_threshold = int(getattr(args, "memory_threshold", DEFAULT_MEMORY_THRESHOLD))
    poll_seconds = int(getattr(args, "poll", DEFAULT_POLL_SECONDS))
    phase = getattr(args, "phase", "all")
    agent_id = getattr(args, "agent", "hippocampus")
    once = bool(getattr(args, "once", False))

    stop = {"flag": False}

    def _handle_sig(_signum, _frame):
        stop["flag"] = True
        print("[dream-daemon] received signal, finishing current poll then exiting", file=sys.stderr)

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    print(
        f"[dream-daemon] starting "
        f"(idle={idle_seconds}s, memory_threshold={memory_threshold}, "
        f"poll={poll_seconds}s, phase={phase})",
        file=sys.stderr,
    )

    while not stop["flag"]:
        db = get_db()
        try:
            decision = should_run_dream_cycle(
                db, idle_seconds=idle_seconds, memory_threshold=memory_threshold
            )
            if decision["should_run"]:
                print(f"[dream-daemon] trigger fired: {decision['reason']}", file=sys.stderr)
                result = run_dream_cycle(db, agent_id=agent_id, phase=phase)
                # Compact one-line status to stdout for log harvesters
                print(json.dumps({
                    "tick_at": _now_sql(),
                    "trigger": decision["reason"],
                    "phases": list(result.get("phases", {}).keys()),
                }))
            else:
                # Heartbeat tick — quiet to stderr only
                print(
                    f"[dream-daemon] tick at {_now_sql()} — "
                    f"idle={decision['idle_seconds']}s, "
                    f"new_memories={decision['new_memories_since_last']}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[dream-daemon] error: {exc}", file=sys.stderr)
        finally:
            db.close()

        if once:
            break

        # Sleep in 1-second slices so SIGTERM is responsive
        slept = 0
        while slept < poll_seconds and not stop["flag"]:
            time.sleep(1)
            slept += 1

    print("[dream-daemon] exited cleanly", file=sys.stderr)
