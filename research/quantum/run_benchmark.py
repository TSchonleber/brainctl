#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
run_benchmark.py — Compare quantum vs classical retrieval on benchmark queries

Runs the 20 canonical queries from retrieval_benchmark_v1.py against both
classical and quantum-inspired retrieval algorithms.

Output: Precision@5 comparison and detailed analysis
"""

import sys
import sqlite3
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path.home() / "agentmemory" / "bin"))
sys.path.insert(0, str(Path.home() / "agentmemory" / "benchmarks"))
sys.path.insert(0, str(Path.home() / "agentmemory" / "research" / "quantum"))

import salience_routing
import quantum_routing_integration


# Benchmark queries from retrieval_benchmark_v1.py
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
        "description": "Branch policy enforcement"
    },
    {
        "id": "Q15",
        "query": "Claude Code IDE extensions local adapters claude_local VS Code",
        "expected_ids": [77],
        "description": "Claude Code architecture"
    },
    {
        "id": "Q16",
        "query": "Hermes scheduler hourly periodic task background job monitoring",
        "expected_ids": [77],
        "description": "Hermes scheduler pattern"
    },
    {
        "id": "Q17",
        "query": "OpenClaw AI agent framework local execution deployment pattern",
        "expected_ids": [92],
        "description": "OpenClaw agent system"
    },
    {
        "id": "Q18",
        "query": "Paperclip task heartbeat assignment checkout workflow status",
        "expected_ids": [93],
        "description": "Paperclip task execution"
    },
    {
        "id": "Q19",
        "query": "Nara external data integration REST API polling synchronization",
        "expected_ids": [92, 93],
        "description": "Nara integration system"
    },
    {
        "id": "Q20",
        "query": "brain.db schema memory relationships confidence consolidation epochs",
        "expected_ids": [77],
        "description": "Brain.db data model"
    },
]


def run_benchmark():
    """Run the benchmark comparison."""
    db_path = Path.home() / "agentmemory" / "db" / "brain.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    print("=" * 80)
    print("QUANTUM-INSPIRED RETRIEVAL ALGORITHM BENCHMARK")
    print("=" * 80)
    print(f"Date: {datetime.now().isoformat()}")
    print(f"Database: {db_path}")
    print(f"Test queries: {len(BENCHMARK_QUERIES)}")
    print()

    classical_hits = 0
    quantum_hits = 0
    blended_hits = 0
    details = []

    for test in BENCHMARK_QUERIES:
        query_id = test["id"]
        query = test["query"]
        expected_ids = set(test["expected_ids"])
        description = test["description"]

        # Classical retrieval
        classical_res = salience_routing.route_memories_hybrid(
            conn, query, top_k=5, vec_available=True
        )
        classical_retrieved = set(r["id"] for r in classical_res)
        classical_hit = len(classical_retrieved & expected_ids) > 0
        classical_scores = [r["salience"] for r in classical_res]

        # Quantum retrieval (pure quantum)
        quantum_res = quantum_routing_integration.route_memories_quantum_hybrid(
            conn, query, top_k=5, quantum_blend=1.0, use_quantum=True
        )
        quantum_retrieved = set(r["id"] for r in quantum_res)
        quantum_hit = len(quantum_retrieved & expected_ids) > 0
        quantum_scores = [r.get("quantum_salience", 0.0) for r in quantum_res]

        # Blended retrieval (50/50 classical + quantum)
        blended_res = quantum_routing_integration.route_memories_quantum_hybrid(
            conn, query, top_k=5, quantum_blend=0.5, use_quantum=True
        )
        blended_retrieved = set(r["id"] for r in blended_res)
        blended_hit = len(blended_retrieved & expected_ids) > 0
        blended_scores = [r["salience"] for r in blended_res]

        if classical_hit:
            classical_hits += 1
        if quantum_hit:
            quantum_hits += 1
        if blended_hit:
            blended_hits += 1

        # Determine if quantum improved
        improved = "✓ IMPROVED" if (quantum_hit and not classical_hit) else ""
        regressed = "✗ REGRESSED" if (not quantum_hit and classical_hit) else ""
        status = improved or regressed or ("=" if quantum_hit == classical_hit else "~")

        print(f"{query_id}: {status}")
        print(f"  Query: {query[:70]}")
        print(f"  Description: {description}")
        print(f"  Expected: {expected_ids}")
        print(f"  Classical: {classical_hit:5} | Top scores: {[round(s, 3) for s in classical_scores[:3]]}")
        print(f"  Quantum:   {quantum_hit:5} | Top scores: {[round(s, 3) for s in quantum_scores[:3]]}")
        print(f"  Blended:   {blended_hit:5} | Top scores: {[round(s, 3) for s in blended_scores[:3]]}")
        print()

        details.append({
            "query_id": query_id,
            "query": query,
            "expected_ids": list(expected_ids),
            "classical_hit": classical_hit,
            "quantum_hit": quantum_hit,
            "blended_hit": blended_hit,
            "classical_top5": [r["id"] for r in classical_res],
            "quantum_top5": [r["id"] for r in quantum_res],
            "blended_top5": [r["id"] for r in blended_res],
        })

    # Summary
    n = len(BENCHMARK_QUERIES)
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    classical_p5 = classical_hits / n
    quantum_p5 = quantum_hits / n
    blended_p5 = blended_hits / n
    quantum_improvement = (quantum_hits - classical_hits)
    improvement_pct = (quantum_improvement / max(1, classical_hits)) * 100 if classical_hits > 0 else 0

    print(f"Precision@5 Results:")
    print(f"  Classical:  {classical_p5:.1%} ({classical_hits}/{n} hits)")
    print(f"  Quantum:    {quantum_p5:.1%} ({quantum_hits}/{n} hits)")
    print(f"  Blended:    {blended_p5:.1%} ({blended_hits}/{n} hits)")
    print()
    print(f"Quantum Improvement: {quantum_improvement:+d} hits ({improvement_pct:+.1f}%)")
    print()

    # Save results
    results = {
        "timestamp": datetime.now().isoformat(),
        "classical_p5": classical_p5,
        "quantum_p5": quantum_p5,
        "blended_p5": blended_p5,
        "quantum_improvement": quantum_improvement,
        "improvement_pct": improvement_pct,
        "details": details,
    }

    results_file = Path.home() / "agentmemory" / "research" / "quantum" / "benchmark_results.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    print(f"Results saved to: {results_file}")
    print("=" * 80)

    conn.close()
    return results


if __name__ == "__main__":
    try:
        results = run_benchmark()
        sys.exit(0)
    except Exception as e:
        import traceback
        print(f"✗ Error: {e}")
        traceback.print_exc()
        sys.exit(1)
