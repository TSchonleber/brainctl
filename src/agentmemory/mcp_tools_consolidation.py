"""brainctl MCP tools — replay priority and reconsolidation window.

Neuroscience grounding:
- Sharp Wave Ripples (SWR) dynamically tag memories for consolidation based on salience,
  not static importance. replay_priority accumulates through use.
- Memory reconsolidation (Nader 2000, Ecker 2015): retrieval opens a ~20-min lability
  window during which the memory can be updated without creating a new trace.
  High prediction error at retrieval = strong reconsolidation trigger.
- Agent-scoped lability: only the agent that opened the window can reconsolidate,
  preventing race conditions in concurrent multi-agent access.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

_LABILITY_MINUTES = 20
_LABILITY_THRESHOLD = 0.35   # cosine distance above which lability opens
_HIGH_SALIENCE_THRESHOLD = 0.8  # salience score above which ripple_tags increments


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _is_labile(row: dict, agent_id: str) -> tuple[bool, str]:
    """Return (is_labile, reason) for a memory row."""
    labile_until = row.get("labile_until")
    labile_agent = row.get("labile_agent_id")

    if not labile_until:
        return False, "no lability window open"

    try:
        exp = datetime.fromisoformat(labile_until.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now > exp:
            return False, "lability window expired"
    except Exception:
        return False, "invalid labile_until timestamp"

    if labile_agent and labile_agent != agent_id:
        return False, f"lability opened by different agent ({labile_agent})"

    return True, "lability window active"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_replay_boost(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    delta: float = 0.5,
    scope: str | None = None,
    **kw,
) -> dict:
    """Manually boost the replay_priority of one or more memories.

    Use this to manually flag memories for priority consolidation — analogous to
    tagging a hippocampal trace for next-cycle Sharp Wave Ripple replay.

    Args:
        memory_id: Specific memory ID to boost (mutually exclusive with scope).
        delta: Amount to add to replay_priority (clamped to [0.01, 5.0]).
        scope: Boost all active memories in this scope (e.g. 'project:foo').
    """
    if memory_id is None and scope is None:
        return {"ok": False, "error": "Provide memory_id or scope"}
    if memory_id is not None and scope is not None:
        return {"ok": False, "error": "Provide memory_id or scope, not both"}

    delta = max(0.01, min(5.0, float(delta)))

    try:
        db = _db()
        if memory_id is not None:
            row = db.execute(
                "SELECT id, replay_priority, retired_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": f"Memory {memory_id} not found"}
            if row["retired_at"]:
                return {"ok": False, "error": f"Memory {memory_id} is retired"}
            db.execute(
                "UPDATE memories SET replay_priority = MIN(10.0, replay_priority + ?) WHERE id = ?",
                (delta, memory_id),
            )
            new_priority = min(10.0, (row["replay_priority"] or 0.0) + delta)
            db.commit()
            return {
                "ok": True,
                "memory_id": memory_id,
                "delta": delta,
                "new_replay_priority": round(new_priority, 4),
            }
        else:
            result = db.execute(
                "UPDATE memories SET replay_priority = MIN(10.0, replay_priority + ?) "
                "WHERE retired_at IS NULL AND scope = ?",
                (delta, scope),
            )
            affected = db.execute("SELECT changes()").fetchone()[0]
            db.commit()
            return {
                "ok": True,
                "scope": scope,
                "delta": delta,
                "affected": affected,
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_replay_queue(
    agent_id: str = "mcp-client",
    limit: int = 20,
    min_priority: float = 0.1,
    scope: str | None = None,
    **kw,
) -> dict:
    """Return memories sorted by replay_priority for consolidation scheduling.

    Returns candidates the consolidation scheduler should process next, ordered
    by accumulated replay priority (highest first). Mirrors the hippocampal
    scheduler's SWR-driven replay queue.

    Args:
        limit: Max memories to return (default 20, max 100).
        min_priority: Exclude memories below this threshold (default 0.1).
        scope: Filter to a specific scope (e.g. 'project:foo').
    """
    limit = max(1, min(100, int(limit)))
    min_priority = float(min_priority)

    try:
        db = _db()
        where_parts = ["retired_at IS NULL", "replay_priority >= ?"]
        params: list = [min_priority]
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        where = " AND ".join(where_parts)

        rows = db.execute(
            f"SELECT id, content, category, scope, confidence, "
            f"replay_priority, ripple_tags, recalled_count, created_at, last_recalled_at "
            f"FROM memories WHERE {where} "
            f"ORDER BY replay_priority DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["content_preview"] = (d.get("content") or "")[:100]
            items.append(d)

        return {
            "ok": True,
            "count": len(items),
            "min_priority": min_priority,
            "scope": scope,
            "queue": items,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_reconsolidation_check(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    **kw,
) -> dict:
    """Check whether a memory is currently in its reconsolidation (lability) window.

    Retrieval opens a lability window when the retrieval context diverges significantly
    from the stored memory content (prediction error > threshold). During this window,
    the memory can be updated via reconsolidate() without creating a new trace.

    Returns lability status, time remaining, and the prediction error that opened it.
    """
    if memory_id is None:
        return {"ok": False, "error": "memory_id is required"}

    try:
        db = _db()
        row = db.execute(
            "SELECT id, content, labile_until, labile_agent_id, retrieval_prediction_error, "
            "recalled_count, confidence FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found"}

        d = dict(row)
        labile, reason = _is_labile(d, agent_id)

        seconds_remaining = 0
        if labile and d.get("labile_until"):
            try:
                exp = datetime.fromisoformat(d["labile_until"].replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                seconds_remaining = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass

        return {
            "ok": True,
            "memory_id": memory_id,
            "labile": labile,
            "reason": reason,
            "labile_until": d.get("labile_until"),
            "labile_agent_id": d.get("labile_agent_id"),
            "retrieval_prediction_error": d.get("retrieval_prediction_error"),
            "seconds_remaining": seconds_remaining,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_reconsolidate(
    agent_id: str = "mcp-client",
    memory_id: int | None = None,
    new_content: str | None = None,
    merge_mode: str = "replace",
    **kw,
) -> dict:
    """Update a memory during its reconsolidation (lability) window.

    During the lability window opened by high-prediction-error retrieval, the memory
    can be updated without creating a new trace — it merges the new information into
    the existing memory, just as biological reconsolidation incorporates retrieved-plus-
    updated content back into long-term storage.

    Only the agent that opened the lability window can reconsolidate.
    After reconsolidation the window is closed (labile_until set to NULL).

    Args:
        memory_id: ID of the memory to update.
        new_content: Updated memory content.
        merge_mode: 'replace' (overwrite content) or 'append' (append to existing).
    """
    if memory_id is None:
        return {"ok": False, "error": "memory_id is required"}
    if not new_content or not new_content.strip():
        return {"ok": False, "error": "new_content is required"}
    if merge_mode not in ("replace", "append"):
        return {"ok": False, "error": "merge_mode must be 'replace' or 'append'"}

    try:
        db = _db()
        row = db.execute(
            "SELECT id, content, labile_until, labile_agent_id, retrieval_prediction_error, "
            "retired_at FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Memory {memory_id} not found"}
        if row["retired_at"]:
            return {"ok": False, "error": f"Memory {memory_id} is retired"}

        d = dict(row)
        labile, reason = _is_labile(d, agent_id)
        if not labile:
            return {
                "ok": False,
                "error": f"Memory {memory_id} is not labile: {reason}",
                "labile_until": d.get("labile_until"),
                "hint": "Reconsolidation requires an open lability window. Retrieve the memory first.",
            }

        old_content = d["content"] or ""
        if merge_mode == "append":
            merged_content = f"{old_content}\n\n[Reconsolidated {_now()}] {new_content.strip()}"
        else:
            merged_content = new_content.strip()

        now = _now_sql()
        db.execute(
            "UPDATE memories SET content = ?, updated_at = ?, "
            "labile_until = NULL, labile_agent_id = NULL, retrieval_prediction_error = NULL "
            "WHERE id = ?",
            (merged_content, now, memory_id),
        )
        db.commit()

        return {
            "ok": True,
            "memory_id": memory_id,
            "merge_mode": merge_mode,
            "old_content_preview": old_content[:100],
            "new_content_preview": merged_content[:100],
            "prediction_error_was": d.get("retrieval_prediction_error"),
            "lability_closed": True,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_consolidation_run(
    agent_id: str = "mcp-client",
    limit: int = 50,
    min_priority: float = 0.1,
    scope: str | None = None,
    promote_threshold_ripple: int = 3,
    promote_threshold_confidence: float = 0.7,
    run_causal_mining: bool = True,
    run_transitive_inference: bool = True,
    **kw,
) -> dict:
    """Run a consolidation pass over the replay queue.

    Implements the SWR-driven offline consolidation cycle:
    1. Fetch top-N memories by replay_priority (the accumulated SWR tag score).
    2. Promote episodic→semantic for memories that have sufficient ripple endorsement
       and confidence (analogous to cortical transfer during slow-wave sleep).
    3. Optionally mine causal chains in the events table (writes direct_cause /
       transitive_cause edges to knowledge_edges).
    4. Zero out replay_priority for all processed memories, resetting the queue.

    Returns stats: processed, promoted, causal edges created/updated.

    Args:
        limit: Max memories to process per pass (default 50, max 500).
        min_priority: Ignore memories below this replay_priority (default 0.1).
        scope: Limit pass to a specific scope (e.g. 'project:foo').
        promote_threshold_ripple: Min ripple_tags for episodic→semantic promotion (default 3).
        promote_threshold_confidence: Min confidence for promotion (default 0.7).
        run_causal_mining: Whether to run mine_causal_chains after the memory pass (default True).
        run_transitive_inference: Whether to run 2-hop KG transitive inference (default True).
    """
    limit = max(1, min(500, int(limit)))
    min_priority = float(min_priority)
    promote_threshold_ripple = max(1, int(promote_threshold_ripple))
    promote_threshold_confidence = max(0.0, min(1.0, float(promote_threshold_confidence)))

    try:
        db = _db()

        # Step 1: Fetch replay queue
        where_parts = ["retired_at IS NULL", "replay_priority >= ?"]
        params: list = [min_priority]
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        where = " AND ".join(where_parts)

        rows = db.execute(
            f"SELECT id, memory_type, ripple_tags, confidence, replay_priority "
            f"FROM memories WHERE {where} ORDER BY replay_priority DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        processed_ids = [r["id"] for r in rows]

        # Step 2: Promote episodic → semantic
        promotion_ids: list[int] = []
        for r in rows:
            if (
                r["memory_type"] == "episodic"
                and (r["ripple_tags"] or 0) >= promote_threshold_ripple
                and (r["confidence"] or 0.0) >= promote_threshold_confidence
            ):
                promotion_ids.append(r["id"])

        if promotion_ids:
            now = _now_sql()
            for mid in promotion_ids:
                db.execute(
                    "UPDATE memories SET memory_type = 'semantic', updated_at = ? WHERE id = ?",
                    (now, mid),
                )

        # Step 3: Zero out replay_priority for all processed memories
        if processed_ids:
            placeholders = ",".join("?" * len(processed_ids))
            db.execute(
                f"UPDATE memories SET replay_priority = 0.0 WHERE id IN ({placeholders})",
                processed_ids,
            )

        db.commit()

        # Step 4: Mine causal chains
        causal_stats: dict = {"events_scanned": 0, "edges_created": 0, "edges_updated": 0}
        if run_causal_mining:
            from .hippocampus import mine_causal_chains
            causal_stats = mine_causal_chains(db)
            db.commit()

        # Step 5: Transitive KG inference (compositional replay, issue #7).
        # For each processed memory, find its entity relations, then compose 2-hop
        # paths A→B→C into derived edges A→(rel1+rel2)→C.
        # Score = confidence(A→B) × confidence(B→C). Prune if score < 0.3.
        # Store as knowledge_edges with source='compositional_replay'.
        transitive_stats: dict = {"paths_scanned": 0, "edges_derived": 0}
        if run_transitive_inference and processed_ids:
            try:
                now_ts = _now_sql()
                paths_scanned = 0
                edges_derived = 0

                # For each entity that appears in knowledge_edges, walk 2-hop paths
                # Extract entities linked to processed memories (via entity_relations or knowledge_edges)
                placeholder = ",".join("?" * len(processed_ids))
                linked_entities = db.execute(
                    f"SELECT DISTINCT entity_id FROM entity_relations WHERE memory_id IN ({placeholder})",
                    processed_ids,
                ).fetchall()
                linked_entity_ids = [r["entity_id"] if hasattr(r, "keys") else r[0] for r in linked_entities]

                if not linked_entity_ids:
                    # Fall back: check knowledge_edges for any entity with high centrality
                    linked_entity_ids = [
                        r["source_id"] if hasattr(r, "keys") else r[0]
                        for r in db.execute(
                            "SELECT DISTINCT source_id FROM knowledge_edges WHERE source_table='entities' LIMIT 20"
                        ).fetchall()
                    ]

                for entity_a in linked_entity_ids[:50]:  # cap at 50 to prevent explosion
                    # Get first-degree outgoing edges from A
                    ab_edges = db.execute(
                        "SELECT target_id, relation_type, confidence FROM knowledge_edges "
                        "WHERE source_table='entities' AND source_id=? AND target_table='entities' "
                        "AND confidence IS NOT NULL",
                        (entity_a,),
                    ).fetchall()

                    for ab in ab_edges:
                        entity_b = ab["target_id"] if hasattr(ab, "keys") else ab[1]
                        rel_ab = ab["relation_type"] if hasattr(ab, "keys") else ab[2]
                        conf_ab = float(ab["confidence"] if hasattr(ab, "keys") else ab[3] or 0.5)

                        # Get first-degree outgoing edges from B (excluding back to A)
                        bc_edges = db.execute(
                            "SELECT target_id, relation_type, confidence FROM knowledge_edges "
                            "WHERE source_table='entities' AND source_id=? AND target_table='entities' "
                            "AND target_id != ? AND confidence IS NOT NULL LIMIT 10",
                            (entity_b, entity_a),
                        ).fetchall()

                        for bc in bc_edges:
                            paths_scanned += 1
                            entity_c = bc["target_id"] if hasattr(bc, "keys") else bc[1]
                            rel_bc = bc["relation_type"] if hasattr(bc, "keys") else bc[2]
                            conf_bc = float(bc["confidence"] if hasattr(bc, "keys") else bc[3] or 0.5)

                            derived_score = round(conf_ab * conf_bc, 4)
                            if derived_score < 0.3:
                                continue  # prune low-confidence derived edges

                            # Check if this derived edge already exists (avoid duplicates)
                            existing = db.execute(
                                "SELECT id, confidence FROM knowledge_edges "
                                "WHERE source_table='entities' AND source_id=? "
                                "AND target_table='entities' AND target_id=? "
                                "AND source='compositional_replay'",
                                (entity_a, entity_c),
                            ).fetchone()

                            derived_rel = f"{rel_ab}+{rel_bc}"[:120] if rel_ab and rel_bc else "transitive"

                            if existing:
                                existing_conf = float(existing["confidence"] if hasattr(existing, "keys") else existing[1] or 0.0)
                                if derived_score > existing_conf:
                                    db.execute(
                                        "UPDATE knowledge_edges SET confidence=?, relation_type=?, updated_at=? WHERE id=?",
                                        (derived_score, derived_rel, now_ts,
                                         existing["id"] if hasattr(existing, "keys") else existing[0]),
                                    )
                                    edges_derived += 1
                            else:
                                db.execute(
                                    "INSERT INTO knowledge_edges "
                                    "(source_table, source_id, target_table, target_id, relation_type, "
                                    "confidence, source, created_at, updated_at) "
                                    "VALUES ('entities',?,'entities',?,?,?,?,?,?)",
                                    (entity_a, entity_c, derived_rel, derived_score,
                                     "compositional_replay", now_ts, now_ts),
                                )
                                edges_derived += 1

                db.commit()
                transitive_stats = {"paths_scanned": paths_scanned, "edges_derived": edges_derived}
            except Exception:
                pass

        db.close()

        return {
            "ok": True,
            "processed": len(processed_ids),
            "promoted_to_semantic": len(promotion_ids),
            "promotion_ids": promotion_ids[:20],
            "causal_mining": causal_stats,
            "transitive_inference": transitive_stats,
            "scope": scope,
            "min_priority": min_priority,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_consolidation_stats(
    agent_id: str = "mcp-client",
    scope: str | None = None,
    **kw,
) -> dict:
    """Return consolidation health stats: replay queue depth, labile memories, ripple events.

    Args:
        scope: Limit stats to a specific scope (e.g. 'project:foo').
    """
    try:
        db = _db()
        where_active = "retired_at IS NULL"
        params_base: list = []
        if scope:
            where_active += " AND scope = ?"
            params_base = [scope]

        total = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active}", params_base
        ).fetchone()[0]

        queued = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active} AND replay_priority >= 0.1",
            params_base,
        ).fetchone()[0]

        high_priority = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active} AND replay_priority >= 2.0",
            params_base,
        ).fetchone()[0]

        avg_priority = db.execute(
            f"SELECT AVG(replay_priority) FROM memories WHERE {where_active}", params_base
        ).fetchone()[0] or 0.0

        total_ripple = db.execute(
            f"SELECT SUM(ripple_tags) FROM memories WHERE {where_active}", params_base
        ).fetchone()[0] or 0

        now_sql = _now_sql()
        labile_count = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where_active} "
            f"AND labile_until IS NOT NULL AND labile_until > ?",
            params_base + [now_sql],
        ).fetchone()[0]

        return {
            "ok": True,
            "scope": scope,
            "total_active_memories": total,
            "queued_for_replay": queued,
            "high_priority_replay": high_priority,
            "avg_replay_priority": round(avg_priority, 4),
            "total_ripple_tags": total_ripple,
            "currently_labile": labile_count,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_dream_cycle(
    agent_id: str = "mcp-client",
    phase: str = "all",
    **kw,
) -> dict:
    """Run the three-phase dream cycle (NREM / REM / Insight).

    NREM replays high-recall memories, strengthens co-active edges, prunes
    dead ones. REM does cross-scope bisociation and proposes bridge edges
    from isolated memories. Insight runs community detection and writes new
    abstract bridge-insight memories from high-betweenness nodes.

    Args:
        phase: 'nrem', 'rem', 'insight', or 'all' (default 'all').
    """
    try:
        from agentmemory.dream import run_dream_cycle
        if phase not in ("nrem", "rem", "insight", "all"):
            return {"ok": False, "error": f"invalid phase: {phase}"}
        db = _db()
        result = run_dream_cycle(db, agent_id=agent_id, phase=phase)
        db.close()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_think(
    agent_id: str = "mcp-client",
    query: str = "",
    seed_limit: int = 5,
    hops: int = 2,
    decay: float = 0.6,
    top_k: int = 20,
    **kw,
) -> dict:
    """Spreading-activation recall — distinct from semantic search.

    Searches the FTS index for `query`, picks the top matches as seed
    memories, then traverses knowledge_edges outward with decaying
    activation. Use search() to find what you remember about a topic;
    use think() to find what your memory associates with that topic.

    Args:
        query: Text query used to pick seed memories.
        seed_limit: How many seed memories to pull from FTS (default 5).
        hops: Edge-traversal depth (default 2).
        decay: Per-hop activation decay multiplier (default 0.6).
        top_k: Maximum activated nodes to return (default 20).
    """
    try:
        from agentmemory.dream import think_from_query
        if not query or not query.strip():
            return {"ok": False, "error": "query is required"}
        db = _db()
        result = think_from_query(
            db, query, seed_limit=seed_limit, hops=hops, decay=decay, top_k=top_k
        )
        db.close()
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="replay_boost",
        description=(
            "Manually boost the replay_priority of a memory or all memories in a scope. "
            "Analogous to manually tagging a hippocampal trace for Sharp Wave Ripple replay. "
            "Higher replay_priority = earlier processing in the consolidation queue."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "memory_id": {
                    "type": "integer",
                    "description": "Specific memory ID to boost (mutually exclusive with scope)",
                },
                "delta": {
                    "type": "number",
                    "description": "Amount to add to replay_priority (clamped to [0.01, 5.0], default 0.5)",
                    "default": 0.5,
                },
                "scope": {
                    "type": "string",
                    "description": "Boost all active memories in this scope (e.g. 'project:foo')",
                },
            },
        },
    ),
    Tool(
        name="replay_queue",
        description=(
            "Return memories sorted by replay_priority for consolidation scheduling. "
            "Highest replay_priority = most urgent consolidation candidates. "
            "Mirrors the hippocampal SWR-driven replay queue. "
            "Use this before a consolidation pass to pick what to process."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "limit": {
                    "type": "integer",
                    "description": "Max memories to return (default 20, max 100)",
                    "default": 20,
                },
                "min_priority": {
                    "type": "number",
                    "description": "Exclude memories below this replay_priority (default 0.1)",
                    "default": 0.1,
                },
                "scope": {
                    "type": "string",
                    "description": "Filter to a specific scope (e.g. 'project:foo')",
                },
            },
        },
    ),
    Tool(
        name="reconsolidation_check",
        description=(
            "Check whether a memory is currently in its reconsolidation (lability) window. "
            "A lability window opens automatically when vsearch retrieves a memory with "
            "high prediction error (cosine distance > 0.35 from query). "
            "During the window, reconsolidate() can update the memory without creating a new trace."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "memory_id": {
                    "type": "integer",
                    "description": "Memory ID to check",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="reconsolidate",
        description=(
            "Update a memory during its active reconsolidation (lability) window. "
            "Only the agent that triggered lability (via vsearch) can reconsolidate. "
            "Closes the lability window after updating. "
            "merge_mode='replace' overwrites content; 'append' adds to existing. "
            "Returns error if the window is closed or belongs to a different agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "memory_id": {
                    "type": "integer",
                    "description": "ID of the labile memory to update",
                },
                "new_content": {
                    "type": "string",
                    "description": "Updated memory content",
                },
                "merge_mode": {
                    "type": "string",
                    "enum": ["replace", "append"],
                    "description": "How to merge: 'replace' overwrites, 'append' adds below existing",
                    "default": "replace",
                },
            },
            "required": ["memory_id", "new_content"],
        },
    ),
    Tool(
        name="consolidation_run",
        description=(
            "Run a consolidation pass over the replay queue. "
            "Fetches top-N memories by replay_priority, promotes eligible episodic→semantic "
            "(ripple_tags >= threshold and confidence >= threshold), mines causal chains in "
            "the events table, then zeros out processed replay_priority scores. "
            "Implements the SWR-driven offline consolidation cycle (cortical transfer analogue)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "limit": {
                    "type": "integer",
                    "description": "Max memories to process per pass (default 50, max 500)",
                    "default": 50,
                },
                "min_priority": {
                    "type": "number",
                    "description": "Ignore memories below this replay_priority (default 0.1)",
                    "default": 0.1,
                },
                "scope": {
                    "type": "string",
                    "description": "Limit pass to a specific scope (e.g. 'project:foo')",
                },
                "promote_threshold_ripple": {
                    "type": "integer",
                    "description": "Min ripple_tags for episodic→semantic promotion (default 3)",
                    "default": 3,
                },
                "promote_threshold_confidence": {
                    "type": "number",
                    "description": "Min confidence for episodic→semantic promotion (default 0.7)",
                    "default": 0.7,
                },
                "run_causal_mining": {
                    "type": "boolean",
                    "description": "Run causal chain mining pass after memory processing (default true)",
                    "default": True,
                },
            },
        },
    ),
    Tool(
        name="consolidation_stats",
        description=(
            "Return consolidation health: replay queue depth, labile memories, ripple event totals. "
            "Use this to monitor the state of memory consolidation for an agent or scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "scope": {
                    "type": "string",
                    "description": "Limit stats to this scope (optional)",
                },
            },
        },
    ),
    Tool(
        name="memory_calibration",
        description=(
            "Return per-category memory calibration diagnostics (agent metacognition). "
            "Surfaces confidence distribution, Brier-score calibration error "
            "(predicted confidence vs. actual recall engagement), staleness distribution, "
            "and coverage gaps. Use to diagnose where an agent's memory quality is weakest."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "scope": {
                    "type": "string",
                    "description": "Limit to a specific scope (e.g. 'project:foo')",
                },
                "staleness_days": {
                    "type": "integer",
                    "description": "Mark memories not updated in this many days as stale (default 30)",
                    "default": 30,
                },
            },
        },
    ),
    Tool(
        name="attention_snapshot",
        description=(
            "Synthesize an agent's current attention state from recent access_log and events. "
            "Returns top query topics, active project, focus score (0=scattered, 1=focused), "
            "and recent event summaries. Gives an agent visibility into its own recent "
            "attention pattern without requiring a separate attention_state table."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "window_minutes": {
                    "type": "integer",
                    "description": "Look-back window in minutes (default 30, max 1440)",
                    "default": 30,
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of top topics / events to return (default 5)",
                    "default": 5,
                },
            },
        },
    ),
    Tool(
        name="free_energy_check",
        description=(
            "Return an agent's epistemic drive and knowledge gap summary from agent_uncertainty_log. "
            "Epistemic drive = mean free_energy of unresolved gaps (high = strong curiosity signal). "
            "Pragmatic drive = resolution rate over past 7 days. "
            "Grounded in Friston (2010): free_energy = (1 - confidence) * importance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "limit": {
                    "type": "integer",
                    "description": "Max gap records to return (default 20)",
                    "default": 20,
                },
                "unresolved_only": {
                    "type": "boolean",
                    "description": "Return only unresolved gaps (default true)",
                    "default": True,
                },
            },
        },
    ),
    Tool(
        name="dream_cycle",
        description=(
            "Run the three-phase dream cycle (NREM / REM / Insight). "
            "NREM: replay high-recall memories, strengthen co-active edges, prune dead ones. "
            "REM: cross-scope bisociation + isolated-memory bridge discovery. "
            "Insight: community detection + abstract bridge-insight memories from high-betweenness nodes. "
            "Lighter and faster than consolidation_run; designed to be called on idle triggers or via the dream-daemon."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "phase": {
                    "type": "string",
                    "enum": ["nrem", "rem", "insight", "all"],
                    "description": "Run a single phase or all three (default 'all')",
                    "default": "all",
                },
            },
        },
    ),
    Tool(
        name="think",
        description=(
            "Spreading-activation recall — distinct from semantic search. "
            "Searches the FTS index for `query` to pick seed memories, then traverses "
            "knowledge_edges outward with decaying activation. Use search() to find what "
            "you remember about a topic; use think() to find what your memory associates "
            "with that topic. Based on Collins & Loftus (1975)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "mcp-client"},
                "query": {"type": "string", "description": "Text query used to pick seed memories"},
                "seed_limit": {
                    "type": "integer",
                    "description": "How many seed memories to pull from FTS (default 5)",
                    "default": 5,
                },
                "hops": {
                    "type": "integer",
                    "description": "Edge-traversal depth (default 2)",
                    "default": 2,
                },
                "decay": {
                    "type": "number",
                    "description": "Per-hop activation decay multiplier (default 0.6)",
                    "default": 0.6,
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max activated nodes to return (default 20)",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    ),
]

def tool_memory_calibration(
    agent_id: str = "mcp-client",
    scope: str | None = None,
    staleness_days: int = 30,
    **kw,
) -> dict:
    """Return per-category memory calibration diagnostics for an agent.

    Metacognition tool: surfaces confidence distribution, calibration error
    (predicted confidence vs. actual recall engagement), coverage gaps, and
    staleness distribution. Gives an agent visibility into where its own
    memory quality is weakest.

    Calibration error per memory = |confidence - recall_ratio| where
    recall_ratio = recalled_count / (recalled_count + 1). This proxy rewards
    memories that are frequently surfaced and penalises confident-but-never-recalled ones.

    Args:
        scope: Limit to a specific scope (e.g. 'project:foo').
        staleness_days: Mark memories not updated in this many days as stale (default 30).
    """
    staleness_days = max(1, int(staleness_days))

    try:
        db = _db()

        where_parts = ["retired_at IS NULL"]
        params: list = []
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        if agent_id and agent_id != "mcp-client":
            where_parts.append("agent_id = ?")
            params.append(agent_id)
        where = " AND ".join(where_parts)

        rows = db.execute(
            f"SELECT id, category, confidence, recalled_count, updated_at, memory_type "
            f"FROM memories WHERE {where}",
            params,
        ).fetchall()

        if not rows:
            return {
                "ok": True,
                "scope": scope,
                "agent_id": agent_id,
                "total_memories": 0,
                "categories": {},
                "overall": {},
            }

        from datetime import timedelta
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=staleness_days)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

        # Aggregate per category
        cat_data: dict = {}
        total_cal_err_sq = 0.0
        total = 0

        for r in rows:
            cat = r["category"] or "uncategorized"
            if cat not in cat_data:
                cat_data[cat] = {
                    "count": 0,
                    "confidence_sum": 0.0,
                    "confidence_min": 1.0,
                    "confidence_max": 0.0,
                    "cal_err_sq_sum": 0.0,
                    "stale_count": 0,
                    "episodic": 0,
                    "semantic": 0,
                }
            d = cat_data[cat]
            conf = r["confidence"] or 0.0
            recalled = r["recalled_count"] or 0
            # recall_ratio: sigmoid-ish proxy — 0 recalls → 0.0, many recalls → approaches 1.0
            recall_ratio = recalled / (recalled + 1)
            cal_err_sq = (conf - recall_ratio) ** 2

            d["count"] += 1
            d["confidence_sum"] += conf
            d["confidence_min"] = min(d["confidence_min"], conf)
            d["confidence_max"] = max(d["confidence_max"], conf)
            d["cal_err_sq_sum"] += cal_err_sq
            if r["updated_at"] and r["updated_at"] < stale_cutoff:
                d["stale_count"] += 1
            if r["memory_type"] == "semantic":
                d["semantic"] += 1
            else:
                d["episodic"] += 1

            total_cal_err_sq += cal_err_sq
            total += 1

        # Build summary per category
        categories = {}
        for cat, d in cat_data.items():
            n = d["count"]
            brier = round(d["cal_err_sq_sum"] / n, 4)
            categories[cat] = {
                "count": n,
                "avg_confidence": round(d["confidence_sum"] / n, 4),
                "confidence_min": round(d["confidence_min"], 4),
                "confidence_max": round(d["confidence_max"], 4),
                "brier_score": brier,
                "stale_count": d["stale_count"],
                "stale_pct": round(d["stale_count"] / n * 100, 1),
                "episodic": d["episodic"],
                "semantic": d["semantic"],
            }

        # Coverage gaps: categories with < 3 memories or brier > 0.15
        coverage_gaps = [
            cat for cat, c in categories.items()
            if c["count"] < 3 or c["brier_score"] > 0.15
        ]

        overall_brier = round(total_cal_err_sq / total, 4) if total else 0.0
        db.close()

        return {
            "ok": True,
            "scope": scope,
            "agent_id": agent_id,
            "total_memories": total,
            "staleness_threshold_days": staleness_days,
            "overall": {
                "brier_score": overall_brier,
                "calibration_quality": (
                    "good" if overall_brier < 0.05
                    else "fair" if overall_brier < 0.15
                    else "poor"
                ),
            },
            "categories": categories,
            "coverage_gaps": coverage_gaps,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_attention_snapshot(
    agent_id: str = "mcp-client",
    window_minutes: int = 30,
    top_n: int = 5,
    **kw,
) -> dict:
    """Synthesize an agent's current attention state from recent access_log and events.

    Aggregates the last N access_log entries and events to produce:
    - Top query topics (most frequent terms in recent searches)
    - Active project (most common project field in recent events)
    - Focus score (0.0 = scattered, 1.0 = highly focused on a single topic)
    - Recent event summary (last 5 event summaries)

    Focus score is computed as: 1 - (unique_query_terms / total_query_terms)
    — high repetition of the same query terms = high focus.

    Args:
        window_minutes: Look-back window in minutes (default 30, max 1440).
        top_n: Number of top topics / events to return (default 5).
    """
    window_minutes = max(1, min(1440, int(window_minutes)))
    top_n = max(1, min(50, int(top_n)))

    try:
        db = _db()

        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        # --- Recent searches ---
        search_rows = db.execute(
            "SELECT query, created_at FROM access_log "
            "WHERE agent_id = ? AND action = 'search' AND query IS NOT NULL "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 200",
            (agent_id, cutoff),
        ).fetchall()

        # Term frequency across query strings
        import re as _re
        term_freq: dict = {}
        all_terms: list[str] = []
        for r in search_rows:
            q = r["query"] or ""
            terms = [t.lower() for t in _re.split(r"\W+", q) if len(t) > 2]
            all_terms.extend(terms)
            for t in terms:
                term_freq[t] = term_freq.get(t, 0) + 1

        top_topics = sorted(term_freq.items(), key=lambda x: x[1], reverse=True)[:top_n]

        # Focus score: 1 - (unique / total), clamped to [0, 1]
        if all_terms:
            unique_ratio = len(set(all_terms)) / len(all_terms)
            focus_score = round(max(0.0, 1.0 - unique_ratio), 4)
        else:
            focus_score = 0.0

        # --- Recent events ---
        event_rows = db.execute(
            "SELECT summary, project, event_type, created_at FROM events "
            "WHERE agent_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 50",
            (agent_id, cutoff),
        ).fetchall()

        # Most common project
        project_freq: dict = {}
        for r in event_rows:
            p = r["project"]
            if p:
                project_freq[p] = project_freq.get(p, 0) + 1
        active_project = max(project_freq, key=lambda p: project_freq[p]) if project_freq else None

        recent_events = [
            {
                "summary": r["summary"],
                "project": r["project"],
                "event_type": r["event_type"],
                "created_at": r["created_at"],
            }
            for r in event_rows[:top_n]
        ]

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "window_minutes": window_minutes,
            "searches_found": len(search_rows),
            "events_found": len(event_rows),
            "top_topics": [{"term": t, "count": c} for t, c in top_topics],
            "focus_score": focus_score,
            "focus_label": (
                "focused" if focus_score >= 0.6
                else "moderate" if focus_score >= 0.3
                else "scattered"
            ),
            "active_project": active_project,
            "recent_events": recent_events,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_free_energy_check(
    agent_id: str = "mcp-client",
    limit: int = 20,
    unresolved_only: bool = True,
    **kw,
) -> dict:
    """Return a summary of an agent's epistemic drive and knowledge gaps.

    Reads agent_uncertainty_log to surface:
    - Active knowledge gaps (unresolved free_energy entries)
    - Epistemic drive: mean free_energy of unresolved gaps (high = strong curiosity signal)
    - Pragmatic drive: resolution rate over the past 7 days
    - Top unresolved domains / gap topics

    Grounded in Friston (2010): free_energy = (1 - confidence) * importance.
    Higher free_energy = stronger signal to explore/resolve the gap.

    Args:
        limit: Max unresolved gaps to return (default 20).
        unresolved_only: When True (default), return only gaps not yet resolved.
    """
    limit = max(1, min(200, int(limit)))

    try:
        db = _db()

        where_parts = ["agent_id = ?"]
        params: list = [agent_id]
        if unresolved_only:
            where_parts.append("resolved_at IS NULL")
        where = " AND ".join(where_parts)

        rows = db.execute(
            f"SELECT id, gap_topic, domain, free_energy, query, result_count, "
            f"avg_confidence, temporal_class, created_at, resolved_at, resolved_by "
            f"FROM agent_uncertainty_log WHERE {where} "
            f"ORDER BY free_energy DESC NULLS LAST LIMIT ?",
            params + [limit],
        ).fetchall()

        gaps = [dict(r) for r in rows]

        # Aggregate stats
        fe_values = [g["free_energy"] for g in gaps if g["free_energy"] is not None]
        epistemic_drive = round(sum(fe_values) / len(fe_values), 4) if fe_values else 0.0

        # Top domains
        domain_freq: dict = {}
        for g in gaps:
            d = g.get("domain") or "unknown"
            domain_freq[d] = domain_freq.get(d, 0) + 1
        top_domains = sorted(domain_freq.items(), key=lambda x: x[1], reverse=True)[:5]

        # Pragmatic drive: resolution rate in last 7 days
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        total_7d = db.execute(
            "SELECT COUNT(*) FROM agent_uncertainty_log WHERE agent_id = ? AND created_at >= ?",
            (agent_id, seven_days_ago),
        ).fetchone()[0]
        resolved_7d = db.execute(
            "SELECT COUNT(*) FROM agent_uncertainty_log "
            "WHERE agent_id = ? AND created_at >= ? AND resolved_at IS NOT NULL",
            (agent_id, seven_days_ago),
        ).fetchone()[0]
        pragmatic_drive = round(resolved_7d / total_7d, 4) if total_7d else 0.0

        total_unresolved = db.execute(
            "SELECT COUNT(*) FROM agent_uncertainty_log WHERE agent_id = ? AND resolved_at IS NULL",
            (agent_id,),
        ).fetchone()[0]

        db.close()

        return {
            "ok": True,
            "agent_id": agent_id,
            "unresolved_gaps": total_unresolved,
            "epistemic_drive": epistemic_drive,
            "epistemic_label": (
                "high" if epistemic_drive > 0.5
                else "moderate" if epistemic_drive > 0.2
                else "low"
            ),
            "pragmatic_drive": pragmatic_drive,
            "resolution_rate_7d": f"{resolved_7d}/{total_7d}",
            "top_domains": [{"domain": d, "count": c} for d, c in top_domains],
            "gaps": gaps,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


DISPATCH: dict = {
    "consolidation_run":       lambda agent_id=None, **kw: tool_consolidation_run(agent_id=agent_id or "mcp-client", **kw),
    "replay_boost":           lambda agent_id=None, **kw: tool_replay_boost(agent_id=agent_id or "mcp-client", **kw),
    "replay_queue":           lambda agent_id=None, **kw: tool_replay_queue(agent_id=agent_id or "mcp-client", **kw),
    "reconsolidation_check":  lambda agent_id=None, **kw: tool_reconsolidation_check(agent_id=agent_id or "mcp-client", **kw),
    "reconsolidate":          lambda agent_id=None, **kw: tool_reconsolidate(agent_id=agent_id or "mcp-client", **kw),
    "consolidation_stats":    lambda agent_id=None, **kw: tool_consolidation_stats(agent_id=agent_id or "mcp-client", **kw),
    "memory_calibration":     lambda agent_id=None, **kw: tool_memory_calibration(agent_id=agent_id or "mcp-client", **kw),
    "attention_snapshot":     lambda agent_id=None, **kw: tool_attention_snapshot(agent_id=agent_id or "mcp-client", **kw),
    "free_energy_check":      lambda agent_id=None, **kw: tool_free_energy_check(agent_id=agent_id or "mcp-client", **kw),
    "dream_cycle":            lambda agent_id=None, **kw: tool_dream_cycle(agent_id=agent_id or "mcp-client", **kw),
    "think":                  lambda agent_id=None, **kw: tool_think(agent_id=agent_id or "mcp-client", **kw),
}
