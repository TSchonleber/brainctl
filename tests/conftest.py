"""Shared fixtures for brainctl test suite."""
import importlib
import sys
import os
import sqlite3
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROD_DB = Path(__file__).resolve().parent.parent / "db" / "brain.db"

from agentmemory.brain import Brain


# --- Auto-restore monkey-patched module-level helpers --------------------
# Several brain-inspired subsystem tests monkey-patch `open_db` and `DB_PATH`
# on their respective `mcp_tools_*` modules to redirect DB access to an
# in-memory or temp-file connection. Those patches don't restore themselves,
# which causes test-order-dependent failures when a downstream test calls
# the production helper expecting the real DB. This fixture snapshots the
# originals before each test and restores them after, regardless of how
# the test exits.
_LEAKY_MODULES = (
    "agentmemory.mcp_tools_thalamus",
    "agentmemory.mcp_tools_basal_ganglia",
    "agentmemory.mcp_tools_cerebellum",
)


@pytest.fixture(autouse=True)
def _restore_module_helpers():
    saved = {}
    for mod_name in _LEAKY_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        saved[mod_name] = {
            "open_db": getattr(mod, "open_db", None),
            "DB_PATH": getattr(mod, "DB_PATH", None),
        }
    yield
    for mod_name, snapshot in saved.items():
        try:
            mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
        except Exception:
            continue
        for attr, value in snapshot.items():
            if value is not None:
                setattr(mod, attr, value)


@pytest.fixture
def brain(tmp_path):
    """Return a Brain instance backed by a temp DB file."""
    db_file = tmp_path / "brain.db"
    return Brain(db_path=str(db_file), agent_id="test-agent")


@pytest.fixture
def brain_with_data(brain):
    """Brain pre-loaded with sample memories, entities, events."""
    brain.remember("User prefers dark mode", category="preference", confidence=0.9)
    brain.remember("Project uses Python 3.12", category="project", confidence=1.0)
    brain.remember("Deploy to staging first", category="lesson", confidence=0.8)
    brain.entity("Alice", "person", observations=["Engineer", "Likes coffee"])
    brain.entity("BrainProject", "project", observations=["Memory system"])
    brain.relate("Alice", "works_on", "BrainProject")
    brain.log("Started dev session", event_type="session", project="brain")
    brain.log("Deployed v1.0", event_type="deploy", project="brain")
    return brain


@pytest.fixture
def cli_db(tmp_path):
    """Create an empty DB with the full production schema for CLI tests.

    Uses brainctl init which loads the packaged init_schema.sql — works
    both locally and in CI (no dependency on a pre-existing brain.db).
    """
    import subprocess
    db_file = tmp_path / "brain.db"

    # Use brainctl init to create full schema (same as pip install user would)
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(SRC)!r}); "
         f"import agentmemory._impl as _i; from pathlib import Path; "
         f"_i.DB_PATH = Path({str(db_file)!r}); "
         f"sys.argv = ['brainctl', 'init', '--path', {str(db_file)!r}]; "
         f"_i.main()"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )

    if result.returncode != 0 or not db_file.exists():
        # Fallback: use Brain class for minimal schema
        Brain(db_path=str(db_file), agent_id="default")

    # Insert test agents to satisfy FK constraints
    conn = sqlite3.connect(str(db_file))
    for aid in ('tester', 'fmt', 'unknown', 'default'):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, "
                "created_at, updated_at) VALUES (?, ?, 'test', 'active', "
                "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
                (aid, aid)
            )
        except Exception:
            pass
    conn.commit()
    conn.close()
    return db_file
