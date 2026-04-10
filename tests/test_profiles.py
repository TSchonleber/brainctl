"""Tests for brainctl context profiles."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory import profiles as prof_mod
from agentmemory.profiles import (
    BUILTIN_PROFILES,
    apply_profile,
    create_profile,
    delete_profile,
    list_profiles,
    resolve_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "brain.db"
    conn = sqlite3.connect(str(p))
    conn.execute(prof_mod._CREATE_TABLE)
    conn.commit()
    conn.close()
    return p


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# resolve_profile
# ---------------------------------------------------------------------------


class TestResolveProfile:
    def test_builtin_found(self):
        p = resolve_profile("writing")
        assert p is not None
        assert p["name"] == "writing"
        assert p["builtin"] is True
        assert "categories" in p
        assert "tables" in p

    def test_unknown_no_db(self):
        assert resolve_profile("nonexistent") is None

    def test_unknown_with_db_returns_none(self, db_path):
        assert resolve_profile("nonexistent", db_path) is None

    def test_all_builtins_resolvable(self):
        for name in BUILTIN_PROFILES:
            p = resolve_profile(name)
            assert p is not None, f"builtin '{name}' did not resolve"

    def test_user_defined_roundtrip(self, db_path):
        create_profile("custom", ["lesson"], ["memories"], [], "test", db_path)
        p = resolve_profile("custom", db_path)
        assert p is not None
        assert p["name"] == "custom"
        assert p["categories"] == ["lesson"]
        assert p["builtin"] is False


# ---------------------------------------------------------------------------
# list_profiles
# ---------------------------------------------------------------------------


class TestListProfiles:
    def test_no_db_returns_builtins(self):
        profiles = list_profiles()
        names = {p["name"] for p in profiles}
        assert set(BUILTIN_PROFILES.keys()) <= names

    def test_with_db_includes_user_defined(self, db_path):
        create_profile("myprofile", ["preference"], ["memories"], [], "Mine", db_path)
        profiles = list_profiles(db_path)
        names = {p["name"] for p in profiles}
        assert "myprofile" in names
        assert "writing" in names  # builtins still present

    def test_builtins_come_first(self, db_path):
        create_profile("aaaa", ["lesson"], ["memories"], [], "", db_path)
        profiles = list_profiles(db_path)
        builtin_indices = [i for i, p in enumerate(profiles) if p.get("builtin")]
        custom_indices = [i for i, p in enumerate(profiles) if not p.get("builtin")]
        # All builtins appear before all custom profiles
        assert max(builtin_indices) < min(custom_indices)


# ---------------------------------------------------------------------------
# create_profile / delete_profile
# ---------------------------------------------------------------------------


class TestCreateDeleteProfile:
    def test_create_basic(self, db_path):
        ok = create_profile("testprof", ["lesson", "decision"], ["memories"], [], "desc", db_path)
        assert ok is True
        p = resolve_profile("testprof", db_path)
        assert p["categories"] == ["lesson", "decision"]

    def test_create_cannot_shadow_builtin(self, db_path, capsys):
        ok = create_profile("writing", ["lesson"], ["memories"], [], "", db_path)
        assert ok is False
        out = capsys.readouterr().err
        assert "writing" in out

    def test_delete_user_defined(self, db_path):
        create_profile("todel", ["lesson"], ["memories"], [], "", db_path)
        ok = delete_profile("todel", db_path)
        assert ok is True
        assert resolve_profile("todel", db_path) is None

    def test_delete_builtin_rejected(self, db_path, capsys):
        ok = delete_profile("writing", db_path)
        assert ok is False

    def test_delete_nonexistent_returns_false(self, db_path):
        ok = delete_profile("does_not_exist", db_path)
        assert ok is False


# ---------------------------------------------------------------------------
# apply_profile
# ---------------------------------------------------------------------------


class TestApplyProfile:
    def test_sets_tables_on_args(self):
        args = _args(profile="writing", tables=None, category=None)
        apply_profile(args, None)
        assert args.tables == "memories,entities"

    def test_sets_profile_categories(self):
        args = _args(profile="writing", tables=None, category=None)
        apply_profile(args, None)
        assert hasattr(args, "_profile_categories")
        assert "preference" in args._profile_categories

    def test_explicit_tables_not_overridden(self):
        args = _args(profile="writing", tables="events", category=None)
        apply_profile(args, None)
        assert args.tables == "events"  # unchanged

    def test_explicit_category_prevents_profile_categories(self):
        args = _args(profile="writing", tables=None, category="project")
        apply_profile(args, None)
        assert not hasattr(args, "_profile_categories")

    def test_no_profile_returns_none(self):
        args = _args(profile=None, tables=None, category=None)
        result = apply_profile(args, None)
        assert result is None

    def test_unknown_profile_exits(self):
        args = _args(profile="unknown_xyz", tables=None, category=None)
        with pytest.raises(SystemExit):
            apply_profile(args, None)

    def test_user_defined_profile_applied(self, db_path):
        create_profile("myprof", ["decision", "lesson"], ["memories", "events"], [], "", db_path)
        args = _args(profile="myprof", tables=None, category=None)
        p = apply_profile(args, db_path)
        assert p["name"] == "myprof"
        assert "memories" in args.tables
        assert "events" in args.tables
        assert args._profile_categories == ["decision", "lesson"]


# ---------------------------------------------------------------------------
# Profile content correctness
# ---------------------------------------------------------------------------


class TestBuiltinProfileContent:
    @pytest.mark.parametrize("name,expected_tables", [
        ("writing", ["memories", "entities"]),
        ("ops", ["memories", "events", "decisions"]),
        ("networking", ["entities", "memories"]),
    ])
    def test_tables(self, name, expected_tables):
        p = resolve_profile(name)
        assert sorted(p["tables"]) == sorted(expected_tables)

    @pytest.mark.parametrize("name", list(BUILTIN_PROFILES.keys()))
    def test_has_description(self, name):
        p = resolve_profile(name)
        assert p.get("description"), f"Profile '{name}' missing description"

    @pytest.mark.parametrize("name", list(BUILTIN_PROFILES.keys()))
    def test_has_categories(self, name):
        p = resolve_profile(name)
        assert p.get("categories"), f"Profile '{name}' has no categories"
