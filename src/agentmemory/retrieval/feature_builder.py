"""Feature extraction for the shared second-stage reranker."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

try:  # pragma: no cover - numpy is optional at import time
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for",
    "from", "has", "have", "how", "i", "in", "is", "it", "its", "of",
    "on", "or", "that", "the", "to", "was", "we", "what", "when", "where",
    "which", "who", "why", "will", "with", "you",
}
_LOW_SIGNAL_TOKENS = {
    "summary", "history", "timeline", "recent", "today", "yesterday", "tomorrow",
    "game", "issue", "problem", "thing", "stuff", "update",
}
_SYNONYMS = {
    "dad": {"father", "parent"},
    "father": {"dad", "parent"},
    "mom": {"mother", "parent"},
    "mother": {"mom", "parent"},
    "workplace": {"work", "works", "job", "office", "occupation", "position", "employer"},
    "occupation": {"job", "work", "works", "position", "career"},
    "position": {"job", "occupation", "work", "works", "role"},
    "educational": {"education", "degree", "school", "background"},
    "education": {"educational", "degree", "school", "background"},
    "background": {"education", "degree", "school"},
    "degree": {"education", "educational", "school", "background"},
    "location": {"where", "place", "city", "hometown", "workplace"},
    "hometown": {"home", "city", "location", "from"},
    "coworker": {"colleague", "work", "works"},
    "hobby": {"enjoy", "enjoys", "love", "loves", "passion", "passionate", "into"},
    "enjoy": {"hobby", "likes", "love", "loves", "passion"},
    "enjoys": {"hobby", "likes", "love", "loves", "passion"},
    "loves": {"hobby", "enjoy", "enjoys", "passion", "passionate"},
    "passionate": {"hobby", "enjoy", "enjoys", "loves"},
    "boss": {"manager", "supervisor"},
    "subordinate": {"employee", "report", "teammate"},
    "aunt": {"relative"},
    "uncle": {"relative"},
    "cousin": {"relative"},
    "living": {"occupation", "job", "work", "works"},
    "email": {"contact", "address"},
    "contact": {"phone", "number", "email"},
    "number": {"phone", "contact"},
}
_ROLE_TERMS = {
    "father", "dad", "mother", "mom", "parent", "coworker", "colleague",
    "friend", "neighbor", "sister", "brother", "wife", "husband", "nephew",
    "niece", "aunt", "uncle", "cousin", "relative", "boss", "manager",
    "supervisor", "subordinate", "employee", "report",
}
_ATTRIBUTE_TERMS = {
    "education", "educational", "background", "degree", "school", "occupation",
    "position", "job", "workplace", "works", "work", "location", "hometown",
    "company", "employer", "role", "status", "key", "code", "value",
    "hobby", "enjoy", "enjoys", "love", "loves", "likes", "passion",
    "passionate", "into", "email", "address", "contact", "number", "phone", "living",
}
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.IGNORECASE,
)
_TEMPORAL_RE = re.compile(
    r"\b(yesterday|today|tomorrow|when|before|after|during|timeline|history|recent|latest|first|last)\b",
    re.IGNORECASE,
)
_LONG_CONTEXT_HINT_RE = re.compile(
    r"\b("
    r"how many|how much|order|earliest|latest|most recent|"
    r"before|after|between|this month|last month|past month|past week|"
    r"current(?:ly)?|previous(?:ly)?|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?:day|week|month|year)s?\s+ago|"
    r"based on|underlying|future|might|would"
    r")\b",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(
    r"(?:^|[|_\s-])(?:sid|session|s)[=_ :#-]*(\d+)|\bsession[_ :#-]*(\d+)\b",
    re.IGNORECASE,
)
_DIALOG_RE = re.compile(r"\bD(\d+):", re.IGNORECASE)
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.:-]+\b")

FEATURE_VERSION_V1 = "v1"
FEATURE_ORDER_V1 = [
    "base_score",
    "retrieval_score",
    "rrf_score",
    "confidence",
    "query_overlap",
    "informative_overlap",
    "tfidf_cosine",
    "exact_phrase",
    "entity_overlap",
    "alias_overlap",
    "query_temporal",
    "candidate_temporal",
    "temporal_anchor_overlap",
    "query_session_hint",
    "candidate_session_hint",
    "session_gap_score",
    "intent_bucket_fit",
    "source_keyword",
    "source_semantic",
    "source_both",
    "source_graph",
    "bucket_memories",
    "bucket_events",
    "bucket_entities",
    "bucket_procedures",
    "bucket_decisions",
    "candidate_age_score",
    "support_evidence_score",
    "status_active",
    "status_stale",
    "status_needs_review",
    "position_score",
    "neighbor_margin",
    "query_length_score",
    "candidate_length_score",
    "procedural_candidate",
]


def _normalize_token(token: str) -> str:
    tok = re.sub(r"[^a-z0-9]+", "", (token or "").lower())
    if len(tok) <= 2 or tok in _STOPWORDS:
        return ""
    if tok.endswith("ies") and len(tok) > 4:
        tok = tok[:-3] + "y"
    elif tok.endswith("ed") and len(tok) > 4:
        tok = tok[:-2]
    elif tok.endswith("es") and len(tok) > 4:
        tok = tok[:-2]
    elif tok.endswith("s") and len(tok) > 3:
        tok = tok[:-1]
    return tok


def _token_set(text: str) -> set[str]:
    tokens = {
        token
        for part in re.split(r"\s+", text or "")
        if (token := _normalize_token(part))
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(_SYNONYMS.get(token, ()))
    return expanded


def _informative_tokens(text: str) -> set[str]:
    return {token for token in _token_set(text) if token not in _LOW_SIGNAL_TOKENS}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def candidate_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "content", "summary", "title", "goal", "description", "search_text",
        "name", "compiled_truth", "why_retrieved",
    ):
        value = candidate.get(key)
        if value:
            parts.append(str(value))
    for key in ("observations", "aliases", "supporting_evidence"):
        value = candidate.get(key)
        if not value:
            continue
        if isinstance(value, str):
            parts.append(value)
        else:
            try:
                parts.append(json.dumps(value, ensure_ascii=True))
            except Exception:
                parts.append(str(value))
    return " ".join(parts)


def _alias_values(candidate: dict[str, Any]) -> list[str]:
    raw = candidate.get("aliases")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(value) for value in raw if value]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return [raw]
        if isinstance(parsed, list):
            return [str(value) for value in parsed if value]
        return [raw]
    return [str(raw)]


def _entity_terms(text: str) -> set[str]:
    return {
        match.group(0).lower()
        for match in _ENTITY_RE.finditer(text or "")
        if len(match.group(0)) > 2
    }


def _intent_bucket_preference(plan: Any, bucket: str) -> float:
    if plan is None:
        return 0.5
    tables = list(getattr(plan, "candidate_tables", []) or [])
    if not tables:
        return 0.5
    try:
        position = tables.index(bucket)
    except ValueError:
        return 0.2
    return max(0.2, 1.0 - (position * 0.12))


def _source_flags(candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    source = str(candidate.get("source") or "").lower()
    return (
        1.0 if source in {"keyword", "procedure_fts", "intent_entity", "intent_decision"} else 0.0,
        1.0 if source == "semantic" else 0.0,
        1.0 if source == "both" else 0.0,
        1.0 if source == "graph" else 0.0,
    )


def _bucket_flags(bucket: str) -> tuple[float, float, float, float, float]:
    return (
        1.0 if bucket == "memories" else 0.0,
        1.0 if bucket == "events" else 0.0,
        1.0 if bucket == "entities" else 0.0,
        1.0 if bucket == "procedures" else 0.0,
        1.0 if bucket == "decisions" else 0.0,
    )


def _temporal_anchor_overlap(query: str, text: str) -> float:
    query_dates = {match.group(0).lower() for match in _DATE_RE.finditer(query or "")}
    cand_dates = {match.group(0).lower() for match in _DATE_RE.finditer(text or "")}
    if not query_dates:
        return 0.0
    return len(query_dates & cand_dates) / len(query_dates)


def _extract_session_hints(text: str) -> list[int]:
    hints = [int(match.group(1) or match.group(2)) for match in _SESSION_RE.finditer(text or "")]
    hints.extend(int(match.group(1)) for match in _DIALOG_RE.finditer(text or ""))
    return hints


def _session_gap_score(query: str, candidate_text_value: str) -> tuple[float, float, float]:
    query_sessions = _extract_session_hints(query)
    candidate_sessions = _extract_session_hints(candidate_text_value)
    if not query_sessions:
        return 0.0, 0.0, 0.0
    if not candidate_sessions:
        return 1.0, 0.0, 0.0
    gap = min(abs(q - c) for q in query_sessions for c in candidate_sessions)
    return 1.0 / (1.0 + gap), 1.0, 1.0


def _role_value_pattern(text: str) -> float:
    return 1.0 if re.search(
        r"\b("
        r"works?\s+(?:as|in|at)|"
        r"is\s+(?:a|an|the)\b|"
        r"owns?\b|owned\s+by|owner\s+(?:is|:)|"
        r"loves?\b|likes?\b|enjoys?\b|"
        r"passionate\s+about|really\s+into|free\s+time|"
        r"originally\s+from|grew\s+up\s+in|hails?\s+from|from\s+[A-Z][A-Za-z]+,\s*[A-Z][A-Za-z]+|"
        r"[\w.+-]+@[\w.-]+|"
        r"(?:phone|contact|number|email)\s+(?:is|address\s+is|number\s+is)?|"
        r"company\s+(?:is|called|named)"
        r")",
        text or "",
        re.IGNORECASE,
    ) else 0.0


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_score(candidate: dict[str, Any]) -> float:
    when = _parse_iso_timestamp(candidate.get("created_at")) or _parse_iso_timestamp(candidate.get("updated_at"))
    if when is None:
        return 0.5
    age_days = max((datetime.now(timezone.utc) - when).total_seconds() / 86400.0, 0.0)
    return 1.0 / (1.0 + age_days / 30.0)


def _tfidf_cosine(query: str, text: str) -> float:
    q_tokens = list(_informative_tokens(query))
    c_tokens = list(_informative_tokens(text))
    if not q_tokens or not c_tokens:
        return 0.0
    docs = [q_tokens, c_tokens]
    vocab = sorted({token for doc in docs for token in doc})
    if not vocab:
        return 0.0
    doc_freq: dict[str, int] = {}
    for token in vocab:
        doc_freq[token] = sum(1 for doc in docs if token in doc)
    n_docs = len(docs)

    def _weights(tokens: Iterable[str]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        if not counts:
            return {}
        max_tf = max(counts.values()) or 1
        weights: dict[str, float] = {}
        for token, count in counts.items():
            tf = count / max_tf
            idf = math.log((1 + n_docs) / (1 + doc_freq[token])) + 1.0
            weights[token] = tf * idf
        return weights

    q_weights = _weights(q_tokens)
    c_weights = _weights(c_tokens)
    dot = sum(q_weights.get(token, 0.0) * c_weights.get(token, 0.0) for token in vocab)
    q_norm = math.sqrt(sum(value * value for value in q_weights.values()))
    c_norm = math.sqrt(sum(value * value for value in c_weights.values()))
    if q_norm == 0.0 or c_norm == 0.0:
        return 0.0
    return dot / (q_norm * c_norm)


def _should_probe_long_context(
    *,
    query: str,
    plan: Any,
    bucket: str,
    text: str,
    position: int,
    current_score: float,
    prev_raw: Any,
    next_raw: Any,
    leader_raw: Any,
) -> bool:
    if bucket != "memories":
        return False

    lowered_query = query or ""
    structured_long_text = (
        len(text) >= 1500
        or "session id:" in text.lower()
        or "session date:" in text.lower()
        or text.count("\n") >= 8
    )
    if not structured_long_text:
        return False

    if position > 4:
        return False

    query_needs_probe = (
        bool(getattr(plan, "requires_temporal_reasoning", False))
        or bool(getattr(plan, "requires_multi_hop", False))
        or bool(getattr(plan, "needs_ordering", False))
        or bool(getattr(plan, "needs_update_resolution", False))
        or bool(getattr(plan, "needs_set_coverage", False))
        or bool(_LONG_CONTEXT_HINT_RE.search(lowered_query))
    )
    if not query_needs_probe:
        return False

    closest_gap_values: list[float] = []
    if prev_raw is not None:
        closest_gap_values.append(abs(current_score - _safe_float(prev_raw)))
    if next_raw is not None:
        closest_gap_values.append(abs(current_score - _safe_float(next_raw)))
    closest_neighbor_gap = min(closest_gap_values) if closest_gap_values else 0.0
    leader_gap = abs(_safe_float(leader_raw, current_score) - current_score)
    return closest_neighbor_gap <= 0.035 and leader_gap <= 0.08


def build_features(
    query: str,
    plan: Any,
    candidate: dict[str, Any],
    *,
    neighbors: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Build numeric features for a candidate row."""

    bucket = str(candidate.get("bucket") or candidate.get("type") or "memories")
    text = candidate_text(candidate)
    query_tokens = _token_set(query)
    query_informative = _informative_tokens(query)
    cand_tokens = _token_set(text)
    cand_informative = _informative_tokens(text)
    query_overlap = len(query_tokens & cand_tokens) / max(len(query_tokens), 1) if query_tokens else 0.0
    informative_overlap = (
        len(query_informative & cand_informative) / max(len(query_informative), 1)
        if query_informative else query_overlap
    )
    exact_phrase = 1.0 if query and len(query.strip()) >= 4 and query.lower().strip() in text.lower() else 0.0
    query_entities = _entity_terms(query) | {term.lower() for term in getattr(plan, "target_entities", []) or []}
    cand_entities = _entity_terms(text)
    entity_overlap = len(query_entities & cand_entities) / max(len(query_entities), 1) if query_entities else 0.0
    aliases = {alias.lower() for alias in _alias_values(candidate) if len(alias) > 2}
    alias_overlap = len(query_entities & aliases) / max(len(query_entities), 1) if query_entities and aliases else 0.0
    role_overlap = 1.0 if (_ROLE_TERMS & query_tokens & cand_tokens) else 0.0
    attribute_overlap = 1.0 if (_ATTRIBUTE_TERMS & query_tokens & cand_tokens) else 0.0
    # Some role/attribute questions ask for a value that is not repeated as a
    # query token in the candidate ("what is Arlo's role" -> "Arlo is the
    # quartermaster"). Let entity-aligned role/key queries activate the value
    # pattern without requiring a synthetic benchmark-style field label.
    value_pattern = _role_value_pattern(text)
    role_value_pattern = max(role_overlap, attribute_overlap) * value_pattern
    if (
        value_pattern > 0.0
        and entity_overlap > 0.0
        and (
            getattr(plan, "needs_role_fact", False)
            or getattr(plan, "needs_synthetic_key_value", False)
        )
    ):
        role_value_pattern = max(role_value_pattern, 1.0)
    query_temporal = 1.0 if (bool(getattr(plan, "requires_temporal_reasoning", False)) or _TEMPORAL_RE.search(query or "")) else 0.0
    candidate_temporal = 1.0 if _TEMPORAL_RE.search(text or "") or _DATE_RE.search(text or "") else 0.0
    temporal_anchor_overlap = _temporal_anchor_overlap(query, text)
    session_gap_score, query_session_hint, candidate_session_hint = _session_gap_score(query, text)
    source_keyword, source_semantic, source_both, source_graph = _source_flags(candidate)
    bucket_memories, bucket_events, bucket_entities, bucket_procedures, bucket_decisions = _bucket_flags(bucket)
    status = str(candidate.get("status") or "").lower()
    position = max(int(candidate.get("_stage_position") or 0), 0)
    prev_raw = (neighbors or {}).get("prev_score")
    next_raw = (neighbors or {}).get("next_score")
    leader_raw = (neighbors or {}).get("leader_score")
    prev_score = _safe_float(prev_raw)
    next_score = _safe_float(next_raw)
    current_score = _safe_float(candidate.get("final_score") or candidate.get("retrieval_score"))
    neighbor_margin = max(current_score - prev_score, current_score - next_score, 0.0)
    confidence = _safe_float(candidate.get("confidence"), 0.5)
    support_evidence_score = min(len(candidate.get("supporting_evidence") or []) / 3.0, 1.0)
    long_context_debug: dict[str, Any] = {"applicable": False}
    if _should_probe_long_context(
        query=query,
        plan=plan,
        bucket=bucket,
        text=text,
        position=position,
        current_score=current_score,
        prev_raw=prev_raw,
        next_raw=next_raw,
        leader_raw=leader_raw,
    ):
        try:
            from agentmemory.retrieval.long_context import analyze_long_context as _analyze_long_context

            long_context_debug = _analyze_long_context(query, plan, candidate, text=text)
        except Exception:
            long_context_debug = {"applicable": False}
    if long_context_debug.get("applicable"):
        candidate["_long_context_debug"] = long_context_debug
    features = {
        "base_score": current_score,
        "retrieval_score": _safe_float(candidate.get("retrieval_score"), current_score),
        "rrf_score": _safe_float(candidate.get("rrf_score")),
        "confidence": confidence,
        "query_overlap": query_overlap,
        "informative_overlap": informative_overlap,
        "tfidf_cosine": _tfidf_cosine(query, text),
        "exact_phrase": exact_phrase,
        "entity_overlap": entity_overlap,
        "alias_overlap": alias_overlap,
        "query_temporal": query_temporal,
        "candidate_temporal": candidate_temporal,
        "temporal_anchor_overlap": temporal_anchor_overlap,
        "query_session_hint": query_session_hint,
        "candidate_session_hint": candidate_session_hint,
        "session_gap_score": session_gap_score,
        "intent_bucket_fit": _intent_bucket_preference(plan, bucket),
        "source_keyword": source_keyword,
        "source_semantic": source_semantic,
        "source_both": source_both,
        "source_graph": source_graph,
        "bucket_memories": bucket_memories,
        "bucket_events": bucket_events,
        "bucket_entities": bucket_entities,
        "bucket_procedures": bucket_procedures,
        "bucket_decisions": bucket_decisions,
        "candidate_age_score": _age_score(candidate),
        "support_evidence_score": support_evidence_score,
        "status_active": 1.0 if status in {"", "active"} else 0.0,
        "status_stale": 1.0 if status in {"stale", "superseded", "retired"} else 0.0,
        "status_needs_review": 1.0 if status == "needs_review" else 0.0,
        "position_score": 1.0 / (1.0 + position),
        "neighbor_margin": neighbor_margin,
        "query_length_score": min(len(query_informative) / 8.0, 1.0),
        "candidate_length_score": min(len(cand_informative) / 64.0, 1.0),
        "procedural_candidate": 1.0 if bucket == "procedures" else 0.0,
        "query_needs_counting": 1.0 if getattr(plan, "needs_counting", False) else 0.0,
        "query_needs_comparison": 1.0 if getattr(plan, "needs_comparison", False) else 0.0,
        "query_needs_ordering": 1.0 if getattr(plan, "needs_ordering", False) else 0.0,
        "query_needs_update_resolution": 1.0 if getattr(plan, "needs_update_resolution", False) else 0.0,
        "query_needs_set_coverage": 1.0 if getattr(plan, "needs_set_coverage", False) else 0.0,
        "query_needs_role_fact": 1.0 if getattr(plan, "needs_role_fact", False) else 0.0,
        "query_needs_synthetic_key_value": 1.0 if getattr(plan, "needs_synthetic_key_value", False) else 0.0,
        "role_overlap": role_overlap,
        "attribute_overlap": attribute_overlap,
        "role_value_pattern": role_value_pattern,
        "query_requires_multi_hop": 1.0 if getattr(plan, "requires_multi_hop", False) else 0.0,
        "long_context_applicable": 1.0 if long_context_debug.get("applicable") else 0.0,
        "long_context_score": _safe_float(long_context_debug.get("score")),
        "long_context_confidence": _safe_float(long_context_debug.get("confidence")),
        "long_context_agreement": _safe_float(long_context_debug.get("agreement")),
        "long_context_uncertainty": _safe_float(long_context_debug.get("uncertainty")),
        "long_context_coverage": _safe_float(long_context_debug.get("coverage")),
        "long_context_precision": _safe_float(long_context_debug.get("precision")),
        "long_context_focused_program": 1.0 if long_context_debug.get("program") not in {None, "", "whole_doc"} else 0.0,
    }
    return {name: round(float(value), 6) for name, value in features.items()}


def vectorize_features(
    feature_dict: dict[str, float],
    *,
    feature_version: str = FEATURE_VERSION_V1,
):
    """Return a numeric feature vector in canonical order."""

    if feature_version != FEATURE_VERSION_V1:
        raise ValueError(f"Unsupported feature version: {feature_version}")
    values = [float(feature_dict.get(name, 0.0)) for name in FEATURE_ORDER_V1]
    if _np is not None:
        return _np.asarray(values, dtype=float)
    return values
