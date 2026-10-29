"""Dentate gyrus pattern separation + CA3 pattern completion for memory writes.

One write-time decision shared by the CLI ``memory add`` path
(``_impl.cmd_memory_add``) and the MCP ``memory_add`` tool
(``mcp_server.tool_memory_add``), so the two stop diverging.

Before a new memory is inserted, its nearest active neighbours (same agent,
scope and category) are compared to it and one of three things happens:

``reinforce`` (CA3 pattern completion)
    The candidate is near-identical to an existing memory and carries no
    differing key token. Nothing is inserted; the existing memory is
    reinforced instead (confidence bump, ``recalled_count + 1``,
    ``alpha + 1``). Logged as ``deduplicate``.

``separate`` (DG pattern separation)
    The candidate is similar to an existing memory but a *key token*
    differs: a number, a date word, a negation, or a proper noun / known
    entity. "X should never call Y" vs "X should call Y" is the canonical
    case. The candidate is written as its own memory and a ``distinct_from``
    knowledge edge links it to each such neighbour so the two traces stay
    orthogonal instead of completing onto each other.

``passthrough``
    Similar, but neither near-identical nor key-token separated. Written
    as normal (the pre-existing behaviour).

Similarity works with no embedder: a normalised lexical signature
(casefold, ``n't`` -> ``not``, punctuation stripped, whitespace collapsed)
compared by token-set Jaccard, over FTS5 candidates. When the caller has an
embedding blob and a vec connection, cosine similarity is computed for vec
neighbours too and either signal can trigger a decision.

Decisions with a similar neighbour are logged to
``hippocampus_pattern_separations`` (migration 059) when that table exists.

Everything here is non-fatal by contract: any DB error degrades to "no
decision" and the write proceeds exactly as it did before this module.
"""

from __future__ import annotations

import functools
import json
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Thresholds (named so they can be tuned against tests/bench --suite dedup)
# ---------------------------------------------------------------------------

#: Lexical (normalised token-set Jaccard) similarity at or above which a
#: candidate with no key-token difference is reinforced instead of written.
#: Same value as ``_impl.DEDUP_CONTENT_SIMILARITY_THRESHOLD``, the pre-DG
#: CLI dedup threshold, so "similar, no key-token difference" keeps the
#: existing behaviour; the gain comes from normalisation and from applying
#: the gate on the MCP path too.
LEXICAL_REINFORCE_THRESHOLD = 0.85

#: Lexical similarity at or above which two memories count as "similar":
#: a key-token difference then triggers separation (distinct_from edge) and
#: the decision is logged. Below it the write is simply novel.
LEXICAL_SIMILAR_THRESHOLD = 0.6

#: Cosine thresholds, used only when an embedding is available. They match
#: the DG constants in ``mcp_tools_hippocampal_subfields``
#: (_DG_DEDUPE_THRESHOLD / _DG_SEPARATE_THRESHOLD).
COSINE_REINFORCE_THRESHOLD = 0.97
COSINE_SIMILAR_THRESHOLD = 0.85

#: Neighbour budget per write.
FTS_CANDIDATE_LIMIT = 10
VEC_CANDIDATE_LIMIT = 5
FALLBACK_SCAN_LIMIT = 200      # recent-rows scan when FTS5 is unavailable
FTS_QUERY_MAX_TOKENS = 24
MAX_SEPARATION_EDGES = 3

#: Relation written between a separated memory and its similar neighbour.
SEPARATION_RELATION = "distinct_from"

#: Reinforcement (mirrors the pre-DG CLI dedup update, plus a Beta alpha
#: bump: re-encountering the same fact is confirming evidence).
REINFORCE_CONFIDENCE_GAIN = 0.3     # conf += (1 - conf) * gain
REINFORCE_CONFIDENCE_CAP = 0.98
REINFORCE_ALPHA_INCREMENT = 1.0
REINFORCE_AB_CAP = 1000.0           # same as _impl._AB_PRIOR_CAP

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

