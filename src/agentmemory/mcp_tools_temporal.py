"""brainctl MCP tools — temporal causality & epochs."""
from __future__ import annotations
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


# ---------------------------------------------------------------------------
# Timestamp helpers (ported from _impl.py)
# ---------------------------------------------------------------------------

def _parse_timestamp(raw: str):
    if not raw:
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _sqlite_ts(raw: str) -> str:
    dt = _parse_timestamp(raw)
    if dt is None:
        raise ValueError(f"Invalid timestamp: {raw}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _minutes_ago(ts_str: str) -> str:
    if not ts_str:
        return "unknown"
    try:
        ts_str = ts_str.strip()
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(ts_str, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}:\d{2})", ts_str)
            if m:
                dt = datetime.fromisoformat(ts_str)
            else:
                return ts_str
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = now - dt
        total_sec = int(delta.total_seconds())
        if total_sec < 60:
            return f"{total_sec}s ago"
        elif total_sec < 3600:
            return f"{total_sec // 60} min ago"
        elif total_sec < 86400:
            return f"{total_sec // 3600}h ago"
        else:
            return f"{total_sec // 86400}d ago"
    except Exception:
        return ts_str


def _epoch_day(started_at_str: str) -> str:
    if not started_at_str:
        return "?"
    try:
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(started_at_str.strip(), fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return "?"
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            dt = dt.replace(tzinfo=None)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        days = (now.date() - dt.date()).days + 1
        return str(days)
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Epoch detection helpers (ported from _impl.py)
# ---------------------------------------------------------------------------

_EPOCH_TOKEN_STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "into", "over",
    "under", "after", "before", "agent", "agents", "event", "events",
    "result", "results", "update", "status", "error", "warning", "task",
    "tasks", "memory", "memories", "project", "work", "done", "added",
    "completed", "created",
}

_CAUSAL_TEMPLATES = [
    ("error",        "task_update",     0.6),
    ("decision",     "task_update",     0.7),
    ("decision",     "result",          0.7),
    ("task_update",  "result",          0.6),
    ("observation",  "decision",        0.5),
    ("observation",  "task_update",     0.4),
    ("handoff",      "task_update",     0.7),
    ("handoff",      "decision",        0.6),
    ("warning",      "decision",        0.6),
    ("warning",      "task_update",     0.5),
    ("result",       "memory_promoted", 0.7),
    ("error",        "decision",        0.7),
]


def _humanize_slug(value: str) -> str:
    cleaned = re.sub(r"[_\-\/]+", " ", (value or "").strip())
    words = [w for w in cleaned.split() if w]
    if not words:
        return "Operational"
    return " ".join(w.capitalize() for w in words)


def _event_topic_tokens(event_row: dict) -> Counter:
    counter: Counter = Counter()
    project = (event_row.get("project") or "").strip()
    if project:
        counter[project.lower()] += 3
    event_type = (event_row.get("event_type") or "").strip()
    if event_type:
        counter[event_type.lower()] += 1
    blob_parts = [
        event_row.get("summary") or "",
        event_row.get("detail") or "",
    ]
    refs_raw = event_row.get("refs")
    if refs_raw:
        try:
            refs = json.loads(refs_raw)
            if isinstance(refs, list):
                blob_parts.extend(str(r) for r in refs if r)
        except (json.JSONDecodeError, TypeError):
            blob_parts.append(str(refs_raw))
    token_blob = " ".join(blob_parts).lower()
    for token in re.findall(r"[a-z0-9][a-z0-9_\-]{2,}", token_blob):
        if token in _EPOCH_TOKEN_STOPWORDS:
            continue
        counter[token] += 1
    return counter


def _counter_cosine(left: Counter, right: Counter) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    dot = 0.0
    for key, lv in left.items():
        dot += lv * right.get(key, 0.0)
    left_norm = sum(v * v for v in left.values()) ** 0.5
    right_norm = sum(v * v for v in right.values()) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def detect_epoch_boundaries(
    events: list[dict],
    *,
    gap_hours: float = 48.0,
    window_size: int = 8,
    min_window: int = 4,
    topic_shift_threshold: float = 0.45,
    min_boundary_distance: int = 8,
    context_decay: float = 0.7,
    cosine_divergence_threshold: float = 0.55,
) -> list[dict]:
    """Detect event boundaries from divergence against recent context.

    ``window_size`` controls the left-side context window. The right side uses a
    short smoothing lookahead of ``min(window_size, 3)`` events to reduce noise
    from one-off vocabulary spikes while still honoring smaller caller-supplied
    windows.

    ``context_decay`` and ``cosine_divergence_threshold`` are kept for backward
    compatibility with earlier rolling-context experiments, but the public
    threshold is now ``topic_shift_threshold`` again.
    """
    candidates = []
    if len(events) < 2:
        return candidates

    for idx in range(1, len(events)):
        prev_ts = _parse_timestamp(events[idx - 1].get("created_at"))
        curr_ts = _parse_timestamp(events[idx].get("created_at"))
        if prev_ts is None or curr_ts is None:
            continue

        gap_h = (curr_ts - prev_ts).total_seconds() / 3600.0
        gap_signal = gap_h >= gap_hours
        left_slice = events[max(0, idx - window_size):idx]
        right_window = max(1, min(3, window_size))
        right_slice = events[idx:min(len(events), idx + right_window)]

        context_similarity = None
        context_divergence = None
        left_top = None
        right_top = None
        reasons = []

        if len(left_slice) >= min_window:
            context_counter: Counter = Counter()
            for row in left_slice:
                context_counter.update(_event_topic_tokens(row))

            current_counter: Counter = Counter()
            for row in right_slice:
                current_counter.update(_event_topic_tokens(row))

            if context_counter and current_counter:
                context_similarity = _counter_cosine(context_counter, current_counter)
                context_divergence = max(0.0, 1.0 - context_similarity)
                if context_divergence >= topic_shift_threshold:
                    reasons.append("context_divergence")
                    left_top = context_counter.most_common(1)[0][0] if context_counter else None
                    right_top = current_counter.most_common(1)[0][0] if current_counter else None
                    if left_top and right_top and left_top != right_top:
                        reasons.append("prediction_error")

        if gap_signal:
            reasons.append("time_gap")

        if not reasons:
            continue

        score = 0.0
        if context_divergence is not None and "context_divergence" in reasons:
            score += context_divergence
        if gap_signal:
            score += min(gap_h / gap_hours, 1.0)

        candidates.append({
            "boundary_index": idx,
            "boundary_at": events[idx].get("created_at"),
            "reasons": reasons,
            "gap_hours": round(gap_h, 2),
            "topic_similarity": None if context_similarity is None else round(context_similarity, 3),
            "context_divergence": None if context_divergence is None else round(context_divergence, 3),
            "left_topic": left_top,
            "right_topic": right_top,
            "score": round(score, 3),
        })

    # Conservative filter: keep stronger candidates and skip nearby weak splits.
    filtered = []
    for cand in sorted(candidates, key=lambda c: c["boundary_index"]):
        if not filtered:
            filtered.append(cand)
            continue
        prev = filtered[-1]
        idx_distance = cand["boundary_index"] - prev["boundary_index"]
        if idx_distance >= min_boundary_distance:
            filtered.append(cand)
            continue
        if cand["score"] > prev["score"] + 0.2:
            filtered[-1] = cand
    return filtered


