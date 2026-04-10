"""Tests for brainctl obsidian export/import/status commands."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
import agentmemory.commands.obsidian as obs_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def brain_db(tmp_path):
    """Fresh brain.db with a few memories and an entity."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    brain.remember("Python type hints improve readability", category="convention")
    brain.remember("Always write tests before merging", category="workflow")
    brain.remember("Use atomic commits for clean history", category="workflow")

    # Add entity manually (must use agent_id already registered by brain)
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO entities (name, entity_type, properties, observations, "
            "agent_id, confidence, scope, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("Alice", "person", '{"role": "engineer"}', '["Joined team 2023"]',
             "test-agent", 1.0, "global", "2024-01-01T00:00:00", "2024-01-01T00:00:00"),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()

    return brain, db_file


@pytest.fixture
def vault(tmp_path):
    """Empty Obsidian vault directory."""
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def mock_db_path(monkeypatch, brain_db):
    """Patch _get_db_path to return our test db."""
    _, db_file = brain_db
    monkeypatch.setattr(obs_mod, "_get_db_path", lambda: db_file)
    return db_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    """Build a fake argparse namespace with sensible defaults."""
    import argparse
    defaults = dict(
        agent="test-agent",
        force=False,
        scope=None,
        category=None,
        dry_run=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _slug
# ---------------------------------------------------------------------------


class TestSlug:
    def test_basic(self):
        assert obs_mod._slug("Hello World") == "hello-world"

    def test_strips_special(self):
        s = obs_mod._slug("foo/bar:baz!")
        assert "/" not in s
        assert "!" not in s

    def test_max_len(self):
        long = "a" * 100
        assert len(obs_mod._slug(long)) <= 40

    def test_empty(self):
        assert obs_mod._slug("") == "memory"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestObsidianExport:
    def test_creates_vault_structure(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        assert (vault / "brainctl" / "memories").exists()
        assert (vault / "brainctl" / "entities").exists()
        assert (vault / "brainctl" / "events").exists()
        assert (vault / "brainctl" / "README.md").exists()

    def test_exports_memories_as_md(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        mem_dir = vault / "brainctl" / "memories"
        files = list(mem_dir.glob("*.md"))
        assert len(files) == 3

    def test_memory_frontmatter(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        mem_dir = vault / "brainctl" / "memories"
        for f in mem_dir.glob("*.md"):
            text = f.read_text()
            assert "brainctl_id:" in text
            assert "brainctl_type: memory" in text
            assert "category:" in text
            assert "confidence:" in text

    def test_memory_content_in_body(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        mem_dir = vault / "brainctl" / "memories"
        all_text = " ".join(f.read_text() for f in mem_dir.glob("*.md"))
        assert "Python type hints" in all_text
        assert "Always write tests" in all_text

    def test_entity_exported(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        ent_dir = vault / "brainctl" / "entities"
        files = list(ent_dir.glob("*.md"))
        assert len(files) >= 1
        all_text = " ".join(f.read_text() for f in files)
        assert "Alice" in all_text
        assert "brainctl_type: entity" in all_text

    def test_force_flag_overwrites(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        # First export: files exist; second export without force: 0 new
        # Second export with force: files are overwritten
        args_force = _make_args(vault_path=str(vault), force=True)
        obs_mod.cmd_obsidian_export(args_force)  # should not crash

        mem_dir = vault / "brainctl" / "memories"
        files = list(mem_dir.glob("*.md"))
        assert len(files) == 3  # same count

    def test_category_filter(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault), category="convention", force=True)
        obs_mod.cmd_obsidian_export(args)

        mem_dir = vault / "brainctl" / "memories"
        files = list(mem_dir.glob("*.md"))
        assert len(files) == 1  # only convention memory

    def test_readme_contains_last_exported(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_export(args)

        readme = (vault / "brainctl" / "README.md").read_text()
        assert "Last exported" in readme

    def test_nonexistent_db_exits(self, tmp_path, vault, monkeypatch):
        monkeypatch.setattr(obs_mod, "_get_db_path", lambda: tmp_path / "missing.db")
        args = _make_args(vault_path=str(vault))
        with pytest.raises(SystemExit):
            obs_mod.cmd_obsidian_export(args)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class TestObsidianImport:
    def test_no_brainctl_dir_exits(self, brain_db, tmp_path, monkeypatch):
        _, db_file = brain_db
        monkeypatch.setattr(obs_mod, "_get_db_path", lambda: db_file)
        empty_vault = tmp_path / "empty_vault"
        empty_vault.mkdir()
        args = _make_args(vault_path=str(empty_vault))
        with pytest.raises(SystemExit):
            obs_mod.cmd_obsidian_import(args)

    def test_import_new_note(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        # Export first to create brainctl/ dir
        obs_mod.cmd_obsidian_export(_make_args(vault_path=str(vault)))

        # Create a new note without brainctl_id
        new_note = vault / "brainctl" / "memories" / "my-new-idea.md"
        new_note.write_text("This is a brand new insight I wrote in Obsidian.")

        before = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]

        obs_mod.cmd_obsidian_import(_make_args(vault_path=str(vault)))

        after = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]
        assert after > before

    def test_dry_run_does_not_write(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        obs_mod.cmd_obsidian_export(_make_args(vault_path=str(vault)))

        new_note = vault / "brainctl" / "memories" / "dry-run-note.md"
        new_note.write_text("This should not be imported in dry-run mode.")

        before = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]

        obs_mod.cmd_obsidian_import(_make_args(vault_path=str(vault), dry_run=True))

        after = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]
        assert after == before  # nothing written

    def test_skips_exported_files(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        obs_mod.cmd_obsidian_export(_make_args(vault_path=str(vault)))

        before = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]

        # Import with no new files — exported files have brainctl_id and are skipped
        obs_mod.cmd_obsidian_import(_make_args(vault_path=str(vault)))

        after = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]
        assert after == before

    def test_skips_short_content(self, brain_db, vault, mock_db_path):
        _, db_file = brain_db
        obs_mod.cmd_obsidian_export(_make_args(vault_path=str(vault)))

        short_note = vault / "brainctl" / "memories" / "short.md"
        short_note.write_text("hi")  # too short

        before = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]

        obs_mod.cmd_obsidian_import(_make_args(vault_path=str(vault)))

        after = sqlite3.connect(str(db_file)).execute(
            "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
        ).fetchone()[0]
        assert after == before


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestObsidianStatus:
    def test_status_no_vault(self, brain_db, tmp_path, mock_db_path, capsys):
        _, db_file = brain_db
        empty_vault = tmp_path / "empty_vault"
        empty_vault.mkdir()
        args = _make_args(vault_path=str(empty_vault))
        obs_mod.cmd_obsidian_status(args)
        out = capsys.readouterr().out
        assert "not yet exported" in out

    def test_status_after_export(self, brain_db, vault, mock_db_path, capsys):
        _, db_file = brain_db
        obs_mod.cmd_obsidian_export(_make_args(vault_path=str(vault)))
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_status(args)
        out = capsys.readouterr().out
        assert "Memories" in out
        assert "Entities" in out

    def test_status_shows_drift(self, brain_db, vault, mock_db_path, capsys):
        _, db_file = brain_db
        # Export first
        obs_mod.cmd_obsidian_export(_make_args(vault_path=str(vault)))
        # Add a new memory (not yet exported)
        brain, _ = brain_db
        brain.remember("New memory not in vault", category="general")

        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_status(args)
        out = capsys.readouterr().out
        # Should show positive drift
        assert "un-exported" in out or "+" in out

    def test_status_missing_db(self, tmp_path, vault, monkeypatch, capsys):
        monkeypatch.setattr(obs_mod, "_get_db_path", lambda: tmp_path / "missing.db")
        args = _make_args(vault_path=str(vault))
        obs_mod.cmd_obsidian_status(args)
        out = capsys.readouterr().out
        assert "NOT FOUND" in out


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


class TestRenderMemoryMd:
    def _make_row(self, **kwargs):
        defaults = {
            "id": 1, "content": "Test content", "category": "general",
            "confidence": 0.9, "tags": "a, b", "scope": "global",
            "created_at": "2024-01-01T00:00:00", "replay_priority": 0.0,
            "file_path": None, "file_line": None,
        }
        defaults.update(kwargs)
        # sqlite3.Row-like: use a simple dict-access object
        return type("Row", (), {"__getitem__": lambda s, k: defaults[k],
                                "get": lambda s, k, d=None: defaults.get(k, d)})()

    def test_frontmatter_present(self):
        row = self._make_row()
        md = obs_mod._render_memory_md(row)
        assert md.startswith("---")
        assert "brainctl_id: 1" in md
        assert "category: general" in md

    def test_content_in_body(self):
        row = self._make_row(content="My special content")
        md = obs_mod._render_memory_md(row)
        assert "My special content" in md

    def test_tags_in_frontmatter(self):
        row = self._make_row(tags="alpha, beta")
        md = obs_mod._render_memory_md(row)
        assert "alpha" in md
        assert "beta" in md

    def test_file_anchor_shown(self):
        row = self._make_row(file_path="/src/main.py", file_line=42)
        md = obs_mod._render_memory_md(row)
        assert "main.py" in md
        assert "42" in md

    def test_no_replay_priority_when_zero(self):
        row = self._make_row(replay_priority=0.0)
        md = obs_mod._render_memory_md(row)
        assert "replay_priority" not in md
