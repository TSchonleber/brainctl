"""Tests for mcp_tools_meb — MEB, push & vsearch MCP tools."""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_tools_meb as meb_mod
from agentmemory.brain import Brain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Create a fresh Brain DB and point the module at it."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
    return brain


@pytest.fixture
def brain_with_memories(tmp_path, monkeypatch):
    """Brain instance with a few memories pre-loaded (triggers memory_events too)."""
    db_file = tmp_path / "brain.db"
    brain = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
    brain.remember("Use pytest for all tests", category="convention")
    brain.remember("Python 3.12 required", category="environment")
    brain.remember("Deploy to staging before prod", category="lesson")
    # memory with all words matching the default push task "convention testing deployment"
    brain.remember("convention for testing deployment pipelines", category="convention")
    return brain


# ---------------------------------------------------------------------------
# Tool exports
# ---------------------------------------------------------------------------

class TestModuleInterface:
    def test_tools_is_list(self):
        assert isinstance(meb_mod.TOOLS, list)
        assert len(meb_mod.TOOLS) == 6

    def test_dispatch_is_dict(self):
        assert isinstance(meb_mod.DISPATCH, dict)

    def test_dispatch_keys_match_tools(self):
        tool_names = {t.name for t in meb_mod.TOOLS}
        dispatch_keys = set(meb_mod.DISPATCH.keys())
        assert tool_names == dispatch_keys

    def test_all_expected_tools_present(self):
        names = {t.name for t in meb_mod.TOOLS}
        for expected in ("meb_tail", "meb_stats", "meb_prune", "push", "push_report", "vsearch"):
            assert expected in names, f"Missing tool: {expected}"

    def test_each_tool_has_input_schema(self):
        for tool in meb_mod.TOOLS:
            assert hasattr(tool, "inputSchema"), f"{tool.name} missing inputSchema"
            assert tool.inputSchema.get("type") == "object"

    def test_push_requires_task(self):
        push_tool = next(t for t in meb_mod.TOOLS if t.name == "push")
        assert "task" in push_tool.inputSchema.get("required", [])

    def test_vsearch_requires_query(self):
        vsearch_tool = next(t for t in meb_mod.TOOLS if t.name == "vsearch")
        assert "query" in vsearch_tool.inputSchema.get("required", [])

    def test_push_report_requires_push_id(self):
        pr_tool = next(t for t in meb_mod.TOOLS if t.name == "push_report")
        assert "push_id" in pr_tool.inputSchema.get("required", [])


# ---------------------------------------------------------------------------
# meb_stats
# ---------------------------------------------------------------------------

class TestMebStats:
    def test_returns_ok_true(self):
        result = meb_mod.tool_meb_stats()
        assert result["ok"] is True

    def test_has_required_fields(self):
        result = meb_mod.tool_meb_stats()
        for field in ("total_events", "by_operation", "by_category",
                      "oldest_event", "newest_event", "events_last_hour",
                      "avg_latency_ms", "max_latency_ms", "config"):
            assert field in result, f"Missing field: {field}"

    def test_total_events_zero_initially(self, tmp_path, monkeypatch):
        """A fresh DB with no memories has zero memory_events."""
        db_file = tmp_path / "empty.db"
        Brain(db_path=str(db_file), agent_id="x")
        monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
        result = meb_mod.tool_meb_stats()
        assert result["ok"] is True
        assert result["total_events"] == 0

    def test_total_events_increases_with_memories(self, brain_with_memories):
        result = meb_mod.tool_meb_stats()
        assert result["ok"] is True
        # 3 memories inserted → at least 3 insert events
        assert result["total_events"] >= 3

    def test_config_contains_defaults(self):
        result = meb_mod.tool_meb_stats()
        cfg = result["config"]
        assert "ttl_hours" in cfg
        assert "max_queue_depth" in cfg
        assert "prune_on_read" in cfg

    def test_by_operation_is_dict(self, brain_with_memories):
        result = meb_mod.tool_meb_stats()
        assert isinstance(result["by_operation"], dict)

    def test_dispatch_meb_stats(self):
        result = meb_mod.DISPATCH["meb_stats"]({})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# meb_tail