def _proposed_epoch_name(segment: list[dict]) -> str:
    projects = [str(r.get("project")).strip() for r in segment if r.get("project")]
    if projects:
        proj, _ = Counter(projects).most_common(1)[0]
        return f"{_humanize_slug(proj)} Sprint"

    token_counter: Counter = Counter()
    for row in segment:
        token_counter.update(_event_topic_tokens(row))
    for noisy in ("observation", "result", "decision", "task_update"):
        token_counter.pop(noisy, None)
    top_tokens = [t for t, _ in token_counter.most_common(2)]
    if len(top_tokens) >= 2:
        return f"{_humanize_slug(top_tokens[0])} {_humanize_slug(top_tokens[1])} Phase"
    if top_tokens:
        return f"{_humanize_slug(top_tokens[0])} Phase"
    return "Operational Phase"


def suggest_epoch_ranges(
    events: list[dict],
    boundaries: list[dict],
    *,
    min_events_per_epoch: int = 5,
) -> list[dict]:
    if not events:
        return []
    kept = []
    start_idx = 0
    for boundary in boundaries:
        idx = boundary["boundary_index"]
        segment_size = idx - start_idx
        if segment_size < min_events_per_epoch and boundary.get("gap_hours", 0) < 72:
            continue
        kept.append(boundary)
        start_idx = idx

    suggestions = []
    segment_start = 0
    for boundary in kept:
        segment = events[segment_start:boundary["boundary_index"]]
        if segment:
            suggestions.append({
                "name": _proposed_epoch_name(segment),
                "started_at": segment[0]["created_at"],
                "ended_at": segment[-1]["created_at"],
                "event_count": len(segment),
                "trigger_next_boundary": {
                    "reasons": boundary["reasons"],
                    "gap_hours": boundary["gap_hours"],
                    "topic_similarity": boundary["topic_similarity"],
                },
            })
        segment_start = boundary["boundary_index"]

    tail = events[segment_start:]
    if tail:
        suggestions.append({
            "name": _proposed_epoch_name(tail),
            "started_at": tail[0]["created_at"],
            "ended_at": None,
            "event_count": len(tail),
            "trigger_next_boundary": None,
        })
    return suggestions


# ---------------------------------------------------------------------------
# Causal graph helpers (ported from _impl.py)
# ---------------------------------------------------------------------------

def _causal_would_create_cycle(conn: sqlite3.Connection, source_id: int, target_id: int) -> bool:
    if source_id == target_id:
        return True
    row = conn.execute("""
        WITH RECURSIVE reach(node) AS (
            SELECT target_id FROM knowledge_edges
            WHERE source_table = 'events' AND target_table = 'events'
              AND source_id = ?
              AND relation_type IN ('causes', 'triggered_by', 'contributes_to', 'follows_from')
            UNION
            SELECT ke.target_id FROM knowledge_edges ke
            JOIN reach r ON ke.source_id = r.node
            WHERE ke.source_table = 'events' AND ke.target_table = 'events'
              AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to', 'follows_from')
        )
        SELECT 1 FROM reach WHERE node = ? LIMIT 1
    """, (target_id, source_id)).fetchone()
    return row is not None


def _causal_edge_exists(conn: sqlite3.Connection, source_id: int, target_id: int, relation: str) -> bool:
    row = conn.execute("""
        SELECT 1 FROM knowledge_edges
        WHERE source_table = 'events' AND source_id = ?
          AND target_table = 'events' AND target_id = ?
          AND relation_type = ?
        LIMIT 1
    """, (source_id, target_id, relation)).fetchone()
    return row is not None


