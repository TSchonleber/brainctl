"""brainctl merge — merge two brain.db files.

Merges the source database INTO the target database.
Uses ATTACH DATABASE to read source without copying the file.

Merge strategies per table:
- agents:           INSERT OR IGNORE (TEXT PK, idempotent)
- memories:         match on content+agent_id+category; keep higher confidence; insert new
- events:           always append (omit id so SQLite auto-assigns)
- entities:         match on name+scope; merge observations+properties; insert new
- knowledge_edges:  INSERT OR IGNORE on unique business key
- decisions:        always append (omit id)
- affect_log:       always append (omit id)
- reflexion_lessons: always append (omit id) — only if table exists in source
- agent_beliefs:    INSERT OR IGNORE on (agent_id, topic) — only if table exists in source
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if the given table (or attached prefix.table) exists."""
    # For attached DB queries use the prefix already in table name (e.g. "src.memories")
    # For plain table names check sqlite_master on conn
    if "." in table:
        prefix, tname = table.split(".", 1)
        row = conn.execute(
            f"SELECT 1 FROM {prefix}.sqlite_master WHERE type='table' AND name=?",
            (tname,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    return row is not None


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return non-generated column names for a table (supports 'prefix.table' notation).

    Uses PRAGMA table_xinfo which exposes the 'hidden' field:
      0 = normal column
      1 = virtual-table hidden column
      2 = stored generated column
      3 = virtual generated column
    We only want hidden < 2 (normal columns) so we never try to INSERT into a
    generated column.
    """
    if "." in table:
        prefix, tname = table.split(".", 1)
        pragma_xinfo = f"PRAGMA {prefix}.table_xinfo({tname})"
        pragma_info = f"PRAGMA {prefix}.table_info({tname})"
    else:
        pragma_xinfo = f"PRAGMA table_xinfo({table})"
        pragma_info = f"PRAGMA table_info({table})"

    try:
        rows = conn.execute(pragma_xinfo).fetchall()
        # table_xinfo: cid, name, type, notnull, dflt_value, pk, hidden
        return [r[1] for r in rows if r[6] < 2]
    except sqlite3.OperationalError:
        # Fallback for older SQLite builds that lack table_xinfo
        rows = conn.execute(pragma_info).fetchall()
        return [r[1] for r in rows]


def _merge_agents(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Merge agents table: INSERT OR IGNORE on TEXT PK."""
    rows_copied = 0
    conflicts = 0

    if not _table_exists(conn, "src.agents"):
        return rows_copied, conflicts

    src_cols = _get_columns(conn, "src.agents")
    col_names = ", ".join(src_cols)
    placeholders = ", ".join("?" * len(src_cols))
    src_rows = conn.execute(f"SELECT {col_names} FROM src.agents").fetchall()

    for row in src_rows:
        exists = conn.execute(
            "SELECT 1 FROM agents WHERE id = ?", (row[0],)
        ).fetchone()
        if exists:
            conflicts += 1
            continue
        if not dry_run:
            conn.execute(
                f"INSERT OR IGNORE INTO agents ({col_names}) VALUES ({placeholders})",
                tuple(row),
            )
        rows_copied += 1

    return rows_copied, conflicts


def _merge_memories(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Merge memories: match on content+agent_id+category, keep higher confidence."""
    rows_copied = 0
    conflicts = 0

    if not _table_exists(conn, "src.memories"):
        return rows_copied, conflicts

    src_cols = _get_columns(conn, "src.memories")
    dst_cols = _get_columns(conn, "memories")
    # Use columns present in both to handle schema drift
    common_cols = [c for c in src_cols if c in dst_cols and c != "id"]

    src_col_str = ", ".join(src_cols)
    src_rows = conn.execute(f"SELECT {src_col_str} FROM src.memories").fetchall()
    src_col_idx = {c: i for i, c in enumerate(src_cols)}

    for row in src_rows:
        content = row[src_col_idx["content"]]
        agent_id = row[src_col_idx["agent_id"]]
        category = row[src_col_idx["category"]]
        src_conf = row[src_col_idx["confidence"]]

        existing = conn.execute(
            "SELECT id, confidence FROM memories WHERE content = ? AND agent_id = ? AND category = ?",
            (content, agent_id, category),
        ).fetchone()

        if existing:
            conflicts += 1
            dst_conf = existing[1]
            if src_conf is not None and dst_conf is not None and src_conf > dst_conf:
                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET confidence = ? WHERE id = ?",
                        (src_conf, existing[0]),
                    )
        else:
            # Insert without id so SQLite assigns a fresh PK
            vals = [row[src_col_idx[c]] for c in common_cols]
            col_str = ", ".join(common_cols)
            placeholders = ", ".join("?" * len(common_cols))
            if not dry_run:
                conn.execute(
                    f"INSERT INTO memories ({col_str}) VALUES ({placeholders})",
                    vals,
                )
            rows_copied += 1

    return rows_copied, conflicts


def _merge_append_only(
    conn: sqlite3.Connection,
    table: str,
    dry_run: bool,
) -> int:
    """Append-only merge: always insert source rows with new IDs."""
    if not _table_exists(conn, f"src.{table}"):
        return 0

    src_cols = _get_columns(conn, f"src.{table}")
    dst_cols = _get_columns(conn, table)
    common_cols = [c for c in src_cols if c in dst_cols and c != "id"]

    src_col_idx = {c: i for i, c in enumerate(src_cols)}
    src_col_str = ", ".join(src_cols)
    src_rows = conn.execute(f"SELECT {src_col_str} FROM src.{table}").fetchall()

    col_str = ", ".join(common_cols)
    placeholders = ", ".join("?" * len(common_cols))

    count = 0
    for row in src_rows:
        vals = [row[src_col_idx[c]] for c in common_cols]
        if not dry_run:
            conn.execute(
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})",
                vals,
            )
        count += 1

    return count


def _merge_entities(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Merge entities: match on name+scope; merge observations+properties; insert new."""
    rows_copied = 0
    conflicts = 0

    if not _table_exists(conn, "src.entities"):
        return rows_copied, conflicts

    src_cols = _get_columns(conn, "src.entities")
    dst_cols = _get_columns(conn, "entities")
    common_cols = [c for c in src_cols if c in dst_cols and c != "id"]

    src_col_idx = {c: i for i, c in enumerate(src_cols)}
    src_col_str = ", ".join(src_cols)
    src_rows = conn.execute(f"SELECT {src_col_str} FROM src.entities").fetchall()

    for row in src_rows:
        name = row[src_col_idx["name"]]
        scope = row[src_col_idx["scope"]]
        src_obs_raw = row[src_col_idx["observations"]] if "observations" in src_col_idx else "[]"
        src_props_raw = row[src_col_idx["properties"]] if "properties" in src_col_idx else "{}"

        existing = conn.execute(
            "SELECT id, observations, properties FROM entities WHERE name = ? AND scope = ? AND retired_at IS NULL",
            (name, scope),
        ).fetchone()

        if existing:
            conflicts += 1
            # Merge observations (union of JSON arrays)
            try:
                dst_obs = json.loads(existing[1] or "[]")
                src_obs = json.loads(src_obs_raw or "[]")
                merged_obs = list(dict.fromkeys(dst_obs + [o for o in src_obs if o not in dst_obs]))
            except (json.JSONDecodeError, TypeError):
                merged_obs = None

            # Merge properties (source overrides target for same keys)
            try:
                dst_props = json.loads(existing[2] or "{}")
                src_props = json.loads(src_props_raw or "{}")
                merged_props = {**dst_props, **src_props}
            except (json.JSONDecodeError, TypeError):
                merged_props = None

            if not dry_run:
                updates = []
                params: list = []
                if merged_obs is not None:
                    updates.append("observations = ?")
                    params.append(json.dumps(merged_obs))
                if merged_props is not None:
                    updates.append("properties = ?")
                    params.append(json.dumps(merged_props))
                if updates:
                    params.append(existing[0])
                    conn.execute(
                        f"UPDATE entities SET {', '.join(updates)} WHERE id = ?",
                        params,
                    )
        else:
            vals = [row[src_col_idx[c]] for c in common_cols]
            col_str = ", ".join(common_cols)
            placeholders = ", ".join("?" * len(common_cols))
            if not dry_run:
                conn.execute(
                    f"INSERT INTO entities ({col_str}) VALUES ({placeholders})",
                    vals,
                )
            rows_copied += 1

    return rows_copied, conflicts


def _merge_knowledge_edges(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Merge knowledge_edges: INSERT OR IGNORE on unique business key."""
    rows_copied = 0
    conflicts = 0

    if not _table_exists(conn, "src.knowledge_edges"):
        return rows_copied, conflicts

    src_cols = _get_columns(conn, "src.knowledge_edges")
    dst_cols = _get_columns(conn, "knowledge_edges")
    common_cols = [c for c in src_cols if c in dst_cols and c != "id"]

    src_col_idx = {c: i for i, c in enumerate(src_cols)}
    src_col_str = ", ".join(src_cols)
    src_rows = conn.execute(f"SELECT {src_col_str} FROM src.knowledge_edges").fetchall()

    col_str = ", ".join(common_cols)
    placeholders = ", ".join("?" * len(common_cols))

    for row in src_rows:
        vals = [row[src_col_idx[c]] for c in common_cols]
        if not dry_run:
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO knowledge_edges ({col_str}) VALUES ({placeholders})",
                    vals,
                )
                # rowcount == 0 means the unique constraint fired
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    conflicts += 1
                else:
                    rows_copied += 1
            except sqlite3.IntegrityError:
                conflicts += 1
        else:
            # In dry_run mode, check if edge already exists
            st = row[src_col_idx["source_table"]]
            si = row[src_col_idx["source_id"]]
            tt = row[src_col_idx["target_table"]]
            ti = row[src_col_idx["target_id"]]
            rt = row[src_col_idx["relation_type"]]
            exists = conn.execute(
                "SELECT 1 FROM knowledge_edges WHERE source_table=? AND source_id=? "
                "AND target_table=? AND target_id=? AND relation_type=?",
                (st, si, tt, ti, rt),
            ).fetchone()
            if exists:
                conflicts += 1
            else:
                rows_copied += 1

    return rows_copied, conflicts


def _merge_agent_beliefs(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Merge agent_beliefs: INSERT OR IGNORE on (agent_id, topic)."""
    rows_copied = 0
    conflicts = 0

    if not _table_exists(conn, "src.agent_beliefs"):
        return rows_copied, conflicts
    if not _table_exists(conn, "agent_beliefs"):
        return rows_copied, conflicts

    src_cols = _get_columns(conn, "src.agent_beliefs")
    dst_cols = _get_columns(conn, "agent_beliefs")
    common_cols = [c for c in src_cols if c in dst_cols and c != "id"]

    src_col_idx = {c: i for i, c in enumerate(src_cols)}
    src_col_str = ", ".join(src_cols)
    src_rows = conn.execute(f"SELECT {src_col_str} FROM src.agent_beliefs").fetchall()

    col_str = ", ".join(common_cols)
    placeholders = ", ".join("?" * len(common_cols))

    for row in src_rows:
        vals = [row[src_col_idx[c]] for c in common_cols]
        agent_id = row[src_col_idx["agent_id"]] if "agent_id" in src_col_idx else None
        topic = row[src_col_idx["topic"]] if "topic" in src_col_idx else None

        exists = conn.execute(
            "SELECT 1 FROM agent_beliefs WHERE agent_id=? AND topic=?",
            (agent_id, topic),
        ).fetchone()
        if exists:
            conflicts += 1
            continue
        if not dry_run:
            conn.execute(
                f"INSERT OR IGNORE INTO agent_beliefs ({col_str}) VALUES ({placeholders})",
                vals,
            )
        rows_copied += 1

    return rows_copied, conflicts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Default table order (respects FK dependencies)
_DEFAULT_TABLES = [
    "agents",
    "memories",
    "events",
    "entities",
    "knowledge_edges",
    "decisions",
    "affect_log",
    "reflexion_lessons",
    "agent_beliefs",
]


def merge(
    source_path: str,
    target_path: str,
    dry_run: bool = False,
    tables: Optional[list[str]] = None,
) -> dict:
    """Merge source brain.db into target brain.db.

    Parameters
    ----------
    source_path:
        Path to the source brain.db (read-only; opened via ATTACH).
    target_path:
        Path to the target brain.db (modified in-place).
    dry_run:
        If True, compute what would happen but make no changes.
    tables:
        Optional list of table names to merge. Defaults to all known tables.

    Returns
    -------
    dict with keys:
        tables_merged      - list of table names actually processed
        rows_copied        - total rows inserted into target
        conflicts_resolved - total rows where a conflict was detected
        skipped            - list of table names skipped (not found in source)
        dry_run            - whether this was a dry run
    """
    source_path = str(Path(source_path).expanduser())
    target_path = str(Path(target_path).expanduser())

    if not Path(source_path).exists():
        raise FileNotFoundError(f"Source DB not found: {source_path}")
    if not Path(target_path).exists():
        raise FileNotFoundError(f"Target DB not found: {target_path}")

    tables_to_merge = tables if tables is not None else list(_DEFAULT_TABLES)

    conn = sqlite3.connect(target_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = OFF")  # allow inserting without all FKs satisfied in order

    try:
        conn.execute("ATTACH DATABASE ? AS src", (source_path,))

        tables_merged: list[str] = []
        skipped: list[str] = []
        total_copied = 0
        total_conflicts = 0

        for table in tables_to_merge:
            # Check source table exists
            src_exists = _table_exists(conn, f"src.{table}")
            if not src_exists:
                skipped.append(table)
                logger.debug("Skipping table %s (not found in source)", table)
                continue

            copied = 0
            conflicts = 0

            if table == "agents":
                copied, conflicts = _merge_agents(conn, dry_run)
            elif table == "memories":
                copied, conflicts = _merge_memories(conn, dry_run)
            elif table == "entities":
                copied, conflicts = _merge_entities(conn, dry_run)
            elif table == "knowledge_edges":
                copied, conflicts = _merge_knowledge_edges(conn, dry_run)
            elif table == "agent_beliefs":
                copied, conflicts = _merge_agent_beliefs(conn, dry_run)
            elif table in ("events", "decisions", "affect_log",
                           "reflexion_lessons"):
                # Check target table exists too
                if not _table_exists(conn, table):
                    skipped.append(table)
                    continue
                copied = _merge_append_only(conn, table, dry_run)
            else:
                skipped.append(table)
                continue

            tables_merged.append(table)
            total_copied += copied
            total_conflicts += conflicts
            logger.debug(
                "Table %s: %d rows copied, %d conflicts",
                table, copied, conflicts,
            )

        if not dry_run:
            conn.commit()

        return {
            "tables_merged": tables_merged,
            "rows_copied": total_copied,
            "conflicts_resolved": total_conflicts,
            "skipped": skipped,
            "dry_run": dry_run,
        }

    except Exception:
        if not dry_run:
            conn.rollback()
        raise
    finally:
        try:
            conn.execute("DETACH DATABASE src")
        except Exception:
            pass
        conn.close()


def status(source_path: str, target_path: str) -> dict:
    """Preview what a merge would do without executing it.

    Equivalent to merge(..., dry_run=True).
    """
    return merge(source_path, target_path, dry_run=True)
