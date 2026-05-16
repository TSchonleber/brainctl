"""Tests for the BG → thalamus modulator cascade and the seed catalog."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class _NoCloseConn:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


def _setup_tempdb_with_both_subsystems() -> str:
    tmpf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpf.close()
    conn = sqlite3.connect(tmpf.name)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
    )
    for migration in (
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "050_thalamus.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "053_thalamus_shadow.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "054_basal_ganglia.sql"),
        str(Path(__file__).resolve().parent.parent / "db" / "migrations" / "055_basal_ganglia_shadow.sql"),
    ):
        with open(migration) as f:
            conn.executescript(f.read())
    conn.close()
    return tmpf.name


def _patch_both_modules(conn):
    import agentmemory.mcp_tools_basal_ganglia as bg
    import agentmemory.mcp_tools_thalamus as th
    wrapped = _NoCloseConn(conn)
    bg.open_db = lambda x: wrapped  # type: ignore[assignment]
    th.open_db = lambda x: wrapped  # type: ignore[assignment]


def test_high_tonic_da_cascades_to_wake_focused():
    db_path = _setup_tempdb_with_both_subsystems()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _patch_both_modules(conn)
        from agentmemory.mcp_tools_basal_ganglia import tool_bg_modulator_set

        r = tool_bg_modulator_set(tonic_da=0.85, lc_ne=0.6, set_by="t")
        assert r["ok"] is True
        cascade = r.get("thalamus_cascade")
        assert cascade is not None
        assert cascade["ok"] is True
        assert cascade["mode"]["mode"] == "wake_focused"
        # arousal = (0.85 + 0.6) / 2 = 0.725
        assert abs(cascade["mode"]["arousal"] - 0.725) < 1e-9
        assert cascade["mode"]["norepinephrine"] == 0.6
        conn.close()
    finally:
        os.unlink(db_path)


def test_low_tonic_da_cascades_to_wake_exploratory():
    db_path = _setup_tempdb_with_both_subsystems()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _patch_both_modules(conn)
        from agentmemory.mcp_tools_basal_ganglia import tool_bg_modulator_set

        r = tool_bg_modulator_set(tonic_da=0.2, set_by="t")
        cascade = r["thalamus_cascade"]
        assert cascade["mode"]["mode"] == "wake_exploratory"
        conn.close()
    finally:
        os.unlink(db_path)


def test_mid_tonic_da_does_not_flip_mode():
    db_path = _setup_tempdb_with_both_subsystems()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _patch_both_modules(conn)
        from agentmemory.mcp_tools_basal_ganglia import tool_bg_modulator_set

        # First set explore via low DA
        tool_bg_modulator_set(tonic_da=0.1, set_by="t")
        # Then mid range — should NOT change mode
        r = tool_bg_modulator_set(tonic_da=0.5, set_by="t")
        cascade = r["thalamus_cascade"]
        assert cascade["mode"]["mode"] == "wake_exploratory"  # unchanged
        conn.close()
    finally:
        os.unlink(db_path)


def test_cascade_disabled_returns_no_thalamus_block():
    db_path = _setup_tempdb_with_both_subsystems()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _patch_both_modules(conn)
        from agentmemory.mcp_tools_basal_ganglia import tool_bg_modulator_set

        r = tool_bg_modulator_set(tonic_da=0.9, cascade_to_thalamus=False, set_by="t")
        assert r["ok"] is True
        assert "thalamus_cascade" not in r
        conn.close()
    finally:
        os.unlink(db_path)


def test_seed_catalog_covers_all_five_loops():
    """The packaged seed catalog should cover every BG loop with ≥ 5 actions."""
    from scripts.seed_bg_catalog import CATALOG
    by_loop: dict[str, int] = {}
    for loop, _key, _desc in CATALOG:
        by_loop[loop] = by_loop.get(loop, 0) + 1
    assert set(by_loop.keys()) == {"motor", "oculomotor", "dlpfc", "lofc", "acc"}
    for loop, count in by_loop.items():
        assert count >= 5, f"loop {loop} has only {count} actions, need >= 5"
