#!/usr/bin/env python3
"""
Retrieval Quality Benchmark v1 — Cortex / COS-86
Run: python3 retrieval_benchmark_v1.py
Measures P@5 for 20 canonical queries against known expected memory IDs.
"""

import subprocess
import json
import sys
from datetime import datetime

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"
BRAINCTL = "/Users/r4vager/bin/brainctl"

# 20 canonical queries with expected memory IDs (ground truth)
# Each query should surface the listed memory IDs in top-5 results.
BENCHMARK_QUERIES = [
    {
        "id": "Q01",
        "query": "hippocampus module interface apply_decay consolidate",
        "expected_ids": [67],
        "description": "Hippocampus QA contract / expected Python interface"
    },
    {
        "id": "Q02",
        "query": "CostClock time tracking invoicing SaaS Next.js",
        "expected_ids": [77, 89],
        "description": "CostClock project overview"
    },
    {
        "id": "Q03",
        "query": "invoice lifecycle draft sent paid overdue",
        "expected_ids": [78],
        "description": "Invoice state machine knowledge"
    },
    {
        "id": "Q04",
        "query": "PAPERCLIP_AGENT_ID identity mismatch auth guardrail",
        "expected_ids": [85, 91],
        "description": "Auth identity mismatch pattern"
    },
    {
        "id": "Q05",
        "query": "Hermes core identity master prompt reshape",
        "expected_ids": [86],
        "description": "Hermes identity change signal"
    },
    {
        "id": "Q06",
        "query": "CostClock security hardening test coverage production readiness issues",
        "expected_ids": [89],
        "description": "CostClock open issues"
    },
    {
        "id": "Q07",
        "query": "Nexus heartbeat Kokoro token checkout fails",
        "expected_ids": [91, 85],
        "description": "Nexus auth / Kokoro binding bug"
    },
    {
        "id": "Q08",
        "query": "Memory Intelligence Division staffed agents registered brain.db",
        "expected_ids": [92, 93],
        "description": "Division staffing status"
    },
    {
        "id": "Q09",
        "query": "brainctl version coherence-check sentinel maintenance cron",
        "expected_ids": [93],
        "description": "System infrastructure state"
    },
    {
        "id": "Q10",
        "query": "hippocampus decay rate temporal class permanent medium short",
        "expected_ids": [77],
        "description": "Decay rate by temporal class"
    },
    {
        "id": "Q11",
        "query": "cadence metrics pipeline agent_state hippocampus cron",
        "expected_ids": [77],
        "description": "Cadence metrics cron"
    },
    {
        "id": "Q12",
        "query": "epoch detect create backfill memory event range",
        "expected_ids": [77],
        "description": "Epoch management"
    },
    {
        "id": "Q13",
        "query": "CostClock cron endpoint daily cleanup authorization bearer secret",
        "expected_ids": [77],
        "description": "CostClock cron auth pattern"
    },
    {
        "id": "Q14",
        "query": "branch policy feature branches PR main direct push forbidden",
        "expected_ids": [77],
        "description": "CostClock branch policy"
    },
    {
        "id": "Q15",
        "query": "22 agents M&I division hermes openclaw nara codex",
        "expected_ids": [92],
        "description": "Agent roster / headcount"
    },
    {
        "id": "Q16",
        "query": "memory spine backup iCloud maintenance nightly",
        "expected_ids": [93],
        "description": "Memory spine backup schedule"
    },
    {
        "id": "Q17",
        "query": "should_accept_memory resolve_contradictions search_memories tests",
        "expected_ids": [67],
        "description": "Hippocampus test surface"
    },
    {
        "id": "Q18",
        "query": "Stripe Supabase Vercel Tailwind TypeScript stack",
        "expected_ids": [77],
        "description": "CostClock tech stack"
    },
    {
        "id": "Q19",
        "query": "rate limiting CSV export SSE notifications eslint",
        "expected_ids": [89],
        "description": "CostClock feature tickets"
    },
    {
        "id": "Q20",
        "query": "compression permanent temporal class hippocampus compressed memory IDs",
        "expected_ids": [77],
        "description": "Memory compression exclusions"
    },
]


