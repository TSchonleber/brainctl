"""brainctl MCP tools — knowledge gaps & expertise."""
from __future__ import annotations
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Gap analysis helpers
# ---------------------------------------------------------------------------

_STALENESS_GAP_DAYS = 7        # memories older than this trigger a staleness hole
_CONFIDENCE_GAP_THRESHOLD = 0.4  # avg_confidence below this = confidence hole


def _days_since(created_at_str: str | None) -> float:
    """Return float days elapsed since the given SQLite/ISO timestamp."""
    if not created_at_str:
        return 0.0
    try:
        ts = created_at_str.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts, fmt)
                    break
                except ValueError:
                    continue
            else:
                return 0.0
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def _compute_coverage_density(count: int, avg_conf: float | None, freshest_at: str | None) -> float:
    """Composite density score: count × avg_confidence × recency_factor."""
    if count == 0 or avg_conf is None:
        return 0.0
    age_days = _days_since(freshest_at)
    recency_factor = max(0.1, 1.0 - 0.02 * age_days)
    return round(count * avg_conf * recency_factor, 4)


def _run_refresh_inline(conn: sqlite3.Connection, now: str) -> int:
    """Refresh knowledge_coverage from current memories. Returns number of scopes updated."""
    scopes = conn.execute(
        "SELECT DISTINCT scope FROM memories WHERE retired_at IS NULL"
    ).fetchall()
    updated = 0
    for row in scopes:
        scope = row["scope"]
        stats = conn.execute("""
            SELECT
                COUNT(*)           AS cnt,
                AVG(confidence)    AS avg_conf,
                MIN(confidence)    AS min_conf,
                MAX(confidence)    AS max_conf,
                MAX(created_at)    AS freshest,
                MIN(created_at)    AS stalest
            FROM memories
            WHERE scope = ? AND retired_at IS NULL
        """, (scope,)).fetchone()
        if not stats or stats["cnt"] == 0:
            continue
        density = _compute_coverage_density(stats["cnt"], stats["avg_conf"], stats["freshest"])
        conn.execute("""
            INSERT INTO knowledge_coverage
                (scope, memory_count, avg_confidence, min_confidence, max_confidence,
                 freshest_memory_at, stalest_memory_at, coverage_density, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                memory_count       = excluded.memory_count,
                avg_confidence     = excluded.avg_confidence,
                min_confidence     = excluded.min_confidence,
                max_confidence     = excluded.max_confidence,
                freshest_memory_at = excluded.freshest_memory_at,
                stalest_memory_at  = excluded.stalest_memory_at,
                coverage_density   = excluded.coverage_density,
                last_computed_at   = excluded.last_computed_at
        """, (scope, stats["cnt"], stats["avg_conf"], stats["min_conf"], stats["max_conf"],
              stats["freshest"], stats["stalest"], density, now))
        updated += 1
    return updated


def _log_gap(conn: sqlite3.Connection, gap_type: str, scope: str, severity: float,
             triggered_by: str | None = None) -> None:
    """Insert a gap record if an identical unresolved gap doesn't already exist."""
    existing = conn.execute(
        "SELECT id FROM knowledge_gaps WHERE gap_type=? AND scope=? AND resolved_at IS NULL",
        (gap_type, scope)
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO knowledge_gaps (gap_type, scope, detected_at, triggered_by, severity) "
        "VALUES (?, ?, ?, ?, ?)",
        (gap_type, scope, _now(), triggered_by, round(severity, 4))
    )


# ---------------------------------------------------------------------------
# Expertise helpers
# ---------------------------------------------------------------------------

_EXPERTISE_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "has", "have", "had", "do", "did", "done", "will", "would", "could",
    "should", "may", "might", "can", "it", "its", "this", "that", "these",
    "those", "not", "no", "so", "if", "then", "when", "where", "how",
    "what", "which", "who", "all", "any", "as", "up", "out", "into",
    "new", "old", "now", "also", "just", "using", "used", "via", "per",
    "add", "get", "set", "run", "use", "see", "let", "put", "try", "fix",
    "make", "take", "give", "show", "need", "more", "some", "than", "only",
}

_EXPERTISE_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _expertise_extract_tokens(text: str | None) -> list[str]:
    tokens = _EXPERTISE_TOKEN_RE.findall((text or "").lower())
    return [t for t in tokens if t not in _EXPERTISE_STOP_WORDS]


