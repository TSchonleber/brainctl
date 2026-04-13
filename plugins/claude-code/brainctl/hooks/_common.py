"""Shared helpers for brainctl Claude Code hook scripts.

Claude Code invokes hooks with a JSON payload on stdin. Hooks communicate
back by printing JSON to stdout (for `additionalContext` injection, etc.)
or plain text. Any hook failure must be non-fatal: a broken memory system
should never block a coding session, so every helper swallows exceptions
and logs to stderr instead.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def read_hook_input() -> dict[str, Any]:
    """Parse Claude Code's hook payload from stdin. Returns `{}` on any error."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception as exc:
        print(f"[brainctl-hook] stdin parse error: {exc}", file=sys.stderr)
        return {}


def agent_id(payload: dict[str, Any]) -> str:
    """Derive a stable agent_id for the current Claude Code session.

    Preference order:
      1. `BRAINCTL_AGENT_ID` env var (explicit override)
      2. `cwd`-derived name  (e.g. `cc:brainctl`)
      3. literal `cc:default`
    """
    override = os.environ.get("BRAINCTL_AGENT_ID")
    if override:
        return override
    cwd = payload.get("cwd") or os.getcwd()
    try:
        name = Path(cwd).name or "default"
    except Exception:
        name = "default"
    return f"cc:{name}"


def project_name(payload: dict[str, Any]) -> str | None:
    """Derive project scope — defaults to the cwd basename, or env override."""
    override = os.environ.get("BRAINCTL_PROJECT")
    if override:
        return override
    cwd = payload.get("cwd") or os.getcwd()
    try:
        return Path(cwd).name or None
    except Exception:
        return None


def get_brain(payload: dict[str, Any]):
    """Construct a `Brain` scoped to the current Claude Code session, or
    return `None` on any failure. Never raises."""
    try:
        from agentmemory import Brain  # type: ignore
    except Exception as exc:
        print(f"[brainctl-hook] brainctl not installed: {exc}", file=sys.stderr)
        return None
    try:
        kwargs: dict[str, Any] = {"agent_id": agent_id(payload)}
        db_path = os.environ.get("BRAIN_DB")
        if db_path:
            kwargs["db_path"] = db_path
        return Brain(**kwargs)
    except Exception as exc:
        print(f"[brainctl-hook] Brain init failed: {exc}", file=sys.stderr)
        return None


def safe_exit(output: dict[str, Any] | None = None, code: int = 0) -> None:
    """Emit a JSON object to stdout (if provided) and exit."""
    if output is not None:
        try:
            sys.stdout.write(json.dumps(output))
            sys.stdout.flush()
        except Exception:
            pass
    sys.exit(code)