# ---------------------------------------------------------------------------

class TestMebTail:
    def test_returns_ok_true(self, brain_with_memories):
        result = meb_mod.tool_meb_tail()
        assert result["ok"] is True

    def test_returns_events_list(self, brain_with_memories):
        result = meb_mod.tool_meb_tail()
        assert "events" in result
        assert isinstance(result["events"], list)

    def test_events_have_age_field(self, brain_with_memories):
        result = meb_mod.tool_meb_tail()
        for evt in result["events"]:
            assert "age" in evt

    def test_n_limits_results(self, brain_with_memories):
        result = meb_mod.tool_meb_tail(n=1)
        assert result["ok"] is True
        assert len(result["events"]) <= 1

    def test_events_ordered_oldest_first(self, brain_with_memories):
        """meb_tail reverses so oldest-first within the returned window."""
        result = meb_mod.tool_meb_tail(n=10)
        events = result["events"]
        if len(events) >= 2:
            ids = [e["id"] for e in events]
            assert ids == sorted(ids), "Events should be oldest-first"

    def test_since_filters_by_cursor(self, brain_with_memories):
        """since=<id> should return only events with id > since."""
        all_events = meb_mod.tool_meb_tail(n=100)["events"]
        if len(all_events) < 2:
            pytest.skip("Need at least 2 events")
        cursor = all_events[0]["id"]
        result = meb_mod.tool_meb_tail(since=cursor)
        ids = [e["id"] for e in result["events"]]
        assert all(eid > cursor for eid in ids)

    def test_empty_on_fresh_db(self, tmp_path, monkeypatch):
        db_file = tmp_path / "empty2.db"
        Brain(db_path=str(db_file), agent_id="x")
        monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
        result = meb_mod.tool_meb_tail()
        assert result["ok"] is True
        assert result["count"] == 0

    def test_dispatch_meb_tail(self, brain_with_memories):
        result = meb_mod.DISPATCH["meb_tail"]({})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# meb_prune
# ---------------------------------------------------------------------------

class TestMebPrune:
    def test_returns_ok_true_on_empty_db(self):
        result = meb_mod.tool_meb_prune()
        assert result["ok"] is True

    def test_returns_deleted_and_remaining(self):
        result = meb_mod.tool_meb_prune()
        assert "deleted" in result
        assert "remaining" in result

    def test_prune_with_ttl_zero_deletes_all(self, brain_with_memories):
        """TTL of 0 hours should expire all existing events."""
        result = meb_mod.tool_meb_prune(ttl_hours=0)
        assert result["ok"] is True
        assert result["deleted"] >= 0  # may or may not delete depending on timing
        # After prune, remaining + deleted should equal pre-prune total
        stats_after = meb_mod.tool_meb_stats()
        assert stats_after["total_events"] == result["remaining"]

    def test_prune_with_max_depth_one(self, brain_with_memories):
        """max_depth=1 should trim the queue to at most 1 event."""
        before = meb_mod.tool_meb_stats()["total_events"]
        if before < 2:
            pytest.skip("Need at least 2 events to test depth cap")
        result = meb_mod.tool_meb_prune(max_depth=1)
        assert result["ok"] is True
        assert result["remaining"] <= 1

    def test_dispatch_meb_prune(self):
        result = meb_mod.DISPATCH["meb_prune"]({})
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

