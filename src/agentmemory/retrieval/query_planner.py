"""Intent-aware query planning for retrieval orchestration."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from intent_classifier import classify_intent as _classify_intent
except Exception:  # pragma: no cover - optional script path
    _classify_intent = None

_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.:-]+\b")
_ENTITY_QUERY_RE = re.compile(
    r"\b("
    r"who(?:\s+is|\s+owns?)?|"
    r"whose|"
    r"owner|maintainer|reviewer|assignee|"
    r"what\s+does|"
    r"prefers?|preference|"
    r"role|responsible|"
    r"works?\s+on"
    r")\b",
    re.IGNORECASE,
)
_TEMPORAL_RE = re.compile(
    r"\b("
    r"yesterday|today|tomorrow|when|timeline|history|recent|overnight|"
    r"last\s+(?:week|month|year|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this\s+(?:week|month|year)|"
    r"past\s+(?:week|month|year|two weeks|three months)|"
    r"most recent|latest|earliest|previous(?:ly)?|current(?:ly)?|"
    r"before|after|between|during|in the past|order of|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?:day|week|month|year)s?\s+ago"
    r")\b",
    re.IGNORECASE,
)
_MULTIHOP_RE = re.compile(
    r"\b("
    r"why|because|rationale|support|evidence|rollback|troubleshoot|debug|fix|"
    r"how many|how much|order|earliest|latest|most recent|"
    r"before|after|between|difference|older|newer|"
    r"compare|combined|total|sum|"
    r"based on|underlying|future|might|would"
    r")\b",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    r"\b("
    r"how many|how much|count|number of|total|sum|combined total"
    r")\b",
    re.IGNORECASE,
)
_COMPARE_RE = re.compile(
    r"\b("
    r"compare|difference|different|versus|vs\.?|better|worse|older|newer|"
    r"more than|less than|changed|relative to"
    r")\b",
    re.IGNORECASE,
)
_ORDER_RE = re.compile(
    r"\b("
    r"before|after|between|order|ordered|sequence|timeline|earliest|latest|"
    r"first|last|most recent|newest|oldest|rank"
    r")\b",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(
    r"\b("
    r"current(?:ly)?|previous(?:ly)?|formerly|used to|now|new|updated|"
    r"latest|most recent|superseded|stale|still|anymore"
    r")\b",
    re.IGNORECASE,
)
_COVERAGE_RE = re.compile(
    r"\b("
    r"all|both|each|every|across|combined|together|list|which sessions|"
    r"what were the sessions|set of"
    r")\b",
    re.IGNORECASE,
)
_ROLE_FACT_RE = re.compile(
    r"\b("
    r"father|dad|mother|mom|parent|coworker|colleague|friend|neighbor|"
    r"brother|sister|nephew|niece|aunt|uncle|cousin|boss|manager|supervisor|subordinate|employee|"
    r"workplace|occupation|position|job|employer|education|educational|"
    r"degree|background|location|hometown|role|hobby|enjoys?|loves?|passion|"
    r"email|contact|phone|number|company|living"
    r")\b",
    re.IGNORECASE,
)
_SYNTHETIC_KV_RE = re.compile(
    r"\b("
    r"id|key|code|value|field|role|status|attribute|group|session|step"
    r")\b|[A-Za-z]+[_-]\d+|\w+[=:]\w+",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"\b("
    r"no answer|"
    r"do not know|"
    r"unknown|"
    r"no memory|"
    r"coverage gap|"
    r"summary of yesterday(?:'s)? .+|"
    r"(?:basketball|baseball|football|soccer|weather|stock market|earnings)\b"
    r")",
    re.IGNORECASE,
)
_ENTITY_BLACKLIST = {"what", "who", "where", "when", "why", "how", "summary"}


@dataclass(slots=True)
class QueryPlan:
    normalized_intent: str
    answer_type: str
    target_entities: list[str] = field(default_factory=list)
    temporal_anchors: list[str] = field(default_factory=list)
    requires_temporal_reasoning: bool = False
    requires_multi_hop: bool = False
    needs_counting: bool = False
    needs_comparison: bool = False
    needs_ordering: bool = False
    needs_update_resolution: bool = False
    needs_set_coverage: bool = False
    needs_role_fact: bool = False
    needs_synthetic_key_value: bool = False
    prefer_memory_types: list[str] = field(default_factory=list)
    candidate_tables: list[str] = field(default_factory=list)
    abstain_allowed: bool = False
    debug_reasons: list[str] = field(default_factory=list)
    classifier_intent: str = "general"
    classifier_confidence: float = 0.0
    format_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_INTENT_ALIASES = {
    "cross_reference": "entity",
    "decision_rationale": "decision",
    "entity_lookup": "factual",
    "event_lookup": "temporal",
    "factual_lookup": "factual",
    "general": "factual",
    "graph_traversal": "graph",
    "historical_timeline": "temporal",
    "how_to": "procedural",
    "orientation": "orientation",
    "procedural": "procedural",
    "research_concept": "factual",
    "task_status": "temporal",
    "troubleshooting": "troubleshooting",
}


_TABLE_ROUTES = {
    "procedural": ["procedures", "memories", "decisions", "events", "context", "policy"],
    "troubleshooting": ["procedures", "events", "memories", "decisions", "context", "policy"],
    "decision": ["decisions", "memories", "procedures", "events", "context"],
    "temporal": ["events", "memories", "context", "entities", "procedures"],
    "factual": ["memories", "entities", "decisions", "context", "events", "procedures"],
    "graph": ["memories", "events", "context", "decisions", "procedures"],
    "orientation": ["memories", "events", "context", "procedures"],
}


def _builtin_classify(query: str) -> tuple[str, float, str]:
    q = query.lower()
    temporalish = bool(_TEMPORAL_RE.search(query))
    multihopish = bool(_MULTIHOP_RE.search(query))
    if _ENTITY_QUERY_RE.search(query):
        return ("factual", 0.72, "builtin:entity_fact")
    if any(token in q for token in ("how to", "how do", "procedure", "rollback", "runbook", "playbook")):
        return ("procedural", 0.82, "builtin:procedural")
    if any(token in q for token in ("error", "syntax", "bug", "failed", "fix", "troubleshoot")):
        return ("troubleshooting", 0.8, "builtin:troubleshooting")
    if any(token in q for token in ("why", "decision", "rationale", "choose", "chose")):
        return ("decision", 0.78, "builtin:decision")
    if temporalish or "what happened" in q:
        reason = "builtin:temporal_multihop" if multihopish else "builtin:temporal"
        return ("temporal", 0.8 if multihopish else 0.78, reason)
    if any(token in q for token in ("who", "what", "where", "which", "entity")):
        return ("factual", 0.6, "builtin:factual")
    return ("factual", 0.45, "builtin:default")


def _extract_entities(query: str) -> list[str]:
    entities = [match.group(0) for match in _ENTITY_RE.finditer(query or "")]
    if not entities:
        pattern_hits = re.findall(
            r"\b(?:what\s+does|who\s+is|who\s+owns|where\s+is|when\s+did)\s+([A-Za-z0-9_.:-]+)",
            query or "",
            flags=re.IGNORECASE,
        )
        entities.extend(pattern_hits)
    seen: set[str] = set()
    out: list[str] = []
    for entity in entities:
        key = entity.lower()
        if key in _ENTITY_BLACKLIST:
            continue
        if key not in seen:
            seen.add(key)
            out.append(entity)
    return out[:8]


def plan_query(
    query: str,
    *,
    requested_tables: Optional[list[str]] = None,
) -> QueryPlan:
    """Return a structured routing plan for the query."""

    classifier_intent = "general"
    classifier_confidence = 0.0
    format_hint = ""
    reasons: list[str] = []

    if _classify_intent is not None:
        try:
            result = _classify_intent(query)
            classifier_intent = getattr(result, "intent", "general")
            classifier_confidence = float(getattr(result, "confidence", 0.0) or 0.0)
            format_hint = getattr(result, "format_hint", "") or ""
            reasons.append(f"classifier:{classifier_intent}")
        except Exception:
            pass

    if classifier_intent == "general":
        builtin_intent, builtin_conf, reason = _builtin_classify(query)
        normalized_intent = builtin_intent
        classifier_confidence = max(classifier_confidence, builtin_conf)
        reasons.append(reason)
    else:
        normalized_intent = _INTENT_ALIASES.get(classifier_intent, "factual")

    query_lower = query.lower()
    temporal_anchors = [m.group(0) for m in _TEMPORAL_RE.finditer(query)]
    answer_type = {
        "decision": "rationale",
        "procedural": "procedure",
        "troubleshooting": "procedure",
        "temporal": "history",
        "graph": "mixed",
        "orientation": "briefing",
    }.get(normalized_intent, "fact")
    prefer_memory_types = {
        "decision": ["semantic", "procedural", "episodic"],
        "procedural": ["procedural", "semantic", "episodic"],
        "troubleshooting": ["procedural", "episodic", "semantic"],
        "temporal": ["episodic", "semantic"],
        "factual": ["semantic", "procedural", "episodic"],
        "graph": ["semantic", "episodic", "procedural"],
        "orientation": ["semantic", "episodic", "procedural"],
    }.get(normalized_intent, ["semantic", "episodic"])

    candidate_tables = list(requested_tables or _TABLE_ROUTES.get(normalized_intent, _TABLE_ROUTES["factual"]))
    requires_temporal = bool(_TEMPORAL_RE.search(query))
    requires_multi_hop = bool(_MULTIHOP_RE.search(query))
    needs_counting = bool(_COUNT_RE.search(query))
    needs_comparison = bool(_COMPARE_RE.search(query))
    needs_ordering = bool(_ORDER_RE.search(query))
    needs_update_resolution = bool(_UPDATE_RE.search(query))
    needs_set_coverage = bool(_COVERAGE_RE.search(query))
    needs_role_fact = bool(_ROLE_FACT_RE.search(query))
    needs_synthetic_key_value = bool(_SYNTHETIC_KV_RE.search(query))
    if requires_multi_hop and normalized_intent in {"temporal", "decision", "graph"}:
        needs_set_coverage = True
    if needs_counting or needs_comparison or needs_ordering:
        needs_set_coverage = True
    abstain_allowed = bool(_NEGATIVE_RE.search(query)) or normalized_intent in {"factual", "troubleshooting", "procedural"}
    if _ENTITY_QUERY_RE.search(query) and normalized_intent == "factual":
        reasons.append("entity_or_role_lookup")
    if requires_temporal:
        reasons.append("temporal_reasoning")
    if requires_multi_hop:
        reasons.append("multi_hop_or_inference")
    if needs_counting:
        reasons.append("operator:counting")
    if needs_comparison:
        reasons.append("operator:comparison")
    if needs_ordering:
        reasons.append("operator:ordering")
    if needs_update_resolution:
        reasons.append("operator:update_resolution")
    if needs_set_coverage:
        reasons.append("operator:set_coverage")
    if needs_role_fact:
        reasons.append("operator:role_fact")
    if needs_synthetic_key_value:
        reasons.append("operator:synthetic_key_value")
    if "summary of yesterday" in query_lower:
        abstain_allowed = True
        reasons.append("negative_or_out_of_domain_summary")
    if " and " in query_lower and len(_extract_entities(query)) == 0:
        reasons.append("ambiguous_composite_query")
        abstain_allowed = True

    return QueryPlan(
        normalized_intent=normalized_intent,
        answer_type=answer_type,
        target_entities=_extract_entities(query),
        temporal_anchors=temporal_anchors,
        requires_temporal_reasoning=requires_temporal,
        requires_multi_hop=requires_multi_hop,
        needs_counting=needs_counting,
        needs_comparison=needs_comparison,
        needs_ordering=needs_ordering,
        needs_update_resolution=needs_update_resolution,
        needs_set_coverage=needs_set_coverage,
        needs_role_fact=needs_role_fact,
        needs_synthetic_key_value=needs_synthetic_key_value,
        prefer_memory_types=prefer_memory_types,
        candidate_tables=candidate_tables,
        abstain_allowed=abstain_allowed,
        debug_reasons=reasons,
        classifier_intent=classifier_intent,
        classifier_confidence=classifier_confidence,
        format_hint=format_hint,
    )
