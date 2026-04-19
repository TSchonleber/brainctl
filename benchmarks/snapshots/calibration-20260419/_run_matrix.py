#!/usr/bin/env python3
"""I5 calibration matrix driver.

Runs each cell of the top-heavy retrieval ablation against:
  * LongMemEval(289) — brain backend (FTS5-only inside Brain.search, hybrid
    via cmd_search delegation; top-heavy paths all fire at benchmark=False).
  * LoCoMo hybrid — cmd backend, all 10 convos.

Emits per-cell result JSON + .traces.jsonl under this directory.

Matrix cells (collapsed from 2^3 -> 3 — see README.md for rationale):

  FULL          topheavy=on, intent=on            (I2/I3/I4 shipped default)
  NO_INTENT     topheavy=on, intent=off           (fetch-narrow kept, no factual bypass)
  ROLLBACK      topheavy=off                       (pre-I2 behavior, Ollama-up)

CE rerank is not reachable through the bench harness (neither Brain.search
nor the bench's _build_cmd_search_fn populate args.rerank), so the CE
dimension is documented as "always off for this matrix." The CE p95 budget
env is still recorded in each cell's envs for reproducibility.

Fixed seed 42 (random.seed(42) in tests/bench/run.py already does this).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
OLLAMA_URL = "http://127.0.0.1:11436/api/embed"

# -----------------------------------------------------------------------------
# Cells
# -----------------------------------------------------------------------------

CELLS = [
    {
        "cell_id": "FULL",
        "description": "topheavy=on, intent=on — shipped I2/I3/I4 default",
        "envs": {
            # No rollback / no disable — defaults.
        },
    },
    {
        "cell_id": "NO_INTENT",
        "description": "topheavy=on, intent=off — keeps fetch-narrow, disables factual-fallback / intent routing",
        "envs": {
            "BRAINCTL_DISABLE_INTENT_ROUTER": "1",
        },
    },
    {
        "cell_id": "ROLLBACK",
        "description": "topheavy=off — all top-heavy features bypassed (pre-I2 behavior, Ollama-up)",
        "envs": {
            "BRAINCTL_TOPHEAVY_ROLLBACK": "1",
        },
    },
]


def _run_cell(cell: dict, bench: str, *, extra_args: list[str]) -> dict:
    cell_id = cell["cell_id"]
    traces_path = HERE / f"{cell_id.lower()}-{bench}.traces.jsonl"
    # Where to dump the JSON stdout
    stdout_path = HERE / f"{cell_id.lower()}-{bench}.json"

    env = os.environ.copy()
    env.setdefault("BRAINCTL_OLLAMA_URL", OLLAMA_URL)
    for k, v in cell["envs"].items():
        env[k] = v

    cmd = [
        sys.executable, "-m", "tests.bench.run",
        "--bench", bench,
        "--traces", str(traces_path),
        *extra_args,
    ]
    print(f"[{cell_id}/{bench}] {' '.join(cmd)}", file=sys.stderr)
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f"{cell_id}/{bench} failed rc={proc.returncode}")

    # Parse the JSON from stdout; the last "{" to end is the payload.
    out_text = proc.stdout.strip()
    # Runner prints a single json block. Find first '{' and parse.
    first_brace = out_text.find("{")
    payload = json.loads(out_text[first_brace:])
    stdout_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    return {
        "payload": payload,
        "elapsed_s": round(elapsed, 2),
        "traces_path": str(traces_path.relative_to(REPO)),
        "stdout_path": str(stdout_path.relative_to(REPO)),
    }


def _extract_metrics(payload: dict) -> dict:
    o = payload.get("overall", {}) or {}
    # Per-query latency list is inside the bench runner payload under
    # per_query_ms (LoCoMo) or per_entry (LongMemEval). Compute p95 directly.
    per_q: list[float] = []
    if isinstance(payload.get("per_query_ms"), list):
        per_q = [float(x) for x in payload["per_query_ms"]]
    elif isinstance(payload.get("per_entry"), list):
        per_q = [float(r.get("t_query_s", 0.0)) * 1000.0 for r in payload["per_entry"]]
    p95 = 0.0
    if per_q:
        ordered = sorted(per_q)
        import math as _m
        idx = max(0, min(len(ordered) - 1, _m.ceil(0.95 * len(ordered)) - 1))
        p95 = round(ordered[idx], 2)
    return {
        "hit_at_1": round(float(o.get("hit_at_1", 0.0)), 4),
        "hit_at_5": round(float(o.get("hit_at_5", 0.0)), 4),
        "hit_at_10": round(float(o.get("hit_at_10", 0.0)), 4),
        "mrr": round(float(o.get("mrr", 0.0)), 4),
        "ndcg_at_5": round(float(o.get("ndcg_at_5", 0.0)), 4),
        "recall_at_5": round(float(o.get("recall_at_5", 0.0)), 4),
        "n_questions": int(o.get("n_questions", 0) or 0),
        "p95_latency_ms": p95,
    }


# -----------------------------------------------------------------------------
# Benches: LongMemEval(289) friendly subset, LoCoMo cmd (all 10 convos)
# -----------------------------------------------------------------------------

BENCH_CONFIGS = [
    {
        "bench_id": "longmemeval-289",
        "bench": "longmemeval",
        "extra_args": [],  # default = retrieval-friendly subset (include_judge_only=False) ~289 entries
    },
    {
        "bench_id": "locomo-hybrid-cmd",
        "bench": "locomo",
        "extra_args": ["--backend", "cmd"],
    },
]


def main() -> int:
    matrix: list[dict] = []
    for cell in CELLS:
        for bc in BENCH_CONFIGS:
            print(f"\n=== Cell {cell['cell_id']} / bench {bc['bench_id']} ===",
                  file=sys.stderr)
            run = _run_cell(cell, bc["bench"], extra_args=bc["extra_args"])
            metrics = _extract_metrics(run["payload"])
            matrix.append({
                "cell_id": cell["cell_id"],
                "cell_description": cell["description"],
                "envs": cell["envs"],
                "flags": {},  # no CLI flags beyond envs for this matrix
                "bench_id": bc["bench_id"],
                "bench": bc["bench"],
                "extra_args": bc["extra_args"],
                "metrics": metrics,
                "elapsed_s": run["elapsed_s"],
                "traces_path": run["traces_path"],
                "stdout_path": run["stdout_path"],
            })
    out = HERE / "matrix.json"
    out.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
