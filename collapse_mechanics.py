#!/usr/bin/env python3
"""
collapse_mechanics.py — Belief Collapse Mechanics for AgentMemory (COS-411)

Implements measurement operators and collapse event logging for the Quantum
Cognition framework. Temporal decoherence is the primary collapse pathway
tracked by Epoch (Temporal Cognition Engineer).

Trigger types:
  task_checkout       — agent commits to a task requiring belief resolution
  direct_query        — yes/no question forces definite answer from superposition
  evidence_threshold  — accumulated evidence spread exceeds 0.4 → collapse
  time_decoherence    — coherence < 0.1 after time passage → natural collapse
"""

import json
import math
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_amplitudes(raw: str | None) -> dict:
    """Parse amplitudes JSON {state: complex_amplitude}. Returns {} on failure."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def is_superposed(memory_id: str | int, db_path: str | Path | None = None) -> bool:
    """
    Returns True if the memory/belief is in quantum superposition.

    Checks agent_beliefs.is_superposed for belief IDs, then falls back to
    memories.hilbert_projection / amplitudes for memory IDs.
    """
    with _connect(db_path) as conn:
        # Check agent_beliefs first (preferred — explicit flag)
        row = conn.execute(
            "SELECT is_superposed FROM agent_beliefs WHERE id = ?",
            (str(memory_id),)
        ).fetchone()
        if row is not None:
            return bool(row["is_superposed"])

        # Fall back to memories: superposed if hilbert_projection is non-null
        # and coherence_syndrome indicates active superposition (> 0)
        row = conn.execute(
            "SELECT hilbert_projection, coherence_syndrome, decoherence_rate FROM memories WHERE id = ?",
            (str(memory_id),)
        ).fetchone()
        if row is not None:
            return (
                row["hilbert_projection"] is not None
                and row["coherence_syndrome"] is not None
                and float(row["coherence_syndrome"] or 0.0) > 0.05
            )

    return False


def compute_collapse_probability(amplitudes: dict, target_state: str) -> float:
    """
    Returns |amplitude[target_state]|² (Born rule probability).

    amplitudes: dict mapping state label → amplitude value.
                Values can be real floats, complex strings ('a+bj'), or dicts
                with keys 're' and 'im'.
    """
    if target_state not in amplitudes:
        return 0.0

    raw = amplitudes[target_state]

    # Parse to complex
    if isinstance(raw, (int, float)):
        z = complex(raw, 0.0)
    elif isinstance(raw, complex):
        z = raw
    elif isinstance(raw, str):
        try:
            z = complex(raw)
        except ValueError:
            return 0.0
    elif isinstance(raw, dict):
        z = complex(raw.get("re", 0.0), raw.get("im", 0.0))
    else:
        return 0.0

    return abs(z) ** 2


def evaluate_coherence(memory_id: str | int, db_path: str | Path | None = None) -> float:
    """
    Returns the current coherence score for a memory or belief (0.0–1.0).

    For agent_beliefs: uses coherence_score column.
    For memories: uses coherence_syndrome (scaled 0–1 by decoherence_rate).
    Temporal decoherence: older beliefs lose coherence at their decoherence_rate.
    """
    with _connect(db_path) as conn:
        # agent_beliefs path
        row = conn.execute(
            "SELECT coherence_score, created_at, last_updated_at FROM agent_beliefs WHERE id = ?",
            (str(memory_id),)
        ).fetchone()
        if row is not None:
            base = float(row["coherence_score"] or 0.0)
            # Apply temporal decay since last update
            updated_at = row["last_updated_at"] or row["created_at"]
            if updated_at:
                age_days = _age_in_days(updated_at)
                # Mild exponential decay: coherence halves every 7 days
                temporal_factor = math.exp(-0.693 * age_days / 7.0)
                return round(base * temporal_factor, 4)
            return round(base, 4)

        # memories path
        row = conn.execute(
            """SELECT coherence_syndrome, decoherence_rate, created_at, last_recalled_at
               FROM memories WHERE id = ?""",
            (str(memory_id),)
        ).fetchone()
        if row is not None:
            syndrome = float(row["coherence_syndrome"] or 0.0)
            rate = float(row["decoherence_rate"] or 0.01)
            ref_ts = row["last_recalled_at"] or row["created_at"]
            age_days = _age_in_days(ref_ts) if ref_ts else 0.0
            # Exponential decoherence: C(t) = syndrome * exp(-rate * t)
            return round(syndrome * math.exp(-rate * age_days), 4)

    return 0.0


def _age_in_days(ts_str: str) -> float:
    """Return age in days from ISO timestamp to now."""
    try:
        # Accept YYYY-MM-DD HH:MM:SS or YYYY-MM-DDTHH:MM:SS
        ts_str = ts_str.replace(" ", "T")
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def log_collapse_event(
    db_path: str | Path | None,
    agent_id: str,
    belief_id: str,
    trigger_type: str,
    trigger_id: str | None,
    collapsed_state: str,
    collapse_probability: float,
    pre_collapse_amplitudes: dict,
    pre_collapse_coherence: float,
) -> str:
    """
    Log a belief collapse event to belief_collapse_events.
    Returns the collapse_event_id.

    trigger_type: task_checkout | direct_query | evidence_threshold | time_decoherence
    """
    event_id = str(uuid.uuid4())
    context = json.dumps({
        "trigger_id": trigger_id,
        "pre_amplitudes": pre_collapse_amplitudes,
        "pre_coherence": pre_collapse_coherence,
    })

    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO belief_collapse_events
               (id, belief_id, agent_id, collapsed_state, measured_amplitude,
                collapse_type, collapse_context, collapse_fidelity, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                belief_id,
                agent_id,
                collapsed_state,
                collapse_probability,
                trigger_type,
                context,
                pre_collapse_coherence,   # use coherence as fidelity proxy
                _now(),
            )
        )
        conn.commit()

    return event_id


def force_collapse(
    db_path: str | Path | None,
    agent_id: str,
    belief_id: str,
    trigger_type: str,
    trigger_id: str | None,
) -> str:
    """
    Sample outcome from amplitude distribution, log the event, update the belief.

    For agent_beliefs: clears is_superposed, updates coherence_score to 0 (pure state).
    For memories: clears coherence_syndrome (collapsed = no longer superposed).

    Returns the collapsed_state label.
    """
    path = Path(db_path) if db_path else DB_PATH

    # Fetch amplitudes and coherence
    pre_amplitudes: dict = {}
    pre_coherence: float = evaluate_coherence(belief_id, path)

    with _connect(path) as conn:
        # Try agent_beliefs
        row = conn.execute(
            "SELECT belief_density_matrix, coherence_score, topic FROM agent_beliefs WHERE id = ?",
            (str(belief_id),)
        ).fetchone()
        if row is not None:
            raw_matrix = row["belief_density_matrix"]
            if raw_matrix:
                pre_amplitudes = _parse_amplitudes(
                    raw_matrix.decode() if isinstance(raw_matrix, bytes) else raw_matrix
                )
            if not pre_amplitudes:
                # Synthesize two basis states from the topic for demo purposes
                topic = row["topic"] or "unknown"
                pre_amplitudes = {f"{topic}_true": 0.7071, f"{topic}_false": 0.7071}

        else:
            # Try memories
            row = conn.execute(
                "SELECT hilbert_projection, coherence_syndrome, content FROM memories WHERE id = ?",
                (str(belief_id),)
            ).fetchone()
            if row is not None:
                hp = row["hilbert_projection"]
                if hp:
                    pre_amplitudes = _parse_amplitudes(hp)
                if not pre_amplitudes:
                    pre_amplitudes = {"true": 0.7071, "false": 0.7071}

        if not pre_amplitudes:
            pre_amplitudes = {"true": 0.5, "false": 0.5}

        # Sample collapsed state using Born rule
        collapsed_state = _sample_born_rule(pre_amplitudes)
        collapse_prob = compute_collapse_probability(pre_amplitudes, collapsed_state)

    # Log the event
    event_id = log_collapse_event(
        path,
        agent_id=agent_id,
        belief_id=belief_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        collapsed_state=collapsed_state,
        collapse_probability=collapse_prob,
        pre_collapse_amplitudes=pre_amplitudes,
        pre_collapse_coherence=pre_coherence,
    )

    # Update belief state: mark as collapsed (pure state, not superposed)
    _apply_collapse_to_db(path, belief_id, collapsed_state, event_id)

    return collapsed_state


def _sample_born_rule(amplitudes: dict) -> str:
    """Sample a state using Born rule (|amplitude|² probabilities)."""
    states = list(amplitudes.keys())
    probs = [compute_collapse_probability(amplitudes, s) for s in states]
    total = sum(probs)
    if total <= 0:
        return random.choice(states)
    probs = [p / total for p in probs]
    r = random.random()
    cumulative = 0.0
    for state, prob in zip(states, probs):
        cumulative += prob
        if r <= cumulative:
            return state
    return states[-1]


def _apply_collapse_to_db(
    db_path: Path,
    belief_id: str,
    collapsed_state: str,
    event_id: str,
):
    """Update belief/memory to reflect collapsed (pure) state."""
    with _connect(db_path) as conn:
        # Try agent_beliefs
        updated = conn.execute(
            """UPDATE agent_beliefs
               SET is_superposed = 0,
                   coherence_score = 0.0,
                   belief_density_matrix = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                json.dumps({"collapsed_to": collapsed_state, "collapse_event_id": event_id}),
                _now(),
                str(belief_id),
            )
        ).rowcount

        if not updated:
            # Try memories: clear coherence_syndrome to signal classical state
            conn.execute(
                """UPDATE memories
                   SET coherence_syndrome = 0.0,
                       hilbert_projection = ?
                   WHERE id = ?""",
                (
                    json.dumps({"collapsed_to": collapsed_state, "collapse_event_id": event_id}),
                    str(belief_id),
                )
            )

        conn.commit()


