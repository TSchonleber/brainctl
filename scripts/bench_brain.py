#!/usr/bin/env python3
"""Microbenchmark: Brain.remember() throughput.

Phase 1.1 connection-lifecycle refactor baseline.

Before Phase 1.1:  remember() re-opened sqlite3, ran 4 PRAGMAs + agent upsert
                   (~15-18 extra SQL stmts) on every call.
After  Phase 1.1:  lazy one-per-instance shared connection; zero per-call
                   setup overhead.

Indicative numbers on this worktree (Python 3.11, sandboxed fs, N=1000):
  before Phase 1.1: remember()  27.91s (  35.8 ops/s)
                    mixed       29.19s (  34.3 ops/s)
  after  Phase 1.1: remember()   9.65s ( 103.6 ops/s)
                    mixed        9.86s ( 101.4 ops/s)
  speedup: ~2.9x for remember-heavy, ~3.0x for mixed read+write workloads

Wall-clock savings scale roughly linearly with call count — the hot-path
win comes from eliminating the sqlite3.connect() + 4 PRAGMAs + agent
upsert + commit that every public method used to do.

Run by hand:

    python3 scripts/bench_brain.py [N]

This is NOT wired into CI — it's a one-shot sanity check.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain


def bench_remember(n: int = 1000) -> float:
    with tempfile.TemporaryDirectory() as td:
        db_file = str(Path(td) / "brain.db")
        brain = Brain(db_path=db_file, agent_id="bench-agent")
        # Prime the lazy connection once so the measurement excludes init.
        brain.remember("warmup")

        t0 = time.perf_counter()
        for i in range(n):
            brain.remember(f"benchmark memory {i}", category="lesson")
        elapsed = time.perf_counter() - t0
        if hasattr(brain, "close"):
            brain.close()
        return elapsed


def bench_mixed(n: int = 1000) -> float:
    with tempfile.TemporaryDirectory() as td:
        db_file = str(Path(td) / "brain.db")
        brain = Brain(db_path=db_file, agent_id="bench-mixed")
        brain.remember("warmup")

        t0 = time.perf_counter()
        for i in range(n):
            brain.remember(f"mixed memory {i}")
            if i % 5 == 0:
                brain.search("memory")
            if i % 10 == 0:
                brain.log("event")
        elapsed = time.perf_counter() - t0
        if hasattr(brain, "close"):
            brain.close()
        return elapsed


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

    print(f"Benchmark: brainctl Brain connection-lifecycle (N={n})")
    print("-" * 60)

    t = bench_remember(n)
    print(f"remember()   x{n}:  {t:7.3f}s  ({n/t:8.1f} ops/s)")

    t = bench_mixed(n)
    print(f"mixed       x{n}:  {t:7.3f}s  ({n/t:8.1f} ops/s)")

    print("-" * 60)
    print("Compare this against the numbers in the header docstring.")
