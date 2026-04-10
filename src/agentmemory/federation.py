"""brainctl — multi-DB federation engine.

Provides read-only union queries across multiple brain.db files.
DBs are opened with ?mode=ro to guarantee no writes occur.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from os.path import basename
from pathlib import Path
from typing import Any

from agentmemory.paths import get_db_path

logger = logging.getLogger(__name__)

# FTS5 special characters — strip everything that isn't word chars or spaces
_FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^?!]')


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5 special characters so MATCH never raises a syntax error."""
    cleaned = _FTS5_SPECIAL.sub(" ", query or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _open_ro(path: str) -> sqlite3.Connection | None:
    """Open a SQLite DB read-only. Returns None on error."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        logger.warning("federation: cannot open %s: %s", path, exc)
        return None


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if the table exists in the connected DB."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_federation_paths() -> list[str]:
    """Return deduplicated list of DB paths to federate.

    BRAIN_DB is always first. BRAIN_DB_FEDERATION (colon-separated) is
    appended. Paths are resolved to absolute form before dedup so that
    equivalent paths in different representations collapse.
    """
    seen: dict[str, None] = {}  # ordered set via dict

    primary = str(get_db_path())
    abs_primary = str(Path(primary).resolve())
    seen[abs_primary] = None

    federation_env = os.environ.get("BRAIN_DB_FEDERATION", "")
    for raw in federation_env.split(":"):
        raw = raw.strip()
        if not raw:
            continue
        abs_path = str(Path(raw).resolve())
        seen[abs_path] = None

    return list(seen.keys())


def federated_stats() -> dict:
    """Aggregate stats across all federated DBs.

    Returns:
        {ok, databases: [{path, accessible, memory_count, event_count,
                          entity_count, agent_count}], totals: {...}}
    """
    paths = get_federation_paths()
    databases: list[dict] = []
    totals = {"memory_count": 0, "event_count": 0, "entity_count": 0, "agent_count": 0}

    for path in paths:
        conn = _open_ro(path)
        if conn is None:
            databases.append({"path": path, "accessible": False})
            continue

        try:
            entry: dict[str, Any] = {"path": path, "accessible": True}
            for col, table in [
                ("memory_count", "memories"),
                ("event_count", "events"),
                ("entity_count", "entities"),
                ("agent_count", "agents"),
            ]:
                if _has_table(conn, table):
                    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                    entry[col] = row[0] if row else 0
                else:
                    entry[col] = 0
                totals[col] += entry[col]
            databases.append(entry)
        except Exception as exc:
            logger.warning("federation: stats error for %s: %s", path, exc)
            databases.append({"path": path, "accessible": False})
        finally:
            conn.close()

    return {"ok": True, "databases": databases, "totals": totals}


def federated_memory_search(
    query: str,
    limit: int = 20,
    category: str | None = None,
) -> dict:
    """FTS5 memory search across all federated DBs.

    Returns:
        {ok, results: [{source_db, table, id, content, category,
                        confidence, created_at}], total_results}
    """
    if not query or not query.strip():
        return {"ok": False, "error": "query must not be empty"}

    safe_query = _sanitize_fts_query(query)
    if not safe_query:
        return {"ok": False, "error": "query is empty after sanitization"}

    paths = get_federation_paths()
    results: list[dict] = []

    for path in paths:
        source = basename(path)
        conn = _open_ro(path)
        if conn is None:
            continue

        try:
            if _has_table(conn, "memories_fts") and _has_table(conn, "memories"):
                try:
                    if category:
                        rows = conn.execute(
                            """
                            SELECT m.id, m.content, m.category, m.confidence,
                                   m.created_at, m.agent_id
                            FROM memories m
                            JOIN memories_fts f ON f.rowid = m.id
                            WHERE f.memories_fts MATCH ?
                              AND m.retired_at IS NULL
                              AND m.category = ?
                            ORDER BY m.created_at DESC
                            LIMIT ?
                            """,
                            (safe_query, category, limit),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            """
                            SELECT m.id, m.content, m.category, m.confidence,
                                   m.created_at, m.agent_id
                            FROM memories m
                            JOIN memories_fts f ON f.rowid = m.id
                            WHERE f.memories_fts MATCH ?
                              AND m.retired_at IS NULL
                            ORDER BY m.created_at DESC
                            LIMIT ?
                            """,
                            (safe_query, limit),
                        ).fetchall()
                    for row in rows:
                        results.append({
                            "source_db": source,
                            "table": "memories",
                            **dict(row),
                        })
                except sqlite3.OperationalError as exc:
                    logger.warning("federation: FTS error in %s: %s", path, exc)
            elif _has_table(conn, "memories"):
                # Fallback: LIKE search
                pattern = f"%{query}%"
                if category:
                    rows = conn.execute(
                        "SELECT id, content, category, confidence, created_at, agent_id "
                        "FROM memories WHERE content LIKE ? AND category = ? "
                        "AND retired_at IS NULL ORDER BY created_at DESC LIMIT ?",
                        (pattern, category, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, content, category, confidence, created_at, agent_id "
                        "FROM memories WHERE content LIKE ? "
                        "AND retired_at IS NULL ORDER BY created_at DESC LIMIT ?",
                        (pattern, limit),
                    ).fetchall()
                for row in rows:
                    results.append({
                        "source_db": source,
                        "table": "memories",
                        **dict(row),
                    })
        except Exception as exc:
            logger.warning("federation: memory search error for %s: %s", path, exc)
        finally:
            conn.close()

    # Sort merged results by created_at descending
    results.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"ok": True, "results": results[:limit], "total_results": len(results)}


def federated_entity_search(
    name: str,
    entity_type: str | None = None,
) -> dict:
    """Entity search across all federated DBs by name.

    An empty name matches all entities (LIKE '%%'); useful when filtering
    by entity_type alone.  At least one of name or entity_type must be
    provided.

    Returns:
        {ok, results: [{source_db, table, id, name, entity_type,
                        observations, created_at}], total_results}
    """
    if (not name or not name.strip()) and not entity_type:
        return {"ok": False, "error": "name must not be empty"}

    paths = get_federation_paths()
    results: list[dict] = []

    for path in paths:
        source = basename(path)
        conn = _open_ro(path)
        if conn is None:
            continue

        try:
            if not _has_table(conn, "entities"):
                continue
            pattern = f"%{name}%"
            if entity_type:
                rows = conn.execute(
                    "SELECT id, name, entity_type, observations, created_at, agent_id "
                    "FROM entities WHERE name LIKE ? AND entity_type = ? "
                    "AND retired_at IS NULL ORDER BY created_at DESC",
                    (pattern, entity_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, entity_type, observations, created_at, agent_id "
                    "FROM entities WHERE name LIKE ? "
                    "AND retired_at IS NULL ORDER BY created_at DESC",
                    (pattern,),
                ).fetchall()
            for row in rows:
                results.append({
                    "source_db": source,
                    "table": "entities",
                    **dict(row),
                })
        except Exception as exc:
            logger.warning("federation: entity search error for %s: %s", path, exc)
        finally:
            conn.close()

    results.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"ok": True, "results": results, "total_results": len(results)}


def federated_search(
    query: str,
    tables: list[str] | None = None,
    limit: int = 20,
    agent_id: str | None = None,
) -> dict:
    """Search memories, events, and entities across all federated DBs.

    Args:
        query:    Text to search for.
        tables:   Which tables to search. Defaults to ['memories', 'events', 'entities'].
        limit:    Max total results to return.
        agent_id: If set, filter results to this agent.

    Returns:
        {ok, results: [{source_db, table, ...row fields}], db_count, total_results}
    """
    if not query or not query.strip():
        return {"ok": False, "error": "query must not be empty"}

    if tables is None:
        tables = ["memories", "events", "entities"]

    safe_query = _sanitize_fts_query(query)
    paths = get_federation_paths()
    results: list[dict] = []
    db_count = 0

    for path in paths:
        source = basename(path)
        conn = _open_ro(path)
        if conn is None:
            continue

        db_count += 1
        try:
            # --- memories ---
            if "memories" in tables and _has_table(conn, "memories"):
                try:
                    if safe_query and _has_table(conn, "memories_fts"):
                        q_args: list[Any] = [safe_query]
                        agent_clause = " AND m.agent_id = ?" if agent_id else ""
                        if agent_id:
                            q_args.append(agent_id)
                        q_args.append(limit)
                        rows = conn.execute(
                            f"""
                            SELECT m.id, m.content, m.category, m.confidence,
                                   m.created_at, m.agent_id
                            FROM memories m
                            JOIN memories_fts f ON f.rowid = m.id
                            WHERE f.memories_fts MATCH ?
                              AND m.retired_at IS NULL{agent_clause}
                            ORDER BY m.created_at DESC LIMIT ?
                            """,
                            q_args,
                        ).fetchall()
                    else:
                        pattern = f"%{query}%"
                        q_args = [pattern]
                        agent_clause = " AND agent_id = ?" if agent_id else ""
                        if agent_id:
                            q_args.append(agent_id)
                        q_args.append(limit)
                        rows = conn.execute(
                            f"SELECT id, content, category, confidence, created_at, agent_id "
                            f"FROM memories WHERE content LIKE ?{agent_clause} "
                            f"AND retired_at IS NULL ORDER BY created_at DESC LIMIT ?",
                            q_args,
                        ).fetchall()
                    for row in rows:
                        results.append({"source_db": source, "table": "memories", **dict(row)})
                except Exception as exc:
                    logger.warning("federation: memories search error in %s: %s", path, exc)

            # --- events ---
            if "events" in tables and _has_table(conn, "events"):
                try:
                    if safe_query and _has_table(conn, "events_fts"):
                        q_args = [safe_query]
                        agent_clause = " AND e.agent_id = ?" if agent_id else ""
                        if agent_id:
                            q_args.append(agent_id)
                        q_args.append(limit)
                        rows = conn.execute(
                            f"""
                            SELECT e.id, e.summary, e.event_type, e.project,
                                   e.importance, e.created_at, e.agent_id
                            FROM events e
                            JOIN events_fts f ON f.rowid = e.id
                            WHERE f.events_fts MATCH ?{agent_clause}
                            ORDER BY e.created_at DESC LIMIT ?
                            """,
                            q_args,
                        ).fetchall()
                    else:
                        pattern = f"%{query}%"
                        q_args = [pattern]
                        agent_clause = " AND agent_id = ?" if agent_id else ""
                        if agent_id:
                            q_args.append(agent_id)
                        q_args.append(limit)
                        rows = conn.execute(
                            f"SELECT id, summary, event_type, project, importance, "
                            f"created_at, agent_id FROM events "
                            f"WHERE summary LIKE ?{agent_clause} "
                            f"ORDER BY created_at DESC LIMIT ?",
                            q_args,
                        ).fetchall()
                    for row in rows:
                        results.append({"source_db": source, "table": "events", **dict(row)})
                except Exception as exc:
                    logger.warning("federation: events search error in %s: %s", path, exc)

            # --- entities ---
            if "entities" in tables and _has_table(conn, "entities"):
                try:
                    pattern = f"%{query}%"
                    q_args = [pattern, pattern]
                    agent_clause = " AND agent_id = ?" if agent_id else ""
                    if agent_id:
                        q_args.append(agent_id)
                    q_args.append(limit)
                    rows = conn.execute(
                        f"SELECT id, name, entity_type, observations, created_at, agent_id "
                        f"FROM entities "
                        f"WHERE (name LIKE ? OR observations LIKE ?){agent_clause} "
                        f"AND retired_at IS NULL "
                        f"ORDER BY created_at DESC LIMIT ?",
                        q_args,
                    ).fetchall()
                    for row in rows:
                        results.append({"source_db": source, "table": "entities", **dict(row)})
                except Exception as exc:
                    logger.warning("federation: entities search error in %s: %s", path, exc)

        except Exception as exc:
            logger.warning("federation: db-level error for %s: %s", path, exc)
            db_count -= 1  # this DB did not contribute cleanly
        finally:
            conn.close()

    # Sort merged results by created_at descending
    results.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {
        "ok": True,
        "results": results[:limit],
        "db_count": db_count,
        "total_results": len(results),
    }
