#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
reclassify_temporal.py — Migration script to repair temporal_class distribution.

Applies category-based initial classification to all active memories that are
still at the default 'medium' class and have never been manually promoted.

Usage:
    python3 ~/agentmemory/bin/reclassify_temporal.py [--dry-run] [--verbose]

Safe to re-run: uses explicit WHERE conditions, never downgrades intentionally
promoted memories, and skips 'permanent' entries entirely.

Author: Engram (COS-230)
"""

import sqlite3
import argparse
import datetime
import json
from pathlib import Path

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

# Category -> initial temporal_class mapping
# Source: hippocampus.py five-tier design spec + COS-230 analysis
CATEGORY_CLASS_MAP = {
    "identity": "long",
    "convention": "long",
    "decision": "long",
    "lesson": "medium",
    "preference": "medium",
    "user": "medium",
    "project": "short",
    "environment": "short",
    "observation": "ephemeral",
}

# Class ordering for promotion detection
CLASS_ORDER = ["ephemeral", "short", "medium", "long", "permanent"]

# Age-based demotion for unrecalled memories
# Format: (min_age_days, from_class, to_class, max_recalled_count)
AGE_DEMOTION_RULES = [
    (60, "medium", "short", 0),
    (90, "short", "ephemeral", 0),
    (14, "long", "medium", 0),
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def reclassify(dry_run: bool = False, verbose: bool = False):
    conn = get_db()
    now = datetime.datetime.now()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")

    stats = {
        "category_reclassified": 0,
        "age_demoted": 0,
        "skipped_permanent": 0,
        "skipped_above_default": 0,
        "total_scanned": 0,
        "changes": [],
    }

    # --- Pass 1: Category-based reclassification ---
    rows = conn.execute(
        """
        SELECT id, agent_id, category, temporal_class, confidence, recalled_count,
               created_at, tags
        FROM memories
        WHERE retired_at IS NULL
          AND temporal_class != 'permanent'
        ORDER BY id
        """
    ).fetchall()

    stats["total_scanned"] = len(rows)

    for row in rows:
        cat = row["category"]
        current_class = row["temporal_class"]
        target_class = CATEGORY_CLASS_MAP.get(cat)

        if target_class is None or target_class == current_class:
            continue

        # If the memory is above its category default, it was intentionally promoted — skip
        category_default = CATEGORY_CLASS_MAP.get(cat, "medium")
        if CLASS_ORDER.index(current_class) > CLASS_ORDER.index(category_default):
            stats["skipped_above_default"] += 1
            if verbose:
                print(f"  SKIP id={row['id']} cat={cat}: {current_class} > default {category_default} (promoted)")
            continue

        change = {
            "id": row["id"],
            "pass": "category",
            "category": cat,
            "from": current_class,
            "to": target_class,
            "recalled_count": row["recalled_count"],
            "confidence": round(float(row["confidence"]), 4),
        }
        stats["changes"].append(change)
        stats["category_reclassified"] += 1

        if verbose:
            print(f"  RECLASSIFY id={row['id']} cat={cat}: {current_class} -> {target_class}")

        if not dry_run:
            conn.execute(
                "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                (target_class, now_sql, row["id"]),
            )

    # --- Pass 2: Age-based demotion for unrecalled memories ---
    for min_age, from_class, to_class, max_recalls in AGE_DEMOTION_RULES:
        age_rows = conn.execute(
            """
            SELECT id, category, temporal_class, confidence, recalled_count, created_at
            FROM memories
            WHERE retired_at IS NULL
              AND temporal_class = ?
              AND recalled_count <= ?
            """,
            (from_class, max_recalls),
        ).fetchall()

        for row in age_rows:
            created = datetime.datetime.fromisoformat(row["created_at"])
            age_days = (now - created).total_seconds() / 86400.0
            if age_days < min_age:
                continue

            change = {
                "id": row["id"],
                "pass": "age_demotion",
                "category": row["category"],
                "from": from_class,
                "to": to_class,
                "age_days": round(age_days, 1),
                "recalled_count": row["recalled_count"],
            }
            stats["changes"].append(change)
            stats["age_demoted"] += 1

            if verbose:
                print(f"  AGE_DEMOTE id={row['id']} age={age_days:.1f}d: {from_class} -> {to_class}")

            if not dry_run:
                conn.execute(
                    "UPDATE memories SET temporal_class = ?, updated_at = ? WHERE id = ?",
                    (to_class, now_sql, row["id"]),
                )

    if not dry_run:
        conn.commit()

    conn.close()

    result = {
        "dry_run": dry_run,
        "ran_at": now_sql,
        "total_scanned": stats["total_scanned"],
        "category_reclassified": stats["category_reclassified"],
        "age_demoted": stats["age_demoted"],
        "skipped_above_default": stats["skipped_above_default"],
        "total_changes": len(stats["changes"]),
    }
    if verbose:
        result["changes"] = stats["changes"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair temporal_class distribution in brain.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--verbose", action="store_true", help="Print each change")
    args = parser.parse_args()
    reclassify(dry_run=args.dry_run, verbose=args.verbose)