def _insert_causal_edge(
    conn: sqlite3.Connection,
    source_id: int,
    target_id: int,
    relation: str,
    confidence: float,
    agent_id: str | None = None,
) -> str:
    """Insert a causal edge with cycle/duplicate checks. Returns 'inserted', 'existing', or 'cycle'."""
    if _causal_edge_exists(conn, source_id, target_id, relation):
        return "existing"
    if _causal_would_create_cycle(conn, source_id, target_id):
        return "cycle"
    conn.execute("""
        INSERT INTO knowledge_edges
            (source_table, source_id, target_table, target_id, relation_type, weight, agent_id)
        VALUES ('events', ?, 'events', ?, ?, ?, ?)
    """, (source_id, target_id, relation, round(confidence, 3), agent_id))
    return "inserted"


def _detect_reference_chains(conn: sqlite3.Connection) -> list:
    """Find events whose refs field explicitly references other events via 'events:N' notation."""
    try:
        rows = conn.execute("""
            SELECT e.id as effect_id,
                   CAST(SUBSTR(ref.value, INSTR(ref.value, ':') + 1) AS INTEGER) as cause_id
            FROM events e, json_each(e.refs) ref
            WHERE ref.value GLOB 'events:*'
              AND CAST(SUBSTR(ref.value, INSTR(ref.value, ':') + 1) AS INTEGER) IN (
                  SELECT id FROM events
              )
        """).fetchall()
        return [(r["effect_id"], r["cause_id"], "triggered_by", 0.9) for r in rows]
    except Exception:
        return []


def _detect_template_edges(conn: sqlite3.Connection, window_minutes: int = 60) -> list:
    """Apply type-based causal templates within a time window."""
    edges = []
    window_days = window_minutes / 1440.0
    for cause_type, effect_type, base_conf in _CAUSAL_TEMPLATES:
        rows = conn.execute("""
            SELECT a.id as a_id, b.id as b_id,
                   (julianday(b.created_at) - julianday(a.created_at)) * 1440.0 as gap_min
            FROM events a
            JOIN events b ON julianday(b.created_at) > julianday(a.created_at)
                AND (julianday(b.created_at) - julianday(a.created_at)) <= ?
                AND a.id != b.id
            WHERE a.event_type = ? AND b.event_type = ?
              AND (a.agent_id = b.agent_id
                   OR (a.project IS NOT NULL AND a.project != '' AND a.project = b.project))
        """, (window_days, cause_type, effect_type)).fetchall()
        window_minutes_f = float(window_minutes)
        for r in rows:
            gap = r["gap_min"] or 0.0
            time_decay = max(0.0, 1.0 - (gap / window_minutes_f) * 0.3)
            confidence = round(base_conf * time_decay, 3)
            edges.append((r["a_id"], r["b_id"], "causes", confidence))
    return edges


def _detect_proximity_edges(conn: sqlite3.Connection, window_minutes: int = 30) -> list:
    """Temporal proximity + shared agent/project heuristic (lowest confidence)."""
    window_days = window_minutes / 1440.0
    rows = conn.execute("""
        SELECT a.id as a_id, b.id as b_id,
               (julianday(b.created_at) - julianday(a.created_at)) * 1440.0 as gap_min,
               (CASE WHEN a.agent_id = b.agent_id THEN 1 ELSE 0 END +
                CASE WHEN a.project IS NOT NULL AND a.project != '' AND a.project = b.project
                     THEN 1 ELSE 0 END
               ) as shared_ctx
        FROM events a
        JOIN events b ON julianday(b.created_at) > julianday(a.created_at)
            AND (julianday(b.created_at) - julianday(a.created_at)) <= ?
            AND a.id != b.id
        WHERE (a.agent_id = b.agent_id
               OR (a.project IS NOT NULL AND a.project != '' AND a.project = b.project))
    """, (window_days,)).fetchall()

    edges = []
    window_minutes_f = float(window_minutes)
    for r in rows:
        shared = r["shared_ctx"] or 0
        gap = r["gap_min"] or 0.0
        if shared < 1:
            continue
        time_factor = max(0.0, 1.0 - (gap / window_minutes_f))
        ctx_factor = min(shared / 2.0, 1.0)
        confidence = round(0.25 + 0.35 * time_factor * ctx_factor, 3)
        edges.append((r["a_id"], r["b_id"], "causes", confidence))
    return edges


