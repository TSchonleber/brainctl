"""Tests for the MCP server dispatch signature adapter.

Regression coverage for issue #97 — the MCP wrapper used to inject
``agent_id`` into every tool call regardless of whether the dispatcher
function accepted it. Extension modules (`mcp_tools_health`,
`mcp_tools_lifecycle`) define their dispatchers as ``_call_X(args: dict)``
and rejected the kwarg with::

    _call_health() got an unexpected keyword argument 'agent_id'

`_invoke_dispatch_fn` introspects the callable and adapts the call shape
so both conventions work.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest

mcp_server = pytest.importorskip("agentmemory.mcp_server")
_invoke = mcp_server._invoke_dispatch_fn


def test_native_kwarg_style_receives_agent_id():
    seen = {}

    def fn(agent_id: str, foo: int = 0, **kw):
        seen["agent_id"] = agent_id
        seen["foo"] = foo
        seen["kw"] = kw
        return {"ok": True}

    out = _invoke(fn, "claude", {"foo": 7, "bar": "x"})
    assert out == {"ok": True}
    assert seen["agent_id"] == "claude"
    assert seen["foo"] == 7
    assert seen["kw"] == {"bar": "x"}


def test_extension_args_dict_style_does_not_receive_agent_id_kwarg():
    """Regression: ``_call_health(args: dict)`` etc. must not crash."""
    seen = {}

    def _call_health(args: dict) -> dict:
        seen["args"] = args
        return {"ok": True, "args": args}

    out = _invoke(_call_health, "claude", {"window_days": 14})
    assert out["ok"] is True
    # agent_id is folded into the args dict so extensions can opt-in to it
    assert seen["args"]["window_days"] == 14
    assert seen["args"]["agent_id"] == "claude"


def test_extension_args_dict_does_not_overwrite_explicit_agent_id():
    seen = {}

    def _call_x(args: dict) -> dict:
        seen.update(args)
        return {}

    _invoke(_call_x, "fallback-agent", {"agent_id": "explicit", "k": 1})
    assert seen["agent_id"] == "explicit"
    assert seen["k"] == 1


def test_var_kwargs_lambda_routes_to_kwarg_style():
    """``lambda **kw: ...`` is a single-param signature but VAR_KEYWORD,
    so it must take the kwarg path, not the args-dict path.
    """
    seen = {}

    def fn(**kw):
        seen.update(kw)
        return {}

    _invoke(fn, "ag", {"a": 1, "b": 2})
    assert seen == {"agent_id": "ag", "a": 1, "b": 2}


def test_real_health_dispatcher_does_not_crash():
    """Smoke test against the actual ``_call_health`` from
    ``mcp_tools_health`` — the original failure site reported in #97.

    We don't assert on payload (it depends on a brain.db) — only that the
    call completes without raising the historical TypeError.
    """
    health_mod = pytest.importorskip("agentmemory.mcp_tools_health")
    fn = health_mod.DISPATCH["health"]
    try:
        _invoke(fn, "test-agent", {"window_days": 1})
    except TypeError as e:
        if "agent_id" in str(e):
            pytest.fail(f"agent_id wrapper bug regressed: {e}")
        raise
    except Exception:
        # other exceptions (db missing, etc.) are out of scope here
        pass