def sanitize_fts_query(query: str) -> str:
    """Remove FTS5 special chars that cause sqlite syntax errors."""
    import re
    # Remove chars that FTS5 treats as special: . & | * " ( ) -
    return re.sub(r'[.&|*()"@#]', ' ', query).strip()


def run_vsearch(query: str, top_k: int = 5) -> list[int]:
    """Run brainctl vsearch and return list of memory IDs in rank order."""
    safe_query = sanitize_fts_query(query)
    try:
        result = subprocess.run(
            [BRAINCTL, "-a", "paperclip-cortex", "vsearch", safe_query,
             "--tables", "memories", "--limit", str(top_k)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        memories = data.get("memories", data.get("results", []))
        if isinstance(data, list):
            memories = data
        return [int(m.get("id", 0)) for m in memories if m.get("id")]
    except Exception as e:
        print(f"  WARN: vsearch failed for query: {e}", file=sys.stderr)
        return []


def precision_at_k(retrieved: list[int], expected: list[int], k: int = 5) -> float:
    """P@K: fraction of top-k retrieved that are in the expected set."""
    if not expected:
        return 0.0
    top_k = retrieved[:k]
    hits = sum(1 for mid in top_k if mid in expected)
    return hits / k


def recall_at_k(retrieved: list[int], expected: list[int], k: int = 5) -> float:
    """Recall@K: fraction of expected items found in top-k."""
    if not expected:
        return 0.0
    top_k = retrieved[:k]
    hits = sum(1 for mid in expected if mid in top_k)
    return hits / len(expected)


def hit_at_k(retrieved: list[int], expected: list[int], k: int = 5) -> bool:
    """Hit@K: at least one expected item in top-k."""
    return any(mid in retrieved[:k] for mid in expected)


def run_benchmark() -> dict:
    print(f"=== Retrieval Benchmark v1 — {datetime.utcnow().isoformat()}Z ===\n")

    results = []
    total_p5 = 0.0
    total_r5 = 0.0
    total_hits = 0

    for item in BENCHMARK_QUERIES:
        retrieved = run_vsearch(item["query"])
        p5 = precision_at_k(retrieved, item["expected_ids"])
        r5 = recall_at_k(retrieved, item["expected_ids"])
        hit = hit_at_k(retrieved, item["expected_ids"])

        total_p5 += p5
        total_r5 += r5
        if hit:
            total_hits += 1

        status = "HIT" if hit else "MISS"
        print(f"[{item['id']}] {status}  P@5={p5:.2f}  R@5={r5:.2f}  | {item['description']}")
        if not hit:
            print(f"       expected={item['expected_ids']}  retrieved={retrieved[:5]}")

        results.append({
            "query_id": item["id"],
            "query": item["query"],
            "expected_ids": item["expected_ids"],
            "retrieved_ids": retrieved,
            "hit_at_5": hit,
            "precision_at_5": p5,
            "recall_at_5": r5,
        })

    n = len(BENCHMARK_QUERIES)
    mean_p5 = total_p5 / n
    mean_r5 = total_r5 / n
    hit_rate = total_hits / n

    print(f"\n--- Summary ---")
    print(f"Queries:     {n}")
    print(f"Hits@5:      {total_hits}/{n}  ({hit_rate:.0%})")
    print(f"Mean P@5:    {mean_p5:.3f}")
    print(f"Mean R@5:    {mean_r5:.3f}")
    print(f"Hit Rate:    {hit_rate:.3f}")

    return {
        "run_at": datetime.utcnow().isoformat() + "Z",
        "version": "v1",
        "n_queries": n,
        "hits_at_5": total_hits,
        "hit_rate": hit_rate,
        "mean_precision_at_5": mean_p5,
        "mean_recall_at_5": mean_r5,
        "results": results,
    }


if __name__ == "__main__":
    summary = run_benchmark()
    out_path = f"/Users/r4vager/agentmemory/benchmarks/results_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nResults saved: {out_path}")