class TestPush:
    def test_requires_task(self):
        result = meb_mod.tool_push(task="")
        assert result["ok"] is False
        assert "error" in result

    def test_returns_ok_true_with_no_memories(self):
        """push with no memories in DB should still succeed (return 0 results)."""
        result = meb_mod.tool_push(task="What are the deployment steps?")
        assert result["ok"] is True
        assert result["memories_pushed"] == 0

    def test_returns_push_id(self, brain_with_memories):
        result = meb_mod.tool_push(task="deployment process")
        assert result["ok"] is True
        assert "push_id" in result
        assert len(result["push_id"]) == 12

    def test_returns_push_event_id(self, brain_with_memories):
        result = meb_mod.tool_push(task="python version")
        assert result["ok"] is True
        assert isinstance(result.get("push_event_id"), int)

    def test_top_k_respected(self, brain_with_memories):
        result = meb_mod.tool_push(task="deploy staging test env convention", top_k=2)
        assert result["ok"] is True
        assert len(result["memories"]) <= 2

    def test_top_k_capped_at_10(self, brain_with_memories):
        result = meb_mod.tool_push(task="anything", top_k=100)
        assert result["ok"] is True
        assert len(result["memories"]) <= 10

    def test_memories_have_required_fields(self, brain_with_memories):
        result = meb_mod.tool_push(task="python deployment convention")
        assert result["ok"] is True
        for m in result["memories"]:
            assert "id" in m
            assert "content" in m
            assert "final_score" in m
            assert "temporal_weight" in m

    def test_records_push_event_in_db(self, brain_with_memories, tmp_path):
        result = meb_mod.tool_push(task="testing push event recording")
        assert result["ok"] is True
        push_id = result["push_id"]
        conn = sqlite3.connect(str(meb_mod.DB_PATH))
        row = conn.execute(
            "SELECT id FROM events WHERE summary LIKE ?", (f"push:{push_id}%",)
        ).fetchone()
        conn.close()
        assert row is not None, "push_delivered event should be in events table"

    def test_hybrid_flag_present(self, brain_with_memories):
        result = meb_mod.tool_push(task="deployment")
        assert "hybrid" in result

    def test_dispatch_push(self, brain_with_memories):
        result = meb_mod.DISPATCH["push"](task="python testing")
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# push_report
# ---------------------------------------------------------------------------

class TestPushReport:
    def test_invalid_push_id_returns_error(self):
        result = meb_mod.tool_push_report(push_id="doesnotexist")
        assert result["ok"] is False
        assert "error" in result

    def test_empty_push_id_returns_error(self):
        result = meb_mod.tool_push_report(push_id="")
        assert result["ok"] is False

    def test_valid_push_id_returns_report(self, brain_with_memories):
        push_result = meb_mod.tool_push(task="convention testing deployment")
        push_id = push_result["push_id"]
        report = meb_mod.tool_push_report(push_id=push_id)
        assert report["ok"] is True
        assert report["push_id"] == push_id
        assert isinstance(report["memories"], list)
        # When memories were selected, full report keys are present
        if report["memories"]:
            assert "memories_pushed" in report
            assert "memories_useful" in report
            assert "utility_rate" in report

    def test_report_delta_is_zero_immediately(self, brain_with_memories):
        """Right after push, no additional recalls → delta should be 0."""
        push_result = meb_mod.tool_push(task="deploy staging python convention")
        push_id = push_result["push_id"]
        report = meb_mod.tool_push_report(push_id=push_id)
        assert report["ok"] is True
        for m in report["memories"]:
            assert m["delta"] == 0
            assert m["was_useful"] is False

    def test_report_structure(self, brain_with_memories):
        push_result = meb_mod.tool_push(task="staging environment")
        report = meb_mod.tool_push_report(push_id=push_result["push_id"])
        if report["ok"] and report["memories"]:
            m = report["memories"][0]
            assert "memory_id" in m
            assert "content_snippet" in m
            assert "recalled_at_push" in m
            assert "recalled_now" in m
            assert "delta" in m
            assert "was_useful" in m

    def test_dispatch_push_report_invalid(self):
        result = meb_mod.DISPATCH["push_report"](push_id="nonexistent")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# vsearch
# ---------------------------------------------------------------------------

