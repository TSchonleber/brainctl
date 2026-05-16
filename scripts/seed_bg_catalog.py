"""Seed bg_actions with a curated catalog of brainctl MCP tools mapped to
the five Alexander/DeLong/Strick parallel cortico-BG-thalamo-cortical loops.

Idempotent: re-running upserts into bg_actions by (loop, action_key).

Loop assignments follow biological mapping:
  motor       — state-changing writes (mutations)
  oculomotor  — retrieval / "where to look"
  dlpfc       — deliberative planning, inference, multi-step reasoning
  lofc        — value / outcome / trust / calibration
  acc         — conflict monitoring, contradiction handling, reflexion

Run:
  python3 scripts/seed_bg_catalog.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root before pip install -e .
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.mcp_tools_basal_ganglia import tool_bg_action_register

CATALOG: list[tuple[str, str, str]] = [
    # (loop, tool_name, description)
    # ─── motor: state-changing writes / mutations ─────────────────────────
    ("motor", "memory_add", "Persist a durable memory"),
    ("motor", "event_add", "Log an event onto the timeline"),
    ("motor", "decision_add", "Record a non-trivial decision"),
    ("motor", "entity_create", "Create an entity in the knowledge graph"),
    ("motor", "entity_observe", "Append observations to an existing entity"),
    ("motor", "entity_relate", "Add a typed relation between entities"),
    ("motor", "context_add", "Add a piece of session/working-memory context"),
    ("motor", "memory_promote", "Promote a memory tier (construct → full)"),
    ("motor", "handoff_add", "Create a structured continuity handoff packet"),

    # ─── oculomotor: retrieval / "where to look" ──────────────────────────
    ("oculomotor", "memory_search", "FTS + vector hybrid memory retrieval"),
    ("oculomotor", "push", "Salience-driven memory push to active context"),
    ("oculomotor", "agent_orient", "Session-start orient: pull pending handoff + events + push"),
    ("oculomotor", "entity_search", "FTS-based entity lookup"),
    ("oculomotor", "entity_get", "Fetch a single entity with relations"),
    ("oculomotor", "event_search", "Search the event timeline"),
    ("oculomotor", "vsearch", "Vector-only similarity search"),
    ("oculomotor", "search_patterns", "Cross-entity pattern discovery"),
    ("oculomotor", "context_search", "Search session context"),
    ("oculomotor", "handoff_latest", "Read the most recent pending handoff"),

    # ─── dlpfc: deliberative planning / inference ─────────────────────────
    ("dlpfc", "reason", "Hybrid L1+L2+L3 inference chain"),
    ("dlpfc", "infer", "L2-only inference"),
    ("dlpfc", "infer_pretask", "Pre-task active-inference gap scan"),
    ("dlpfc", "infer_gapfill", "Resolve gaps with inference"),
    ("dlpfc", "think", "Free-form deliberation"),
    ("dlpfc", "world_predict", "Predict task outcome from world model"),
    ("dlpfc", "world_project", "Project future state from current capabilities"),
    ("dlpfc", "abstract_summarize", "Hierarchical temporal abstraction"),

    # ─── lofc: value / outcome / trust ────────────────────────────────────
    ("lofc", "outcome_annotate", "Record outcome for a task — drives δ broadcast"),
    ("lofc", "outcome_report", "Memory lift / Brier / P@5 retrospective"),
    ("lofc", "trust_calibrate", "Update trust score from new evidence"),
    ("lofc", "trust_show", "Inspect trust breakdown for a memory"),
    ("lofc", "memory_calibration", "Self-monitor confidence vs realized outcomes"),
    ("lofc", "memory_utility_rate", "How often pushed memories were recalled"),
    ("lofc", "retrieval_effectiveness", "Aggregate retrieval quality"),
    ("lofc", "world_resolve", "Resolve a previous prediction against actual"),

    # ─── acc: conflict / contradiction / reflexion ────────────────────────
    ("acc", "belief_collapse", "AGM-style retract loser on conflict"),
    ("acc", "belief_conflicts_scan", "Scan for conflicting beliefs"),
    ("acc", "belief_conflicts_list", "Enumerate active belief conflicts"),
    ("acc", "resolve_conflict", "Resolve memory contradictions"),
    ("acc", "tom_conflicts_list", "Theory-of-mind belief conflicts between agents"),
    ("acc", "tom_conflicts_resolve", "Resolve cross-agent belief contradictions"),
    ("acc", "reflexion_write", "Log a lesson learned from failure"),
    ("acc", "reflexion_failure_recurrence", "Detect repeated failure patterns"),
    ("acc", "free_energy_check", "Pre-task uncertainty / surprise check"),
]


def main() -> dict[str, int]:
    counts: dict[str, int] = {}
    new = 0
    for loop, key, desc in CATALOG:
        result = tool_bg_action_register(
            loop=loop,
            action_key=f"tool:{key}",
            description=desc,
        )
        if result.get("ok"):
            counts[loop] = counts.get(loop, 0) + 1
            if (result.get("action") or {}).get("description") == desc:
                new += 1
    return {"total": sum(counts.values()), "by_loop": counts, "new_or_updated": new}


if __name__ == "__main__":
    import json
    print(json.dumps(main(), indent=2))
