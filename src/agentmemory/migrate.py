"""brainctl migration runner.

Reads migration SQL files from db/migrations/, applies unapplied ones in order,
tracks applied migrations in schema_versions table.

============================================================================
DUPLICATE-VERSION HISTORY (read this before adding a new migration)
============================================================================

Up to v2.1.x, eight migration files in db/migrations/ shared four version
numbers (012, 013, 017, 023). The runner sorts files alphabetically and
records the integer version once it finishes the FIRST file at that
version; the SECOND file at the same version was then silently skipped
forever on fresh DBs because its version was already in
``schema_versions``.

Subsystems lost on fresh installs:
  * 012_theory_of_mind.sql        (agent_beliefs, belief_conflicts,
                                    agent_perspective_models, agent_bdi_state)
  * 013_global_workspace.sql      (workspace_config/broadcasts/acks/phi)
  * 017_memory_rbac.sql           (memories.visibility/read_acl)
  * 023_uncertainty_log_search_columns.sql
                                  (agent_uncertainty_log search columns)

In v2.2.0 these four files were renumbered to slots 043-046 (the next
free integers above the previous high-water 042) and made idempotent
(``CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS``; ADD COLUMN duplicate-column
errors swallowed by ``_apply_sql`` below). Existing brain.db installs
that already have the affected tables — including the user's own DB,
which had ad-hoc patches — pick up the renumbered migrations as no-ops
on the next ``brainctl migrate`` run.

DESIGN RATIONALE — why option (a) renumber, not (b) suffix-ordinal:
  1. SQLite triggers reference tables lazily; CREATE TRIGGER does not
     validate referenced tables, so moving a defining migration to a
     LATER slot does not break create-time ordering for
     trigger-bearing downstream migrations.
  2. The only cross-migration table reference among the lost-pair
     payloads is ``017_global_workspace_memory_columns`` inserting into
     ``workspace_broadcasts`` (defined in 013_global_workspace) inside
     a trigger body. That trigger only fires on UPDATE OF gw_broadcast,
     never at migrate time.
  3. Option (b) would have required a schema_versions table migration
     of its own (adding a suffix column) — solving a problem the
     simpler renumber doesn't have.

WHEN ADDING A NEW MIGRATION:
  * Pick the next unused integer above the current max (use
    ``brainctl migrate --status-verbose`` or ``ls db/migrations/``).
  * Use ``CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS``.
  * For ALTER TABLE ADD COLUMN, just write it plain — ``_apply_sql``
    catches the duplicate-column failure on re-apply. SQLite has no
    native ``ADD COLUMN IF NOT EXISTS`` syntax.
  * If you write the inner ``INSERT INTO schema_version (version, ...)``
    row, make sure the integer matches the file's ordinal.
  * Run ``python3 -c "from agentmemory import migrate;
    migrate._check_no_duplicate_versions()"`` — it will raise loudly
    if you accidentally introduce another duplicate.

============================================================================
"""
import sqlite3
import re
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone


MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "db" / "migrations"


# Errors that mean "this DDL already happened, treat as no-op". Matched
# case-insensitively against the SQLite error text. Empirically observed
# error messages from sqlite 3.51.x are listed; see _apply_sql below.
#
# DELIBERATELY NARROW: every fragment here is a load-bearing claim that
# the matching error proves the DDL already happened. Don't add general
# "object exists" wording or you'll start swallowing real bugs (e.g. a
# misspelled column in a SELECT).
_IDEMPOTENT_ERROR_FRAGMENTS = (
    "duplicate column name",  # ALTER TABLE ADD COLUMN of existing column
)

