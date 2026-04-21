"""Datetime hygiene gate — audit I21.

Blocks new uses of ``datetime.utcnow()`` (deprecated in Py3.12+, always
naive, silently breaks ordering against Z-suffixed DB timestamps).

Deliberately does NOT track bare ``datetime.now()`` right now. An earlier
draft used a line-numbered allowlist, which produced noise on every PR
that shuffled lines in the allowlisted files. The naked ``.now()``
cleanup will come as part of the coordinated local→UTC timestamp
migration (the one blocked on deciding what to do with hippocampus's
internal-consistency writers) — gating it before that's done only
creates churn.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parent.parent / "src" / "agentmemory"

_UTCNOW_PATTERN = re.compile(r"\bdatetime\.utcnow\(\s*\)")


def _walk_py_files():
    for p in SRC.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_no_datetime_utcnow_calls():
    """datetime.utcnow() is deprecated in Py3.12+ — use datetime.now(timezone.utc)."""
    offenders: list[str] = []
    for path in _walk_py_files():
        rel = path.relative_to(SRC).as_posix()
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # allow commentary referencing the deprecated name
            if _UTCNOW_PATTERN.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "datetime.utcnow() is deprecated in Py3.12+ and produces a naive "
        "datetime that silently breaks ordering against Z-suffixed DB "
        "timestamps. Replace with datetime.now(timezone.utc).\n\n"
        + "\n".join(offenders)
    )
