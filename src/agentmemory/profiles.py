"""
brainctl context profiles — task-scoped search presets.

A profile is a named configuration that scopes search to the memory categories,
tables, and entity types relevant for a specific task mode. Agents pass
--profile NAME instead of manually specifying --tables and --category on every
query.

Built-in profiles cover the most common agent task modes. Users can define
custom profiles, stored in brain.db (context_profiles table).

Usage
-----
CLI:   brainctl search "query" --profile writing
       brainctl memory search "query" --profile research
       brainctl profile list
       brainctl profile create myprofile --categories lesson,decision --tables memories,events

MCP:   tool_memory_search(agent_id, query, profile="writing")
       tool_search(agent_id, query, profile="ops")
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "writing": {
        "description": "Voice, style, and writing conventions",
        "categories": ["preference", "convention", "lesson"],
        "tables": ["memories", "entities"],
        "entity_types": [],
    },
    "meeting": {
        "description": "Contacts, interaction history, and project context",
        "categories": ["user", "project", "preference"],
        "tables": ["memories", "events", "entities"],
        "entity_types": ["person"],
    },
    "research": {
        "description": "Technical knowledge and learned lessons",
        "categories": ["integration", "convention", "lesson", "environment"],
        "tables": ["memories", "entities"],
        "entity_types": [],
    },
    "ops": {
        "description": "Operational context and decision history",
        "categories": ["project", "decision", "lesson"],
        "tables": ["memories", "events", "decisions"],
        "entity_types": [],
    },
    "networking": {
        "description": "Contacts and relationship context",
        "categories": ["user"],
        "tables": ["entities", "memories"],
        "entity_types": ["person", "organization"],
    },
    "review": {
        "description": "Retrospective context for periodic reviews",
        "categories": ["lesson", "decision", "project"],
        "tables": ["memories", "events", "decisions"],
        "entity_types": [],
    },
}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS context_profiles (
    name         TEXT PRIMARY KEY,
    description  TEXT,
    categories   TEXT,
    tables       TEXT,
    entity_types TEXT,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE)
    conn.commit()


def resolve_profile(name: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return profile dict for name, checking builtins then user-defined in DB."""
    if name in BUILTIN_PROFILES:
        return dict(BUILTIN_PROFILES[name]) | {"name": name, "builtin": True}
    if db_path is None:
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        row = conn.execute(
            "SELECT * FROM context_profiles WHERE name = ?", (name,)
        ).fetchone()
        conn.close()
        if row:
            return {
                "name": row["name"],
                "description": row["description"] or "",
                "categories": json.loads(row["categories"] or "[]"),
                "tables": json.loads(row["tables"] or "[]"),
                "entity_types": json.loads(row["entity_types"] or "[]"),
                "builtin": False,
            }
    except Exception:
        pass
    return None


def list_profiles(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return all profiles: built-ins first, then user-defined from DB."""
    profiles = [
        dict(v) | {"name": k, "builtin": True}
        for k, v in BUILTIN_PROFILES.items()
    ]
    if db_path is None:
        return profiles
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT * FROM context_profiles ORDER BY name"
        ).fetchall()
        conn.close()
        builtin_names = set(BUILTIN_PROFILES)
        for row in rows:
            if row["name"] not in builtin_names:
                profiles.append({
                    "name": row["name"],
                    "description": row["description"] or "",
                    "categories": json.loads(row["categories"] or "[]"),
                    "tables": json.loads(row["tables"] or "[]"),
                    "entity_types": json.loads(row["entity_types"] or "[]"),
                    "builtin": False,
                })
    except Exception:
        pass
    return profiles


def create_profile(
    name: str,
    categories: List[str],
    tables: List[str],
    entity_types: List[str],
    description: str,
    db_path: Path,
) -> bool:
    """Create or replace a user-defined profile. Built-ins cannot be overwritten."""
    if name in BUILTIN_PROFILES:
        print(
            f"Cannot overwrite built-in profile '{name}'. "
            f"Choose a different name.",
            file=sys.stderr,
        )
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO context_profiles "
            "(name, description, categories, tables, entity_types) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                description,
                json.dumps(categories),
                json.dumps(tables),
                json.dumps(entity_types),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error creating profile: {e}", file=sys.stderr)
        return False


def delete_profile(name: str, db_path: Path) -> bool:
    """Delete a user-defined profile. Built-ins cannot be deleted."""
    if name in BUILTIN_PROFILES:
        print(
            f"Cannot delete built-in profile '{name}'.",
            file=sys.stderr,
        )
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        _ensure_table(conn)
        cur = conn.execute("DELETE FROM context_profiles WHERE name = ?", (name,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0
    except Exception as e:
        print(f"Error deleting profile: {e}", file=sys.stderr)
        return False


def apply_profile(args: Any, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    Resolve args.profile and apply its constraints to args in-place.

    Rules:
    - Explicit --tables always wins over profile.tables
    - Explicit --category always wins over profile.categories
    - Profile categories are stored as args._profile_categories (list)
      for multi-category filtering in search functions

    Returns the resolved profile dict, or None if no --profile was set.
    Calls sys.exit(1) if the profile name is unknown.
    """
    profile_name = getattr(args, "profile", None)
    if not profile_name:
        return None

    profile = resolve_profile(profile_name, db_path)
    if not profile:
        print(
            f"Unknown profile '{profile_name}'. "
            f"Run `brainctl profile list` to see available profiles.",
            file=sys.stderr,
        )
        sys.exit(1)

    if profile.get("tables") and not getattr(args, "tables", None):
        args.tables = ",".join(profile["tables"])

    if profile.get("categories") and not getattr(args, "category", None):
        args._profile_categories = profile["categories"]

    if profile.get("entity_types"):
        args._profile_entity_types = profile["entity_types"]

    return profile