def _expertise_scope_to_domain(scope: str | None) -> str | None:
    if not scope or scope == "global":
        return None
    parts = scope.split(":", 1)
    if len(parts) == 2:
        return parts[1].split(":")[0]
    return scope


def _ensure_expertise_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_expertise (
            agent_id       TEXT NOT NULL REFERENCES agents(id),
            domain         TEXT NOT NULL,
            strength       REAL NOT NULL DEFAULT 0.0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_active    TEXT,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (agent_id, domain)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expertise_domain ON agent_expertise(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expertise_strength ON agent_expertise(strength DESC)")
    conn.commit()


def _build_expertise_for_agent(conn: sqlite3.Connection, agent_id: str) -> int:
    """Build/refresh expertise domains for one agent. Returns number of domains upserted."""
    domain_evidence: dict[str, list[str]] = {}

    rows = conn.execute(
        "SELECT scope, category, content, created_at FROM memories "
        "WHERE agent_id=? AND retired_at IS NULL",
        (agent_id,)
    ).fetchall()
    for r in rows:
        ts = r["created_at"]
        d = _expertise_scope_to_domain(r["scope"])
        if d:
            domain_evidence.setdefault(d, []).append(ts)
        if r["category"] and r["category"] not in ("identity",):
            domain_evidence.setdefault(r["category"], []).append(ts)
        for tok in _expertise_extract_tokens(r["content"])[:8]:
            domain_evidence.setdefault(tok, []).append(ts)

    rows = conn.execute(
        "SELECT project, event_type, summary, created_at FROM events WHERE agent_id=?",
        (agent_id,)
    ).fetchall()
    for r in rows:
        ts = r["created_at"]
        if r["project"]:
            domain_evidence.setdefault(r["project"], []).append(ts)
        if r["event_type"] and r["event_type"] not in ("session_start", "session_end"):
            domain_evidence.setdefault(r["event_type"], []).append(ts)
        for tok in _expertise_extract_tokens(r["summary"])[:8]:
            domain_evidence.setdefault(tok, []).append(ts)

    if not domain_evidence:
        return 0

    now_dt = datetime.now(timezone.utc)

    def _rw(ts_str: str) -> float:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_days = max(0.0, (now_dt - ts).total_seconds() / 86400)
            return math.exp(-0.03 * age_days)
        except Exception:
            return 0.5

    max_count = max(len(v) for v in domain_evidence.values())

    upserted = 0
    for domain, timestamps in domain_evidence.items():
        count = len(timestamps)
        avg_recency = sum(_rw(t) for t in timestamps) / count
        strength = round(math.sqrt((count / max_count) * avg_recency), 4)
        last_active = max(timestamps)
        conn.execute(
            """
            INSERT INTO agent_expertise (agent_id, domain, strength, evidence_count, last_active, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(agent_id, domain) DO UPDATE SET
                strength=excluded.strength,
                evidence_count=excluded.evidence_count,
                last_active=excluded.last_active,
                updated_at=excluded.updated_at
            """,
            (agent_id, domain, strength, count, last_active)
        )
        upserted += 1

    conn.commit()
    return upserted


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_gaps_refresh(**kwargs) -> dict:
    """Recompute knowledge_coverage stats from current memories."""
    try:
        conn = _db()
        now = _now()
        updated = _run_refresh_inline(conn, now)
        conn.commit()
        return {"ok": True, "scopes_updated": updated, "computed_at": now}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_gaps_scan(**kwargs) -> dict:
    """Scan for coverage holes, staleness holes, and confidence holes."""
    try:
        conn = _db()
        now = _now()
        report: dict[str, Any] = {
            "coverage_holes": [],
            "staleness_holes": [],
            "confidence_holes": [],
            "scanned_at": now,
        }

        # Ensure coverage stats are current
        _run_refresh_inline(conn, now)

        # 1. Coverage holes: active agents without any coverage entry
        agent_scopes = {
            f"agent:{r['id']}"
            for r in conn.execute(
                "SELECT id FROM agents WHERE status='active'"
            ).fetchall()
        }
        covered_scopes = {
            r["scope"]
            for r in conn.execute("SELECT scope FROM knowledge_coverage").fetchall()
        }

        for scope in agent_scopes - covered_scopes:
            severity = 1.0
            _log_gap(conn, "coverage_hole", scope, severity, triggered_by="gap-scan")
            report["coverage_holes"].append({"scope": scope, "severity": severity})

        # 2. Staleness holes
        stale_rows = conn.execute("""
            SELECT scope, freshest_memory_at, memory_count
            FROM knowledge_coverage
            WHERE freshest_memory_at IS NOT NULL
              AND (julianday('now') - julianday(freshest_memory_at)) > ?
        """, (_STALENESS_GAP_DAYS,)).fetchall()

        for row in stale_rows:
            age = _days_since(row["freshest_memory_at"])
            severity = min(1.0, (age - _STALENESS_GAP_DAYS) / 30.0)
            _log_gap(conn, "staleness_hole", row["scope"], severity, triggered_by="gap-scan")
            report["staleness_holes"].append({
                "scope": row["scope"],
                "freshest_at": row["freshest_memory_at"],
                "age_days": round(age, 1),
                "severity": round(severity, 4),
            })

        # 3. Confidence holes
        conf_rows = conn.execute("""
            SELECT scope, avg_confidence, memory_count
            FROM knowledge_coverage
            WHERE avg_confidence IS NOT NULL AND avg_confidence < ?
        """, (_CONFIDENCE_GAP_THRESHOLD,)).fetchall()

        for row in conf_rows:
            severity = round(
                (_CONFIDENCE_GAP_THRESHOLD - row["avg_confidence"]) / _CONFIDENCE_GAP_THRESHOLD,
                4,
            )
            _log_gap(conn, "confidence_hole", row["scope"], severity, triggered_by="gap-scan")
            report["confidence_holes"].append({
                "scope": row["scope"],
                "avg_confidence": round(row["avg_confidence"], 4),
                "severity": severity,
            })

        conn.commit()
        report["ok"] = True
        report["total_gaps"] = (
            len(report["coverage_holes"])
            + len(report["staleness_holes"])
            + len(report["confidence_holes"])
        )
        return report
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_gaps_list(gap_type: str | None = None, limit: int = 50, **kwargs) -> dict:
    """List unresolved knowledge gaps, sorted by severity."""
    try:
        conn = _db()
        query = "SELECT * FROM knowledge_gaps WHERE resolved_at IS NULL"
        params: list[Any] = []
        if gap_type:
            query += " AND gap_type = ?"
            params.append(gap_type)
        query += " ORDER BY severity DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        gaps = _rows_to_list(rows)

        # Enrich with coverage stats where available
        for gap in gaps:
            cov = conn.execute(
                "SELECT memory_count, avg_confidence, freshest_memory_at, coverage_density "
                "FROM knowledge_coverage WHERE scope=?",
                (gap["scope"],),
            ).fetchone()
            if cov:
                gap["coverage"] = dict(cov)

        total_unresolved = conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_gaps WHERE resolved_at IS NULL"
        ).fetchone()["n"]

        return {"ok": True, "total_unresolved": total_unresolved, "gaps": gaps}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_gaps_resolve(gap_id: int, note: str | None = None, **kwargs) -> dict:
    """Mark a gap as resolved."""
    try:
        conn = _db()
        row = conn.execute(
            "SELECT id FROM knowledge_gaps WHERE id=?", (gap_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Gap {gap_id} not found"}
        now = _now()
        conn.execute(
            "UPDATE knowledge_gaps SET resolved_at=?, resolution_note=? WHERE id=?",
            (now, note, gap_id),
        )
        conn.commit()
        return {"ok": True, "gap_id": gap_id, "resolved_at": now}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_expertise_build(agent_id: str | None = None, **kwargs) -> dict:
    """Build or refresh the expertise index for one or all active agents."""
    try:
        conn = _db()
        _ensure_expertise_table(conn)

        if agent_id:
            agent_ids = [agent_id]
        else:
            rows = conn.execute("SELECT id FROM agents WHERE status='active'").fetchall()
            agent_ids = [r["id"] for r in rows]

        results = []
        for aid in agent_ids:
            n = _build_expertise_for_agent(conn, aid)
            results.append({"agent_id": aid, "domains_indexed": n})

        return {"ok": True, "agents_processed": len(results), "results": results}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_expertise_show(agent_id: str, limit: int = 20, **kwargs) -> dict:
    """Show expertise profile for a specific agent."""
    try:
        conn = _db()
        _ensure_expertise_table(conn)

        rows = conn.execute(
            "SELECT domain, strength, evidence_count, brier_score, last_active "
            "FROM agent_expertise WHERE agent_id=? ORDER BY strength DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()

        if not rows:
            return {
                "ok": True,
                "agent_id": agent_id,
                "expertise": [],
                "message": f"No expertise data for '{agent_id}'. Run expertise_build first.",
            }

        return {"ok": True, "agent_id": agent_id, "expertise": _rows_to_list(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_expertise_list(
    domain: str | None = None,
    min_strength: float = 0.0,
    limit: int = 50,
    **kwargs,
) -> dict:
    """List all agents with expertise, optionally filtered by domain."""
    try:
        conn = _db()
        _ensure_expertise_table(conn)

        if domain:
            rows = conn.execute(
                "SELECT agent_id, domain, strength, evidence_count, brier_score, last_active "
                "FROM agent_expertise WHERE domain LIKE ? AND strength >= ? "
                "ORDER BY strength DESC LIMIT ?",
                (f"%{domain}%", min_strength, limit),
            ).fetchall()
            return {"ok": True, "count": len(rows), "entries": _rows_to_list(rows)}
        else:
            rows = conn.execute(
                """
                SELECT agent_id,
                       MIN(domain) AS top_domain,
                       MAX(strength) AS top_strength,
                       MAX(brier_score) AS brier_score,
                       COUNT(*) AS domain_count,
                       MAX(last_active) AS last_active
                FROM agent_expertise
                WHERE strength >= ?
                GROUP BY agent_id
                ORDER BY top_strength DESC
                LIMIT ?
                """,
                (min_strength, limit),
            ).fetchall()
            return {"ok": True, "count": len(rows), "agents": _rows_to_list(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_expertise_update(
    agent_id: str,
    domain: str,
    brier: float | None = None,
    strength: float | None = None,
    **kwargs,
) -> dict:
    """Update brier_score and/or strength for an agent+domain pair."""
    try:
        if brier is None and strength is None:
            return {"ok": False, "error": "Provide at least one of 'brier' or 'strength'"}

        if brier is not None and not (0.0 <= brier <= 2.0):
            return {"ok": False, "error": "brier_score must be between 0.0 and 2.0"}

        if strength is not None and not (0.0 <= strength <= 1.0):
            return {"ok": False, "error": "strength must be between 0.0 and 1.0"}

        conn = _db()
        _ensure_expertise_table(conn)

        row = conn.execute(
            "SELECT agent_id FROM agent_expertise WHERE agent_id=? AND domain=?",
            (agent_id, domain),
        ).fetchone()
        if not row:
            return {
                "ok": False,
                "error": f"No expertise entry for agent='{agent_id}' domain='{domain}'. Run expertise_build first.",
            }

        updates = []
        params: list[Any] = []
        if brier is not None:
            updates.append("brier_score=?")
            params.append(brier)
        if strength is not None:
            updates.append("strength=?")
            params.append(strength)
        updates.append("updated_at=datetime('now')")
        params.extend([agent_id, domain])

        conn.execute(
            f"UPDATE agent_expertise SET {', '.join(updates)} WHERE agent_id=? AND domain=?",
            params,
        )
        conn.commit()

        result: dict[str, Any] = {"ok": True, "agent_id": agent_id, "domain": domain}
        if brier is not None:
            result["brier_score"] = brier
        if strength is not None:
            result["strength"] = strength
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_whosknows(
    topic: str,
    top_n: int = 10,
    min_strength: float = 0.05,
    **kwargs,
) -> dict:
    """Find agents with expertise matching a topic."""
    try:
        if not topic.strip():
            return {"ok": False, "error": "topic is required"}

        conn = _db()
        _ensure_expertise_table(conn)

        tokens = _expertise_extract_tokens(topic)
        raw_words = [w.lower() for w in topic.split() if len(w) >= 3]
        all_terms = list(dict.fromkeys(tokens + raw_words))

        if not all_terms:
            return {"ok": False, "error": "No meaningful tokens in topic query"}

        like_clauses = " OR ".join("e.domain LIKE ?" for _ in all_terms)
        like_params = [f"%{t}%" for t in all_terms]

        rows = conn.execute(
            f"""
            SELECT
                e.agent_id,
                a.display_name,
                SUM(e.strength) AS total_score,
                COUNT(DISTINCT e.domain) AS matched_domains,
                GROUP_CONCAT(e.domain || ':' || ROUND(e.strength,3), ', ') AS domain_breakdown,
                MAX(e.last_active) AS last_active
            FROM agent_expertise e
            JOIN agents a ON a.id = e.agent_id
            WHERE ({like_clauses})
              AND e.strength >= ?
              AND a.status = 'active'
            GROUP BY e.agent_id
            ORDER BY total_score DESC
            LIMIT ?
            """,
            like_params + [min_strength, top_n],
        ).fetchall()

        return {
            "ok": True,
            "topic": topic,
            "terms_searched": all_terms,
            "results": _rows_to_list(rows),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="gaps_refresh",
        description=(
            "Recompute knowledge_coverage stats from current active memories. "
            "Updates coverage density, avg/min/max confidence, and freshness per scope. "
            "Run before gaps_scan to ensure stats are current."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="gaps_scan",
        description=(
            "Scan the knowledge base for gaps: coverage holes (active agents with no memories), "
            "staleness holes (scopes with outdated memories), and confidence holes "
            "(scopes with low avg confidence). Logs new gaps to knowledge_gaps table."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="gaps_list",
        description=(
            "List unresolved knowledge gaps sorted by severity. "
            "Optionally filter by gap type. Each gap is enriched with coverage stats."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "gap_type": {
                    "type": "string",
                    "description": "Filter by gap type: coverage_hole, staleness_hole, confidence_hole",
                    "enum": ["coverage_hole", "staleness_hole", "confidence_hole"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of gaps to return",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="gaps_resolve",
        description="Mark a knowledge gap as resolved, optionally with a resolution note.",
        inputSchema={
            "type": "object",
            "properties": {
                "gap_id": {
                    "type": "integer",
                    "description": "ID of the gap to resolve",
                },
                "note": {
                    "type": "string",
                    "description": "Optional resolution note explaining how the gap was addressed",
                },
            },
            "required": ["gap_id"],
        },
    ),
    Tool(
        name="expertise_build",
        description=(
            "Build or refresh the expertise index for one or all active agents. "
            "Mines memories and events to derive domain expertise scores. "
            "Run before expertise_show, expertise_list, or whosknows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID to build expertise for. Omit to process all active agents.",
                },
            },
        },
    ),
    Tool(
        name="expertise_show",
        description="Show the expertise profile (top domains with strength scores) for a specific agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID to show expertise for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of domains to return",
                    "default": 20,
                },
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="expertise_list",
        description=(
            "List agents with expertise. Without a domain filter, returns one row per agent "
            "with their top domain and aggregate domain count. "
            "With a domain filter, returns all agents matching that domain."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter to agents with expertise in this domain (substring match)",
                },
                "min_strength": {
                    "type": "number",
                    "description": "Minimum strength threshold (0.0-1.0)",
                    "default": 0.0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="expertise_update",
        description=(
            "Manually update brier_score and/or strength for an agent+domain pair. "
            "Useful for calibrating expertise scores based on observed outcomes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain name",
                },
                "brier": {
                    "type": "number",
                    "description": "New Brier score (0.0-2.0, lower is better)",
                },
                "strength": {
                    "type": "number",
                    "description": "New strength score (0.0-1.0)",
                },
            },
            "required": ["agent_id", "domain"],
        },
    ),
    Tool(
        name="whosknows",
        description=(
            "Find agents with expertise matching a topic. "
            "Tokenizes the topic and searches for matching domains in the expertise index. "
            "Returns agents ranked by total matching score."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Topic to search for — e.g. 'database migrations' or 'React UI'",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Maximum number of agents to return",
                    "default": 10,
                },
                "min_strength": {
                    "type": "number",
                    "description": "Minimum strength threshold for matched domains",
                    "default": 0.05,
                },
            },
            "required": ["topic"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch table (maps tool name -> function)
# ---------------------------------------------------------------------------

DISPATCH: dict[str, Any] = {
    "gaps_refresh": tool_gaps_refresh,
    "gaps_scan": tool_gaps_scan,
    "gaps_list": tool_gaps_list,
    "gaps_resolve": tool_gaps_resolve,
    "expertise_build": tool_expertise_build,
    "expertise_show": tool_expertise_show,
    "expertise_list": tool_expertise_list,
    "expertise_update": tool_expertise_update,
    "whosknows": tool_whosknows,
}