# ---------------------------------------------------------------------------
# Collapse trigger hooks
# ---------------------------------------------------------------------------

def check_and_collapse_on_query(
    belief_id: str,
    agent_id: str,
    query: str,
    db_path: str | Path | None = None,
) -> Optional[str]:
    """
    Direct query trigger: if belief is superposed, force collapse.
    Returns collapsed state or None if not superposed.
    """
    if not is_superposed(belief_id, db_path):
        return None
    return force_collapse(db_path, agent_id, belief_id, "direct_query", query)


def check_and_collapse_on_task(
    belief_id: str,
    agent_id: str,
    task_id: str,
    db_path: str | Path | None = None,
) -> Optional[str]:
    """
    Task checkout trigger: if belief is superposed, force collapse.
    Returns collapsed state or None if not superposed.
    """
    if not is_superposed(belief_id, db_path):
        return None
    return force_collapse(db_path, agent_id, belief_id, "task_checkout", task_id)


def check_and_collapse_on_evidence(
    belief_id: str,
    agent_id: str,
    evidence_id: str,
    evidence_amplitudes: dict,
    db_path: str | Path | None = None,
) -> Optional[str]:
    """
    Evidence threshold trigger: if evidence_score_spread > 0.4, force collapse.
    Returns collapsed state or None if below threshold.
    """
    if not evidence_amplitudes:
        return None
    values = [compute_collapse_probability(evidence_amplitudes, s) for s in evidence_amplitudes]
    if not values or max(values) - min(values) <= 0.4:
        return None
    if not is_superposed(belief_id, db_path):
        return None
    return force_collapse(db_path, agent_id, belief_id, "evidence_threshold", evidence_id)