NEGATION_WORDS = frozenset({
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor",
    "nowhere", "cannot", "without", "dont", "doesnt", "didnt", "isnt",
    "wasnt", "arent", "werent", "wont", "cant", "couldnt", "shouldnt",
    "wouldnt", "mustnt", "neednt", "hasnt", "havent", "hadnt", "aint",
    "forbidden", "prohibited", "disallowed",
})

# Unambiguous date words. "may", "march", "sat", "sun" are left out because
# they are also ordinary words; capitalised mid-sentence they are still
# caught by the proper-noun rule.
DATE_WORDS = frozenset({
    "january", "february", "april", "june", "july", "august", "september",
    "october", "november", "december",
    "jan", "feb", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri",
    "today", "tomorrow", "yesterday", "tonight",
    "weekday", "weekdays", "weekend", "weekends",
})

# Function words never treated as proper nouns even when capitalised.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does",
    "for", "from", "has", "have", "he", "her", "his", "how", "i", "if", "in",
    "is", "it", "its", "my", "of", "on", "or", "our", "she", "so", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "us", "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "you", "your", "all", "any", "each", "every", "only",
})

# Numbers keep their internal separators ("4.2", "9:30", "1,000") so a
# version or a time stays one token; everything else splits on non-word
# characters and underscores.
_TOKEN_RE = re.compile(r"\d+(?:[.,:/]\d+)*|[^\W\d_]+(?:\d+[^\W\d_]*)*|\d+", re.UNICODE)
_CONTRACTION_RE = re.compile(r"n['’]t\b", re.IGNORECASE)
_IRREGULAR_NEG_RE = re.compile(r"\b(ca|wo|sha)n['’]t\b", re.IGNORECASE)
_IRREGULAR_NEG = {"ca": "can", "wo": "will", "sha": "shall"}
_SENTENCE_END = frozenset(".!?;:\n")


# ---------------------------------------------------------------------------
# Signature + key tokens (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Tok:
    norm: str
    surface: str
    initial: bool       # first token of a sentence


def _tokens(text: str) -> List[_Tok]:
    text = _IRREGULAR_NEG_RE.sub(
        lambda m: _IRREGULAR_NEG[m.group(1).lower()] + " not", text or "")
    text = _CONTRACTION_RE.sub(" not", text)
    out: List[_Tok] = []
    prev_end = 0
    for m in _TOKEN_RE.finditer(text):
        gap = text[prev_end:m.start()]
        initial = not out or any(ch in _SENTENCE_END for ch in gap)
        out.append(_Tok(m.group(0).casefold(), m.group(0), initial))
        prev_end = m.end()
    return out


def normalize(text: str) -> str:
    """Normalised lexical signature: casefold, ``n't`` -> ``not``, punctuation
    stripped, whitespace collapsed."""
    return " ".join(t.norm for t in _tokens(text))


@functools.lru_cache(maxsize=1024)
def token_set(text: str) -> FrozenSet[str]:
    # Cached: one write compares its content against up to ~15 neighbours,
    # and a bulk ingest re-reads the same neighbours write after write.
    return frozenset(t.norm for t in _tokens(text))


def jaccard(a: str, b: str) -> float:
    """Token-set Jaccard over the normalised signatures of ``a`` and ``b``."""
    sa, sb = token_set(a), token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _mostly_upper(toks: Sequence[_Tok]) -> bool:
    alpha = [t.surface for t in toks if t.surface.isalpha() and len(t.surface) > 1]
    return bool(alpha) and sum(1 for s in alpha if s.isupper()) * 2 > len(alpha)


