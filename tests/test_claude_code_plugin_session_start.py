from __future__ import annotations

import importlib.util
from pathlib import Path


SESSION_START = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "claude-code"
    / "brainctl"
    / "hooks"
    / "session_start.py"
)


def load_session_start_module():
    spec = importlib.util.spec_from_file_location("brainctl_claude_session_start", SESSION_START)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_format_context_always_injects_wrap_up_discipline():
    module = load_session_start_module()

    text = module.format_context({})

    assert "Before ending this Claude Code session" in text
    assert "mcp__brainctl__agent_wrap_up" in text
    assert "Do not rely on the automatic SessionEnd hook" in text


def test_format_context_preserves_orient_snapshot_sections():
    module = load_session_start_module()

    text = module.format_context(
        {
            "handoff": {"goal": "ship continuity", "next_step": "patch hook"},
            "recent_events": [{"event_type": "warning", "summary": "auto wrap-up stale"}],
            "memories": [{"category": "lesson", "content": "Manual wrap_up beats hook summaries."}],
            "stats": {"active_memories": 3, "total_events": 4, "total_entities": 5},
        }
    )

    assert "Goal: ship continuity" in text
    assert "Next step: patch hook" in text
    assert "[warning] auto wrap-up stale" in text
    assert "[lesson] Manual wrap_up beats hook summaries." in text
    assert "3 memories, 4 events, 5 entities" in text
    assert "mcp__brainctl__agent_wrap_up" in text
