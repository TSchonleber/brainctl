#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
quantum_routing_integration.py — Integration layer for quantum-inspired routing

Wraps quantum_amplitude_scorer into brainctl's routing pipeline.
Provides drop-in replacement for classical route_memories_hybrid.

Usage:
    from quantum_routing_integration import route_memories_quantum_hybrid
    results = route_memories_quantum_hybrid(conn, query, top_k=10)
"""

import sys
import sqlite3
from pathlib import Path
from typing import Optional, Dict, List

# Add paths
sys.path.insert(0, str(Path.home() / "agentmemory" / "bin"))
sys.path.insert(0, str(Path.home() / "agentmemory" / "research" / "quantum"))

try:
    import salience_routing
    SALIENCE_ROUTING_AVAILABLE = True
except ImportError:
    SALIENCE_ROUTING_AVAILABLE = False

try:
    import quantum_amplitude_scorer
    QUANTUM_AVAILABLE = True
except ImportError:
    QUANTUM_AVAILABLE = False


def route_memories_quantum_hybrid(
    conn: sqlite3.Connection,
    query: str,
    top_k: int = 10,
    scope: Optional[str] = None,
    agent_id: Optional[str] = None,
    min_salience: float = 0.15,
    quantum_blend: float = 0.7,  # 0.0 = pure classical, 1.0 = pure quantum
    vec_available: bool = True,
    neuro: Optional[dict] = None,
    use_quantum: bool = True,
) -> List[Dict]:
    """
    Hybrid routing combining classical and quantum salience scoring.

    First performs classical BM25+vector retrieval to get candidate pool,
    then re-ranks using quantum amplitude scoring if enabled.

    Args:
        conn: Database connection
        query: Search query
        top_k: Number of results to return
        scope: Memory scope filter
        agent_id: Agent ID filter
        min_salience: Minimum salience threshold
        quantum_blend: How much to weight quantum vs classical score
                      (0.0=100% classical, 1.0=100% quantum)
        vec_available: Whether sqlite-vec is available
        neuro: Neuromodulation state dict
        use_quantum: Whether to enable quantum re-ranking

    Returns:
        List of memory dicts with blended salience scores
    """
    if not SALIENCE_ROUTING_AVAILABLE:
        raise ImportError("salience_routing module required")

    # Step 1: Get classical results (BM25+vector hybrid)
    classical_results = salience_routing.route_memories_hybrid(
        conn,
        query=query,
        top_k=top_k * 2,  # Fetch extra for re-ranking
        scope=scope,
        agent_id=agent_id,
        min_salience=0.0,  # Lower threshold to get more candidates
        vec_available=vec_available,
        neuro=neuro,
    )

    if not classical_results or not use_quantum or not QUANTUM_AVAILABLE:
        # Fallback to classical
        return [c for c in classical_results[:top_k] if c["salience"] >= min_salience]

    # Step 2: Prepare data for quantum re-ranking
    candidate_ids = [r["id"] for r in classical_results]
    candidate_data = {r["id"]: r for r in classical_results}
    similarities = {r["id"]: r.get("similarity", 0.0) for r in classical_results}

    # Step 3: Compute quantum salience scores
    quantum_results = quantum_amplitude_scorer.route_memories_quantum(
        conn=conn,
        candidate_ids=candidate_ids,
        candidate_data=candidate_data,
        similarities=similarities,
        top_k=top_k * 2,
        min_salience=0.0,
    )

    # Step 4: Blend classical and quantum scores
    quantum_scores = {r["id"]: r["salience"] for r in quantum_results}

    blended = []
    for classical_result in classical_results:
        mem_id = classical_result["id"]
        classical_salience = classical_result["salience"]
        quantum_salience = quantum_scores.get(mem_id, 0.0)

        # Blend: hybrid_salience = (1-w)*classical + w*quantum
        blended_salience = (
            (1.0 - quantum_blend) * classical_salience +
            quantum_blend * quantum_salience
        )

        result = dict(classical_result)
        result["salience"] = round(blended_salience, 4)
        result["classical_salience"] = round(classical_salience, 4)
        result["quantum_salience"] = round(quantum_salience, 4)
        result["method"] = f"hybrid_blended_q{int(quantum_blend*100)}"
        blended.append(result)

    # Sort by blended salience
    blended.sort(key=lambda x: x["salience"], reverse=True)

    # Filter by threshold and return
    final_results = [r for r in blended[:top_k] if r["salience"] >= min_salience]
    return final_results


def benchmark_quantum_vs_classical(
    conn: sqlite3.Connection,
    test_queries: List[Dict],
) -> Dict:
    """
    Benchmark quantum vs classical retrieval on test queries.

    Args:
        conn: Database connection
        test_queries: List of dicts with keys: query, expected_ids, description

    Returns:
        Dict with benchmark results
    """
    if not SALIENCE_ROUTING_AVAILABLE:
        raise ImportError("salience_routing module required")

    results = {
        "total_queries": len(test_queries),
        "classical_p5": 0.0,
        "quantum_p5": 0.0,
        "blended_p5": 0.0,
        "quantum_improvement": 0.0,
        "details": []
    }

    classical_hits = 0
    quantum_hits = 0
    blended_hits = 0

    for test in test_queries:
        query = test["query"]
        expected_ids = set(test["expected_ids"])
        desc = test.get("description", "")

        # Classical retrieval
        classical_res = salience_routing.route_memories_hybrid(
            conn, query, top_k=5, vec_available=True
        )
        classical_retrieved = set(r["id"] for r in classical_res)
        classical_hit = len(classical_retrieved & expected_ids) > 0

        # Quantum retrieval
        quantum_res = route_memories_quantum_hybrid(
            conn, query, top_k=5, quantum_blend=1.0, use_quantum=True
        )
        quantum_retrieved = set(r["id"] for r in quantum_res)
        quantum_hit = len(quantum_retrieved & expected_ids) > 0

        # Blended retrieval
        blended_res = route_memories_quantum_hybrid(
            conn, query, top_k=5, quantum_blend=0.5, use_quantum=True
        )
        blended_retrieved = set(r["id"] for r in blended_res)
        blended_hit = len(blended_retrieved & expected_ids) > 0

        if classical_hit:
            classical_hits += 1
        if quantum_hit:
            quantum_hits += 1
        if blended_hit:
            blended_hits += 1

        results["details"].append({
            "query": query,
            "description": desc,
            "expected_ids": list(expected_ids),
            "classical_hit": classical_hit,
            "quantum_hit": quantum_hit,
            "blended_hit": blended_hit,
            "classical_top5": [r["id"] for r in classical_res],
            "quantum_top5": [r["id"] for r in quantum_res],
        })

    # Compute precision @ 5
    n = len(test_queries)
    results["classical_p5"] = classical_hits / n if n > 0 else 0.0
    results["quantum_p5"] = quantum_hits / n if n > 0 else 0.0
    results["blended_p5"] = blended_hits / n if n > 0 else 0.0
    results["quantum_improvement"] = (quantum_hits - classical_hits) / max(1, classical_hits)

    return results


if __name__ == "__main__":
    # Quick test
    try:
        db_path = Path.home() / "agentmemory" / "db" / "brain.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        test_query = "memory consolidation decay"
        print(f"Testing query: {test_query!r}\n")

        # Classical
        classical = salience_routing.route_memories_hybrid(conn, test_query, top_k=5)
        print(f"Classical results ({len(classical)}):")
        for r in classical[:3]:
            print(f"  [{r['salience']:.3f}] {r['content'][:60]}")

        # Quantum hybrid
        if QUANTUM_AVAILABLE:
            quantum_hybrid = route_memories_quantum_hybrid(
                conn, test_query, top_k=5, quantum_blend=0.5
            )
            print(f"\nQuantum hybrid results ({len(quantum_hybrid)}):")
            for r in quantum_hybrid[:3]:
                print(f"  [{r['salience']:.3f}] (q:{r.get('quantum_salience',0):.3f}) {r['content'][:60]}")
        else:
            print("\n✗ Quantum module not available")

        conn.close()

    except Exception as e:
        import traceback
        print(f"✗ Error: {e}")
        traceback.print_exc()
        sys.exit(1)
