"""brainctl migration runner.

Reads migration SQL files from db/migrations/, applies unapplied ones in order,
tracks applied migrations in schema_versions table.
"""
import sqlite3
import re
import sys
from pathlib import Path
from datetime import datetime, timezone


MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "db" / "migrations"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _get_migrations() -> list[tuple[int, str, Path]]:
    """Return sorted list of (version, name, path) for all migration files."""
    migrations = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r'^(\d+)_(.+)\.sql$', f.name)
        if m:
            version = int(m.group(1))
            name = m.group(2).replace('_', ' ')
            migrations.append((version, name, f))
    return migrations


def _ensure_schema_versions(conn: sqlite3.Connection) -> None:
    """Create schema_versions tracking table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _get_applied(conn: sqlite3.Connection) -> set[int]:
    """Return set of already-applied migration versions."""
    try:
        rows = conn.execute("SELECT version FROM schema_versions").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


def status(db_path: str) -> dict:
    """Return migration status report."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    _ensure_schema_versions(conn)
    applied = _get_applied(conn)
    migrations = _get_migrations()

    rows = conn.execute(
        "SELECT version, name, applied_at FROM schema_versions ORDER BY version"
    ).fetchall() if applied else []
    applied_rows = [dict(r) for r in rows]

    pending = [(v, n, p) for v, n, p in migrations if v not in applied]
    conn.close()

    return {
        "total": len(migrations),
        "applied": len(applied),
        "pending": len(pending),
        "applied_migrations": applied_rows,
        "pending_migrations": [{"version": v, "name": n, "file": str(p.name)} for v, n, p in pending],
    }


def run(db_path: str, dry_run: bool = False) -> dict:
    """Apply all pending migrations. Returns result dict."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema_versions(conn)
    applied_set = _get_applied(conn)
    migrations = _get_migrations()
    pending = [(v, n, p) for v, n, p in migrations if v not in applied_set]

    if not pending:
        conn.close()
        return {"ok": True, "applied": 0, "dry_run": dry_run, "message": "Already up to date."}

    applied = []
    errors = []
    for version, name, path in pending:
        sql = path.read_text()
        if dry_run:
            applied.append({"version": version, "name": name, "file": path.name, "dry_run": True})
            continue
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _utc_now_iso())
            )
            conn.commit()
            applied.append({"version": version, "name": name, "file": path.name})
        except Exception as exc:
            errors.append({"version": version, "name": name, "error": str(exc)})
            break  # stop on first error

    conn.close()
    return {
        "ok": len(errors) == 0,
        "applied": len(applied),
        "dry_run": dry_run,
        "migrations": applied,
        "errors": errors,
    }