def _proper_nouns(toks: Sequence[_Tok]) -> Set[str]:
    """Casefolded tokens that look like proper nouns in their own text:
    capitalised (or internally capitalised, e.g. ``LaunchDarkly``, ``iOS``)
    and not sentence-initial. Shouting (mostly-uppercase text) carries no
    capitalisation signal."""
    if _mostly_upper(toks):
        return set()
    out: Set[str] = set()
    for t in toks:
        if t.norm in _STOPWORDS or not t.surface[:1].isalpha():
            continue
        has_upper = any(ch.isupper() for ch in t.surface)
        if not has_upper:
            continue
        inner_upper = any(ch.isupper() for ch in t.surface[1:])
        if t.initial and not inner_upper:
            continue
        out.add(t.norm)
    return out


def key_token_class(token: str, proper: Set[str], entities: Set[str]) -> Optional[str]:
    """Return the key-token class of a normalised token, or None."""
    if any(ch.isdigit() for ch in token):
        return "number"
    if token in NEGATION_WORDS:
        return "negation"
    if token in DATE_WORDS:
        return "date"
    if token in proper or token in entities:
        return "entity"
    return None


def key_token_diff(a: str, b: str,
                   entity_names: Iterable[str] = ()) -> Dict[str, List[str]]:
    """Key tokens present in exactly one of ``a`` / ``b``, grouped by class.

    Only the symmetric difference of the two normalised token sets is
    inspected, so pure surface changes (case, punctuation, whitespace,
    SHOUTING) never count. An empty dict means "no key-token difference".
    """
    ta, tb = _tokens(a), _tokens(b)
    sa, sb = {t.norm for t in ta}, {t.norm for t in tb}
    entities = {e.casefold() for e in entity_names if e}
    pa, pb = _proper_nouns(ta), _proper_nouns(tb)
    # Sentence-initial capitals carry no proper-noun signal on their own,
    # but a swapped *subject* ("Alice is the on-call engineer" vs "Bob is
    # the on-call engineer") does: both texts open with a different
    # capitalised content word.
    if ta and tb and ta[0].norm != tb[0].norm and not (_mostly_upper(ta) or _mostly_upper(tb)):
        heads = (ta[0], tb[0])
        if all(h.surface[:1].isupper() and h.norm not in _STOPWORDS for h in heads):
            pa.add(ta[0].norm)
            pb.add(tb[0].norm)
    diff: Dict[str, List[str]] = {}
    for tok in sorted(sa ^ sb):
        cls = key_token_class(tok, pa if tok in sa else pb, entities)
        if cls:
            diff.setdefault(cls, []).append(tok)
    return diff


def format_key_diff(diff: Dict[str, List[str]], limit: int = 200) -> str:
    s = ";".join(f"{cls}:{','.join(toks)}" for cls, toks in sorted(diff.items()))
    return s[:limit]


# ---------------------------------------------------------------------------
# Three-way decision (pure)
# ---------------------------------------------------------------------------

REINFORCE = "reinforce"
SEPARATE = "separate"
PASSTHROUGH = "passthrough"
NOVEL = "novel"

# hippocampus_pattern_separations.decision CHECK vocabulary
_LOG_DECISION = {REINFORCE: "deduplicate", SEPARATE: "separate", PASSTHROUGH: "passthrough"}


@dataclass
class Neighbor:
    id: int
    content: str
    confidence: float = 1.0
    lexical: float = 0.0
    cosine: Optional[float] = None
    key_diff: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def similarity(self) -> float:
        return max(self.lexical, self.cosine if self.cosine is not None else 0.0)

    @property
    def method(self) -> str:
        if self.cosine is not None and self.cosine >= self.lexical:
            return "cosine"
        return "lexical"

    def near_identical(self) -> bool:
        return (self.lexical >= LEXICAL_REINFORCE_THRESHOLD
                or (self.cosine is not None and self.cosine >= COSINE_REINFORCE_THRESHOLD))

    def similar(self) -> bool:
        return (self.lexical >= LEXICAL_SIMILAR_THRESHOLD
                or (self.cosine is not None and self.cosine >= COSINE_SIMILAR_THRESHOLD))


