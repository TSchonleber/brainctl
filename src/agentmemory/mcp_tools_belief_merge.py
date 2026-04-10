"""brainctl MCP tools — CRDT-inspired belief merge and conflict resolution."""
from __future__ import annotations
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

_PROPAGATION_DECAY = 0.85


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _tom_tables_exist(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    return "agent_beliefs" in tables


def _log_access(conn, agent_id, action, target_table=None, target_id=None, query=None):
    """Best-effort access log write — silently ignored if table is missing."""
    try:
        conn.execute(
            "INSERT INTO access_log "
            "(agent_id, action, target_table, target_id, query, result_count, tokens_consumed) "
            "VALUES (?,?,?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query, None, None),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_belief_conflicts_scan(
    topic_filter: str | None = None,
    min_confidence: float = 0.3,
    limit: int = 50,
) -> dict:
    """Scan all beliefs and identify topics where agents hold conflicting beliefs."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        q = """
            SELECT topic, agent_id, id, belief_content, confidence, last_updated_at
            FROM agent_beliefs
            WHERE invalidated_at IS NULL
              AND confidence >= ?
        """
        params: list[Any] = [min_confidence]

        if topic_filter:
            q += " AND topic LIKE ?"
            params.append(f"%{topic_filter}%")

        q += " ORDER BY topic, agent_id"
        rows = conn.execute(q, params).fetchall()

        # Group by topic and find topics with multiple distinct belief contents
        topic_beliefs: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            topic_beliefs[row["topic"]].append(dict(row))

        conflicts = []
        for topic, beliefs in topic_beliefs.items():
            if len(beliefs) < 2:
                continue

            # Check if there are at least 2 distinct belief contents
            unique_contents = {b["belief_content"] for b in beliefs}
            if len(unique_contents) < 2:
                continue

            # Compute severity: "high" if the top two confidences are within 0.1 of each other
            confidences = sorted([b["confidence"] for b in beliefs], reverse=True)
            if len(confidences) >= 2:
                conf_gap = abs(confidences[0] - confidences[1])
                if conf_gap < 0.1:
                    severity = "high"
                elif conf_gap < 0.3:
                    severity = "medium"
                else:
                    severity = "low"
            else:
                severity = "low"

            conflicts.append({
                "topic": topic,
                "agents": [b["agent_id"] for b in beliefs],
                "beliefs": [
                    {
                        "id": b["id"],
                        "agent_id": b["agent_id"],
                        "belief_content": b["belief_content"],
                        "confidence": b["confidence"],
                        "last_updated_at": b["last_updated_at"],
                    }
                    for b in beliefs
                ],
                "severity": severity,
            })

            if len(conflicts) >= limit:
                break

        return {"ok": True, "conflict_count": len(conflicts), "conflicts": conflicts}
    finally:
        conn.close()


def tool_belief_merge(
    topic: str,
    strategy: str = "highest_confidence",
    agent_id: str = "mcp-client",
    dry_run: bool = True,
) -> dict:
    """Merge conflicting beliefs on a topic using a specified strategy."""
    valid_strategies = {"highest_confidence", "most_recent", "weighted_average", "human_review"}
    if strategy not in valid_strategies:
        return {
            "ok": False,
            "error": f"Unknown strategy '{strategy}'. Choose from: {', '.join(sorted(valid_strategies))}",
        }

    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        rows = conn.execute(
            """
            SELECT id, agent_id, belief_content, confidence, last_updated_at
            FROM agent_beliefs
            WHERE topic = ? AND invalidated_at IS NULL
            ORDER BY confidence DESC
            """,
            (topic,),
        ).fetchall()

        if len(rows) < 2:
            return {
                "ok": False,
                "error": f"Not enough active beliefs on topic '{topic}' to merge (found {len(rows)}).",
            }

        beliefs = [dict(r) for r in rows]
        now = _now()

        winner_belief_id: int | None = None
        merged_content: str = ""
        merged_confidence: float = 0.0
        invalidated_ids: list[int] = []

        if strategy == "highest_confidence":
            winner = beliefs[0]  # already sorted DESC by confidence
            winner_belief_id = winner["id"]
            merged_content = winner["belief_content"]
            merged_confidence = winner["confidence"]
            invalidated_ids = [b["id"] for b in beliefs if b["id"] != winner_belief_id]

        elif strategy == "most_recent":
            # Sort by last_updated_at DESC; handle None gracefully
            sorted_by_time = sorted(
                beliefs,
                key=lambda b: b["last_updated_at"] or "",
                reverse=True,
            )
            winner = sorted_by_time[0]
            winner_belief_id = winner["id"]
            merged_content = winner["belief_content"]
            merged_confidence = winner["confidence"]
            invalidated_ids = [b["id"] for b in beliefs if b["id"] != winner_belief_id]

        elif strategy == "weighted_average":
            total_conf = sum(b["confidence"] for b in beliefs)
            avg_conf = total_conf / len(beliefs) if beliefs else 0.0
            source_parts = [
                f"[{b['agent_id']} ({b['confidence']:.2f})]: {b['belief_content']}"
                for b in beliefs
            ]
            merged_content = "Merged belief from multiple sources — " + "; ".join(source_parts)
            merged_confidence = min(1.0, avg_conf)
            winner_belief_id = None
            invalidated_ids = [b["id"] for b in beliefs]

        elif strategy == "human_review":
            winner_belief_id = None
            merged_content = f"Flagged for human review on topic '{topic}'"
            merged_confidence = 0.0
            invalidated_ids = []  # don't invalidate — just mark as assumption + create conflict record

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "topic": topic,
                "strategy": strategy,
                "winner_belief_id": winner_belief_id,
                "merged_content": merged_content,
                "confidence": merged_confidence,
                "invalidated_ids": invalidated_ids,
            }

        # Apply the merge
        if strategy in ("highest_confidence", "most_recent"):
            # Invalidate losers
            for bid in invalidated_ids:
                conn.execute(
                    "UPDATE agent_beliefs SET invalidated_at=?, invalidation_reason=? WHERE id=?",
                    (now, f"Superseded by belief_merge strategy={strategy}", bid),
                )

        elif strategy == "weighted_average":
            # Invalidate all originals; insert a new merged belief under agent_id
            for bid in invalidated_ids:
                conn.execute(
                    "UPDATE agent_beliefs SET invalidated_at=?, invalidation_reason=? WHERE id=?",
                    (now, "Merged via belief_merge weighted_average", bid),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_beliefs
                  (agent_id, topic, belief_content, confidence, is_assumption,
                   last_updated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (agent_id, topic, merged_content, merged_confidence, now, now, now),
            )
            new_row = conn.execute(
                "SELECT id FROM agent_beliefs WHERE agent_id=? AND topic=? AND invalidated_at IS NULL",
                (agent_id, topic),
            ).fetchone()
            if new_row:
                winner_belief_id = new_row["id"]

        elif strategy == "human_review":
            # Mark all beliefs as assumptions
            conn.execute(
                "UPDATE agent_beliefs SET is_assumption=1, updated_at=? WHERE topic=? AND invalidated_at IS NULL",
                (now, topic),
            )
            # Create a belief_conflicts record for each pair
            for i in range(len(beliefs)):
                for j in range(i + 1, len(beliefs)):
                    ba = beliefs[i]
                    bb = beliefs[j]
                    conn.execute(
                        """
                        INSERT INTO belief_conflicts
                          (topic, agent_a_id, agent_b_id, belief_a, belief_b, conflict_type,
                           severity, detected_at, requires_supervisor_intervention)
                        VALUES (?, ?, ?, ?, ?, 'factual', 0.8, ?, 1)
                        """,
                        (topic, ba["agent_id"], bb["agent_id"],
                         ba["belief_content"], bb["belief_content"], now),
                    )

        conn.commit()
        _log_access(conn, agent_id, "belief_merge", "agent_beliefs", winner_belief_id, topic)
        conn.commit()

        return {
            "ok": True,
            "dry_run": False,
            "topic": topic,
            "strategy": strategy,
            "winner_belief_id": winner_belief_id,
            "merged_content": merged_content,
            "confidence": merged_confidence,
            "invalidated_ids": invalidated_ids,
        }
    finally:
        conn.close()


def tool_belief_propagate(
    source_agent_id: str,
    topic: str,
    min_shared_context_score: float = 0.5,
) -> dict:
    """Propagate a belief update from source agent to agents with shared context."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        # Get the source belief
        source_belief = conn.execute(
            """
            SELECT id, belief_content, confidence
            FROM agent_beliefs
            WHERE agent_id=? AND topic=? AND invalidated_at IS NULL
            """,
            (source_agent_id, topic),
        ).fetchone()

        if not source_belief:
            return {
                "ok": False,
                "error": f"No active belief found for agent '{source_agent_id}' on topic '{topic}'.",
            }

        source_belief_dict = dict(source_belief)
        original_confidence = source_belief_dict["confidence"]
        propagated_confidence = round(original_confidence * _PROPAGATION_DECAY, 6)

        # Find all other active agents
        all_agents = conn.execute(
            "SELECT id FROM agents WHERE id != ? AND status = 'active'",
            (source_agent_id,),
        ).fetchall()
        all_agent_ids = [r["id"] for r in all_agents]

        # Compute shared context score via workspace_acks overlap
        # Get set of broadcast_ids acknowledged by the source agent
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        has_workspace_acks = "workspace_acks" in tables

        source_broadcasts: set[int] = set()
        if has_workspace_acks:
            source_rows = conn.execute(
                "SELECT broadcast_id FROM workspace_acks WHERE agent_id=?",
                (source_agent_id,),
            ).fetchall()
            source_broadcasts = {r["broadcast_id"] for r in source_rows}

        propagated_to: list[str] = []
        now = _now()

        for other_agent_id in all_agent_ids:
            # Compute shared context score
            if has_workspace_acks and source_broadcasts:
                other_rows = conn.execute(
                    "SELECT broadcast_id FROM workspace_acks WHERE agent_id=?",
                    (other_agent_id,),
                ).fetchall()
                other_broadcasts = {r["broadcast_id"] for r in other_rows}

                union = source_broadcasts | other_broadcasts
                intersection = source_broadcasts & other_broadcasts
                score = len(intersection) / len(union) if union else 0.0
            else:
                # If no workspace_acks data, skip (score=0)
                score = 0.0

            if score < min_shared_context_score:
                continue

            # Upsert the belief for this recipient
            existing = conn.execute(
                "SELECT id, confidence FROM agent_beliefs WHERE agent_id=? AND topic=? AND invalidated_at IS NULL",
                (other_agent_id, topic),
            ).fetchone()

            if existing:
                # Only propagate if our version has higher confidence
                if propagated_confidence <= existing["confidence"]:
                    continue
                conn.execute(
                    """
                    UPDATE agent_beliefs
                    SET belief_content=?, confidence=?, last_updated_at=?, updated_at=?
                    WHERE agent_id=? AND topic=? AND invalidated_at IS NULL
                    """,
                    (source_belief_dict["belief_content"], propagated_confidence,
                     now, now, other_agent_id, topic),
                )
            else:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_beliefs
                      (agent_id, topic, belief_content, confidence, is_assumption,
                       last_updated_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (other_agent_id, topic, source_belief_dict["belief_content"],
                     propagated_confidence, now, now, now),
                )

            propagated_to.append(other_agent_id)

        conn.commit()
        _log_access(conn, source_agent_id, "belief_propagate", "agent_beliefs",
                    source_belief_dict["id"], topic)
        conn.commit()

        return {
            "ok": True,
            "topic": topic,
            "belief_id": source_belief_dict["id"],
            "source_agent_id": source_agent_id,
            "original_confidence": original_confidence,
            "propagated_confidence": propagated_confidence,
            "propagated_to": propagated_to,
        }
    finally:
        conn.close()


def tool_belief_consensus(
    topic: str,
    min_confidence: float = 0.3,
) -> dict:
    """Compute confidence-weighted consensus belief across all agents on a topic."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        rows = conn.execute(
            """
            SELECT agent_id, belief_content, confidence
            FROM agent_beliefs
            WHERE topic=? AND invalidated_at IS NULL AND confidence >= ?
            """,
            (topic, min_confidence),
        ).fetchall()

        if not rows:
            return {
                "ok": True,
                "topic": topic,
                "consensus_content": None,
                "consensus_confidence": 0.0,
                "agent_count": 0,
                "agreement_score": 0.0,
            }

        beliefs = [dict(r) for r in rows]
        agent_count = len(beliefs)

        # Group by content and sum confidence weights
        content_weights: dict[str, float] = defaultdict(float)
        for b in beliefs:
            content_weights[b["belief_content"]] += b["confidence"]

        # The consensus content is the one with highest summed confidence
        consensus_content = max(content_weights, key=lambda c: content_weights[c])
        total_confidence = sum(b["confidence"] for b in beliefs)

        # consensus_confidence = weighted average of confidences
        consensus_confidence = total_confidence / agent_count if agent_count else 0.0

        # agreement_score: 1.0 if all same content, else fraction of weight held by top content
        if len(content_weights) == 1:
            agreement_score = 1.0
        else:
            top_weight = content_weights[consensus_content]
            agreement_score = top_weight / total_confidence if total_confidence > 0 else 0.0

        return {
            "ok": True,
            "topic": topic,
            "consensus_content": consensus_content,
            "consensus_confidence": round(consensus_confidence, 6),
            "agent_count": agent_count,
            "agreement_score": round(agreement_score, 6),
        }
    finally:
        conn.close()


def tool_belief_diff(
    agent_a: str,
    agent_b: str,
    limit: int = 20,
) -> dict:
    """Show belief differences between two agents on all shared topics."""
    conn = _db()
    try:
        if not _tom_tables_exist(conn):
            return {
                "ok": False,
                "error": "Theory of Mind tables not found. Apply migration 012_theory_of_mind.sql.",
            }

        # Get all active beliefs for both agents
        rows_a = conn.execute(
            """
            SELECT topic, id, belief_content, confidence
            FROM agent_beliefs
            WHERE agent_id=? AND invalidated_at IS NULL
            """,
            (agent_a,),
        ).fetchall()

        rows_b = conn.execute(
            """
            SELECT topic, id, belief_content, confidence
            FROM agent_beliefs
            WHERE agent_id=? AND invalidated_at IS NULL
            """,
            (agent_b,),
        ).fetchall()

        beliefs_a = {r["topic"]: dict(r) for r in rows_a}
        beliefs_b = {r["topic"]: dict(r) for r in rows_b}

        shared_topics = set(beliefs_a.keys()) & set(beliefs_b.keys())

        divergent: list[dict] = []
        aligned: list[dict] = []

        for topic in sorted(shared_topics):
            ba = beliefs_a[topic]
            bb = beliefs_b[topic]

            if ba["belief_content"] == bb["belief_content"]:
                aligned.append({
                    "topic": topic,
                    "shared_content": ba["belief_content"],
                })
            else:
                delta = abs(ba["confidence"] - bb["confidence"])
                divergent.append({
                    "topic": topic,
                    "belief_a": ba["belief_content"],
                    "belief_b": bb["belief_content"],
                    "confidence_a": ba["confidence"],
                    "confidence_b": bb["confidence"],
                    "delta": round(delta, 6),
                })

            if len(divergent) + len(aligned) >= limit:
                break

        return {
            "ok": True,
            "agent_a": agent_a,
            "agent_b": agent_b,
            "shared_topics": len(shared_topics),
            "divergent": divergent[:limit],
            "aligned": aligned[:limit],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="belief_conflicts_scan",
        description=(
            "Scan all beliefs across all agents and identify topics where agents hold "
            "conflicting beliefs. Two beliefs conflict if they share the same topic but "
            "have different content and are both active (not invalidated) with confidence "
            "above min_confidence. Severity is 'high' when the two most-confident agents "
            "differ by less than 0.1."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic_filter":   {"type": "string",  "description": "Filter by topic substring"},
                "min_confidence": {"type": "number",  "description": "Minimum confidence to include (default: 0.3)", "default": 0.3},
                "limit":          {"type": "integer", "description": "Max conflicts to return (default: 50)", "default": 50},
            },
        },
    ),
    Tool(
        name="belief_merge",
        description=(
            "Merge conflicting beliefs on a topic using a specified strategy. "
            "Strategies: 'highest_confidence' keeps the most-confident belief; "
            "'most_recent' keeps the newest; 'weighted_average' creates a new merged belief; "
            "'human_review' marks all as assumptions and creates a belief_conflicts record. "
            "Use dry_run=true to preview without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic":    {"type": "string",  "description": "Topic key to merge beliefs on"},
                "strategy": {"type": "string",  "description": "Merge strategy: highest_confidence, most_recent, weighted_average, human_review", "default": "highest_confidence"},
                "agent_id": {"type": "string",  "description": "Agent ID to credit merged belief to (default: mcp-client)", "default": "mcp-client"},
                "dry_run":  {"type": "boolean", "description": "Preview without writing (default: true)", "default": True},
            },
            "required": ["topic"],
        },
    ),
    Tool(
        name="belief_propagate",
        description=(
            "Propagate a belief from a source agent to other agents who share context. "
            "Shared context is measured by overlap in workspace_acks (Jaccard similarity). "
            "Only agents above min_shared_context_score receive the belief, at a reduced "
            "confidence (original * 0.85)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_agent_id":         {"type": "string", "description": "Agent whose belief to propagate"},
                "topic":                   {"type": "string", "description": "Topic key to propagate"},
                "min_shared_context_score": {"type": "number", "description": "Minimum Jaccard similarity to receive propagation (default: 0.5)", "default": 0.5},
            },
            "required": ["source_agent_id", "topic"],
        },
    ),
    Tool(
        name="belief_consensus",
        description=(
            "Compute the consensus belief across all agents on a given topic. "
            "Returns the content with highest summed confidence, along with an "
            "agreement_score (1.0 = unanimous, 0.0 = maximally split)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic":          {"type": "string", "description": "Topic key to analyse"},
                "min_confidence": {"type": "number", "description": "Minimum confidence to include (default: 0.3)", "default": 0.3},
            },
            "required": ["topic"],
        },
    ),
    Tool(
        name="belief_diff",
        description=(
            "Show belief differences between two agents across all topics they both hold. "
            "Returns divergent topics (different content) and aligned topics (same content)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_a": {"type": "string",  "description": "First agent ID"},
                "agent_b": {"type": "string",  "description": "Second agent ID"},
                "limit":   {"type": "integer", "description": "Max topics to return (default: 20)", "default": 20},
            },
            "required": ["agent_a", "agent_b"],
        },
    ),
]

DISPATCH: dict = {
    "belief_conflicts_scan": lambda agent_id=None, **kw: tool_belief_conflicts_scan(**kw),
    "belief_merge":          lambda agent_id=None, **kw: tool_belief_merge(**kw),
    "belief_propagate":      lambda agent_id=None, **kw: tool_belief_propagate(**kw),
    "belief_consensus":      lambda agent_id=None, **kw: tool_belief_consensus(**kw),
    "belief_diff":           lambda agent_id=None, **kw: tool_belief_diff(**kw),
}
