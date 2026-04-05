#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
situation_model_builder.py — COS-193
4-phase situation model construction pipeline for brain.db.
Based on COS-123 research (~/agentmemory/research/wave3/04_situation_models.md).

Usage:
  python3 situation_model_builder.py build "project:agentmemory"
  python3 situation_model_builder.py list
  python3 situation_model_builder.py get "project:agentmemory"
  python3 situation_model_builder.py get "project:agentmemory" --format json
  python3 situation_model_builder.py refresh-stale   # rebuild stale models
  python3 situation_model_builder.py archive "project:agentmemory"
"""

import sqlite3
import json
import sys
import re
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
TTL_SECONDS = 21600  # 6 hours


@dataclass
class SituationModel:
    anchor: str
    name: str = ""
    memories: list = field(default_factory=list)
    events: list = field(default_factory=list)
    timeline: list = field(default_factory=list)
    agents: dict = field(default_factory=dict)
    phases: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    narrative: str = ""
    coherence_score: float = 0.0
    completeness: float = 0.0
    source_memory_ids: list = field(default_factory=list)
    source_event_ids: list = field(default_factory=list)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_cached(anchor: str, force: bool = False) -> Optional[dict]:
    """Return cached model if still within TTL, else None."""
    if force:
        return None
    conn = connect()
    row = conn.execute(
        "SELECT *, (julianday('now') - julianday(updated_at)) * 86400 AS age_sec "
        "FROM situation_models WHERE query_anchor = ? AND status != 'archived'",
        (anchor,)
    ).fetchone()
    conn.close()
    if row and row["age_sec"] < row["ttl_seconds"]:
        return dict(row)
    return None


def detect_contradictions(memories: list) -> list:
    """
    Simple contradiction detection: flag same-entity memories with opposing signal words.
    Full semantic contradiction detection is a Phase 2 enhancement (see COS-179 follow-up).
    """
    negation_pairs = [
        ("done", "not done"), ("complete", "incomplete"), ("blocked", "unblocked"),
        ("active", "inactive"), ("success", "failure"), ("pass", "fail"),
    ]
    contradictions = []
    for i, m1 in enumerate(memories):
        for m2 in memories[i+1:]:
            c1, c2 = m1["content"].lower(), m2["content"].lower()
            for pos, neg in negation_pairs:
                if pos in c1 and neg in c2:
                    contradictions.append({
                        "memory_id_a": str(m1["id"]),
                        "memory_id_b": str(m2["id"]),
                        "contradiction": f"Memory {m1['id']} says '{pos}'; memory {m2['id']} says '{neg}'",
                        "resolution": "newer_wins" if m1["created_at"] < m2["created_at"] else "manual"
                    })
                elif neg in c1 and pos in c2:
                    contradictions.append({
                        "memory_id_a": str(m1["id"]),
                        "memory_id_b": str(m2["id"]),
                        "contradiction": f"Memory {m1['id']} says '{neg}'; memory {m2['id']} says '{pos}'",
                        "resolution": "newer_wins" if m1["created_at"] < m2["created_at"] else "manual"
                    })
    return contradictions


def build_situation_model(anchor: str, force: bool = False) -> SituationModel:
    """
    Build or rebuild a situation model for the given anchor.
    Phase 1: Construction (retrieve all relevant memories + events)
    Phase 2: Integration (build timeline, extract agents, detect contradictions)
    Phase 3: Scoring (coherence + completeness)
    Phase 4: Caching (persist to situation_models table with TTL)
    """
    cached = get_cached(anchor, force=force)
    if cached:
        m = SituationModel(anchor=anchor, name=cached["name"])
        m.narrative = cached["narrative"] or ""
        m.coherence_score = cached["coherence_score"]
        m.completeness = cached["completeness"]
        m.source_memory_ids = json.loads(cached["source_memory_ids"] or "[]")
        m.source_event_ids = json.loads(cached["source_event_ids"] or "[]")
        return m

    conn = connect()
    anchor_clean = re.sub(r'^(project:|incident:|agent:)', '', anchor)
    model = SituationModel(anchor=anchor, name=f"situation:{anchor}")

    # ─────────────────────────────────────────────
    # PHASE 1: Construction — gather raw materials
    # ─────────────────────────────────────────────
    memories = conn.execute("""
        SELECT m.id, m.content, m.category, m.scope, m.confidence,
               m.created_at, m.recalled_count, a.id as agent_name
        FROM memories m
        JOIN agents a ON m.agent_id = a.id
        WHERE m.retired_at IS NULL
          AND (m.scope LIKE ? OR m.content LIKE ?)
        ORDER BY m.created_at ASC
    """, [f"%{anchor_clean}%", f"%{anchor_clean}%"]).fetchall()

    events = conn.execute("""
        SELECT e.id, e.summary, e.event_type, e.importance,
               e.project, e.created_at, a.id as agent_name
        FROM events e
        JOIN agents a ON e.agent_id = a.id
        WHERE (e.summary LIKE ? OR e.project LIKE ?)
        ORDER BY e.created_at ASC
        LIMIT 100
    """, [f"%{anchor_clean}%", f"%{anchor_clean}%"]).fetchall()

    model.source_memory_ids = [str(m["id"]) for m in memories]
    model.source_event_ids = [str(e["id"]) for e in events]

    # ─────────────────────────────────────────────
    # PHASE 2: Integration — build coherent structure
    # ─────────────────────────────────────────────

    # 2a. Timeline from events
    timeline_entries = []
    for ev in events:
        timeline_entries.append({
            "at": ev["created_at"],
            "agent": ev["agent_name"],
            "event": ev["summary"][:150],
            "type": ev["event_type"],
            "importance": ev["importance"]
        })
    timeline_entries.sort(key=lambda x: x["at"])
    model.timeline = timeline_entries

    # 2b. Agent role map
    agent_map = {}
    for ev in events:
        name = ev["agent_name"]
        if name not in agent_map:
            agent_map[name] = {"role": "participant", "event_count": 0, "last_action": None}
        agent_map[name]["event_count"] += 1
        agent_map[name]["last_action"] = ev["summary"][:100]
    # Promote high-activity agents to 'owner'
    if agent_map:
        max_events = max(v["event_count"] for v in agent_map.values())
        for name, info in agent_map.items():
            if info["event_count"] == max_events and max_events > 2:
                info["role"] = "owner"
    model.agents = agent_map

    # 2c. Phase detection from event signals
    phases = []
    done_kws = {"done", "shipped", "complete", "delivered", "deployed", "merged", "closed"}
    blocked_kws = {"blocked", "failed", "error", "crash", "rejected"}
    in_progress_kws = {"started", "begun", "working", "implementing", "researching"}
    for ev in events:
        summary_lower = ev["summary"].lower()
        words = set(summary_lower.split())
        if words & done_kws:
            status = "done"
        elif words & blocked_kws:
            status = "blocked"
        elif words & in_progress_kws:
            status = "in_progress"
        else:
            continue
        phases.append({
            "milestone": ev["summary"][:120],
            "agent": ev["agent_name"],
            "at": ev["created_at"],
            "status": status
        })
    model.phases = phases

    # 2d. Contradiction detection
    model.contradictions = detect_contradictions(list(memories))

    # ─────────────────────────────────────────────
    # PHASE 3: Scoring
    # ─────────────────────────────────────────────
    n_mem = len(memories)
    n_ev = len(events)
    n_phases = len(phases)
    n_agents = len(agent_map)
    n_contradictions = len(model.contradictions)

    # Completeness: enough sources?
    if n_mem >= 5 and n_ev >= 10:
        completeness = 0.9
    elif n_mem >= 2 or n_ev >= 3:
        completeness = 0.6
    elif n_mem >= 1 or n_ev >= 1:
        completeness = 0.3
    else:
        completeness = 0.0

    # Coherence components
    temporal_ok = 1.0  # sorted, monotone by construction
    contradiction_penalty = min(0.5, n_contradictions * 0.1)
    agent_coverage = min(1.0, n_agents / max(n_phases, 1))
    causal_density = min(1.0, n_phases / max(n_ev, 1))

    coherence = (
        temporal_ok * 0.25 +
        (1.0 - contradiction_penalty) * 0.30 +
        completeness * 0.20 +
        agent_coverage * 0.15 +
        causal_density * 0.10
    )
    model.coherence_score = round(coherence, 3)
    model.completeness = round(completeness, 3)

    # ─────────────────────────────────────────────
    # PHASE 4: Narrative synthesis + cache
    # ─────────────────────────────────────────────
    agent_names = ", ".join(list(agent_map.keys())[:5]) or "unknown"
    done_phases = [p for p in phases if p["status"] == "done"]
    blocked_phases = [p for p in phases if p["status"] == "blocked"]
    latest_event = timeline_entries[-1]["event"] if timeline_entries else "no recent events"

    status_line = f"{len(done_phases)} milestones done"
    if blocked_phases:
        status_line += f", {len(blocked_phases)} blocked"

    model.narrative = (
        f"Situation: {anchor_clean}\n"
        f"Agents: {agent_names}\n"
        f"Progress: {status_line}\n"
        f"Most recent: {latest_event}\n"
        f"Sources: {n_mem} memories, {n_ev} events | "
        f"Coherence: {model.coherence_score:.2f} | Completeness: {model.completeness:.2f}"
    )

    structured = {
        "anchor": anchor,
        "agents": agent_map,
        "phases": phases[-10:],
        "timeline": timeline_entries[-20:],
        "contradictions": model.contradictions,
        "open_questions": model.open_questions,
        "coherence": model.coherence_score,
        "completeness": model.completeness,
        "built_at": now_utc()
    }

    # Determine model status
    model_status = "active"
    if model.coherence_score < 0.3:
        model_status = "contradictory"
    elif completeness == 0.0:
        model_status = "stale"

    # Save to DB (upsert by name)
    model_id = str(uuid.uuid4()).replace("-", "")
    conn.execute("""
        INSERT INTO situation_models
          (id, name, query_anchor, narrative, structured, coherence_score,
           completeness, status, source_memory_ids, source_event_ids,
           last_event_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(name) DO UPDATE SET
          narrative=excluded.narrative,
          structured=excluded.structured,
          coherence_score=excluded.coherence_score,
          completeness=excluded.completeness,
          status=excluded.status,
          source_memory_ids=excluded.source_memory_ids,
          source_event_ids=excluded.source_event_ids,
          last_event_id=excluded.last_event_id,
          updated_at=CURRENT_TIMESTAMP
    """, [
        model_id, model.name, model.anchor, model.narrative,
        json.dumps(structured), model.coherence_score, model.completeness,
        model_status,
        json.dumps(model.source_memory_ids), json.dumps(model.source_event_ids),
        model.source_event_ids[-1] if model.source_event_ids else None,
    ])

    # Store contradiction records
    for c in model.contradictions:
        conn.execute("""
            INSERT OR IGNORE INTO situation_model_contradictions
              (model_id, memory_id_a, memory_id_b, contradiction, resolution)
            VALUES ((SELECT id FROM situation_models WHERE name=?), ?, ?, ?, ?)
        """, [model.name, c["memory_id_a"], c["memory_id_b"],
               c["contradiction"], c.get("resolution")])

    conn.commit()
    conn.close()
    return model


def list_models():
    conn = connect()
    rows = conn.execute("""
        SELECT name, query_anchor, status, coherence_score, completeness,
               updated_at,
               (julianday('now') - julianday(updated_at)) * 86400 AS age_sec,
               ttl_seconds
        FROM situation_models
        WHERE status != 'archived'
        ORDER BY updated_at DESC
    """).fetchall()
    conn.close()
    for r in rows:
        fresh = "FRESH" if r["age_sec"] < r["ttl_seconds"] else "STALE"
        print(f"[{fresh}] {r['name']}  coherence={r['coherence_score']:.2f}  "
              f"completeness={r['completeness']:.2f}  status={r['status']}  "
              f"updated={r['updated_at'][:19]}")
    if not rows:
        print("No situation models found.")


def get_model(anchor: str, fmt: str = "narrative"):
    conn = connect()
    row = conn.execute(
        "SELECT * FROM situation_models WHERE query_anchor=? OR name=?",
        (anchor, anchor)
    ).fetchone()
    conn.close()
    if not row:
        print(f"No model found for anchor: {anchor}")
        return
    if fmt == "json":
        print(json.dumps(json.loads(row["structured"]), indent=2))
    else:
        print(row["narrative"])
        print(f"\n[coherence={row['coherence_score']:.2f}  completeness={row['completeness']:.2f}  "
              f"status={row['status']}  updated={row['updated_at'][:19]}]")


def refresh_stale():
    conn = connect()
    stale = conn.execute("""
        SELECT query_anchor FROM situation_models
        WHERE status != 'archived'
          AND (julianday('now') - julianday(updated_at)) * 86400 >= ttl_seconds
    """).fetchall()
    conn.close()
    if not stale:
        print("No stale models to refresh.")
        return
    for row in stale:
        anchor = row["query_anchor"]
        model = build_situation_model(anchor, force=True)
        print(f"Refreshed: {model.name}  coherence={model.coherence_score:.2f}")


def archive_model(anchor: str):
    conn = connect()
    conn.execute(
        "UPDATE situation_models SET status='archived', updated_at=CURRENT_TIMESTAMP "
        "WHERE query_anchor=? OR name=?",
        (anchor, anchor)
    )
    conn.commit()
    conn.close()
    print(f"Archived: {anchor}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "build" and len(args) >= 2:
        force = "--force" in args
        anchor = args[1]
        model = build_situation_model(anchor, force=force)
        print(f"Built: {model.name}")
        print(f"Coherence: {model.coherence_score:.2f} | Completeness: {model.completeness:.2f}")
        print(f"Sources: {len(model.source_memory_ids)} memories, {len(model.source_event_ids)} events")
        print(f"\nNarrative:\n{model.narrative}")
    elif cmd == "list":
        list_models()
    elif cmd == "get" and len(args) >= 2:
        fmt = "json" if "--format" in args and args[args.index("--format") + 1] == "json" else "narrative"
        get_model(args[1], fmt=fmt)
    elif cmd == "refresh-stale":
        refresh_stale()
    elif cmd == "archive" and len(args) >= 2:
        archive_model(args[1])
    elif cmd == "rebuild" and len(args) >= 2:
        model = build_situation_model(args[1], force=True)
        print(f"Rebuilt: {model.name}  coherence={model.coherence_score:.2f}")
        print(model.narrative)
    else:
        print(__doc__)
        sys.exit(1)