@dataclass
class Decision:
    action: str                                   # reinforce|separate|passthrough|novel
    neighbor: Optional[Neighbor] = None           # closest similar neighbour
    separated_from: List[Neighbor] = field(default_factory=list)

    @property
    def similarity(self) -> float:
        return round(self.neighbor.similarity, 4) if self.neighbor else 0.0

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"decision": self.action}
        if self.neighbor is not None:
            out.update({
                "neighbor_id": self.neighbor.id,
                "similarity": self.similarity,
                "method": self.neighbor.method,
            })
            if self.neighbor.key_diff:
                out["key_token_diff"] = self.neighbor.key_diff
        if self.separated_from:
            out["distinct_from"] = [n.id for n in self.separated_from]
        return out


def decide(content: str, neighbors: Sequence[Neighbor],
           entity_names: Iterable[str] = ()) -> Decision:
    """Three-way DG/CA3 decision for ``content`` against scored neighbours.

    Fills in each neighbour's ``lexical`` similarity and ``key_diff``
    (``cosine`` is supplied by the caller when embeddings exist).
    """
    entity_names = list(entity_names)
    similar: List[Neighbor] = []
    for n in neighbors:
        n.lexical = jaccard(content, n.content)
        if not n.similar():
            continue
        n.key_diff = key_token_diff(content, n.content, entity_names)
        similar.append(n)
    if not similar:
        return Decision(NOVEL)
    similar.sort(key=lambda n: (n.similarity, n.lexical), reverse=True)

    completable = [n for n in similar if n.near_identical() and not n.key_diff]
    if completable:
        return Decision(REINFORCE, neighbor=completable[0])
    separated = [n for n in similar if n.key_diff]
    if separated:
        return Decision(SEPARATE, neighbor=separated[0],
                        separated_from=separated[:MAX_SEPARATION_EDGES])
    return Decision(PASSTHROUGH, neighbor=similar[0])


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------

def _row_get(row: Any, key: str, idx: int) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[idx]


def _fts_query(content: str) -> str:
    seen: List[str] = []
    for t in _tokens(content):
        tok = t.norm
        if tok in seen or (tok in _STOPWORDS and tok not in NEGATION_WORDS):
            continue
        if not any(ch.isalnum() for ch in tok):
            continue
        seen.append(tok)
        if len(seen) >= FTS_QUERY_MAX_TOKENS:
            break
    return " OR ".join('"' + tok.replace('"', "") + '"' for tok in seen)


_CAND_SELECT = "SELECT m.id, m.content, m.confidence FROM memories m "
_CAND_FILTER = ("m.agent_id = ? AND m.scope = ? AND m.category = ? "
                "AND m.retired_at IS NULL")


def find_neighbors(db, content: str, agent_id: str, scope: str, category: str,
                   blob: Optional[bytes] = None,
                   get_vec_db: Optional[Callable[[], Any]] = None) -> List[Neighbor]:
    """Active memories in the same agent / scope / category that could be
    the same trace as ``content``: FTS5 bm25 candidates, plus vec neighbours
    (with cosine) when ``blob`` and ``get_vec_db`` are given."""
    found: Dict[int, Neighbor] = {}
    params = (agent_id, scope or "global", category)
    q = _fts_query(content)
    rows: List[Any] = []
    fts_ok = False
    if q:
        try:
            rows = db.execute(
                _CAND_SELECT + "JOIN memories_fts f ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? AND " + _CAND_FILTER +
                " ORDER BY f.rank LIMIT ?",
                (q, *params, FTS_CANDIDATE_LIMIT),
            ).fetchall()
            fts_ok = True
        except Exception:
            rows = []
    if not fts_ok:
        try:
            rows = db.execute(
                _CAND_SELECT + "WHERE " + _CAND_FILTER + " ORDER BY m.id DESC LIMIT ?",
                (*params, FALLBACK_SCAN_LIMIT),
            ).fetchall()
        except Exception:
            rows = []
    for r in rows:
        mid = int(_row_get(r, "id", 0))
        found[mid] = Neighbor(mid, _row_get(r, "content", 1) or "",
                              float(_row_get(r, "confidence", 2) or 0.0))

    if blob and get_vec_db is not None:
        try:
            _add_vec_neighbors(db, found, blob, get_vec_db, params)
        except Exception:
            pass
    return list(found.values())