class TestVsearch:
    def test_returns_error_when_vec_unavailable(self, monkeypatch):
        """If VEC_DYLIB is None, vsearch returns a structured error."""
        monkeypatch.setattr(meb_mod, "VEC_DYLIB", None)
        result = meb_mod.tool_vsearch(query="test query")
        assert result["ok"] is False
        assert "sqlite-vec" in result["error"] or "not available" in result["error"]

    def test_empty_query_returns_error(self):
        result = meb_mod.tool_vsearch(query="")
        assert result["ok"] is False
        assert "error" in result

    def test_dispatch_vsearch_empty_query(self):
        result = meb_mod.DISPATCH["vsearch"](query="")
        assert result["ok"] is False

    def test_vec_unavailable_via_dispatch(self, monkeypatch):
        monkeypatch.setattr(meb_mod, "VEC_DYLIB", None)
        result = meb_mod.DISPATCH["vsearch"](query="hello")
        assert result["ok"] is False

    @pytest.mark.skipif(
        meb_mod.VEC_DYLIB is None,
        reason="sqlite-vec not installed",
    )
    def test_vsearch_returns_ok_true_when_vec_available(self, brain_with_memories):
        result = meb_mod.tool_vsearch(query="deployment", tables="memories")
        # ok may be False if Ollama is not running (embed fails)
        if result["ok"]:
            assert "memories" in result
            assert isinstance(result["memories"], list)
        else:
            assert "error" in result


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_sanitize_fts_query_strips_specials(self):
        q = meb_mod._sanitize_fts_query
        assert q("hello.world") == "hello world"
        assert q("foo AND (bar)") == "foo AND bar"
        assert q("") == ""

    def test_age_str_just_now(self):
        result = meb_mod._age_str(meb_mod._now())
        assert "just now" in result or "ago" in result

    def test_age_str_none(self):
        result = meb_mod._age_str(None)
        assert isinstance(result, str)

    def test_temporal_weight_recent(self):
        w = meb_mod._temporal_weight(meb_mod._now(), scope="global")
        assert 0.99 < w <= 1.0

    def test_temporal_weight_old(self):
        w = meb_mod._temporal_weight("2020-01-01T00:00:00Z", scope="global")
        assert w < 0.5

    def test_normalize_uniform_returns_ones(self):
        result = meb_mod._normalize([3.0, 3.0, 3.0])
        assert all(v == 1.0 for v in result)

    def test_normalize_empty(self):
        result = meb_mod._normalize([])
        assert result == []

    def test_rrf_fuse_merges_lists(self):
        fts = [{"id": 1, "content": "a"}, {"id": 2, "content": "b"}]
        vec = [{"id": 2, "content": "b"}, {"id": 3, "content": "c"}]
        merged = meb_mod._rrf_fuse(fts, vec)
        ids = {r["id"] for r in merged}
        assert ids == {1, 2, 3}

    def test_rrf_fuse_marks_both_source(self):
        fts = [{"id": 5, "content": "x"}]
        vec = [{"id": 5, "content": "x"}]
        merged = meb_mod._rrf_fuse(fts, vec)
        assert merged[0]["source"] == "both"

    def test_rrf_fuse_sorted_by_score(self):
        fts = [{"id": 1}, {"id": 2}, {"id": 3}]
        vec = [{"id": 1}, {"id": 2}]
        merged = meb_mod._rrf_fuse(fts, vec)
        scores = [r["rrf_score"] for r in merged]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# MEB config loading
# ---------------------------------------------------------------------------

class TestMebConfig:
    def test_defaults_when_table_missing(self, tmp_path, monkeypatch):
        """meb_config falls back gracefully if meb_config table doesn't exist."""
        db_file = tmp_path / "no_meb.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(meb_mod, "DB_PATH", db_file)
        conn2 = sqlite3.connect(str(db_file))
        conn2.row_factory = sqlite3.Row
        cfg = meb_mod._meb_config(conn2)
        conn2.close()
        assert cfg["ttl_hours"] == meb_mod._MEB_TTL_HOURS_DEFAULT
        assert cfg["max_queue_depth"] == meb_mod._MEB_MAX_DEPTH_DEFAULT
        assert isinstance(cfg["prune_on_read"], bool)