# Per-statement-kind tolerated errors. Looser than the global list because
# they only apply when we know the surrounding DDL kind makes the error
# semantically a no-op:
#   * CREATE INDEX on a missing column on an existing table = the table
#     was created by an earlier migration with a different column shape,
#     and the new index simply can't apply. Logging is the right answer;
#     crashing the whole migration breaks the user's upgrade for a
#     legacy column rename they may not even use.
#   * CREATE TRIGGER, same logic.
#   * "already exists" on each of the four CREATE kinds is the fresh-install
#     idempotency case: init_schema.sql is a snapshot that already declares
#     many objects which migrations also declare. Before 2.4.8, migration
#     002's `CREATE INDEX idx_epochs_started` aborted the whole run on every
#     fresh install. Fixed per-kind (not as a global fragment) so we still
#     crash loudly on e.g. an unrelated "view main.foo already exists" in
#     a non-CREATE statement path or a genuinely duplicated idempotent
#     rebuild that would mask data loss.
_PER_KIND_TOLERATED_FRAGMENTS = {
    "CREATE INDEX":   ("no such column", "already exists"),
    "CREATE TABLE":   ("already exists",),
    "CREATE TRIGGER": ("no such column", "already exists"),
    "CREATE VIEW":    ("already exists",),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _get_migrations() -> list[tuple[int, str, Path]]:
    """Return sorted list of (version, name, path) for all migration files.

    Raises ``ValueError`` if two files share the same integer version —
    that's the bug we shipped through v2.1.x and never want to ship again.
    Bypass via env var ``BRAINCTL_ALLOW_DUPLICATE_MIGRATIONS=1`` only if
    you're auditing an old branch; on main this should never trigger.
    """
    migrations = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r'^(\d+)_(.+)\.sql$', f.name)
        if m:
            version = int(m.group(1))
            name = m.group(2).replace('_', ' ')
            migrations.append((version, name, f))

    import os
    if not os.environ.get("BRAINCTL_ALLOW_DUPLICATE_MIGRATIONS"):
        _check_no_duplicate_versions(migrations)
    return migrations


def _check_no_duplicate_versions(
    migrations: list[tuple[int, str, Path]] | None = None
) -> None:
    """Hard-fail if any integer version is shared by two or more files.

    Called from ``_get_migrations`` on every status/run/status_verbose
    invocation. Also exposed as a standalone helper so a contributor
    adding a migration can sanity-check their file name in one line:
    ``python3 -c "from agentmemory import migrate; migrate._check_no_duplicate_versions()"``.
    """
    if migrations is None:
        # Re-glob without the recursion guard.
        migrations = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            m = re.match(r'^(\d+)_(.+)\.sql$', f.name)
            if m:
                migrations.append((int(m.group(1)), m.group(2), f))

    by_version: dict[int, list[str]] = defaultdict(list)
    for version, _name, path in migrations:
        by_version[version].append(path.name)
    dupes = {v: names for v, names in by_version.items() if len(names) > 1}
    if dupes:
        lines = [f"  version {v:03d}: {', '.join(sorted(names))}" for v, names in sorted(dupes.items())]
        raise ValueError(
            "Duplicate-version migration files detected — alphabetical sort would "
            "silently skip the second file at each shared version. Rename one of "
            "each pair to a free integer slot (use the next number above the "
            "current high-water mark).\n" + "\n".join(lines)
        )


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


