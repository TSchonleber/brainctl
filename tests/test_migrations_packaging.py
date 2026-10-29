"""Migration SQL files ship inside the ``agentmemory`` package.

They used to live only at the repo root (``db/migrations/``), outside the
``src/`` package tree, so a regular (non-editable) install had none:
``brainctl migrate`` reported "Already up to date." and ``brainctl init``
recorded no migrations in ``schema_versions``. These tests pin the packaged
location, the package-data globs that ship it, the loud failure when the
directory is missing, and the refusal to replay every migration over a
populated database whose tracker is empty (the state such installs left
behind).
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "src" / "agentmemory"


def test_migrations_resolve_next_to_the_package() -> None:
    import agentmemory
    from agentmemory import migrate

    pkg_dir = Path(agentmemory.__file__).resolve().parent
    assert migrate.MIGRATIONS_DIR.resolve() == (pkg_dir / "db" / "migrations").resolve()
    assert migrate._get_migrations(), "no numbered migrations found"


def test_package_data_ships_every_migration() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    globs = config["tool"]["setuptools"]["package-data"]["agentmemory"]
    shipped = {p.resolve() for pattern in globs for p in PKG_DIR.glob(pattern)}

    migrations = sorted((PKG_DIR / "db" / "migrations").glob("*.sql"))
    assert migrations
    missing = [p.name for p in migrations if p.resolve() not in shipped]
    assert not missing, f"not covered by package-data: {missing}"


def test_missing_migrations_dir_fails_loudly(tmp_path, monkeypatch) -> None:
    from agentmemory import migrate

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path / "absent")
    with pytest.raises(FileNotFoundError):
        migrate._get_migrations()


def test_untracked_database_with_memories_is_not_replayed(tmp_path) -> None:
    from agentmemory import migrate
    from agentmemory.brain import Brain

    db_path = tmp_path / "brain.db"
    brain = Brain(str(db_path), agent_id="default")
    brain.remember("a fact worth keeping", category="observation")
    brain.close()

    result = migrate.run(str(db_path), backup=False)

    assert result["ok"] is False
    assert result["applied"] == 0
    assert result["errors"][0]["name"] == "untracked database"
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0] == 0
    finally:
        conn.close()


def test_untracked_empty_database_still_migrates(tmp_path) -> None:
    from agentmemory import migrate
    from agentmemory.brain import Brain

    db_path = tmp_path / "brain.db"
    Brain(str(db_path), agent_id="default").close()

    result = migrate.run(str(db_path), backup=False)

    assert result["ok"] is True, result.get("errors")
    assert result["applied"] > 0
