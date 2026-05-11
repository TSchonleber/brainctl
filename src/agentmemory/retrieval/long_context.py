"""Bounded long-context evidence probing for shared retrieval reranking.

RLM/SRLM-inspired adaptation for brainctl:

- Treat the candidate text as an external environment rather than a single bag
  of tokens.
- Run a small portfolio of deterministic chunking "programs" over that
  environment.
- Select the most reliable program using agreement + uncertainty, not just the
  single highest raw score.

This stays local, bounded, and depth-1 on purpose. Reproduction work on RLMs
shows deeper recursion can overthink and blow up latency; here we only probe a
short list of chunk views over the same candidate row.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for",
    "from", "has", "have", "how", "i", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "to", "was", "we", "what", "when", "where", "which", "who",
    "why", "will", "with", "you",
}
_LOW_SIGNAL_TOKENS = {
    "summary", "history", "timeline", "recent", "today", "yesterday", "tomorrow",
    "issue", "problem", "thing", "stuff", "update",
}
_TEMPORAL_RE = re.compile(
    r"\b(yesterday|today|tomorrow|when|before|after|during|timeline|history|recent|latest|first|last)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(r"\bsession[_ :#-]*(\d+)\b", re.IGNORECASE)
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.:-]+\b")
_TURNISH_RE = re.compile(r"^\s*(?:[A-Z][A-Za-z0-9_.-]+:|.+\bsaid,\s+\")", re.IGNORECASE)


@dataclass(slots=True)
class ProbeChunk:
    index: int
    text: str
    score: float
    coverage: float
    precision: float
    entity_overlap: float
    temporal_overlap: float
    exact_phrase: float


@dataclass(slots=True)
class ProbeProgramResult:
    name: str
    score: float
    confidence: float
    uncertainty: float
    agreement: float
    coverage: float
    precision: float
    length_penalty: float
    chunk_count: int
    top_chunk: ProbeChunk | None


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
    return {
        token
        for part in re.split(r"\s+", text or "")
        if (token := _normalize_token(part))
    }


def _informative_tokens(text: str) -> set[str]:
    return {token for token in _token_set(text) if token not in _LOW_SIGNAL_TOKENS}


def _entity_terms(text: str) -> set[str]:
    return {
        match.group(0).lower()
        for match in _ENTITY_RE.finditer(text or "")
        if len(match.group(0)) > 2
    }


def _deobfuscate(text: str) -> str:
    value = text or ""
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"[_*/`~]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _safe_window(items: list[str], size: int, stride: int) -> list[str]:
    if not items:
        return []
    if len(items) <= size:
        return ["\n".join(items)]
    out: list[str] = []
    for start in range(0, len(items), max(stride, 1)):
        chunk = items[start:start + size]
        if not chunk:
            continue
        out.append("\n".join(chunk))
        if start + size >= len(items):
            break
    return out


def _cap_chunks(chunks: list[str], max_chunks: int) -> list[str]:
    if len(chunks) <= max_chunks:
        return chunks
    if max_chunks <= 1:
        return [chunks[0]]
    step = (len(chunks) - 1) / float(max_chunks - 1)
    selected: list[str] = []
    seen: set[int] = set()
    for idx in range(max_chunks):
        pick = int(round(idx * step))
        if pick in seen:
            continue
        seen.add(pick)
        selected.append(chunks[pick])
    return selected


def _whole_doc_program(text: str, max_chunks: int) -> list[str]:
    return [text[:48000]] if text else []


def _line_window_program(text: str, max_chunks: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return []
    return _cap_chunks(_safe_window(lines, size=6, stride=3), max_chunks)


def _sentence_window_program(text: str, max_chunks: int) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    if len(sentences) < 2:
        return []
    return _cap_chunks(_safe_window(sentences, size=3, stride=1), max_chunks)


def _turn_window_program(text: str, max_chunks: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    turnish = [line for line in lines if _TURNISH_RE.search(line)]
    if len(turnish) < 2:
        return []
    return _cap_chunks(_safe_window(turnish, size=4, stride=2), max_chunks)


def _anchor_window_program(
    text: str,
    query: str,
    *,
    target_entities: list[str],
    temporal_query: bool,
    max_chunks: int,
) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    informative = _informative_tokens(query)
    entities = {value.lower() for value in target_entities if value}
    header = []
    if lines[:2] and any("session id" in line.lower() or "session date" in line.lower() for line in lines[:3]):
        header = lines[:3]
    anchor_indexes: list[int] = []
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if informative and any(token in lowered for token in informative):
            anchor_indexes.append(idx)
            continue
        if entities and any(entity in lowered for entity in entities):
            anchor_indexes.append(idx)
            continue
        if temporal_query and (_TEMPORAL_RE.search(line) or _DATE_RE.search(line)):
            anchor_indexes.append(idx)
            continue
    if not anchor_indexes:
        return []
    chunks: list[str] = []
    seen: set[str] = set()
    for idx in anchor_indexes:
        start = max(0, idx - 2)
        end = min(len(lines), idx + 3)
        window = header + lines[start:end]
        chunk = "\n".join(window)
        if chunk and chunk not in seen:
            seen.add(chunk)
            chunks.append(chunk)
    return _cap_chunks(chunks, max_chunks)


def _candidate_programs(
    text: str,
    query: str,
    *,
    target_entities: list[str],
    temporal_query: bool,
    max_chunks: int,
) -> dict[str, list[str]]:
    programs = {
        "whole_doc": _whole_doc_program(text, max_chunks),
        "line_windows": _line_window_program(text, max_chunks),
        "sentence_windows": _sentence_window_program(text, max_chunks),
        "turn_windows": _turn_window_program(text, max_chunks),
        "anchor_windows": _anchor_window_program(
            text,
            query,
            target_entities=target_entities,
            temporal_query=temporal_query,
            max_chunks=max_chunks,
        ),
    }
    return {name: chunks for name, chunks in programs.items() if chunks}


def _chunk_score(
    query: str,
    chunk: str,
    *,
    target_entities: list[str],
    temporal_query: bool,
) -> ProbeChunk:
    informative = _informative_tokens(query)
    query_tokens = _token_set(query)
    chunk_tokens = _token_set(chunk)
    chunk_informative = _informative_tokens(chunk)
    query_entities = _entity_terms(query) | {value.lower() for value in target_entities if value}
    chunk_entities = _entity_terms(chunk)
    overlap = len(query_tokens & chunk_tokens) / max(len(query_tokens), 1) if query_tokens else 0.0
    coverage = len(informative & chunk_informative) / max(len(informative), 1) if informative else overlap
    precision = len(informative & chunk_informative) / max(len(chunk_informative), 1) if chunk_informative else 0.0
    exact_phrase = 1.0 if query and len(query.strip()) >= 4 and query.lower().strip() in chunk.lower() else 0.0
    entity_overlap = len(query_entities & chunk_entities) / max(len(query_entities), 1) if query_entities else 0.0
    temporal_overlap = 0.0
    if temporal_query:
        temporal_overlap = 1.0 if (_TEMPORAL_RE.search(chunk) or _DATE_RE.search(chunk) or _SESSION_RE.search(chunk)) else 0.0
    concentration = min(1.0, 12.0 / max(len(chunk_informative), 12))
    score = (
        coverage * 0.34
        + precision * 0.18
        + overlap * 0.12
        + exact_phrase * 0.14
        + entity_overlap * 0.12
        + temporal_overlap * (0.10 if temporal_query else 0.0)
        + concentration * 0.10
    )
    return ProbeChunk(
        index=0,
        text=chunk,
        score=round(min(score, 1.0), 6),
        coverage=round(coverage, 6),
        precision=round(precision, 6),
        entity_overlap=round(entity_overlap, 6),
        temporal_overlap=round(temporal_overlap, 6),
        exact_phrase=round(exact_phrase, 6),
    )


def _program_signature(chunk: ProbeChunk | None) -> set[str]:
    if chunk is None:
        return set()
    return _informative_tokens(chunk.text)


def _is_focused_program(program: ProbeProgramResult, *, candidate_chars: int) -> bool:
    if program.name == "whole_doc" or program.top_chunk is None or candidate_chars <= 0:
        return False
    span_ratio = len(program.top_chunk.text) / float(candidate_chars)
    return span_ratio < 0.85


def analyze_long_context(
    query: str,
    plan: Any,
    candidate: dict[str, Any],
    *,
    text: str,
) -> dict[str, Any]:
    """Return depth-1 context-program evidence for a long candidate row."""

    if os.environ.get("BRAINCTL_LONG_CONTEXT_PROBES", "1") in {"0", "false", "False"}:
        return {"applicable": False, "reason": "disabled"}

    min_chars = int(os.environ.get("BRAINCTL_LONG_CONTEXT_MIN_CHARS", "900") or "900")
    max_chunks = int(os.environ.get("BRAINCTL_LONG_CONTEXT_MAX_CHUNKS", "24") or "24")
    candidate_text = _deobfuscate(text)
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    structured_session = any(
        "session id" in line.lower() or "session date" in line.lower()
        for line in raw_lines[:4]
    )
    if len(candidate_text) < min_chars and not structured_session and len(raw_lines) < 5:
        return {"applicable": False, "reason": "short_text"}

    target_entities = list(getattr(plan, "target_entities", []) or [])
    temporal_query = bool(getattr(plan, "requires_temporal_reasoning", False)) or bool(_TEMPORAL_RE.search(query or ""))
    programs = _candidate_programs(
        candidate_text,
        query,
        target_entities=target_entities,
        temporal_query=temporal_query,
        max_chunks=max_chunks,
    )
    if not programs:
        return {"applicable": False, "reason": "no_programs"}

    evaluated: list[ProbeProgramResult] = []
    for name, chunks in programs.items():
        scored: list[ProbeChunk] = []
        for index, chunk in enumerate(chunks):
            base = _chunk_score(
                query,
                chunk,
                target_entities=target_entities,
                temporal_query=temporal_query,
            )
            scored.append(
                ProbeChunk(
                    index=index,
                    text=base.text,
                    score=base.score,
                    coverage=base.coverage,
                    precision=base.precision,
                    entity_overlap=base.entity_overlap,
                    temporal_overlap=base.temporal_overlap,
                    exact_phrase=base.exact_phrase,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        top_chunk = scored[0] if scored else None
        second_score = scored[1].score if len(scored) > 1 else 0.0
        coverage = top_chunk.coverage if top_chunk else 0.0
        precision = top_chunk.precision if top_chunk else 0.0
        margin = max((top_chunk.score - second_score) if top_chunk else 0.0, 0.0)
        confidence = min(1.0, coverage * 0.45 + precision * 0.15 + margin * 0.40)
        length_penalty = min(1.0, len(chunks) / max(max_chunks, 1))
        score = min(1.0, (top_chunk.score if top_chunk else 0.0) * 0.82 + coverage * 0.12 + precision * 0.06)
        evaluated.append(
            ProbeProgramResult(
                name=name,
                score=round(score, 6),
                confidence=round(confidence, 6),
                uncertainty=1.0,  # set after agreement pass
                agreement=0.0,
                coverage=round(coverage, 6),
                precision=round(precision, 6),
                length_penalty=round(length_penalty, 6),
                chunk_count=len(chunks),
                top_chunk=top_chunk,
            )
        )

    focused = [program for program in evaluated if _is_focused_program(program, candidate_chars=len(candidate_text))]
    if not focused:
        return {
            "applicable": False,
            "reason": "no_focused_program",
            "program_scores": {
                program.name: {
                    "score": program.score,
                    "confidence": program.confidence,
                    "agreement": program.agreement,
                    "uncertainty": program.uncertainty,
                    "chunk_count": program.chunk_count,
                }
                for program in evaluated
            },
        }

    max_score = max(program.score for program in focused)
    consistent = [program for program in focused if program.score >= max_score - 0.08]
    for program in evaluated:
        sig = _program_signature(program.top_chunk)
        peers = []
        for other in consistent:
            if other is program:
                continue
            other_sig = _program_signature(other.top_chunk)
            if not sig and not other_sig:
                peers.append(1.0)
                continue
            union = len(sig | other_sig)
            if union == 0:
                peers.append(0.0)
            else:
                peers.append(len(sig & other_sig) / union)
        agreement = sum(peers) / len(peers) if peers else (1.0 if len(consistent) == 1 else 0.0)
        program.agreement = round(agreement, 6)
        program.uncertainty = round(
            min(
                1.0,
                (1.0 - agreement) * 0.45
                + (1.0 - program.confidence) * 0.40
                + program.length_penalty * 0.15,
            ),
            6,
        )

    selected = min(
        consistent,
        key=lambda item: (
            round(item.uncertainty, 6),
            -round(item.score, 6),
            -round(item.agreement, 6),
            item.chunk_count,
        ),
    )
    return {
        "applicable": True,
        "program": selected.name,
        "score": selected.score,
        "confidence": selected.confidence,
        "agreement": selected.agreement,
        "uncertainty": selected.uncertainty,
        "coverage": selected.coverage,
        "precision": selected.precision,
        "chunk_count": selected.chunk_count,
        "top_chunk_excerpt": (selected.top_chunk.text[:320] if selected.top_chunk else ""),
        "program_scores": {
            program.name: {
                "score": program.score,
                "confidence": program.confidence,
                "agreement": program.agreement,
                "uncertainty": program.uncertainty,
                "chunk_count": program.chunk_count,
            }
            for program in evaluated
        },
    }