def _unpack(blob: bytes) -> List[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob[:n * 4]))


def _cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return None
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)) / (na * nb)))


def _add_vec_neighbors(db, found: Dict[int, Neighbor], blob: bytes,
                       get_vec_db: Callable[[], Any], params: Tuple) -> None:
    vdb = get_vec_db()
    if not vdb:
        return
    try:
        cand = _unpack(blob)
        hits = vdb.execute(
            "SELECT rowid FROM vec_memories WHERE embedding MATCH ? AND k=?",
            (blob, VEC_CANDIDATE_LIMIT),
        ).fetchall()
        ids = {int(_row_get(h, "rowid", 0)) for h in hits} | set(found)
        for mid in ids:
            e = vdb.execute(
                "SELECT vector FROM embeddings WHERE source_table='memories' AND source_id=?",
                (mid,),
            ).fetchone()
            if not e:
                continue
            cos = _cosine(cand, _unpack(bytes(_row_get(e, "vector", 0))))
            if cos is None:
                continue
            if mid not in found:
                r = db.execute(_CAND_SELECT + "WHERE m.id = ? AND " + _CAND_FILTER,
                               (mid, *params)).fetchone()
                if not r:
                    continue
                found[mid] = Neighbor(mid, _row_get(r, "content", 1) or "",
                                      float(_row_get(r, "confidence", 2) or 0.0))
            found[mid].cosine = cos
    finally:
        try:
            vdb.close()
        except Exception:
            pass


ENTITY_LOOKUP_MAX_TOKENS = 500


def _entity_names(db, texts: Iterable[str]) -> List[str]:
    """Single-token known entity names that occur in any of ``texts``.

    One bounded query per write: the candidate tokens are
    matched against ``lower(entities.name)`` in SQL rather than scanning
    the whole entities table in Python."""
    toks: Set[str] = set()
    for t in texts:
        toks |= token_set(t)
    toks = {t for t in toks if len(t) > 2 and t not in _STOPWORDS}
    if not toks:
        return []
    toks_l = sorted(toks)[:ENTITY_LOOKUP_MAX_TOKENS]
    try:
        rows = db.execute(
            "SELECT DISTINCT lower(name) FROM entities WHERE retired_at IS NULL "
            "AND lower(name) IN (" + ",".join("?" * len(toks_l)) + ")",
            toks_l,
        ).fetchall()
    except Exception:
        return []
    return [_row_get(r, "lower(name)", 0) for r in rows]


def evaluate(db, content: str, agent_id: str, scope: str, category: str,
             blob: Optional[bytes] = None,
             get_vec_db: Optional[Callable[[], Any]] = None) -> Decision:
    """Find neighbours and decide. Never raises: errors -> ``novel``."""
    if not agent_id or not (content or "").strip():
        return Decision(NOVEL)
    try:
        neighbors = find_neighbors(db, content, agent_id, scope, category,
                                   blob=blob, get_vec_db=get_vec_db)
        if not neighbors:
            return Decision(NOVEL)
        entity_names = _entity_names(db, [content] + [n.content for n in neighbors])
        return decide(content, neighbors, entity_names)
    except Exception:
        return Decision(NOVEL)


