#!/usr/bin/env python3
"""
intent_classifier.py — Query intent classification for brainctl search (COS-417)

Heuristic-first classifier: regex + keyword matching covers ~80% of agent queries.
Returns an IntentResult with intent label, confidence, and recommended table routing.

Intent taxonomy (derived from 337 real access_log queries):
  cross_reference   — COS-xxx, PAP-xxx ticket lookups
  troubleshooting   — errors, blockers, conflicts, bugs
  task_status       — pending/assigned work, heartbeat, inbox
  entity_lookup     — named agents, teams, proper nouns
  historical_timeline — wave N summaries, past events, audit trail
  how_to            — procedure, branch policy, how-to
  decision_rationale — why something was decided, architecture decisions
  research_concept  — theory, algorithms, academic topics
  orientation       — startup/bootstrap context, "what's going on"
  factual_lookup    — specific facts, config keys (fallback)
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class IntentResult:
    intent: str
    confidence: float            # 0.0 – 1.0
    tables: list                 # recommended table order (most → least important)
    matched_rule: str            # which rule fired
    format_hint: str             # how to format results


# ---------------------------------------------------------------------------
# Table routing presets
# ---------------------------------------------------------------------------

# Each entry is (primary_tables, secondary_tables).
# The final merged list fed to --tables is primary + secondary (de-duped).
_TABLE_ROUTES = {
    "cross_reference":    ["events", "memories", "context"],
    "troubleshooting":    ["events", "memories", "context"],
    "task_status":        ["events", "context", "memories"],
    "entity_lookup":      ["memories", "context", "events"],   # entities not in universal search pipeline
    "historical_timeline":["events", "context", "memories"],
    "how_to":             ["memories", "context"],
    "decision_rationale": ["memories", "context", "events"],
    "research_concept":   ["memories", "context"],
    "orientation":        ["memories", "events", "context"],
    "factual_lookup":     ["memories", "context", "events"],   # same as default
}

_FORMAT_HINTS = {
    "cross_reference":    "group by ticket id, show event_type and summary",
    "troubleshooting":    "timeline order, highlight error events first",
    "task_status":        "list format: status + assignee + created_at",
    "entity_lookup":      "entity card: name + type + related facts",
    "historical_timeline":"chronological order, show created_at prominently",
    "how_to":             "numbered steps if available, bullet points otherwise",
    "decision_rationale": "lead with decision, follow with rationale context",
    "research_concept":   "concept summary first, references second",
    "orientation":        "compact briefing: recent events + active memories",
    "factual_lookup":     "single best answer, confidence score shown",
}

# ---------------------------------------------------------------------------
# Rule definitions (order matters — first match wins)
# ---------------------------------------------------------------------------

# Pattern: (intent, confidence, matched_rule, [keyword_patterns], [regex_patterns])
# keyword_patterns: strings that must appear anywhere in lowercased query
# regex_patterns: compiled regexes tested against original query

_TICKET_RE = re.compile(r'\b[A-Z]{2,6}-\d+\b')
# Matches named entities: "CostClock", "M&I Division agents", "Memory Intelligence Division"
_PROPER_NOUN_ALONE_RE = re.compile(r'^[A-Z][A-Za-z]{2,}(\s+[A-Z&][A-Za-z&/]*)*(\s+(agents?|team|division|inc|llc|ai))?\s*$')
_WAVE_RE = re.compile(r'\bwave\s*\d+\b', re.IGNORECASE)
_HOW_RE = re.compile(r'\bhow\s+(to|do|does|can|should)\b', re.IGNORECASE)
_WHY_RE = re.compile(r'\bwhy\b', re.IGNORECASE)
# First-person/identity statement (Hermes memory dumps stored as queries)
_IDENTITY_STMT_RE = re.compile(
    r'^(I |My |The vault|Chief wakes|Continuity is|Tasks that|Learn the|'
    r'Letting |Compensating|Repeating|A file that|[0-9]+ agents? total)',
    re.IGNORECASE
)
_AGENT_NAMES = {
    "kokoro", "hermes", "legion", "scribe", "weaver", "cipher",
    "cortex", "engram", "nexus", "tempo", "stratos", "tensor",
    "probe", "aegis", "sentinel", "epoch", "hippocampus",
}

def _kw(query_lower: str, keywords: list[str]) -> Optional[str]:
    """Return first matching keyword, or None."""
    for kw in keywords:
        if kw in query_lower:
            return kw
    return None


def classify_intent(query: str) -> IntentResult:
    """
    Classify a search query into an intent category.
    Heuristic-first; returns factual_lookup as fallback.

    ~80% of real agent queries are handled by these rules.
    """
    q = query.strip()
    ql = q.lower()

    # ---- Rule 1: Cross-reference (ticket ID in query) ----
    if _TICKET_RE.search(q):
        return IntentResult(
            intent="cross_reference",
            confidence=0.95,
            tables=_TABLE_ROUTES["cross_reference"],
            matched_rule="ticket_id_regex",
            format_hint=_FORMAT_HINTS["cross_reference"],
        )

    # ---- Rule 2: Troubleshooting ----
    _TROUBLE_KW = [
        "blocked", " error", "fail", "409", "conflict", "stall", "anomal",
        "broken", "crash", "contention", "mismatch", "stuck", "checkout fail",
        "checkout return", "write contention", "unblock", " bug", "scope filter",
        "409 conflict", " lock ", "deadlock", "validate integrity", "integrity",
        "wal backup", "sentinel", "sentinel-2", "wal checkpoint",
    ]
    hit = _kw(ql, _TROUBLE_KW)
    if hit:
        return IntentResult(
            intent="troubleshooting",
            confidence=0.88,
            tables=_TABLE_ROUTES["troubleshooting"],
            matched_rule=f"trouble_kw:{hit}",
            format_hint=_FORMAT_HINTS["troubleshooting"],
        )

    # ---- Rule 3: Task status ----
    _TASK_KW = [
        "pending", "assigned", "current task", "heartbeat", "inbox",
        "assignment", "in progress", "next steps", "pending work",
        "recent task", "current priority", "paperclip work", "my task",
        "what am i", "todo", "backlog",
    ]
    hit = _kw(ql, _TASK_KW)
    if hit:
        return IntentResult(
            intent="task_status",
            confidence=0.85,
            tables=_TABLE_ROUTES["task_status"],
            matched_rule=f"task_kw:{hit}",
            format_hint=_FORMAT_HINTS["task_status"],
        )

    # ---- Rule 4: How-to ----
    if _HOW_RE.search(q):
        return IntentResult(
            intent="how_to",
            confidence=0.88,
            tables=_TABLE_ROUTES["how_to"],
            matched_rule="how_to_regex",
            format_hint=_FORMAT_HINTS["how_to"],
        )

    # ---- Rule 5: Historical timeline ----
    _HIST_KW = [
        "what happened", "history", "audit", "timeline", "overnight",
        "recent heartbeat", "last session", "last cycle", "recently",
        "session transcript",
    ]
    hit = _kw(ql, _HIST_KW)
    wave_hit = _WAVE_RE.search(q)
    if hit or wave_hit:
        return IntentResult(
            intent="historical_timeline",
            confidence=0.85,
            tables=_TABLE_ROUTES["historical_timeline"],
            matched_rule=f"hist_kw:{hit or 'wave_regex'}",
            format_hint=_FORMAT_HINTS["historical_timeline"],
        )

    # ---- Rule 6: Decision rationale ----
    _DECISION_KW = [
        "decision", "rationale", "chose", "architecture decision",
        "why did", "constraint", "policy", "strategy", "deploy on",
        "branch policy",
    ]
    hit = _kw(ql, _DECISION_KW)
    if hit or _WHY_RE.search(q):
        return IntentResult(
            intent="decision_rationale",
            confidence=0.80,
            tables=_TABLE_ROUTES["decision_rationale"],
            matched_rule=f"decision_kw:{hit or 'why_regex'}",
            format_hint=_FORMAT_HINTS["decision_rationale"],
        )

    # ---- Rule 7: Research concept ----
    _RESEARCH_KW = [
        "bayesian", "quantum", "entropy", "information theory", "mdl",
        "friston", "free energy", "active inference", "continual learning",
        "catastrophic forgetting", "epistem", "theory of mind", "bdi",
        "cognitive protocol", "cognitive load", "neuromodulation",
        "reflexion", "global workspace", "mmr", "rrf", "faiss",
        "page rank", "pagerank", "community detection", "centrality",
        "kolmogorov", "surprise scoring", "write gate", "write decision",
        "distillation", "consolidation", "hippocampus", "decay", "salience",
        "embedding", "vector", "sparse", "hybrid search", "recall_count",
        "temporal class", "temporal_class", "proactive interference",
        "gini", "rif", "hhi", "ewc", "ewc importance", "bisociation",
        "dream pass", "knowledge_edges", "metacognition", "snr metric",
        "memory health", "coverage", "distillation ratio", "schema migration",
        "trust score", "calibration", "reconsolidation", "provenance",
        "retrieval utility", "retrieval benchmark", "hit rate", "p@5",
        "memory pruning", "memory hygiene", "redundancy", "dedupl",
        "context routing", "agent routing", "specialization routing",
        "adaptive weight", "adaptive retrieval", "compute_adaptive",
        "recall distribution", "memory retrieval", "recall search",
        "search retrieval", "fts5", "sqlite", "belief revision",
        "agm", "context ingestion", "attention budget",
        "intelligence synthesis", "cognitive evolution",
        "agent specialization", "proactive memory push",
        "open source", "packaging", "seeded", "belief seeding",
    ]
    hit = _kw(ql, _RESEARCH_KW)
    if hit:
        return IntentResult(
            intent="research_concept",
            confidence=0.85,
            tables=_TABLE_ROUTES["research_concept"],
            matched_rule=f"research_kw:{hit}",
            format_hint=_FORMAT_HINTS["research_concept"],
        )

    # ---- Rule 8: Identity statements (Hermes/agent self-description stored as queries) ----
    if _IDENTITY_STMT_RE.match(q):
        return IntentResult(
            intent="orientation",
            confidence=0.75,
            tables=_TABLE_ROUTES["orientation"],
            matched_rule="identity_statement",
            format_hint=_FORMAT_HINTS["orientation"],
        )

    # ---- Rule 9: Entity lookup (agent names, proper nouns alone, product names) ----
    words = set(ql.split())
    agent_hit = words & _AGENT_NAMES
    if agent_hit:
        return IntentResult(
            intent="entity_lookup",
            confidence=0.82,
            tables=_TABLE_ROUTES["entity_lookup"],
            matched_rule=f"agent_name:{next(iter(agent_hit))}",
            format_hint=_FORMAT_HINTS["entity_lookup"],
        )
    if _PROPER_NOUN_ALONE_RE.match(q):
        return IntentResult(
            intent="entity_lookup",
            confidence=0.72,
            tables=_TABLE_ROUTES["entity_lookup"],
            matched_rule="proper_noun_alone",
            format_hint=_FORMAT_HINTS["entity_lookup"],
        )

    # ---- Rule 10: Orientation ----
    _ORIENT_KW = [
        "project status", "what's going on", "orientation", "startup",
        "bootstrap", "status update", "current state", "company status",
        "intelligence brief", "what am i doing", "brief",
    ]
    hit = _kw(ql, _ORIENT_KW)
    if hit:
        return IntentResult(
            intent="orientation",
            confidence=0.78,
            tables=_TABLE_ROUTES["orientation"],
            matched_rule=f"orient_kw:{hit}",
            format_hint=_FORMAT_HINTS["orientation"],
        )

    # ---- Fallback: Factual lookup ----
    return IntentResult(
        intent="factual_lookup",
        confidence=0.50,
        tables=_TABLE_ROUTES["factual_lookup"],
        matched_rule="fallback",
        format_hint=_FORMAT_HINTS["factual_lookup"],
    )


# ---------------------------------------------------------------------------
# Batch classify (for benchmarking)
# ---------------------------------------------------------------------------

def batch_classify(queries: list[str]) -> list[dict]:
    results = []
    intent_counts: dict[str, int] = {}
    for q in queries:
        r = classify_intent(q)
        intent_counts[r.intent] = intent_counts.get(r.intent, 0) + 1
        results.append({
            "query": q,
            "intent": r.intent,
            "confidence": r.confidence,
            "rule": r.matched_rule,
            "tables": r.tables,
        })
    return results


# ---------------------------------------------------------------------------
# CLI entry point (brainctl intent <query>)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: intent_classifier.py <query>")
        sys.exit(1)
    query = " ".join(sys.argv[1:])
    result = classify_intent(query)
    print(json.dumps({
        "query": query,
        "intent": result.intent,
        "confidence": result.confidence,
        "matched_rule": result.matched_rule,
        "tables": result.tables,
        "format_hint": result.format_hint,
    }, indent=2))
