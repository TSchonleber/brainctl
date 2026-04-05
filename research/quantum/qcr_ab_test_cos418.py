#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
qcr_ab_test_cos418.py — COS-418 Empirical A/B Test: QCR Scoring vs Baseline Retrieval

Design:
  - Baseline: salience_routing.route_memories_hybrid (FTS5 + cosine + RRF + recency)
  - Treatment A: quantum_blend=0.7 (default QCR)
  - Treatment B: quantum_blend=1.0 (pure QCR amplitude scoring)
  - 50 queries: 20 canonical (updated ground truth) + 30 from access_log patterns
  - Metrics: P@5, R@5, MRR, latency per query (ms), result overlap rate
  - Secondary: scoring stability (std-dev of salience), memory overhead

Author: Epoch (COS-418)
Date: 2026-04-02
"""

from __future__ import annotations

import sys
import time
import json
import sqlite3
import statistics
import resource
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set, Tuple, Optional

sys.path.insert(0, str(Path.home() / "agentmemory" / "bin"))
sys.path.insert(0, str(Path.home() / "agentmemory" / "benchmarks"))
sys.path.insert(0, str(Path.home() / "agentmemory" / "research" / "quantum"))

import salience_routing
import quantum_routing_integration


DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"
RESULTS_PATH = Path.home() / "agentmemory" / "research" / "quantum" / "qcr_ab_results_cos418.json"

# ---------------------------------------------------------------------------
# Ground-truth query set (50 queries)
# expected_ids: any of these in top-5 = hit
# Notes on ID selection:
#   - 130: large consolidated permanent memory containing CostClock, hippocampus,
#           brainctl, epoch, Paperclip facts. Valid answer for many queries.
#   - 93: agent memory spine state, brainctl/sentinel infrastructure
#   - 127: push gate threshold, distillation policy
#   - 407: causal event graph, knowledge edges
#   - 125: Kernel brainctl tools integration
#   - 383: brainctl reason/infer, neuro-symbolic
#   - 78: invoice lifecycle specific
#   - 86: Hermes identity
#   - 406: COS-81 epoch analysis
#   - 743: epoch analyze_access_patterns
#   - 532: distill threshold lesson
#   - 558: memory health rescue
#   - 802: distill cron upgrade
#   - 804: Paperclip temporal cognition project
# ---------------------------------------------------------------------------

BENCHMARK_QUERIES: List[Dict] = [
    # === GROUP 1: Canonical (20 queries) — updated ground truth ===
    {
        "id": "Q01", "group": "canonical",
        "query": "hippocampus module interface apply_decay consolidate",
        "expected_ids": [130, 93],
        "description": "Hippocampus decay/consolidate interface"
    },
    {
        "id": "Q02", "group": "canonical",
        "query": "CostClock time tracking invoicing SaaS Next.js",
        "expected_ids": [130, 78, 106],
        "description": "CostClock project overview"
    },
    {
        "id": "Q03", "group": "canonical",
        "query": "invoice lifecycle draft sent paid overdue state",
        "expected_ids": [78, 130, 106],
        "description": "Invoice state machine"
    },
    {
        "id": "Q04", "group": "canonical",
        "query": "PAPERCLIP_AGENT_ID identity mismatch auth guardrail",
        "expected_ids": [130, 93],
        "description": "Auth identity mismatch pattern"
    },
    {
        "id": "Q05", "group": "canonical",
        "query": "Hermes core identity master prompt reshape",
        "expected_ids": [86, 376],
        "description": "Hermes identity change signal"
    },
    {
        "id": "Q06", "group": "canonical",
        "query": "CostClock security hardening test coverage production readiness",
        "expected_ids": [130, 78],
        "description": "CostClock production readiness issues"
    },
    {
        "id": "Q07", "group": "canonical",
        "query": "Nexus heartbeat Kokoro token checkout authentication fails",
        "expected_ids": [130, 93],
        "description": "Nexus auth / Kokoro binding"
    },
    {
        "id": "Q08", "group": "canonical",
        "query": "Memory Intelligence Division agents registered brain.db",
        "expected_ids": [93, 376],
        "description": "Division staffing status"
    },
    {
        "id": "Q09", "group": "canonical",
        "query": "brainctl coherence-check sentinel maintenance cron schedule",
        "expected_ids": [93, 130],
        "description": "System infrastructure / cron"
    },
    {
        "id": "Q10", "group": "canonical",
        "query": "hippocampus decay rate temporal class permanent medium short ephemeral",
        "expected_ids": [130, 93],
        "description": "Decay rates by temporal class"
    },
    {
        "id": "Q11", "group": "canonical",
        "query": "cadence metrics pipeline agent_state hippocampus cron write",
        "expected_ids": [130, 802],
        "description": "Cadence metrics cron"
    },
    {
        "id": "Q12", "group": "canonical",
        "query": "epoch detect create backfill memory event range null",
        "expected_ids": [130, 743],
        "description": "Epoch management"
    },
    {
        "id": "Q13", "group": "canonical",
        "query": "CostClock cron endpoint daily cleanup authorization bearer secret",
        "expected_ids": [130, 78],
        "description": "CostClock cron auth pattern"
    },
    {
        "id": "Q14", "group": "canonical",
        "query": "branch policy feature branches PR main direct push forbidden",
        "expected_ids": [130],
        "description": "Branch policy enforcement"
    },
    {
        "id": "Q15", "group": "canonical",
        "query": "Claude Code IDE extensions local adapters claude_local VS Code",
        "expected_ids": [130, 93],
        "description": "Claude Code architecture"
    },
    {
        "id": "Q16", "group": "canonical",
        "query": "Hermes scheduler hourly periodic task background job monitoring",
        "expected_ids": [802, 376],
        "description": "Scheduler / periodic tasks"
    },
    {
        "id": "Q17", "group": "canonical",
        "query": "OpenClaw AI agent framework local execution deployment",
        "expected_ids": [93, 376, 125],
        "description": "OpenClaw agent system"
    },
    {
        "id": "Q18", "group": "canonical",
        "query": "Paperclip task heartbeat assignment checkout workflow status",
        "expected_ids": [93, 130, 804],
        "description": "Paperclip task execution"
    },
    {
        "id": "Q19", "group": "canonical",
        "query": "Nara external data integration REST API polling synchronization",
        "expected_ids": [93, 376],
        "description": "Nara integration system"
    },
    {
        "id": "Q20", "group": "canonical",
        "query": "brain.db schema memory relationships confidence consolidation epochs",
        "expected_ids": [130, 93, 407, 743],
        "description": "Brain.db data model"
    },

    # === GROUP 2: Access-log derived (30 queries) — realistic agent queries ===
    {
        "id": "Q21", "group": "access_log",
        "query": "brainctl search FTS5 access_log query",
        "expected_ids": [130, 93],
        "description": "Brainctl FTS5 search internals"
    },
    {
        "id": "Q22", "group": "access_log",
        "query": "query intent classification search routing agent",
        "expected_ids": [125, 383],
        "description": "Query routing/classification"
    },
    {
        "id": "Q23", "group": "access_log",
        "query": "graph algorithms brain.db knowledge edges",
        "expected_ids": [407, 383],
        "description": "Knowledge graph edges"
    },
    {
        "id": "Q24", "group": "access_log",
        "query": "information theory entropy memory valuation write gate",
        "expected_ids": [127, 532],
        "description": "Information-theoretic write gate"
    },
    {
        "id": "Q25", "group": "access_log",
        "query": "costclock pentest security api vulnerability",
        "expected_ids": [130, 78],
        "description": "CostClock security assessment"
    },
    {
        "id": "Q26", "group": "access_log",
        "query": "agentmemory open source packaging release",
        "expected_ids": [93, 376],
        "description": "Agentmemory packaging"
    },
    {
        "id": "Q27", "group": "access_log",
        "query": "implementation backlog research wave quantum",
        "expected_ids": [376, 804],
        "description": "Implementation backlog research"
    },
    {
        "id": "Q28", "group": "access_log",
        "query": "billing hotfix invoice idempotency recurring",
        "expected_ids": [78, 130],
        "description": "Billing/invoice hotfix"
    },
    {
        "id": "Q29", "group": "access_log",
        "query": "epoch recall contradiction temporal memory",
        "expected_ids": [743, 130],
        "description": "Epoch recall/contradiction"
    },
    {
        "id": "Q30", "group": "access_log",
        "query": "sentinel validation integrity brainctl coherence",
        "expected_ids": [93, 558],
        "description": "Sentinel validation"
    },
    {
        "id": "Q31", "group": "access_log",
        "query": "wave12 distillation lag recall gini improvement",
        "expected_ids": [127, 532, 558],
        "description": "Distillation improvement metrics"
    },
    {
        "id": "Q32", "group": "access_log",
        "query": "embedding coverage consolidation backfill",
        "expected_ids": [93, 407],
        "description": "Embedding consolidation"
    },
    {
        "id": "Q33", "group": "access_log",
        "query": "recall distribution monopoly dominance high-frequency",
        "expected_ids": [93, 127, 130],
        "description": "Recall distribution analysis"
    },
    {
        "id": "Q34", "group": "access_log",
        "query": "permanent memory high confidence recalled architecture decision",
        "expected_ids": [127, 407, 93],
        "description": "Permanent memory characteristics"
    },
    {
        "id": "Q35", "group": "access_log",
        "query": "proactive interference bayesian confidence update",
        "expected_ids": [383, 127],
        "description": "Proactive interference / Bayesian"
    },
    {
        "id": "Q36", "group": "access_log",
        "query": "decay hippocampus consolidation memory cycle",
        "expected_ids": [130, 93, 410],
        "description": "Hippocampus decay cycle"
    },
    {
        "id": "Q37", "group": "access_log",
        "query": "agent_expertise source reliability weighting social epistemology",
        "expected_ids": [383, 125],
        "description": "Agent expertise weighting"
    },
    {
        "id": "Q38", "group": "access_log",
        "query": "bayesian brain alpha beta schema confidence update",
        "expected_ids": [383, 127],
        "description": "Bayesian confidence schema"
    },
    {
        "id": "Q39", "group": "access_log",
        "query": "heartbeat dispatch backlog agent assignment wake",
        "expected_ids": [93, 130],
        "description": "Heartbeat dispatch system"
    },
    {
        "id": "Q40", "group": "access_log",
        "query": "memory event bus MEB world model propagation",
        "expected_ids": [130, 93],
        "description": "Memory Event Bus (MEB)"
    },
    {
        "id": "Q41", "group": "access_log",
        "query": "context ingestion attention budget tier weaver",
        "expected_ids": [125, 383],
        "description": "Context ingestion budget"
    },
    {
        "id": "Q42", "group": "access_log",
        "query": "memory health distillation pipeline cron hourly",
        "expected_ids": [802, 558, 532],
        "description": "Distillation pipeline health"
    },
    {
        "id": "Q43", "group": "access_log",
        "query": "brainctl reason infer neuro-symbolic L1 L2 L3",
        "expected_ids": [383, 125],
        "description": "Neuro-symbolic reasoning commands"
    },
    {
        "id": "Q44", "group": "access_log",
        "query": "CostClock invoice compression test memories scope",
        "expected_ids": [106, 78, 130],
        "description": "Invoice memory compression"
    },
    {
        "id": "Q45", "group": "access_log",
        "query": "Paperclip temporal cognition epoch filed issues COG BRN",
        "expected_ids": [804, 376, 743],
        "description": "Temporal cognition project"
    },
    {
        "id": "Q46", "group": "access_log",
        "query": "causal event graph auto-detection edges knowledge",
        "expected_ids": [407, 383],
        "description": "Causal graph implementation"
    },
    {
        "id": "Q47", "group": "access_log",
        "query": "brainctl push gate distillation policy threshold memories",
        "expected_ids": [127, 532],
        "description": "Push gate policy"
    },
    {
        "id": "Q48", "group": "access_log",
        "query": "massive session CKO identity created agents hired woke managed",
        "expected_ids": [376],
        "description": "CKO identity session"
    },
    {
        "id": "Q49", "group": "access_log",
        "query": "hippocampus compression source_event_id distillation ratio",
        "expected_ids": [410, 130],
        "description": "Hippocampus compression issue COS-319"
    },
    {
        "id": "Q50", "group": "access_log",
        "query": "memory health rescue retired operational noise reclassified",
        "expected_ids": [558, 532],
        "description": "Memory health rescue operation"
    },
]


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def precision_at_k(retrieved: List[int], relevant: Set[int], k: int = 5) -> float:
    top_k = retrieved[:k]
    hits = sum(1 for r in top_k if r in relevant)
    return hits / k


def recall_at_k(retrieved: List[int], relevant: Set[int], k: int = 5) -> float:
    top_k = retrieved[:k]
    hits = sum(1 for r in top_k if r in relevant)
    return hits / len(relevant) if relevant else 0.0


def mrr(retrieved: List[int], relevant: Set[int]) -> float:
    for rank, r in enumerate(retrieved, 1):
        if r in relevant:
            return 1.0 / rank
    return 0.0


def hit_at_k(retrieved: List[int], relevant: Set[int], k: int = 5) -> bool:
    return any(r in relevant for r in retrieved[:k])


def overlap_rate(list_a: List[int], list_b: List[int], k: int = 5) -> float:
    set_a = set(list_a[:k])
    set_b = set(list_b[:k])
    return len(set_a & set_b) / k


def score_stability(scores: List[float]) -> float:
    """Std-dev of top-5 scores — lower = flatter distribution, higher = more discriminative."""
    if len(scores) < 2:
        return 0.0
    return statistics.stdev(scores[:5])


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

def run_ab_test(conn: sqlite3.Connection) -> Dict:
    n = len(BENCHMARK_QUERIES)
    print(f"\n{'='*80}")
    print("COS-418: QCR A/B TEST — Baseline vs Quantum Amplitude Scoring")
    print(f"{'='*80}")
    print(f"Date:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database:  {DB_PATH}")
    print(f"Memories:  {conn.execute('SELECT COUNT(*) FROM memories WHERE retired_at IS NULL').fetchone()[0]} active")
    print(f"Queries:   {n} (20 canonical + 30 access_log)")
    print()

    # Memory before
    mem_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    results = []
    # Aggregates: [baseline, qcr_07, qcr_10]
    hits5 = [0, 0, 0]
    sum_p5 = [0.0, 0.0, 0.0]
    sum_r5 = [0.0, 0.0, 0.0]
    sum_mrr = [0.0, 0.0, 0.0]
    latencies = [[], [], []]
    overlaps_07 = []  # baseline vs QCR-0.7
    overlaps_10 = []  # baseline vs QCR-1.0
    stabilities = [[], [], []]

    labels = ["Baseline", "QCR-0.7", "QCR-1.0"]

    for test in BENCHMARK_QUERIES:
        qid = test["id"]
        query = test["query"]
        relevant = set(test["expected_ids"])
        group = test["group"]

        # --- Baseline ---
        t0 = time.perf_counter()
        base_res = salience_routing.route_memories_hybrid(
            conn, query, top_k=5, vec_available=True
        )
        t_base = (time.perf_counter() - t0) * 1000  # ms

        # --- QCR blend=0.7 ---
        t0 = time.perf_counter()
        qcr07_res = quantum_routing_integration.route_memories_quantum_hybrid(
            conn, query, top_k=5, quantum_blend=0.7, use_quantum=True
        )
        t_qcr07 = (time.perf_counter() - t0) * 1000

        # --- QCR blend=1.0 (pure quantum) ---
        t0 = time.perf_counter()
        qcr10_res = quantum_routing_integration.route_memories_quantum_hybrid(
            conn, query, top_k=5, quantum_blend=1.0, use_quantum=True
        )
        t_qcr10 = (time.perf_counter() - t0) * 1000

        base_ids = [r["id"] for r in base_res]
        qcr07_ids = [r["id"] for r in qcr07_res]
        qcr10_ids = [r["id"] for r in qcr10_res]

        base_scores = [r.get("salience", 0.0) for r in base_res]
        qcr07_scores = [r.get("salience", 0.0) for r in qcr07_res]
        qcr10_scores = [r.get("salience", 0.0) for r in qcr10_res]

        all_ids = [base_ids, qcr07_ids, qcr10_ids]
        all_scores = [base_scores, qcr07_scores, qcr10_scores]
        all_latencies = [t_base, t_qcr07, t_qcr10]

        row_p5 = []
        row_r5 = []
        row_mrr = []
        row_hit = []

        for i, (ids, scores, lat) in enumerate(zip(all_ids, all_scores, all_latencies)):
            p5 = precision_at_k(ids, relevant)
            r5 = recall_at_k(ids, relevant)
            m = mrr(ids, relevant)
            hit = hit_at_k(ids, relevant)

            sum_p5[i] += p5
            sum_r5[i] += r5
            sum_mrr[i] += m
            if hit:
                hits5[i] += 1
            latencies[i].append(lat)
            stabilities[i].append(score_stability(scores))

            row_p5.append(p5)
            row_r5.append(r5)
            row_mrr.append(m)
            row_hit.append(hit)

        ov_07 = overlap_rate(base_ids, qcr07_ids)
        ov_10 = overlap_rate(base_ids, qcr10_ids)
        overlaps_07.append(ov_07)
        overlaps_10.append(ov_10)

        # Status indicator
        base_hit = row_hit[0]
        qcr07_hit = row_hit[1]
        if qcr07_hit and not base_hit:
            status = "↑ QCR WINS"
        elif base_hit and not qcr07_hit:
            status = "↓ QCR LOSES"
        elif base_hit and qcr07_hit:
            status = "= BOTH HIT"
        else:
            status = "- BOTH MISS"

        print(f"{qid} [{group:10s}] {status}")
        print(f"  Query:   {query[:70]}")
        print(f"  Expected: {sorted(relevant)}")
        print(f"  Baseline  hit={base_hit}  P@5={row_p5[0]:.2f}  R@5={row_r5[0]:.2f}  MRR={row_mrr[0]:.2f}  {t_base:.1f}ms  ids={base_ids}")
        print(f"  QCR-0.7   hit={qcr07_hit} P@5={row_p5[1]:.2f}  R@5={row_r5[1]:.2f}  MRR={row_mrr[1]:.2f}  {t_qcr07:.1f}ms  ids={qcr07_ids}")
        print(f"  QCR-1.0   hit={row_hit[2]} P@5={row_p5[2]:.2f}  R@5={row_r5[2]:.2f}  MRR={row_mrr[2]:.2f}  {t_qcr10:.1f}ms  ids={qcr10_ids}")
        print(f"  Overlap(base↔QCR-0.7)={ov_07:.0%}  Overlap(base↔QCR-1.0)={ov_10:.0%}")
        print()

        results.append({
            "query_id": qid,
            "group": group,
            "query": query,
            "expected_ids": list(relevant),
            "baseline": {
                "ids": base_ids, "hit": base_hit,
                "p5": row_p5[0], "r5": row_r5[0], "mrr": row_mrr[0],
                "latency_ms": round(t_base, 2),
                "scores": [round(s, 4) for s in base_scores],
            },
            "qcr_07": {
                "ids": qcr07_ids, "hit": qcr07_hit,
                "p5": row_p5[1], "r5": row_r5[1], "mrr": row_mrr[1],
                "latency_ms": round(t_qcr07, 2),
                "scores": [round(s, 4) for s in qcr07_scores],
            },
            "qcr_10": {
                "ids": qcr10_ids, "hit": row_hit[2],
                "p5": row_p5[2], "r5": row_r5[2], "mrr": row_mrr[2],
                "latency_ms": round(t_qcr10, 2),
                "scores": [round(s, 4) for s in qcr10_scores],
            },
            "overlap_base_qcr07": round(ov_07, 3),
            "overlap_base_qcr10": round(ov_10, 3),
        })

    # Memory after
    mem_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mem_delta_kb = (mem_after - mem_before) // 1024

    # Summary
    mean_p5 = [s / n for s in sum_p5]
    mean_r5 = [s / n for s in sum_r5]
    mean_mrr = [s / n for s in sum_mrr]
    mean_lat = [statistics.mean(l) for l in latencies]
    p95_lat = [sorted(l)[int(0.95 * len(l))] for l in latencies]
    mean_stability = [statistics.mean(s) for s in stabilities]
    mean_ov_07 = statistics.mean(overlaps_07)
    mean_ov_10 = statistics.mean(overlaps_10)

    # QCR improvements vs baseline
    p5_delta_07 = mean_p5[1] - mean_p5[0]
    p5_delta_10 = mean_p5[2] - mean_p5[0]
    lat_overhead_07 = mean_lat[1] - mean_lat[0]
    lat_overhead_10 = mean_lat[2] - mean_lat[0]

    print(f"\n{'='*80}")
    print("RESULTS SUMMARY")
    print(f"{'='*80}")
    print(f"{'Metric':<28} {'Baseline':>12} {'QCR-0.7':>12} {'QCR-1.0':>12}")
    print(f"{'-'*28} {'-'*12} {'-'*12} {'-'*12}")
    print(f"{'Hit Rate (H@5)':<28} {hits5[0]/n:>12.1%} {hits5[1]/n:>12.1%} {hits5[2]/n:>12.1%}")
    print(f"{'Mean P@5':<28} {mean_p5[0]:>12.3f} {mean_p5[1]:>12.3f} {mean_p5[2]:>12.3f}")
    print(f"{'Mean R@5':<28} {mean_r5[0]:>12.3f} {mean_r5[1]:>12.3f} {mean_r5[2]:>12.3f}")
    print(f"{'Mean MRR':<28} {mean_mrr[0]:>12.3f} {mean_mrr[1]:>12.3f} {mean_mrr[2]:>12.3f}")
    print(f"{'Mean Latency (ms)':<28} {mean_lat[0]:>12.1f} {mean_lat[1]:>12.1f} {mean_lat[2]:>12.1f}")
    print(f"{'P95 Latency (ms)':<28} {p95_lat[0]:>12.1f} {p95_lat[1]:>12.1f} {p95_lat[2]:>12.1f}")
    print(f"{'Score Stability (σ)':<28} {mean_stability[0]:>12.4f} {mean_stability[1]:>12.4f} {mean_stability[2]:>12.4f}")
    print()
    print(f"Overlap (Baseline ↔ QCR-0.7):  {mean_ov_07:.1%}  (% of top-5 results shared)")
    print(f"Overlap (Baseline ↔ QCR-1.0):  {mean_ov_10:.1%}")
    print()
    print(f"P@5 Delta (QCR-0.7 vs baseline): {p5_delta_07:+.3f}")
    print(f"P@5 Delta (QCR-1.0 vs baseline): {p5_delta_10:+.3f}")
    print(f"Latency Overhead (QCR-0.7):      {lat_overhead_07:+.1f}ms")
    print(f"Latency Overhead (QCR-1.0):      {lat_overhead_10:+.1f}ms")
    print(f"Memory overhead (resident):      {mem_delta_kb}KB")

    # Success criteria evaluation
    print()
    print("=" * 80)
    print("SUCCESS CRITERIA EVALUATION")
    print("=" * 80)

    sc1 = p5_delta_07 >= 0.10
    sc2 = lat_overhead_07 <= 50.0
    sc3_base = hits5[0]
    sc3_qcr = hits5[1]
    # No regression = QCR doesn't lose queries baseline hit
    regressions = sum(1 for r in results if r["baseline"]["hit"] and not r["qcr_07"]["hit"])
    sc3 = regressions == 0

    print(f"[{'PASS' if sc1 else 'FAIL'}] P@5 improvement >= 10%:  {p5_delta_07:+.1%} (need +10%)")
    print(f"[{'PASS' if sc2 else 'FAIL'}] Latency overhead <= 50ms: {lat_overhead_07:+.1f}ms QCR-0.7")
    print(f"[{'PASS' if sc3 else 'FAIL'}] No regression on baseline hits: {regressions} regression(s)")

    if not sc1 and p5_delta_07 < 0.05:
        verdict = "FAIL — QCR improvement < 5%. Recommendation: merge schema only (COS-401), shelve amplitude scoring."
    elif not sc1:
        verdict = "BORDERLINE — improvement exists but below 10% threshold. Consider extended testing."
    else:
        verdict = "PASS — QCR demonstrates sufficient improvement."

    print()
    print(f"Verdict: {verdict}")
    print("=" * 80)

    summary = {
        "timestamp": datetime.now().isoformat(),
        "n_queries": n,
        "baseline": {
            "hit_rate": round(hits5[0] / n, 4),
            "mean_p5": round(mean_p5[0], 4),
            "mean_r5": round(mean_r5[0], 4),
            "mean_mrr": round(mean_mrr[0], 4),
            "mean_latency_ms": round(mean_lat[0], 2),
            "p95_latency_ms": round(p95_lat[0], 2),
            "mean_score_stability": round(mean_stability[0], 4),
        },
        "qcr_07": {
            "hit_rate": round(hits5[1] / n, 4),
            "mean_p5": round(mean_p5[1], 4),
            "mean_r5": round(mean_r5[1], 4),
            "mean_mrr": round(mean_mrr[1], 4),
            "mean_latency_ms": round(mean_lat[1], 2),
            "p95_latency_ms": round(p95_lat[1], 2),
            "mean_score_stability": round(mean_stability[1], 4),
        },
        "qcr_10": {
            "hit_rate": round(hits5[2] / n, 4),
            "mean_p5": round(mean_p5[2], 4),
            "mean_r5": round(mean_r5[2], 4),
            "mean_mrr": round(mean_mrr[2], 4),
            "mean_latency_ms": round(mean_lat[2], 2),
            "p95_latency_ms": round(p95_lat[2], 2),
            "mean_score_stability": round(mean_stability[2], 4),
        },
        "deltas": {
            "p5_qcr07_vs_baseline": round(p5_delta_07, 4),
            "p5_qcr10_vs_baseline": round(p5_delta_10, 4),
            "latency_overhead_ms_07": round(lat_overhead_07, 2),
            "latency_overhead_ms_10": round(lat_overhead_10, 2),
        },
        "overlap": {
            "mean_base_qcr07": round(mean_ov_07, 4),
            "mean_base_qcr10": round(mean_ov_10, 4),
        },
        "memory_overhead_kb": mem_delta_kb,
        "success_criteria": {
            "p5_improvement_gte_10pct": sc1,
            "latency_overhead_lte_50ms": sc2,
            "no_regression_on_baseline": sc3,
            "regressions": regressions,
        },
        "verdict": verdict,
        "queries": results,
    }

    with open(RESULTS_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nFull results saved to: {RESULTS_PATH}")
    return summary


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        run_ab_test(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