def _strip_line_comment(line: str) -> str:
    """Return ``line`` with any trailing ``-- comment`` removed.

    SQLite line comments start at the first unquoted ``--``. We don't fully
    parse string literals — none of our migrations end a logical statement
    inside one, but we do correctly skip inline comments AFTER a terminating
    ``;``, which is what tripped up the splitter on the
    ``('enabled', '1');     -- kill switch`` line in 044_global_workspace.
    """
    in_single = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "'":
            # Toggle quoted-string state. Doubled '' inside a literal is
            # SQLite's escape and remains "in" the string.
            in_single = not in_single
        elif not in_single and c == "-" and i + 1 < n and line[i + 1] == "-":
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    ``executescript`` is all-or-nothing per script; we want statement-level
    error recovery so an idempotent ``ALTER TABLE ADD COLUMN`` whose column
    already exists doesn't abort the whole migration. The split is naive but
    handles the constructs our migrations use (multi-line CREATE TABLE,
    CREATE TRIGGER ... BEGIN ... END;, comments, blank lines, and inline
    line comments after terminating ``;``).

    Notes for future maintainers:
      * BEGIN/END blocks (triggers) are recognized and kept as one statement
        until matching END;.
      * Inline ``-- comment`` after a terminating ``;`` is handled via
        ``_strip_line_comment``. A semicolon hidden inside a quoted string
        literal still won't terminate the statement (we'd need a real
        tokenizer for that); none of our migrations rely on that.
    """
    statements: list[str] = []
    current: list[str] = []
    in_trigger_body = False

    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        stripped_upper = line.strip().upper()
        if not in_trigger_body and (
            stripped_upper.startswith("CREATE TRIGGER")
            or stripped_upper.startswith("CREATE TEMP TRIGGER")
        ):
            in_trigger_body = True
        current.append(line)
        if in_trigger_body:
            # BEGIN ... END; ends a trigger body. Strip any inline comment
            # before checking so e.g. "END;  -- close" still matches.
            tail = _strip_line_comment(line).strip().upper()
            if tail in ("END;", "END ;"):
                statements.append("\n".join(current).strip())
                current = []
                in_trigger_body = False
        else:
            # A statement terminates on a `;` that is not inside a comment.
            # Strip inline `-- ...` first so the very common pattern
            # `...);    -- inline note` is detected as a terminator.
            cleaned = _strip_line_comment(line)
            if cleaned.endswith(";"):
                statements.append("\n".join(current).strip())
                current = []

    # Trailing fragment (file without trailing newline / no terminating ;)
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)

    # Filter empties and comment-only statements (those would be no-ops in
    # ``execute`` but generate noisy "incomplete input" if they slip through).
    out = []
    for s in statements:
        # Strip both line and block comments (block via -- line by line);
        # if nothing meaningful remains, skip.
        non_comment = "\n".join(_strip_line_comment(l) for l in s.splitlines()).strip()
        if non_comment:
            out.append(s)
    return out


def _apply_sql(conn: sqlite3.Connection, sql: str, file_label: str) -> tuple[int, list[str]]:
    """Apply SQL one statement at a time, swallowing idempotent failures.

    Returns ``(stmts_run, idempotent_skipped)`` where ``idempotent_skipped``
    is a list of human-readable notes about statements that were no-ops on
    re-application (e.g. ``ADD COLUMN visibility`` when the column was
    already present from init_schema.sql).

    Any error that does NOT match an idempotent fragment is re-raised; the
    caller is expected to record it in the ``errors`` list and bail out of
    the run.
    """
    stmts = _split_sql_statements(sql)
    skipped: list[str] = []
    run_count = 0
    for stmt in stmts:
        try:
            conn.execute(stmt)
            run_count += 1
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            first_line = stmt.strip().splitlines()[0][:100]

            # Globally idempotent errors — true regardless of statement kind.
            if any(frag in msg for frag in _IDEMPOTENT_ERROR_FRAGMENTS):
                skipped.append(f"{file_label}: idempotent skip — {exc}: `{first_line}`")
                continue

            # Per-statement-kind tolerance: only when the leading DDL
            # keyword matches a known-safe pattern AND the error text is on
            # that pattern's tolerated list. The leading-token detection
            # collapses whitespace so multi-line CREATE statements still
            # match. Comment/blank prefix lines are skipped — the splitter
            # attaches inline header comments to the following statement
            # (e.g. `-- CHECK constraint triggers\nCREATE TRIGGER ...`),
            # so naively taking the first two tokens yields `-- CHECK`
            # instead of `CREATE TRIGGER`.
            leading = ""
            for ln in stmt.splitlines():
                stripped = ln.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                leading = " ".join(stripped.split()[:2]).upper()
                break
            kind_tolerated = _PER_KIND_TOLERATED_FRAGMENTS.get(leading, ())
            if any(frag in msg for frag in kind_tolerated):
                skipped.append(
                    f"{file_label}: tolerated {leading} skip — {exc}: `{first_line}`"
                )
                continue

            raise
    conn.commit()
    return run_count, skipped


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
    idempotent_notes: list[str] = []
    for version, name, path in pending:
        sql = path.read_text()
        if dry_run:
            applied.append({"version": version, "name": name, "file": path.name, "dry_run": True})
            continue
        try:
            stmts_run, skipped = _apply_sql(conn, sql, path.name)
            idempotent_notes.extend(skipped)
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _utc_now_iso())
            )
            conn.commit()
            applied.append({
                "version": version,
                "name": name,
                "file": path.name,
                "stmts_run": stmts_run,
                "idempotent_skipped": len(skipped),
            })
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
        "idempotent_notes": idempotent_notes,
    }


def mark_applied_up_to(db_path: str, up_to: int, dry_run: bool = False) -> dict:
    """Backfill schema_versions for migrations 1..up_to.

    For users whose brain.db predates the migration tracking framework:
    their schema already has the effects of many migrations, but
    schema_versions is empty (or partial), so `brainctl migrate` would
    try to re-apply everything and crash on column collisions.

    This command marks migrations 1..up_to as applied (INSERT OR IGNORE)
    with a 'backfilled' suffix on the name, so the runner will skip them
    on subsequent `brainctl migrate` invocations. It does NOT execute the
    migration SQL — it only updates the tracker.

    Guard: refuses if up_to is LOWER than the max version already tracked.
    That would mark earlier migrations as applied when the user presumably
    already skipped them intentionally. Backfilling ABOVE the current
    high-water mark is always allowed — this handles the case where a
    user ran `brainctl migrate`, got a few through, crashed, and now needs
    to skip the rest.

    Args:
        db_path: path to brain.db
        up_to: highest migration version to mark as applied (inclusive)
        dry_run: preview only; no writes

    Returns:
        dict with keys: ok, dry_run, marked (list of versions), skipped
        (list of already-tracked versions), guard_error (if refused), etc.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _ensure_schema_versions(conn)

    migrations = _get_migrations()
    max_available = max((v for v, _, _ in migrations), default=0)

    if up_to < 1:
        conn.close()
        return {
            "ok": False,
            "dry_run": dry_run,
            "guard_error": f"--mark-applied-up-to must be >= 1 (got {up_to})",
        }
    if up_to > max_available:
        conn.close()
        return {
            "ok": False,
            "dry_run": dry_run,
            "guard_error": (
                f"--mark-applied-up-to {up_to} exceeds highest available migration "
                f"version ({max_available}). Check db/migrations/ or pass a lower N."
            ),
        }

    # Guard: refuse if up_to is lower than the max currently-tracked version.
    # Allow equal or higher — users who crashed mid-migrate need to backfill
    # above their current high-water mark.
    existing = conn.execute(
        "SELECT version, name, applied_at FROM schema_versions ORDER BY version"
    ).fetchall()
    existing_versions = {r["version"] for r in existing}
    max_tracked = max(existing_versions, default=0)

    if existing_versions and up_to < max_tracked:
        conn.close()
        return {
            "ok": False,
            "dry_run": dry_run,
            "max_tracked": max_tracked,
            "guard_error": (
                f"--mark-applied-up-to {up_to} is lower than the highest already-tracked "
                f"migration ({max_tracked}). Backfilling below the current high-water mark "
                f"would mark migrations as applied that were presumably skipped for a reason. "
                f"If you really need to rewrite tracker state, edit schema_versions directly."
            ),
        }

    # Figure out which versions to mark. Every migration version in [1..up_to]
    # that isn't already tracked gets a row. With duplicate-version files
    # banned by ``_check_no_duplicate_versions``, every version maps to
    # exactly one file — no collapsing needed.
    targets: dict[int, str] = {}
    for version, name, _path in migrations:
        if version <= up_to and version not in existing_versions:
            targets[version] = name

    if not targets:
        conn.close()
        return {
            "ok": True,
            "dry_run": dry_run,
            "marked": [],
            "skipped": sorted(existing_versions & set(range(1, up_to + 1))),
            "max_tracked": max_tracked,
            "up_to": up_to,
            "message": "Nothing to backfill — schema_versions is already at or above up_to.",
        }

    if dry_run:
        conn.close()
        return {
            "ok": True,
            "dry_run": True,
            "would_mark": [{"version": v, "name": n} for v, n in sorted(targets.items())],
            "already_tracked": sorted(existing_versions),
            "up_to": up_to,
        }

    applied_at = _utc_now_iso()
    marked = []
    for version, name in sorted(targets.items()):
        tracker_name = f"{name} (backfilled)"
        conn.execute(
            "INSERT OR IGNORE INTO schema_versions (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (version, tracker_name, applied_at),
        )
        marked.append({"version": version, "name": tracker_name})
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "dry_run": False,
        "marked": marked,
        "applied_at": applied_at,
        "up_to": up_to,
        "previous_max_tracked": max_tracked,
    }


