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

# Thalamus sector → cerebellum partner mapping for Phase 3 precision lookup.
# memory_recall is the oculomotor (retrieval) channel; belief & pii_sensitive
# both flow through acc (conflict monitoring); efferent / external traffic
# are motor; consolidation is dlpfc (deliberative).
_SECTOR_TO_CEREBELLUM_PARTNER = {
    "memory_recall": "oculomotor_partner",
    "belief": "acc_partner",
    "pii_sensitive": "acc_partner",
    "sensory_external": "motor_partner",
    "agent_efferent": "motor_partner",
    "consolidation": "dlpfc_partner",
}


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

        # Phase 3: cerebellum partner precision (one query per partner,
        # cached for the duration of this salience call). Never raises;
        # returns 0.5 (neutral) when the cerebellum schema is missing or
        # has no learned weights yet.
        partner_precision_cache: dict[str, float] = {}
        try:
            from agentmemory.mcp_tools_cerebellum import cerebellum_partner_precision
            for partner_name in set(_SECTOR_TO_CEREBELLUM_PARTNER.values()):
                partner_precision_cache[partner_name] = cerebellum_partner_precision(partner_name)
        except Exception:
            partner_precision_cache = {}

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
            # Phase 3: cerebellum precision multiplier — boost candidates
            # whose partner the cerebellum predicts confidently. Maps to
            # [0.7, 1.3] so it nudges ranking without dominating it.
            partner = _SECTOR_TO_CEREBELLUM_PARTNER.get(sector)
            cerebellum_confidence = partner_precision_cache.get(partner, 0.5) if partner else 0.5
            cerebellum_multiplier = 0.7 + 0.6 * cerebellum_confidence
            integrated = _clamp(
                (0.55 * bottomup_score + 0.45 * topdown_score) * precision * sector_gain * cerebellum_multiplier,
                default=0.0,
            )
            row = {
                "candidate_id": candidate_id,
                "candidate_type": candidate_type,
                "sector": sector,
                "bottomup_score": round(bottomup_score, 4),
                "topdown_score": round(topdown_score, 4),
                "precision": round(precision, 4),
                "cerebellum_confidence": round(cerebellum_confidence, 4),
                "cerebellum_multiplier": round(cerebellum_multiplier, 4),
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


VALID_MODES = {
    "wake_focused",
    "wake_exploratory",
    "drowsy",
    "consolidate",
    "offline",
}
VALID_BURST_REASONS = {"novelty", "high_pe", "distractor_break_through", "manual"}


def tool_thalamus_gate_set(
    channel_id: str,
    suppression: float | None = None,
    topdown_bias: float | None = None,
    bottomup_drive: float | None = None,
    armed_for_burst: bool | None = None,
    bias_source: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Write top-down attention bias / suppression onto a thalamic gate row.

    Phase 2 write tool. Updates `thalamic_gate` for an existing relay channel.
    All numeric fields are clamped to [0, 1]. The corresponding relay must
    already exist (created via thalamus_relay_create).
    """
    if not channel_id or not isinstance(channel_id, str):
        return {"ok": False, "error": "channel_id is required"}
    if (
        suppression is None
        and topdown_bias is None
        and bottomup_drive is None
        and armed_for_burst is None
    ):
        return {"ok": False, "error": "at least one of suppression/topdown_bias/bottomup_drive/armed_for_burst is required"}

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        gate_row = db.execute(
            "SELECT channel_id, sector FROM thalamic_gate WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if not gate_row:
            return {"ok": False, "error": f"no thalamic_gate row for channel_id={channel_id} (create the relay first)"}

        sets: list[str] = []
        params: list[Any] = []
        if suppression is not None:
            sets.append("suppression = ?")
            params.append(_clamp(suppression, default=0.0))
        if topdown_bias is not None:
            sets.append("topdown_bias = ?")
            params.append(_clamp(topdown_bias, default=0.0))
        if bottomup_drive is not None:
            sets.append("bottomup_drive = ?")
            params.append(_clamp(bottomup_drive, default=0.0))
        if armed_for_burst is not None:
            sets.append("armed_for_burst = ?")
            params.append(1 if armed_for_burst else 0)
        if bias_source is not None:
            sets.append("bias_source = ?")
            params.append(str(bias_source))
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")

        sql = f"UPDATE thalamic_gate SET {', '.join(sets)} WHERE channel_id = ?"  # nosec B608
        params.append(channel_id)
        db.execute(sql, params)
        db.commit()

        updated = db.execute(
            "SELECT * FROM thalamic_gate WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return {"ok": True, "gate": dict(updated) if updated else None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_thalamus_burst(
    channel_id: str,
    payload_ref: str | None = None,
    reason: str = "novelty",
    salience: float | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Fire a sparse high-salience burst event on a thalamic channel.

    Phase 2 write tool. Normally invoked by the gate when an armed channel
    sees a high-prediction-error write; exposed here for tooling and tests.
    """
    if not channel_id or not isinstance(channel_id, str):
        return {"ok": False, "error": "channel_id is required"}
    if reason not in VALID_BURST_REASONS:
        return {"ok": False, "error": f"reason must be one of {sorted(VALID_BURST_REASONS)}"}
    salience_val = _clamp(salience if salience is not None else 1.0, default=1.0)

    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        gate = db.execute(
            "SELECT sector FROM thalamic_gate WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if not gate:
            return {"ok": False, "error": f"no thalamic_gate row for channel_id={channel_id}"}
        sector = gate["sector"]
        cursor = db.execute(
            """
            INSERT INTO thalamic_bursts (channel_id, sector, reason, payload_ref, salience)
            VALUES (?, ?, ?, ?, ?)
            """,
            (channel_id, sector, reason, payload_ref, salience_val),
        )
        db.execute(
            """
            UPDATE thalamic_gate
            SET last_burst_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'),
                armed_for_burst = 0,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            WHERE channel_id = ?
            """,
            (channel_id,),
        )
        db.commit()
        burst = db.execute(
            "SELECT * FROM thalamic_bursts WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return {"ok": True, "burst": dict(burst) if burst else None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_thalamus_shadow_stats(
    days: int = 7,
    sector: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Summarize shadow-mode gate decisions logged by the W(m) hookpoint.

    Phase 2 observability tool. Returns per-decision counts, per-sector
    breakdown, and the rate at which the thalamic gate *would have*
    diverged from current W(m) behavior. Use to validate the gate before
    flipping shadow mode off in a future phase.
    """
    try:
        days_int = max(1, int(days))
    except (TypeError, ValueError):
        return {"ok": False, "error": "days must be an integer"}

    db = _db()
    try:
        if not _table_exists(db, "thalamic_shadow_decisions"):
            return {"ok": False, "error": "thalamic_shadow_decisions table missing (apply migration 053)"}

        where_clauses = [f"decision_at >= datetime('now', '-{days_int} days')"]
        params: list[Any] = []
        if sector:
            where_clauses.append("sector = ?")
            params.append(sector)
        where_sql = "WHERE " + " AND ".join(where_clauses)

        total = db.execute(
            f"SELECT COUNT(*) FROM thalamic_shadow_decisions {where_sql}",  # nosec B608
            params,
        ).fetchone()[0]

        by_decision = db.execute(
            f"""
            SELECT decision, COUNT(*) AS n
            FROM thalamic_shadow_decisions
            {where_sql}
            GROUP BY decision
            ORDER BY n DESC
            """,  # nosec B608
            params,
        ).fetchall()

        by_sector = db.execute(
            f"""
            SELECT sector, decision, COUNT(*) AS n
            FROM thalamic_shadow_decisions
            {where_sql}
            GROUP BY sector, decision
            ORDER BY sector, n DESC
            """,  # nosec B608
            params,
        ).fetchall()

        recent_divergent = db.execute(
            f"""
            SELECT decision_at, sector, decision, reason, suppression, surprise_score
            FROM thalamic_shadow_decisions
            {where_sql} AND decision != 'pass'
            ORDER BY decision_at DESC
            LIMIT 20
            """,  # nosec B608
            params,
        ).fetchall()

        return {
            "ok": True,
            "window_days": days_int,
            "sector_filter": sector,
            "total_decisions": total,
            "by_decision": [dict(r) for r in by_decision],
            "by_sector": [dict(r) for r in by_sector],
            "divergence_rate": (
                round(1 - (next((r["n"] for r in by_decision if r["decision"] == "pass"), 0) / total), 4)
                if total > 0
                else 0.0
            ),
            "recent_divergent": [dict(r) for r in recent_divergent],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_thalamus_mode_set(
    mode: str,
    set_by: str | None = None,
    arousal: float | None = None,
    acetylcholine: float | None = None,
    norepinephrine: float | None = None,
    retrieval_breadth_multiplier: float | None = None,
    similarity_threshold_delta: float | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Update the global thalamic_mode row (id=1).

    Phase 2 write tool. Switches the global operating mode and optionally
    the neuromodulator dials. Mode must be one of the canonical enum values.
    """
    if mode not in VALID_MODES:
        return {"ok": False, "error": f"mode must be one of {sorted(VALID_MODES)}"}
    db = _db()
    try:
        schema_error = _require_schema(db)
        if schema_error:
            return {"ok": False, "error": schema_error}
        sets = ["mode = ?"]
        params: list[Any] = [mode]
        if arousal is not None:
            sets.append("arousal = ?")
            params.append(_clamp(arousal, default=0.5))
        if acetylcholine is not None:
            sets.append("acetylcholine = ?")
            params.append(_clamp(acetylcholine, default=0.5))
        if norepinephrine is not None:
            sets.append("norepinephrine = ?")
            params.append(_clamp(norepinephrine, default=0.5))
        if retrieval_breadth_multiplier is not None:
            try:
                rbm = float(retrieval_breadth_multiplier)
            except (TypeError, ValueError):
                return {"ok": False, "error": "retrieval_breadth_multiplier must be numeric"}
            if rbm < 0:
                return {"ok": False, "error": "retrieval_breadth_multiplier must be >= 0"}
            sets.append("retrieval_breadth_multiplier = ?")
            params.append(rbm)
        if similarity_threshold_delta is not None:
            try:
                std = float(similarity_threshold_delta)
            except (TypeError, ValueError):
                return {"ok": False, "error": "similarity_threshold_delta must be numeric"}
            sets.append("similarity_threshold_delta = ?")
            params.append(std)
        if set_by is not None:
            sets.append("set_by = ?")
            params.append(str(set_by))
        sets.append("set_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')")
        sql = f"UPDATE thalamic_mode SET {', '.join(sets)} WHERE id = 1"  # nosec B608
        db.execute(sql, params)
        db.commit()
        row = db.execute("SELECT * FROM thalamic_mode WHERE id = 1").fetchone()
        return {"ok": True, "mode": dict(row) if row else None}
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
    Tool(
        name="thalamus_gate_set",
        description=(
            "Phase 2 write tool. Update a thalamic_gate row's suppression, top-down bias, "
            "bottom-up drive, or burst armed flag. Channel must already exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "suppression": {"type": "number", "description": "0.0 (open) to 1.0 (fully suppressed)"},
                "topdown_bias": {"type": "number", "description": "0.0 to 1.0; PFC-equivalent task-context weight"},
                "bottomup_drive": {"type": "number", "description": "0.0 to 1.0; traffic-driven drive"},
                "armed_for_burst": {"type": "boolean"},
                "bias_source": {"type": "string", "description": "Agent or system that set this bias (for audit)"},
            },
            "required": ["channel_id"],
        },
    ),
    Tool(
        name="thalamus_burst",
        description=(
            "Phase 2 write tool. Fire a sparse high-salience burst event on a channel. "
            "Normally fired automatically when armed channels see high prediction-error; "
            "exposed for tooling/tests."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "payload_ref": {"type": "string", "description": "Optional reference like 'memory:1879' or 'event:20377'"},
                "reason": {"type": "string", "enum": sorted(VALID_BURST_REASONS), "default": "novelty"},
                "salience": {"type": "number", "description": "0.0 to 1.0"},
            },
            "required": ["channel_id"],
        },
    ),
    Tool(
        name="thalamus_shadow_stats",
        description=(
            "Phase 2 observability. Summarize shadow-mode gate decisions from the W(m) "
            "hookpoint: by-decision counts, by-sector breakdown, divergence rate, and recent "
            "non-pass examples. Use to validate the gate before enforcement mode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "description": "Window in days (>= 1)"},
                "sector": {"type": "string", "description": "Optional sector filter"},
            },
        },
    ),
    Tool(
        name="thalamus_mode_set",
        description=(
            "Phase 2 write tool. Switch the global thalamic mode (id=1) and optionally tune "
            "neuromodulator dials. Mode change is logged via set_by/set_at."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": sorted(VALID_MODES)},
                "set_by": {"type": "string"},
                "arousal": {"type": "number"},
                "acetylcholine": {"type": "number"},
                "norepinephrine": {"type": "number"},
                "retrieval_breadth_multiplier": {"type": "number"},
                "similarity_threshold_delta": {"type": "number"},
            },
            "required": ["mode"],
        },
    ),
]

_THALAMUS_TOOLS = {
    "thalamus_status": tool_thalamus_status,
    "thalamus_salience": tool_thalamus_salience,
    "thalamus_relay_create": tool_thalamus_relay_create,
    "thalamus_gate_set": tool_thalamus_gate_set,
    "thalamus_burst": tool_thalamus_burst,
    "thalamus_shadow_stats": tool_thalamus_shadow_stats,
    "thalamus_mode_set": tool_thalamus_mode_set,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _THALAMUS_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    """Return tool descriptors and dispatch map for mcp_server integration."""
    return TOOLS, DISPATCH