def reinforce(db, decision: Decision, now: str) -> Optional[float]:
    """CA3 completion: strengthen the existing trace. Returns the new
    confidence. Does not commit."""
    n = decision.neighbor
    if n is None:
        return None
    new_conf = min(REINFORCE_CONFIDENCE_CAP,
                   n.confidence + (1.0 - n.confidence) * REINFORCE_CONFIDENCE_GAIN)
    db.execute(
        "UPDATE memories SET confidence = ?, recalled_count = recalled_count + 1, "
        "alpha = CASE WHEN COALESCE(alpha, 1.0) + COALESCE(beta, 1.0) >= ? "
        "             THEN COALESCE(alpha, 1.0) "
        "             ELSE COALESCE(alpha, 1.0) + ? END, "
        "last_recalled_at = ?, updated_at = ? WHERE id = ?",
        (new_conf, REINFORCE_AB_CAP, REINFORCE_ALPHA_INCREMENT, now, now, n.id),
    )
    return new_conf


def link_separated(db, memory_id: int, decision: Decision, agent_id: str,
                   now: str) -> List[int]:
    """DG separation: ``distinct_from`` edges from the new memory to each
    similar-but-different neighbour. Does not commit."""
    linked: List[int] = []
    for n in decision.separated_from:
        try:
            db.execute(
                "INSERT INTO knowledge_edges (source_table, source_id, target_table, "
                "target_id, relation_type, weight, agent_id, created_at) "
                "VALUES ('memories', ?, 'memories', ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_table, source_id, target_table, target_id, relation_type) "
                "DO NOTHING",
                (memory_id, n.id, SEPARATION_RELATION,
                 round(max(0.0, min(1.0, n.similarity)), 4), agent_id, now),
            )
            linked.append(n.id)
        except Exception:
            continue
    return linked


def log_decision(db, decision: Decision, memory_id: Optional[int],
                 agent_id: str, scope: str) -> None:
    """Audit row in hippocampus_pattern_separations (skipped when the table
    is missing or the write had no similar neighbour). Does not commit."""
    if decision.neighbor is None or decision.action not in _LOG_DECISION:
        return
    tag = format_key_diff(decision.neighbor.key_diff) if decision.action == SEPARATE else None
    try:
        db.execute(
            "INSERT INTO hippocampus_pattern_separations (memory_id, nearest_neighbor_id, "
            "cosine_distance, decision, separation_tag, scope, agent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, decision.neighbor.id, round(1.0 - decision.neighbor.similarity, 4),
             _LOG_DECISION[decision.action], tag, scope or "global", agent_id),
        )
    except Exception:
        pass


def after_insert(db, memory_id: int, decision: Decision, agent_id: str,
                 scope: str, now: str) -> Dict[str, Any]:
    """Post-INSERT bookkeeping for separate / passthrough decisions.
    Commits. Returns the summary dict for the write payload (empty for
    ``novel``)."""
    if decision.action == NOVEL:
        return {}
    try:
        if decision.action == SEPARATE:
            link_separated(db, memory_id, decision, agent_id, now)
        log_decision(db, decision, memory_id, agent_id, scope)
        db.commit()
    except Exception:
        pass
    return decision.summary()


def apply_reinforce(db, decision: Decision, agent_id: str, scope: str,
                    now: str) -> Dict[str, Any]:
    """Reinforce + log + commit. Returns the write payload for the caller."""
    new_conf = reinforce(db, decision, now)
    log_decision(db, decision, None, agent_id, scope)
    db.commit()
    return {
        "ok": True,
        "deduplicated": True,
        "reinforced_memory_id": decision.neighbor.id,
        "similarity": decision.similarity,
        "new_confidence": round(new_conf, 4) if new_conf is not None else None,
        "pattern_separation": decision.summary(),
    }


__all__ = [
    "COSINE_REINFORCE_THRESHOLD", "COSINE_SIMILAR_THRESHOLD",
    "LEXICAL_REINFORCE_THRESHOLD", "LEXICAL_SIMILAR_THRESHOLD",
    "SEPARATION_RELATION", "REINFORCE", "SEPARATE", "PASSTHROUGH", "NOVEL",
    "Decision", "Neighbor", "normalize", "token_set", "jaccard",
    "key_token_diff", "decide", "find_neighbors", "evaluate", "reinforce",
    "link_separated", "log_decision", "after_insert", "apply_reinforce",
]