def check_and_collapse_on_time(
    belief_id: str,
    agent_id: str,
    threshold: float = 0.1,
    db_path: str | Path | None = None,
) -> Optional[str]:
    """
    Time decoherence trigger: if coherence < threshold, force collapse.
    Returns collapsed state or None.
    """
    coherence = evaluate_coherence(belief_id, db_path)
    if coherence >= threshold:
        return None
    if not is_superposed(belief_id, db_path):
        return None
    return force_collapse(db_path, agent_id, belief_id, "time_decoherence", None)


# ---------------------------------------------------------------------------
# Query helpers (for brainctl collapse-log and collapse-stats)
# ---------------------------------------------------------------------------

def list_collapse_events(
    db_path: str | Path | None = None,
    belief_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List collapse events, optionally filtered by belief or agent."""
    with _connect(db_path) as conn:
        clauses = []
        params: list = []
        if belief_id:
            clauses.append("belief_id = ?")
            params.append(belief_id)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""SELECT id, belief_id, agent_id, collapsed_state, measured_amplitude,
                       collapse_type, collapse_context, collapse_fidelity, created_at
                FROM belief_collapse_events
                {where}
                ORDER BY created_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def collapse_stats(db_path: str | Path | None = None) -> dict:
    """Aggregate statistics: trigger distribution, avg probability, total collapses."""
    with _connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM belief_collapse_events"
        ).fetchone()[0]

        by_type = conn.execute(
            """SELECT collapse_type, COUNT(*) as cnt,
                      AVG(measured_amplitude) as avg_prob,
                      AVG(collapse_fidelity) as avg_fidelity
               FROM belief_collapse_events
               GROUP BY collapse_type
               ORDER BY cnt DESC"""
        ).fetchall()

        avg_prob = conn.execute(
            "SELECT AVG(measured_amplitude) FROM belief_collapse_events"
        ).fetchone()[0]

        recent = conn.execute(
            """SELECT COUNT(*) FROM belief_collapse_events
               WHERE created_at > datetime('now', '-7 days')"""
        ).fetchone()[0]

        return {
            "total_collapses": total,
            "collapses_last_7d": recent,
            "avg_collapse_probability": round(avg_prob or 0.0, 4),
            "by_trigger_type": [
                {
                    "trigger": r["collapse_type"],
                    "count": r["cnt"],
                    "avg_probability": round(r["avg_prob"] or 0.0, 4),
                    "avg_fidelity": round(r["avg_fidelity"] or 0.0, 4),
                }
                for r in by_type
            ],
        }