def _build_causal_graph(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Full pipeline: detect causal edges and insert into knowledge_edges."""
    stats: dict[str, int] = {"found": 0, "inserted": 0, "cycle": 0, "existing": 0}

    ref_edges = _detect_reference_chains(conn)
    template_edges = _detect_template_edges(conn, window_minutes=60)
    proximity_edges = _detect_proximity_edges(conn, window_minutes=30)

    # Merge: (src, tgt) -> (relation, confidence), keep highest confidence per pair
    all_edges: dict = {}

    for effect_id, cause_id, relation, conf in ref_edges:
        key = (cause_id, effect_id)
        if key not in all_edges or all_edges[key][1] < conf:
            all_edges[key] = (relation, conf)

    for src_id, tgt_id, relation, conf in template_edges:
        key = (src_id, tgt_id)
        if key not in all_edges or all_edges[key][1] < conf:
            all_edges[key] = (relation, conf)

    for src_id, tgt_id, relation, conf in proximity_edges:
        key = (src_id, tgt_id)
        if key not in all_edges:
            all_edges[key] = (relation, conf)

    stats["found"] = len(all_edges)

    if not dry_run:
        for (src_id, tgt_id), (relation, conf) in all_edges.items():
            outcome = _insert_causal_edge(conn, src_id, tgt_id, relation, conf)
            stats[outcome] = stats.get(outcome, 0) + 1
        conn.commit()
    else:
        for (src_id, tgt_id), (relation, conf) in all_edges.items():
            if _causal_edge_exists(conn, src_id, tgt_id, relation):
                stats["existing"] += 1
            elif _causal_would_create_cycle(conn, src_id, tgt_id):
                stats["cycle"] += 1
            else:
                stats["inserted"] += 1

    return stats


# ---------------------------------------------------------------------------
# Tool handler functions
# ---------------------------------------------------------------------------

def _temporal_causes(event_id: int, depth: int = 6, min_confidence: float = 0.0) -> dict:
    """Forward traversal: what did event X cause? (downstream effects chain)."""
    conn = _db()
    seed = conn.execute(
        "SELECT id, event_type, summary, agent_id, project, created_at FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not seed:
        return {"ok": False, "error": f"event {event_id} not found"}

    try:
        rows = conn.execute("""
            WITH RECURSIVE fwd(caused_id, chain_conf, depth, path) AS (
                SELECT ke.target_id, ke.weight, 1,
                       CAST(ke.source_id AS TEXT) || '->' || CAST(ke.target_id AS TEXT)
                FROM knowledge_edges ke
                WHERE ke.source_table = 'events' AND ke.target_table = 'events'
                  AND ke.source_id = ?
                  AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
                  AND ke.weight >= ?
                UNION ALL
                SELECT ke.target_id, fwd.chain_conf * ke.weight, fwd.depth + 1,
                       fwd.path || '->' || CAST(ke.target_id AS TEXT)
                FROM knowledge_edges ke
                JOIN fwd ON ke.source_id = fwd.caused_id
                WHERE ke.source_table = 'events' AND ke.target_table = 'events'
                  AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
                  AND ke.weight >= ?
                  AND fwd.depth < ?
                  AND INSTR(fwd.path, CAST(ke.target_id AS TEXT)) = 0
            )
            SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.project, e.created_at,
                   MIN(fwd.depth) as depth, MAX(fwd.chain_conf) as chain_confidence
            FROM fwd JOIN events e ON e.id = fwd.caused_id
            GROUP BY e.id
            ORDER BY depth ASC, chain_confidence DESC
        """, (event_id, min_confidence, min_confidence, depth)).fetchall()
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": f"query failed: {exc}"}

    return {
        "ok": True,
        "seed": dict(seed),
        "direction": "forward",
        "description": "downstream effects — what did this event cause?",
        "chain_length": len(rows),
        "chain": [dict(r) for r in rows],
    }


def _temporal_effects(event_id: int, depth: int = 6, min_confidence: float = 0.0) -> dict:
    """Backward traversal: why did event X happen? (upstream causes)."""
    conn = _db()
    seed = conn.execute(
        "SELECT id, event_type, summary, agent_id, project, created_at FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not seed:
        return {"ok": False, "error": f"event {event_id} not found"}

    try:
        rows = conn.execute("""
            WITH RECURSIVE bwd(cause_id, chain_conf, depth, path) AS (
                SELECT ke.source_id, ke.weight, 1,
                       CAST(ke.target_id AS TEXT) || '<-' || CAST(ke.source_id AS TEXT)
                FROM knowledge_edges ke
                WHERE ke.source_table = 'events' AND ke.target_table = 'events'
                  AND ke.target_id = ?
                  AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
                  AND ke.weight >= ?
                UNION ALL
                SELECT ke.source_id, bwd.chain_conf * ke.weight, bwd.depth + 1,
                       bwd.path || '<-' || CAST(ke.source_id AS TEXT)
                FROM knowledge_edges ke
                JOIN bwd ON ke.target_id = bwd.cause_id
                WHERE ke.source_table = 'events' AND ke.target_table = 'events'
                  AND ke.relation_type IN ('causes', 'triggered_by', 'contributes_to')
                  AND ke.weight >= ?
                  AND bwd.depth < ?
                  AND INSTR(bwd.path, CAST(ke.source_id AS TEXT)) = 0
            )
            SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.project, e.created_at,
                   MIN(bwd.depth) as depth, MAX(bwd.chain_conf) as chain_confidence
            FROM bwd JOIN events e ON e.id = bwd.cause_id
            GROUP BY e.id
            ORDER BY depth ASC, chain_confidence DESC
        """, (event_id, min_confidence, min_confidence, depth)).fetchall()
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": f"query failed: {exc}"}

    return {
        "ok": True,
        "seed": dict(seed),
        "direction": "backward",
        "description": "upstream causes — why did this event happen?",
        "chain_length": len(rows),
        "chain": [dict(r) for r in rows],
    }


def _temporal_chain(event_id: int, depth: int = 4, min_confidence: float = 0.0) -> dict:
    """Bidirectional causal chain: upstream causes + downstream effects."""
    conn = _db()
    seed = conn.execute(
        "SELECT id, event_type, summary, agent_id, project, created_at FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not seed:
        return {"ok": False, "error": f"event {event_id} not found"}

    try:
        fwd = conn.execute("""
            WITH RECURSIVE fwd(caused_id, chain_conf, depth, path) AS (
                SELECT ke.target_id, ke.weight, 1,
                       CAST(ke.source_id AS TEXT)||'->'||CAST(ke.target_id AS TEXT)
                FROM knowledge_edges ke
                WHERE ke.source_table='events' AND ke.target_table='events'
                  AND ke.source_id=? AND ke.weight>=?
                  AND ke.relation_type IN ('causes','triggered_by','contributes_to')
                UNION ALL
                SELECT ke.target_id, fwd.chain_conf*ke.weight, fwd.depth+1,
                       fwd.path||'->'||CAST(ke.target_id AS TEXT)
                FROM knowledge_edges ke JOIN fwd ON ke.source_id=fwd.caused_id
                WHERE ke.source_table='events' AND ke.target_table='events'
                  AND ke.relation_type IN ('causes','triggered_by','contributes_to')
                  AND ke.weight>=? AND fwd.depth<?
                  AND INSTR(fwd.path,CAST(ke.target_id AS TEXT))=0
            )
            SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.created_at,
                   MIN(fwd.depth) as depth, MAX(fwd.chain_conf) as chain_confidence
            FROM fwd JOIN events e ON e.id=fwd.caused_id
            GROUP BY e.id ORDER BY depth ASC
        """, (event_id, min_confidence, min_confidence, depth)).fetchall()

        bwd = conn.execute("""
            WITH RECURSIVE bwd(cause_id, chain_conf, depth, path) AS (
                SELECT ke.source_id, ke.weight, 1,
                       CAST(ke.target_id AS TEXT)||'<-'||CAST(ke.source_id AS TEXT)
                FROM knowledge_edges ke
                WHERE ke.source_table='events' AND ke.target_table='events'
                  AND ke.target_id=? AND ke.weight>=?
                  AND ke.relation_type IN ('causes','triggered_by','contributes_to')
                UNION ALL
                SELECT ke.source_id, bwd.chain_conf*ke.weight, bwd.depth+1,
                       bwd.path||'<-'||CAST(ke.source_id AS TEXT)
                FROM knowledge_edges ke JOIN bwd ON ke.target_id=bwd.cause_id
                WHERE ke.source_table='events' AND ke.target_table='events'
                  AND ke.relation_type IN ('causes','triggered_by','contributes_to')
                  AND ke.weight>=? AND bwd.depth<?
                  AND INSTR(bwd.path,CAST(ke.source_id AS TEXT))=0
            )
            SELECT DISTINCT e.id, e.event_type, e.summary, e.agent_id, e.created_at,
                   MIN(bwd.depth) as depth, MAX(bwd.chain_conf) as chain_confidence
            FROM bwd JOIN events e ON e.id=bwd.cause_id
            GROUP BY e.id ORDER BY depth ASC
        """, (event_id, min_confidence, min_confidence, depth)).fetchall()
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": f"query failed: {exc}"}

    return {
        "ok": True,
        "seed": dict(seed),
        "upstream_causes": [dict(r) for r in bwd],
        "downstream_effects": [dict(r) for r in fwd],
        "upstream_count": len(bwd),
        "downstream_count": len(fwd),
    }


def _temporal_auto_detect(dry_run: bool = False) -> dict:
    """Run causal edge auto-detection pipeline over all events."""
    conn = _db()
    stats = _build_causal_graph(conn, dry_run=dry_run)
    label = "Would insert" if dry_run else "Inserted"
    return {
        "ok": True,
        "dry_run": dry_run,
        "stats": stats,
        "message": (
            f"{label} {stats.get('inserted', 0)} causal edges "
            f"({stats.get('existing', 0)} already existed, "
            f"{stats.get('cycle', 0)} cycles prevented, "
            f"{stats.get('found', 0)} total candidates)"
        ),
    }


def _temporal_context() -> dict:
    """Return a structured temporal snapshot of the current brain state."""
    conn = _db()
    now_local = datetime.now().astimezone()
    tz_name = now_local.strftime("%Z") or "local"
    now_str = now_local.strftime(f"%Y-%m-%d %H:%M {tz_name}")

    result: dict[str, Any] = {"ok": True, "timestamp": now_str}

    # --- Current epoch ---
    try:
        epoch_row = conn.execute(
            "SELECT * FROM epochs WHERE started_at <= strftime('%Y-%m-%dT%H:%M:%S', 'now') "
            "AND (ended_at IS NULL OR ended_at > strftime('%Y-%m-%dT%H:%M:%S', 'now')) "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if epoch_row:
            epoch_name = epoch_row["name"]
            epoch_day = _epoch_day(epoch_row["started_at"])
            parent_row = None
            if epoch_row["parent_epoch_id"]:
                parent_row = conn.execute(
                    "SELECT * FROM epochs WHERE id = ?", (epoch_row["parent_epoch_id"],)
                ).fetchone()
            if parent_row:
                parent_day = _epoch_day(parent_row["started_at"])
                result["current_epoch"] = {
                    "name": epoch_name,
                    "day": epoch_day,
                    "parent_name": parent_row["name"],
                    "parent_day": parent_day,
                }
            else:
                result["current_epoch"] = {"name": epoch_name, "day": epoch_day}
        else:
            result["current_epoch"] = None
    except sqlite3.OperationalError:
        result["current_epoch"] = None

    # --- Project age ---
    try:
        first_event = conn.execute("SELECT min(created_at) as first_at FROM events").fetchone()
        total_events = conn.execute("SELECT count(*) as cnt FROM events").fetchone()["cnt"]
        total_memories = conn.execute(
            "SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL"
        ).fetchone()["cnt"]
        if first_event and first_event["first_at"]:
            raw = first_event["first_at"].strip()
            dt0 = None
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt0 = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if dt0:
                if dt0.tzinfo:
                    dt0 = dt0.replace(tzinfo=None)
                days_active = (datetime.now(timezone.utc).date() - dt0.date()).days + 1
                result["project_age"] = {
                    "days_active": days_active,
                    "total_events": total_events,
                    "active_memories": total_memories,
                }
            else:
                result["project_age"] = {"total_events": total_events, "active_memories": total_memories}
        else:
            result["project_age"] = {"total_events": 0, "active_memories": 0}
    except sqlite3.OperationalError:
        result["project_age"] = None

    # --- Last activity ---
    try:
        last_event = conn.execute(
            "SELECT agent_id, summary, created_at FROM events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if last_event:
            result["last_activity"] = {
                "ago": _minutes_ago(last_event["created_at"]),
                "agent_id": last_event["agent_id"],
                "summary": (last_event["summary"] or "")[:80],
            }
        else:
            result["last_activity"] = None
    except sqlite3.OperationalError:
        result["last_activity"] = None

    # --- Cadence ---
    try:
        events_24h = conn.execute(
            "SELECT count(*) as cnt FROM events WHERE created_at >= datetime('now', '-24 hours')"
        ).fetchone()["cnt"]
        active_agents_24h = conn.execute(
            "SELECT count(DISTINCT agent_id) as cnt FROM events WHERE created_at >= datetime('now', '-24 hours')"
        ).fetchone()["cnt"]
        if events_24h > 10:
            cadence = "HIGH"
        elif events_24h > 3:
            cadence = "MEDIUM"
        else:
            cadence = "LOW"
        result["cadence"] = {
            "level": cadence,
            "events_24h": events_24h,
            "active_agents_24h": active_agents_24h,
        }
    except sqlite3.OperationalError:
        result["cadence"] = None

    # --- Active agents (last 24h) ---
    try:
        active_agent_rows = conn.execute(
            "SELECT DISTINCT agent_id FROM events WHERE created_at >= datetime('now', '-24 hours') ORDER BY agent_id"
        ).fetchall()
        result["active_agents"] = [r["agent_id"] for r in active_agent_rows]
    except sqlite3.OperationalError:
        result["active_agents"] = []

    # --- Dormant agents (>48h) ---
    try:
        all_agents = conn.execute("SELECT id FROM agents WHERE status = 'active'").fetchall()
        all_agent_ids = {r["id"] for r in all_agents}
        recently_active = conn.execute(
            "SELECT DISTINCT agent_id FROM events WHERE created_at >= datetime('now', '-48 hours')"
        ).fetchall()
        recently_active_ids = {r["agent_id"] for r in recently_active}
        result["dormant_agents"] = sorted(all_agent_ids - recently_active_ids)
    except sqlite3.OperationalError:
        result["dormant_agents"] = []

    # --- Recent decisions ---
    try:
        decisions_48h = conn.execute(
            "SELECT count(*) as cnt FROM decisions WHERE created_at >= datetime('now', '-48 hours')"
        ).fetchone()["cnt"]
        recent_decision_titles = conn.execute(
            "SELECT title FROM decisions WHERE created_at >= datetime('now', '-48 hours') ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        result["recent_decisions"] = {
            "count_48h": decisions_48h,
            "titles": [r["title"][:40] for r in recent_decision_titles],
        }
    except sqlite3.OperationalError:
        result["recent_decisions"] = None

    # --- Memory health ---
    try:
        active_mem = conn.execute(
            "SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL"
        ).fetchone()["cnt"]
        decayed_mem = conn.execute(
            "SELECT count(*) as cnt FROM memories WHERE retired_at IS NULL AND confidence < 0.3"
        ).fetchone()["cnt"]
        retired_mem = conn.execute(
            "SELECT count(*) as cnt FROM memories WHERE retired_at IS NOT NULL"
        ).fetchone()["cnt"]
        result["memory_health"] = {
            "active": active_mem,
            "low_confidence": decayed_mem,
            "retired": retired_mem,
        }
    except sqlite3.OperationalError:
        result["memory_health"] = None

    # --- Stale areas ---
    try:
        stale_scope_rows = conn.execute(
            "SELECT scope, max(updated_at) as last_update FROM memories "
            "WHERE retired_at IS NULL "
            "GROUP BY scope "
            "HAVING last_update < datetime('now', '-7 days') "
            "ORDER BY last_update ASC"
        ).fetchall()
        result["stale_scopes"] = [r["scope"] for r in stale_scope_rows]
    except sqlite3.OperationalError:
        result["stale_scopes"] = []

    # --- Open causal threads ---
    try:
        open_threads = conn.execute(
            "SELECT e.id, e.agent_id, e.summary, e.created_at FROM events e "
            "WHERE e.event_type IN ('warning', 'handoff') "
            "AND e.created_at >= datetime('now', '-7 days') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM events r "
            "  WHERE r.agent_id = e.agent_id "
            "  AND r.event_type = 'result' "
            "  AND r.created_at > e.created_at"
            ") "
            "ORDER BY e.created_at DESC LIMIT 5"
        ).fetchall()
        result["open_causal_threads"] = [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "summary": (r["summary"] or "")[:50],
                "created_at": r["created_at"],
            }
            for r in open_threads
        ]
    except sqlite3.OperationalError:
        result["open_causal_threads"] = []

    return result


def _event_link(
    cause_event_id: int,
    effect_event_id: int,
    relation: str = "causes",
    confidence: float = 0.9,
    agent: str | None = None,
) -> dict:
    """Agent-reported causation: explicitly link two events as cause->effect."""
    conn = _db()
    cause_row = conn.execute("SELECT id FROM events WHERE id = ?", (cause_event_id,)).fetchone()
    if not cause_row:
        return {"ok": False, "error": f"cause event {cause_event_id} not found"}
    effect_row = conn.execute("SELECT id FROM events WHERE id = ?", (effect_event_id,)).fetchone()
    if not effect_row:
        return {"ok": False, "error": f"effect event {effect_event_id} not found"}

    agent_id = agent or "unknown"
    outcome = _insert_causal_edge(conn, cause_event_id, effect_event_id, relation, confidence, agent_id=agent_id)

    if outcome == "inserted":
        conn.commit()
        return {
            "ok": True,
            "edge": {
                "cause_event_id": cause_event_id,
                "effect_event_id": effect_event_id,
                "relation": relation,
                "confidence": confidence,
                "reported_by": agent_id,
            },
        }
    elif outcome == "existing":
        return {"ok": False, "error": "edge already exists", "cause": cause_event_id, "effect": effect_event_id}
    else:
        return {"ok": False, "error": "would create cycle in causal DAG", "cause": cause_event_id, "effect": effect_event_id}


def _epoch_detect(
    gap_hours: float = 48.0,
    window_size: int = 8,
    min_window: int = 4,
    topic_shift_threshold: float = 0.2,
    min_boundary_distance: int = 8,
    min_events: int = 5,
    verbose: bool = False,
) -> dict:
    """Auto-detect epoch boundaries from event history."""
    conn = _db()
    rows = conn.execute(
        "SELECT id, event_type, summary, detail, project, refs, metadata, created_at "
        "FROM events ORDER BY datetime(created_at) ASC, id ASC"
    ).fetchall()
    events = [dict(r) for r in rows]
    boundaries = detect_epoch_boundaries(
        events,
        gap_hours=gap_hours,
        window_size=window_size,
        min_window=min_window,
        topic_shift_threshold=topic_shift_threshold,
        min_boundary_distance=min_boundary_distance,
    )
    suggestions = suggest_epoch_ranges(events, boundaries, min_events_per_epoch=min_events)
    payload: dict[str, Any] = {
        "ok": True,
        "event_count": len(events),
        "boundary_count": len(boundaries),
        "suggested_epochs": suggestions,
    }
    if verbose:
        payload["boundaries"] = boundaries
    return payload


def _epoch_create(
    name: str,
    started: str,
    description: str | None = None,
    ended: str | None = None,
    parent: int | None = None,
) -> dict:
    """Create a named epoch and backfill existing events/memories into it."""
    conn = _db()
    try:
        started_at = _sqlite_ts(started)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    ended_at = None
    if ended:
        try:
            ended_at = _sqlite_ts(ended)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if ended_at < started_at:
            return {"ok": False, "error": "--ended must be >= --started"}

    try:
        cursor = conn.execute(
            "INSERT INTO epochs (name, description, started_at, ended_at, parent_epoch_id) VALUES (?, ?, ?, ?, ?)",
            (name, description, started_at, ended_at, parent),
        )
        epoch_id = cursor.lastrowid

        if ended_at:
            mem_res = conn.execute(
                "UPDATE memories SET epoch_id = ? "
                "WHERE epoch_id IS NULL AND created_at >= ? AND created_at <= ?",
                (epoch_id, started_at, ended_at),
            )
            evt_res = conn.execute(
                "UPDATE events SET epoch_id = ? "
                "WHERE epoch_id IS NULL AND created_at >= ? AND created_at <= ?",
                (epoch_id, started_at, ended_at),
            )
        else:
            mem_res = conn.execute(
                "UPDATE memories SET epoch_id = ? "
                "WHERE epoch_id IS NULL AND created_at >= ?",
                (epoch_id, started_at),
            )
            evt_res = conn.execute(
                "UPDATE events SET epoch_id = ? "
                "WHERE epoch_id IS NULL AND created_at >= ?",
                (epoch_id, started_at),
            )

        conn.commit()
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": f"epochs table not available: {exc}"}

    return {
        "ok": True,
        "epoch_id": epoch_id,
        "name": name,
        "started_at": started_at,
        "ended_at": ended_at,
        "parent_epoch_id": parent,
        "backfilled": {
            "memories": mem_res.rowcount,
            "events": evt_res.rowcount,
        },
    }


def _epoch_list(active_only: bool = False, limit: int | None = None) -> dict:
    """List all epochs with event and memory counts."""
    conn = _db()
    try:
        sql = (
            "SELECT e.*, "
            "(SELECT count(*) FROM events ev WHERE ev.epoch_id = e.id) AS event_count, "
            "(SELECT count(*) FROM memories m WHERE m.epoch_id = e.id) AS memory_count "
            "FROM epochs e"
        )
        params: list = []
        if active_only:
            sql += (
                " WHERE e.started_at <= strftime('%Y-%m-%dT%H:%M:%S', 'now')"
                " AND (e.ended_at IS NULL OR e.ended_at > strftime('%Y-%m-%dT%H:%M:%S', 'now'))"
            )
        sql += " ORDER BY datetime(e.started_at) DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": f"epochs table not available: {exc}"}
    return {"ok": True, "epochs": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# MCP dispatch
# ---------------------------------------------------------------------------

def _handle(name: str, args: dict) -> Any:
    if name == "temporal_causes":
        return _temporal_causes(
            event_id=int(args["event_id"]),
            depth=int(args.get("depth", 6)),
            min_confidence=float(args.get("min_confidence", 0.0)),
        )
    if name == "temporal_effects":
        return _temporal_effects(
            event_id=int(args["event_id"]),
            depth=int(args.get("depth", 6)),
            min_confidence=float(args.get("min_confidence", 0.0)),
        )
    if name == "temporal_chain":
        return _temporal_chain(
            event_id=int(args["event_id"]),
            depth=int(args.get("depth", 4)),
            min_confidence=float(args.get("min_confidence", 0.0)),
        )
    if name == "temporal_auto_detect":
        return _temporal_auto_detect(dry_run=bool(args.get("dry_run", False)))
    if name == "temporal_context":
        return _temporal_context()
    if name == "event_link":
        return _event_link(
            cause_event_id=int(args["cause_event_id"]),
            effect_event_id=int(args["effect_event_id"]),
            relation=str(args.get("relation", "causes")),
            confidence=float(args.get("confidence", 0.9)),
            agent=args.get("agent"),
        )
    if name == "epoch_detect":
        return _epoch_detect(
            gap_hours=float(args.get("gap_hours", 48.0)),
            window_size=int(args.get("window_size", 8)),
            min_window=int(args.get("min_window", 4)),
            topic_shift_threshold=float(args.get("topic_shift_threshold", 0.45)),
            min_boundary_distance=int(args.get("min_boundary_distance", 8)),
            min_events=int(args.get("min_events", 5)),
            verbose=bool(args.get("verbose", False)),
        )
    if name == "epoch_create":
        return _epoch_create(
            name=str(args["name"]),
            started=str(args["started"]),
            description=args.get("description"),
            ended=args.get("ended"),
            parent=int(args["parent"]) if args.get("parent") is not None else None,
        )
    if name == "epoch_list":
        return _epoch_list(
            active_only=bool(args.get("active_only", False)),
            limit=int(args["limit"]) if args.get("limit") is not None else None,
        )
    return {"ok": False, "error": f"unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="temporal_causes",
        description=(
            "Forward traversal: what did event X cause? "
            "Returns the downstream effects chain starting from the given event."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "Source event ID to trace forward from"},
                "depth": {"type": "integer", "default": 6, "description": "Maximum chain depth"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "Minimum edge confidence (0.0–1.0)"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="temporal_effects",
        description=(
            "Backward traversal: why did event X happen? "
            "Returns the upstream causes of the given event."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "Target event ID to trace backward from"},
                "depth": {"type": "integer", "default": 6, "description": "Maximum chain depth"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "Minimum edge confidence (0.0–1.0)"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="temporal_chain",
        description=(
            "Bidirectional causal chain: returns both upstream causes and downstream effects "
            "for the given event."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "Pivot event ID"},
                "depth": {"type": "integer", "default": 4, "description": "Maximum chain depth in each direction"},
                "min_confidence": {"type": "number", "default": 0.0, "description": "Minimum edge confidence (0.0–1.0)"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="temporal_auto_detect",
        description=(
            "Run the causal edge auto-detection pipeline over all events and insert detected "
            "edges into knowledge_edges."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "default": False, "description": "If true, detect but do not insert edges"},
            },
            "required": [],
        },
    ),
    Tool(
        name="temporal_context",
        description=(
            "Return a structured temporal snapshot of the current brain state: current epoch, "
            "project age, recent activity, agent cadence, memory health, and open causal threads."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="event_link",
        description=(
            "Explicitly link two events as cause -> effect (agent-reported causation). "
            "Prevents cycles in the causal DAG."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cause_event_id": {"type": "integer", "description": "ID of the causing event"},
                "effect_event_id": {"type": "integer", "description": "ID of the resulting event"},
                "relation": {
                    "type": "string",
                    "default": "causes",
                    "enum": ["causes", "triggered_by", "contributes_to"],
                    "description": "Relation type",
                },
                "confidence": {"type": "number", "default": 0.9, "description": "Edge confidence (0.0–1.0)"},
                "agent": {"type": "string", "description": "Agent ID reporting the link"},
            },
            "required": ["cause_event_id", "effect_event_id"],
        },
    ),
    Tool(
        name="epoch_detect",
        description=(
            "Auto-detect epoch boundaries from event history using time gaps and recent-context divergence. "
            "Returns suggested epoch ranges but does not create them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "gap_hours": {"type": "number", "default": 48.0, "description": "Time gap (hours) that triggers a boundary even without contextual signal"},
                "window_size": {"type": "integer", "default": 8, "description": "Left-side context window size; right-side smoothing lookahead is min(window_size, 3)"},
                "min_window": {"type": "integer", "default": 4, "description": "Minimum events needed on the left side before divergence analysis runs"},
                "topic_shift_threshold": {"type": "number", "default": 0.45, "description": "Minimum recent-context divergence threshold required to flag a non-gap boundary"},
                "min_boundary_distance": {"type": "integer", "default": 8, "description": "Minimum event distance between boundaries"},
                "min_events": {"type": "integer", "default": 5, "description": "Minimum events per suggested epoch"},
                "verbose": {"type": "boolean", "default": False, "description": "Include raw boundary details in response"},
            },
            "required": [],
        },
    ),
    Tool(
        name="epoch_create",
        description=(
            "Create a named epoch and backfill existing events/memories into it. "
            "Optionally nest within a parent epoch."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Epoch name"},
                "started": {"type": "string", "description": "Start timestamp (ISO 8601 or YYYY-MM-DD)"},
                "description": {"type": "string", "description": "Optional description"},
                "ended": {"type": "string", "description": "End timestamp (optional; open epoch if omitted)"},
                "parent": {"type": "integer", "description": "Parent epoch ID for nesting"},
            },
            "required": ["name", "started"],
        },
    ),
    Tool(
        name="epoch_list",
        description="List all epochs with their event and memory counts.",
        inputSchema={
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "default": False, "description": "Only return currently-active epochs"},
                "limit": {"type": "integer", "description": "Maximum number of epochs to return"},
            },
            "required": [],
        },
    ),
]

DISPATCH: dict = {tool.name: _handle for tool in TOOLS}
