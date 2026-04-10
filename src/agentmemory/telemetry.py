"""brainctl telemetry — unified health dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _safe_int(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _safe_float(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> float:
    try:
        row = conn.execute(sql, params).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _score_to_grade(score: float) -> str:
    if score >= 0.90:
        return "A"
    if score >= 0.85:
        return "B+"
    if score >= 0.70:
        return "B"
    if score >= 0.50:
        return "C"
    if score >= 0.30:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Section builders — each returns a sub-dict and a score contribution (0–1)
# ---------------------------------------------------------------------------

def _memory_section(conn: sqlite3.Connection, agent_id: str | None) -> tuple[dict, float]:
    """Memory counts and avg_confidence."""
    agent_clause = "AND agent_id = ?" if agent_id else ""
    params = (agent_id,) if agent_id else ()

    total = _safe_int(conn, f"SELECT COUNT(*) FROM memories WHERE 1=1 {agent_clause}", params)
    active = _safe_int(
        conn,
        f"SELECT COUNT(*) FROM memories WHERE retired_at IS NULL {agent_clause}",
        params,
    )
    retired = total - active
    avg_conf = _safe_float(
        conn,
        f"SELECT AVG(confidence) FROM memories WHERE retired_at IS NULL {agent_clause}",
        params,
    )

    # Score: avg_confidence scaled (higher = better), capped at 1
    score = min(avg_conf, 1.0) if active > 0 else 0.5  # no memories → neutral

    return {
        "count": total,
        "active": active,
        "retired": retired,
        "avg_confidence": round(avg_conf, 4),
    }, score


def _events_section(conn: sqlite3.Connection, agent_id: str | None) -> tuple[dict, float]:
    """Event counts, last-7d count, top types."""
    if not _table_exists(conn, "events"):
        return {"count": 0, "last_7d": 0, "top_types": []}, 0.5

    agent_clause = "AND agent_id = ?" if agent_id else ""
    params = (agent_id,) if agent_id else ()

    total = _safe_int(conn, f"SELECT COUNT(*) FROM events WHERE 1=1 {agent_clause}", params)

    # last_7d — use julianday arithmetic for pure-SQLite compatibility
    last_7d = _safe_int(
        conn,
        f"SELECT COUNT(*) FROM events WHERE julianday('now') - julianday(created_at) <= 7 {agent_clause}",
        params,
    )

    try:
        type_rows = conn.execute(
            f"SELECT event_type, COUNT(*) as cnt FROM events WHERE 1=1 {agent_clause} "
            "GROUP BY event_type ORDER BY cnt DESC LIMIT 5",
            params,
        ).fetchall()
        top_types = [{"type": r["event_type"], "count": r["cnt"]} for r in type_rows]
    except Exception:
        top_types = []

    # Score based on recent activity
    score = min(1.0, last_7d / 10.0) if last_7d > 0 else (0.3 if total > 0 else 0.1)

    return {
        "count": total,
        "last_7d": last_7d,
        "top_types": top_types,
    }, score


def _entities_section(conn: sqlite3.Connection, agent_id: str | None) -> tuple[dict, float]:
    """Entity counts, active, top types."""
    if not _table_exists(conn, "entities"):
        return {"count": 0, "active": 0, "top_types": []}, 0.5

    agent_clause = "AND agent_id = ?" if agent_id else ""
    params = (agent_id,) if agent_id else ()

    total = _safe_int(conn, f"SELECT COUNT(*) FROM entities WHERE 1=1 {agent_clause}", params)
    active = _safe_int(
        conn,
        f"SELECT COUNT(*) FROM entities WHERE retired_at IS NULL {agent_clause}",
        params,
    )

    try:
        type_rows = conn.execute(
            f"SELECT entity_type, COUNT(*) as cnt FROM entities WHERE retired_at IS NULL {agent_clause} "
            "GROUP BY entity_type ORDER BY cnt DESC LIMIT 5",
            params,
        ).fetchall()
        top_types = [{"type": r["entity_type"], "count": r["cnt"]} for r in type_rows]
    except Exception:
        top_types = []

    score = 0.8 if active > 0 else 0.5  # entities are a bonus, not required

    return {
        "count": total,
        "active": active,
        "top_types": top_types,
    }, score


def _decisions_section(conn: sqlite3.Connection, agent_id: str | None) -> tuple[dict, float]:
    """Decision count."""
    if not _table_exists(conn, "decisions"):
        return {"count": 0}, 0.5

    agent_clause = "AND agent_id = ?" if agent_id else ""
    params = (agent_id,) if agent_id else ()

    count = _safe_int(conn, f"SELECT COUNT(*) FROM decisions WHERE 1=1 {agent_clause}", params)
    score = 0.8 if count > 0 else 0.5

    return {"count": count}, score


def _affect_section(conn: sqlite3.Connection, agent_id: str | None) -> tuple[dict | None, float]:
    """Latest affect state."""
    if not _table_exists(conn, "affect_log"):
        return None, 0.5

    try:
        q = "SELECT valence, arousal, dominance, affect_label, functional_state, created_at FROM affect_log"
        params: tuple = ()
        if agent_id:
            q += " WHERE agent_id = ?"
            params = (agent_id,)
        q += " ORDER BY created_at DESC LIMIT 1"

        row = conn.execute(q, params).fetchone()
        if not row:
            return None, 0.5

        valence = row["valence"] if row["valence"] is not None else 0.0
        arousal = row["arousal"] if row["arousal"] is not None else 0.0

        # Score: valence in healthy range (> 0), arousal not too high
        val_score = min(1.0, max(0.0, (valence + 1.0) / 2.0))  # map [-1,1] → [0,1]
        aro_score = 1.0 - min(1.0, max(0.0, arousal))           # lower arousal = calmer
        affect_score = (val_score * 0.6 + aro_score * 0.4)

        return {
            "current_state": row["affect_label"],
            "functional_state": row["functional_state"],
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "recorded_at": row["created_at"],
        }, affect_score

    except Exception:
        return None, 0.5


def _budget_section(conn: sqlite3.Connection, agent_id: str | None) -> tuple[dict, float]:
    """Token estimate from access_log."""
    if not _table_exists(conn, "access_log"):
        return {"token_estimate": 0, "per_agent": []}, 0.5

    try:
        q = "SELECT agent_id, COALESCE(SUM(tokens_consumed), 0) AS total FROM access_log"
        if agent_id:
            q += " WHERE agent_id = ?"
            rows = conn.execute(q, (agent_id,)).fetchall()
        else:
            q += " GROUP BY agent_id ORDER BY total DESC LIMIT 10"
            rows = conn.execute(q).fetchall()

        per_agent = [
            {"agent_id": r["agent_id"], "tokens": r["total"]}
            for r in rows
            if r["agent_id"]
        ]
        token_estimate = sum(r["tokens"] for r in per_agent)
        score = 1.0 if token_estimate < 100_000 else (0.7 if token_estimate < 500_000 else 0.4)

        return {
            "token_estimate": token_estimate,
            "per_agent": per_agent,
        }, score

    except Exception:
        return {"token_estimate": 0, "per_agent": []}, 0.5


def _compute_alerts(
    memory: dict,
    events: dict,
    affect: dict | None,
) -> list[str]:
    """Generate human-readable alert strings from section data."""
    alerts: list[str] = []

    # Memory alerts
    if memory["active"] == 0:
        alerts.append("No active memories — brain is empty")
    elif memory["avg_confidence"] < 0.4:
        alerts.append(
            f"Average memory confidence is low ({memory['avg_confidence']:.2f}) — "
            "consider reviewing or retiring low-quality memories"
        )

    # Event alerts
    if events["count"] > 0 and events["last_7d"] == 0:
        alerts.append("No events logged in the last 7 days — event pipeline may be stalled")

    # Affect alerts
    if affect is not None:
        if affect.get("valence") is not None and affect["valence"] < -0.5:
            alerts.append(
                f"Affect valence critically low ({affect['valence']:.2f}) — "
                "agent may be in distress"
            )
        if affect.get("arousal") is not None and affect["arousal"] > 0.85:
            alerts.append(
                f"Affect arousal very high ({affect['arousal']:.2f}) — "
                "agent may be overloaded"
            )

    return alerts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_dashboard(db_path: str, agent_id: str | None = None) -> dict[str, Any]:
    """
    Compute and return a unified health dashboard for brain.db.

    Parameters
    ----------
    db_path:  Path to brain.db (string).
    agent_id: If provided, filter counts to this agent only.

    Returns
    -------
    dict with keys: health_score, grade, memory, events, entities,
    decisions, affect, budget, alerts, computed_at.
    """
    conn = _connect(db_path)
    try:
        memory_data, mem_score = _memory_section(conn, agent_id)
        events_data, evt_score = _events_section(conn, agent_id)
        entities_data, ent_score = _entities_section(conn, agent_id)
        decisions_data, dec_score = _decisions_section(conn, agent_id)
        affect_data, aff_score = _affect_section(conn, agent_id)
        budget_data, bud_score = _budget_section(conn, agent_id)
    finally:
        conn.close()

    # Weighted composite health score
    # Weights reflect importance to overall brain health
    weights = {
        "memory":    0.35,
        "events":    0.20,
        "entities":  0.10,
        "decisions": 0.10,
        "affect":    0.15,
        "budget":    0.10,
    }
    scores = {
        "memory":    mem_score,
        "events":    evt_score,
        "entities":  ent_score,
        "decisions": dec_score,
        "affect":    aff_score,
        "budget":    bud_score,
    }
    health_score = sum(scores[k] * weights[k] for k in weights)
    health_score = round(min(1.0, max(0.0, health_score)), 4)

    alerts = _compute_alerts(memory_data, events_data, affect_data)

    return {
        "health_score": health_score,
        "grade": _score_to_grade(health_score),
        "memory": memory_data,
        "events": events_data,
        "entities": entities_data,
        "decisions": decisions_data,
        "affect": affect_data,
        "budget": budget_data,
        "alerts": alerts,
        "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