def status_verbose(db_path: str) -> dict:
    """Like status(), plus per-migration heuristic: does the schema already
    look like this migration has run?

    The heuristic is cheap and best-effort: for each migration file, parse
    ADD COLUMN statements and CREATE TABLE statements, then check
    sqlite_master / PRAGMA table_xinfo to see if those columns/tables
    exist. If everything the migration would create already exists, mark it
    "likely-applied". If nothing exists, mark it "pending". Mixed state →
    "partial".

    Uses PRAGMA table_xinfo (not table_info) so generated virtual columns
    added via `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS AS (...)
    VIRTUAL` are detected — table_info hides them.

    The ADD COLUMN regex tolerates `IF NOT EXISTS` between COLUMN and the
    column name, so migrations like
    `ALTER TABLE access_log ADD COLUMN IF NOT EXISTS tokens_consumed`
    don't get mis-classified with `IF` as the column name.

    Used by `brainctl doctor` and `brainctl migrate --status-verbose` to
    help users pick a safe value for `--mark-applied-up-to N`.
    """
    base = status(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    # Cheap helpers: collect existing tables and (table → columns).
    # table_xinfo includes hidden + generated virtual columns; table_info
    # hides them. Migration 024 (confidence_alpha/beta) was invisible to
    # table_info and would false-negative here otherwise.
    existing_tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    table_columns: dict[str, set[str]] = {}
    for t in existing_tables:
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_xinfo({t})").fetchall()}
            table_columns[t] = cols
        except sqlite3.OperationalError:
            table_columns[t] = set()
    conn.close()

    # Regex the SQL files — imperfect but good enough for a hint.
    # ADD COLUMN pattern tolerates `IF NOT EXISTS` between COLUMN and the
    # identifier so migration 023's `ALTER TABLE access_log ADD COLUMN IF
    # NOT EXISTS tokens_consumed` doesn't capture `IF` as the column name.
    add_col_re = re.compile(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        re.IGNORECASE,
    )
    create_tbl_re = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        re.IGNORECASE,
    )

    annotated = []
    for version, name, path in _get_migrations():
        sql = path.read_text()
        expected_cols = add_col_re.findall(sql)   # list of (table, col)
        expected_tbls = create_tbl_re.findall(sql)  # list of table names

        col_hits = 0
        col_total = 0
        for table, col in expected_cols:
            col_total += 1
            if col in table_columns.get(table, set()):
                col_hits += 1

        tbl_hits = 0
        tbl_total = 0
        for table in expected_tbls:
            tbl_total += 1
            if table in existing_tables:
                tbl_hits += 1

        total_checks = col_total + tbl_total
        hits = col_hits + tbl_hits

        if total_checks == 0:
            heuristic = "unknown"  # no DDL we can introspect (trigger-only, UPDATE-only, etc.)
        elif hits == total_checks:
            heuristic = "likely-applied"
        elif hits == 0:
            heuristic = "pending"
        else:
            heuristic = "partial"

        annotated.append({
            "version": version,
            "name": name,
            "file": path.name,
            "heuristic": heuristic,
            "ddl_hits": f"{hits}/{total_checks}",
        })

    return {
        **base,
        "migrations_verbose": annotated,
    }
