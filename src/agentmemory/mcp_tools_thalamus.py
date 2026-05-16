"""brainctl MCP tools — thalamus inspection and relay catalog."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import days_since, open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_SECTORS = {
    "sensory_external",
    "agent_efferent",
    "memory_recall",
    "belief",
    "consolidation",
    "pii_sensitive",
}
VALID_TRANSPORTS = {"first_order", "higher_order"}
VALID_DEFAULT_MODES = {"tonic", "burst"}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows_to_list(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _require_schema(conn: sqlite3.Connection) -> str | None:
    missing = [
        table
        for table in (
            "thalamic_relays",
            "thalamic_gate",
            "thalamic_mode",
            "thalamic_salience",
            "thalamic_bursts",
        )
        if not _table_exists(conn, table)
    ]
    if missing:
        return "thalamus schema missing tables: " + ", ".join(missing)
    return None


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {t for t in re.split(r"\W+", value.lower()) if len(t) > 1}


def _candidate_text(candidate: dict[str, Any]) -> str:
    parts = []
    for key in ("content", "summary", "title", "name", "text", "category", "scope", "sector"):
        value = candidate.get(key)
        if value is not None:
            parts.append(str(value))
    return " ".join(parts)


def _candidate_id(candidate: Any, index: int) -> str:
    if isinstance(candidate, dict):
        for key in ("candidate_id", "id", "memory_id", "event_id", "belief_id", "entity_id"):
            value = candidate.get(key)
            if value is not None:
                return str(value)
    return str(index)


def _candidate_type(candidate: dict[str, Any]) -> str:
    value = candidate.get("candidate_type") or candidate.get("type") or candidate.get("table")
    if value:
        normalized = str(value).lower().rstrip("s")
        if normalized in {"memory", "event", "belief", "entity", "relay"}:
            return normalized
    return "memory"


def _sector_for_candidate(candidate: dict[str, Any]) -> str:
    explicit = candidate.get("sector")
    if explicit in VALID_SECTORS:
        return str(explicit)
    haystack = _candidate_text(candidate).lower()
    candidate_type = _candidate_type(candidate)
    if any(term in haystack for term in ("pii", "secret", "credential", "wallet", "private-key", "token")):
        return "pii_sensitive"
    if candidate_type == "belief" or "belief" in haystack or "decision" in haystack:
        return "belief"
    if any(term in haystack for term in ("consolidation", "replay", "dream", "hippocampus")):
        return "consolidation"
    if candidate.get("source") in {"user", "webhook", "file", "log"} or "user" in haystack:
        return "sensory_external"
    if candidate.get("agent_id") or candidate.get("source_agent"):
        return "agent_efferent"
    return "memory_recall"


def _numeric_hint(candidate: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in candidate and candidate[key] is not None:
            return _clamp(candidate[key], default=default)
    return default


def _project_match(project: str | None, candidate: dict[str, Any], text: str) -> float:
    if not project:
        return 0.0
    needle = project.lower()
    scope = str(candidate.get("scope") or "").lower()
    if scope == f"project:{needle}" or scope == needle:
        return 1.0
    if needle in scope or needle in text.lower():
        return 0.65
    return 0.0


def _query_match(query: str | None, text: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    text_tokens = _tokens(text)
    if not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _sector_suppression(conn: sqlite3.Connection) -> dict[str, float]:
    if not _table_exists(conn, "thalamic_gate"):
        return {}
    rows = conn.execute(
        """
        SELECT sector, AVG(suppression) AS suppression
        FROM thalamic_gate
        GROUP BY sector
        """
    ).fetchall()
    return {row["sector"]: float(row["suppression"] or 0.0) for row in rows}


def _normalize_modulators(modulator_sources: Any) -> str:
    if modulator_sources is None:
        return json.dumps([])
    if isinstance(modulator_sources, str):
        stripped = modulator_sources.strip()
        if not stripped:
            return json.dumps([])
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in stripped.split(",") if item.strip()]
    else:
        parsed = list(modulator_sources)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("modulator_sources must be a list of strings or a JSON array of strings")
    return json.dumps(parsed, sort_keys=True)


def tool_thalamus_status(
    agent_id: str | None = None,
    project: str | None = None,
    top_n: int = 10,
    **kw: Any,
) -> dict[str, Any]:
    """Return current thalamus mode, suppression, armed channels, salience, and bursts."""
    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}

        top_n = max(1, min(int(top_n or 10), 50))
        mode_row = db.execute("SELECT * FROM thalamic_mode WHERE id=1").fetchone()
        mode = dict(mode_row) if mode_row else {
            "id": 1,
            "mode": "wake_focused",
            "arousal": 0.5,
            "acetylcholine": 0.5,
            "norepinephrine": 0.5,
            "retrieval_breadth_multiplier": 1.0,
            "similarity_threshold_delta": 0.0,
            "set_by": None,
            "set_at": None,
        }

        filter_sql = ""
        filter_params: list[Any] = []
        if project:
            filter_sql = "WHERE r.topographic_key = ? OR r.topographic_key = ?"
            filter_params = [project, f"project:{project}"]

        suppression_rows = db.execute(
            f"""
            SELECT
                g.sector,
                COUNT(*) AS channel_count,
                ROUND(AVG(g.suppression), 4) AS mean_suppression,
                ROUND(MAX(g.suppression), 4) AS max_suppression,
                SUM(CASE WHEN g.armed_for_burst = 1 THEN 1 ELSE 0 END) AS armed_count
            FROM thalamic_gate g
            JOIN thalamic_relays r ON r.channel_id = g.channel_id
            {filter_sql}
            GROUP BY g.sector
            ORDER BY g.sector
            """,  # nosec B608 - filter_sql is one of two source literals above.
            filter_params,
        ).fetchall()

        armed_channels = db.execute(
            f"""
            SELECT
                g.channel_id, g.sector, g.suppression, g.topdown_bias,
                g.bottomup_drive, g.last_burst_at, r.target, r.transport,
                r.topographic_key
            FROM thalamic_gate g
            JOIN thalamic_relays r ON r.channel_id = g.channel_id
            {filter_sql + (' AND ' if filter_sql else 'WHERE ') + 'g.armed_for_burst = 1'}
            ORDER BY g.suppression DESC, g.bottomup_drive DESC, g.channel_id ASC
            LIMIT ?
            """,  # nosec B608 - filter SQL is restricted to the source literals above.
            filter_params + [top_n],
        ).fetchall()

        salience_where = []
        salience_params: list[Any] = []
        if agent_id:
            salience_where.append("computed_for_agent = ?")
            salience_params.append(agent_id)
        salience_sql = "WHERE " + " AND ".join(salience_where) if salience_where else ""
        top_salience = db.execute(
            f"""
            SELECT candidate_id, candidate_type, sector, bottomup_score,
                   topdown_score, precision, integrated, computed_for_agent,
                   computed_at
            FROM thalamic_salience
            {salience_sql}
            ORDER BY integrated DESC, computed_at DESC
            LIMIT ?
            """,  # nosec B608 - salience_sql is built from fixed predicates.
            salience_params + [top_n],
        ).fetchall()

        recent_bursts = db.execute(
            """
            SELECT id, channel_id, sector, reason, payload_ref, salience,
                   fired_at, consumed_by, consumed_at
            FROM thalamic_bursts
            ORDER BY fired_at DESC, id DESC
            LIMIT ?
            """,
            (top_n,),
        ).fetchall()

        relay_count = db.execute("SELECT COUNT(*) FROM thalamic_relays").fetchone()[0]
        gate_count = db.execute("SELECT COUNT(*) FROM thalamic_gate").fetchone()[0]
        return {
            "ok": True,
            "agent_id": agent_id,
            "project": project,
            "mode": mode,
            "relay_count": relay_count,
            "gate_count": gate_count,
            "suppression_by_sector": _rows_to_list(suppression_rows),
            "armed_channels": _rows_to_list(armed_channels),
            "top_salience": _rows_to_list(top_salience),
            "recent_bursts": _rows_to_list(recent_bursts),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_thalamus_salience(
    candidates: list[Any],
    agent_id: str,
    project: str | None = None,
    query: str | None = None,
    write_cache: bool = False,
    **kw: Any,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Rank candidates by integrated bottom-up and top-down salience."""
    if not agent_id:
        return {"ok": False, "error": "agent_id is required"}
    if candidates is None:
        return {"ok": False, "error": "candidates is required"}
    if not isinstance(candidates, list):
        return {"ok": False, "error": "candidates must be a list"}

    db = _db()
    try:
        suppression = _sector_suppression(db)
        scored: list[dict[str, Any]] = []
        for idx, raw in enumerate(candidates):
            candidate = raw if isinstance(raw, dict) else {"content": str(raw)}
            candidate_id = _candidate_id(candidate, idx)
            candidate_type = _candidate_type(candidate)
            text = _candidate_text(candidate)
            sector = _sector_for_candidate(candidate)
            novelty = _numeric_hint(candidate, "novelty", "surprise", "worthiness", default=0.5)
            salience_hint = _numeric_hint(candidate, "salience", "score", "rank_score", default=0.5)
            recency = max(0.0, 1.0 - min(days_since(str(candidate.get("created_at") or "")), 30.0) / 30.0)
            bottomup_score = _clamp(0.55 * novelty + 0.25 * recency + 0.20 * salience_hint, default=0.5)
            topdown_score = _clamp(
                0.75 * _query_match(query, text) + 0.25 * _project_match(project, candidate, text),
                default=0.0,
            )
            precision = _clamp(candidate.get("precision", candidate.get("confidence", candidate.get("trust_score", 1.0))), default=1.0)
            sector_gain = 1.0 - 0.5 * suppression.get(sector, 0.0)
            integrated = _clamp((0.55 * bottomup_score + 0.45 * topdown_score) * precision * sector_gain, default=0.0)
            row = {
                "candidate_id": candidate_id,
                "candidate_type": candidate_type,
                "sector": sector,
                "bottomup_score": round(bottomup_score, 4),
                "topdown_score": round(topdown_score, 4),
                "precision": round(precision, 4),
                "integrated": round(integrated, 4),
            }
            scored.append(row)

        scored.sort(key=lambda item: (-item["integrated"], item["candidate_type"], item["candidate_id"]))

        if write_cache and _table_exists(db, "thalamic_salience"):
            db.execute(
                "DELETE FROM thalamic_salience WHERE computed_at < datetime('now', '-24 hours')"
            )
            for row in scored:
                db.execute(
                    """
                    INSERT INTO thalamic_salience (
                        candidate_id, candidate_type, bottomup_score,
                        topdown_score, precision, integrated, sector,
                        computed_for_agent, computed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))
                    ON CONFLICT(candidate_id, candidate_type, computed_for_agent) DO UPDATE SET
                        bottomup_score = excluded.bottomup_score,
                        topdown_score = excluded.topdown_score,
                        precision = excluded.precision,
                        integrated = excluded.integrated,
                        sector = excluded.sector,
                        computed_at = excluded.computed_at
                    """,
                    (
                        row["candidate_id"],
                        row["candidate_type"],
                        row["bottomup_score"],
                        row["topdown_score"],
                        row["precision"],
                        row["integrated"],
                        row["sector"],
                        agent_id,
                    ),
                )
            db.commit()

        return scored
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_thalamus_relay_create(
    channel_id: str,
    sector: str,
    driver_source: str,
    target: str,
    transport: str,
    modulator_sources: Any = None,
    default_mode: str = "tonic",
    default_gain: float = 1.0,
    topographic_key: str | None = None,
    efference_copy_target: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Create or update a typed thalamic relay and matching gate row."""
    if not channel_id or not isinstance(channel_id, str):
        return {"ok": False, "error": "channel_id is required"}
    if sector not in VALID_SECTORS:
        return {"ok": False, "error": f"sector must be one of {sorted(VALID_SECTORS)}"}
    if transport not in VALID_TRANSPORTS:
        return {"ok": False, "error": "transport must be 'first_order' or 'higher_order'"}
    if default_mode not in VALID_DEFAULT_MODES:
        return {"ok": False, "error": "default_mode must be 'tonic' or 'burst'"}
    if not driver_source or not target:
        return {"ok": False, "error": "driver_source and target are required"}
    try:
        gain = float(default_gain)
    except (TypeError, ValueError):
        return {"ok": False, "error": "default_gain must be numeric"}
    if gain < 0:
        return {"ok": False, "error": "default_gain must be >= 0"}
    try:
        modulators_json = _normalize_modulators(modulator_sources)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        db.execute(
            """
            INSERT INTO thalamic_relays (
                channel_id, sector, driver_source, modulator_sources_json,
                target, transport, default_mode, default_gain, topographic_key,
                efference_copy_target, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            ON CONFLICT(channel_id) DO UPDATE SET
                sector = excluded.sector,
                driver_source = excluded.driver_source,
                modulator_sources_json = excluded.modulator_sources_json,
                target = excluded.target,
                transport = excluded.transport,
                default_mode = excluded.default_mode,
                default_gain = excluded.default_gain,
                topographic_key = excluded.topographic_key,
                efference_copy_target = excluded.efference_copy_target,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            """,
            (
                channel_id,
                sector,
                driver_source,
                modulators_json,
                target,
                transport,
                default_mode,
                gain,
                topographic_key,
                efference_copy_target,
            ),
        )
        db.execute(
            """
            INSERT INTO thalamic_gate (channel_id, sector, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            ON CONFLICT(channel_id) DO UPDATE SET
                sector = excluded.sector,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            """,
            (channel_id, sector),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM thalamic_relays WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        relay = dict(row) if row else {}
        if relay.get("modulator_sources_json"):
            relay["modulator_sources"] = json.loads(relay["modulator_sources_json"])
        return {"ok": True, "relay": relay}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


TOOLS: list[Tool] = [
    Tool(
        name="thalamus_status",
        description=(
            "Inspect the thalamus subsystem: current global mode, per-sector suppression, "
            "armed channels, cached salience rows, and recent burst events."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Optional agent filter for cached salience"},
                "project": {"type": "string", "description": "Optional project/topographic key filter"},
                "top_n": {"type": "integer", "default": 10, "description": "Maximum rows per section"},
            },
        },
    ),
    Tool(
        name="thalamus_salience",
        description=(
            "Compute deterministic integrated salience for candidate items using bottom-up "
            "novelty/recency, top-down query/project match, precision, and sector suppression."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "description": "Candidate dicts or strings to score.",
                    "items": {"type": ["object", "string"]},
                },
                "agent_id": {"type": "string", "description": "Agent context for scoring"},
                "project": {"type": "string", "description": "Optional project context"},
                "query": {"type": "string", "description": "Optional active query/task terms"},
                "write_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, write the computed rows into thalamic_salience.",
                },
            },
            "required": ["candidates", "agent_id"],
        },
    ),
    Tool(
        name="thalamus_relay_create",
        description=(
            "Create or update an idempotent typed thalamic relay and its gate row. "
            "Use transport='first_order' for external ingress and 'higher_order' for inter-module relays."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "sector": {"type": "string", "enum": sorted(VALID_SECTORS)},
                "driver_source": {"type": "string"},
                "target": {"type": "string"},
                "transport": {"type": "string", "enum": sorted(VALID_TRANSPORTS)},
                "modulator_sources": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "List or JSON array of modulator source identifiers.",
                },
                "default_mode": {"type": "string", "enum": sorted(VALID_DEFAULT_MODES), "default": "tonic"},
                "default_gain": {"type": "number", "default": 1.0},
                "topographic_key": {"type": "string"},
                "efference_copy_target": {"type": "string"},
            },
            "required": ["channel_id", "sector", "driver_source", "target", "transport"],
        },
    ),
]

_THALAMUS_TOOLS = {
    "thalamus_status": tool_thalamus_status,
    "thalamus_salience": tool_thalamus_salience,
    "thalamus_relay_create": tool_thalamus_relay_create,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _THALAMUS_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    """Return tool descriptors and dispatch map for mcp_server integration."""
    return TOOLS, DISPATCH
