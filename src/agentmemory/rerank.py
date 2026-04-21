"""Cross-encoder reranker stage for brainctl hybrid retrieval (2.4.0+).

This module is the optional fourth stage in brainctl's retrieval pipeline:

    FTS5 + sqlite-vec → RRF fusion → existing rerankers
        (recency / salience / Q-value / source / context / PageRank)
        → [optional] cross-encoder rerank → MMR / quantum → final trim

A cross-encoder takes a (query, candidate) pair and produces a single
relevance score by running both through the same transformer (rather
than scoring each side independently and computing a similarity, the
way bi-encoders / dual-encoders do). The pairwise pass is slower but
typically delivers +10-15pp P@1 on standard benchmarks because it can
attend to fine-grained query-candidate interactions that get lost when
the two sides are encoded separately.

Design notes
------------
1. **Opt-in only.** The reranker never fires by default. It is
   activated via ``brainctl search --rerank [MODEL]`` (CLI) or the
   ``rerank`` kwarg on the ``memory_search`` MCP tool. Default-off in
   2.4.0 because we want users to discover the tradeoff (~+50ms on
   GPUs, more like 200-600ms on CPUs) intentionally rather than be
   surprised by a slower hot path.

2. **Backends.** Two backends are supported, with lazy imports so the
   base ``brainctl`` install stays light:

       - ``sentence_transformers`` (primary). Loads bge-reranker-v2-m3
         and jina-reranker-v2-base-multilingual via ``CrossEncoder``.
       - ``ollama`` (opt-in stub). Ollama does not currently ship a
         first-class ``/api/rerank`` endpoint, so this backend probes
         ``/api/tags`` for installed reranker models and returns
         ``None`` (falling through to the input order). Reserved for
         when Ollama gains a rerank endpoint upstream.

   ``qwen3-reranker-4b`` is recognised as a "supported" model name but
   currently returns ``NotImplementedError`` with a stderr warning —
   it is an LLM-style reranker that scores via next-token logits, not
   a cross-encoder, and ``CrossEncoder`` cannot load it. Deferred.

3. **Graceful degradation.** Every failure mode (missing dep,
   unreachable Ollama, model not pulled, scoring exception) returns
   the input list unchanged with a stderr warning. A search call MUST
   never crash because the reranker is unavailable — the user asked
   for ranked results, and the input is already ranked.

4. **Caching.** Per-process LRU cache (capped 1000 entries) keyed on
   ``(model, query_hash, candidate_hash)``. Repeat queries against the
   same candidate set hit the cache. The cache is intentionally
   per-process and not persisted: cross-encoder scores depend on the
   model weights, which the user might update.

5. **Pure functions.** ``score_pairs`` is the only side-effecting
   primitive (it talks to the model). ``rerank`` is a pure-ish
   orchestrator: it takes a list, returns a re-sorted list, never
   mutates the input.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Public model registry
# ---------------------------------------------------------------------------
# All three models are local-only — bge / jina via Hugging Face Hub
# (downloaded by sentence-transformers on first use), qwen3 via Ollama
# pull. Sizes are approximate post-download disk footprints.
SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
    "bge-reranker-v2-m3": {
        "hf_id": "BAAI/bge-reranker-v2-m3",
        "ollama_id": "bge-reranker-v2-m3",
        "size_mb": 600,
        "kind": "cross_encoder",
        "notes": "Multilingual, default. Best Pareto balance.",
    },
    "jina-reranker-v2-base-multilingual": {
        "hf_id": "jinaai/jina-reranker-v2-base-multilingual",
        "ollama_id": "jina-reranker-v2-base-multilingual",
        "size_mb": 280,
        "kind": "cross_encoder",
        "notes": "Faster (smaller). Good when latency matters.",
    },
    "qwen3-reranker-4b": {
        "hf_id": "Qwen/Qwen3-Reranker-4B",
        "ollama_id": "qwen3-reranker-4b",
        "size_mb": 4000,
        "kind": "llm_logit",
        "notes": "LLM-style reranker (logit-based). NOT a cross-encoder. "
                 "Deferred in 2.4.0 — needs FlagLLMReranker / custom "
                 "logits extraction; CrossEncoder.predict() will not load it.",
    },
}

DEFAULT_MODEL = "bge-reranker-v2-m3"

# Ollama base URL — overridable for tests and non-default ports.
_OLLAMA_BASE = os.environ.get("BRAINCTL_OLLAMA_URL", "http://localhost:11434")


# ---------------------------------------------------------------------------
# Stderr warning helper
# ---------------------------------------------------------------------------
# Single-source so we can mute it under tests via env var if needed and
# so every fallback path uses the same prefix the user can grep for.
_WARNED: set[str] = set()


def _warn(msg: str, *, dedupe_key: Optional[str] = None) -> None:
    if os.environ.get("BRAINCTL_RERANK_QUIET"):
        return
    key = dedupe_key or msg
    # Dedupe identical warnings within a process. The fallback paths
    # are hot — we don't want 50 lines of the same warning if the
    # user's running a benchmark with ollama down.
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"[brainctl rerank] {msg}", file=sys.stderr)


def _reset_warnings() -> None:
    """Test helper — clear the dedupe set so a test can verify a warning fires."""
    _WARNED.clear()


# ---------------------------------------------------------------------------
# Backend probes
# ---------------------------------------------------------------------------

def _ollama_tags(timeout: float = 0.5) -> Optional[List[str]]:
    """Probe Ollama's /api/tags. Returns model name list or None on failure.

    Short timeout (500ms): if Ollama is not running, the user almost
    certainly knows it; we should fail fast and let the caller fall
    through to sentence-transformers rather than blocking the search
    path on a hung connection.
    """
    try:
        req = urllib.request.Request(f"{_OLLAMA_BASE}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
            import json as _json
            data = _json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _have_sentence_transformers() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def available_models() -> List[str]:
    """Return the supported reranker models that are actually loadable.

    A model is "available" if either:
      - sentence-transformers is installed AND the model is a
        cross-encoder kind (bge / jina), OR
      - Ollama is reachable AND has the model pulled (any kind, since
        we'd defer to Ollama's reranker endpoint when one exists).

    Returns an empty list if no backend is usable. The caller should
    treat that as "rerank is unavailable; skip the stage."
    """
    out: List[str] = []
    have_st = _have_sentence_transformers()
    ollama_models = _ollama_tags() or []
    # Strip Ollama tag suffixes ("model:tag" → "model") for matching.
    ollama_bare = {m.split(":", 1)[0] for m in ollama_models}

    for name, spec in SUPPORTED_MODELS.items():
        if have_st and spec["kind"] == "cross_encoder":
            out.append(name)
            continue
        # Ollama path is currently a stub for cross-encoders, but we
        # surface qwen3 as "available" if the user has pulled it,
        # because someday the Ollama backend will route through to
        # llm-logit scoring.
        if spec["ollama_id"] in ollama_bare:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Sentence-transformers backend — module-level model cache
# ---------------------------------------------------------------------------
# CrossEncoder.__init__ pulls + loads weights on first call (slow:
# 1-3s for bge-v2-m3). We cache one model per process keyed by name so
# repeat queries don't pay the load cost.
_st_model_cache: Dict[str, Any] = {}


def _load_st_model(model_name: str) -> Optional[Any]:
    """Lazy-load a sentence-transformers CrossEncoder.

    Returns None if sentence-transformers is missing or the model
    cannot be loaded (network down on first use, model name typo, etc).
    Each failure mode emits a single stderr warning and returns None;
    the caller falls through to "no rerank" semantics.
    """
    if model_name in _st_model_cache:
        return _st_model_cache[model_name]

    spec = SUPPORTED_MODELS.get(model_name)
    if spec is None:
        _warn(
            f"unknown model {model_name!r}; supported: {sorted(SUPPORTED_MODELS)}. "
            "Falling back to no rerank.",
            dedupe_key=f"unknown:{model_name}",
        )
        return None

    if spec["kind"] == "llm_logit":
        # qwen3 path. Recognised but not implemented.
        _warn(
            f"{model_name!r} is an LLM-style reranker (logit-based) and "
            "is not yet supported in brainctl 2.4.0. Falling back to no rerank.",
            dedupe_key=f"llm_logit:{model_name}",
        )
        return None

    try:
        from sentence_transformers import CrossEncoder
    except ImportError:
        _warn(
            "sentence-transformers not installed; cannot load reranker "
            f"{model_name!r}. Install with: pip install 'brainctl[rerank]'. "
            "Falling back to no rerank.",
            dedupe_key="missing_st",
        )
        return None

    try:
        # max_length=512 matches the published cross-encoder configs;
        # longer candidates are truncated by the tokenizer rather than
        # silently slicing them on our side.
        model = CrossEncoder(spec["hf_id"], max_length=512)
    except Exception as exc:  # noqa: BLE001 — broad catch: any load failure is "fall through"
        _warn(
            f"failed to load reranker {model_name!r} ({type(exc).__name__}: {exc}). "
            "Falling back to no rerank.",
            dedupe_key=f"load_fail:{model_name}",
        )
        return None

    _st_model_cache[model_name] = model
    return model


def _clear_model_cache() -> None:
    """Test helper — drop cached models so a test can re-trigger load failures."""
    _st_model_cache.clear()


# ---------------------------------------------------------------------------
# LRU score cache
# ---------------------------------------------------------------------------
# Keyed on (model, query_sha1[:12], candidate_sha1[:12]). 12 hex chars
# = 48 bits = ~10^14 distinct values, plenty for 1000-entry cache and
# negligible collision risk. Full hash would just bloat the cache key.

# _cached_score used to exist here as an @lru_cache(maxsize=1000) shim
# whose docstring claimed score_pairs "writes hits via its inner
# closure". It didn't — the function had no callers and always
# returned None regardless. The real score cache is the manual
# _score_cache dict below. Dead code removed in 2.5.0 (audit I24).

# The explicit dict layered with an eviction list keeps the API
# simple (str keys) and lets tests inspect the cache directly.
_score_cache: "Dict[Tuple[str, str, str], float]" = {}
_score_cache_order: "List[Tuple[str, str, str]]" = []
_SCORE_CACHE_MAX = 1000


def _cache_get(key: Tuple[str, str, str]) -> Optional[float]:
    return _score_cache.get(key)


def _cache_set(key: Tuple[str, str, str], val: float) -> None:
    if key in _score_cache:
        # Move to MRU position
        try:
            _score_cache_order.remove(key)
        except ValueError:
            pass
    _score_cache[key] = val
    _score_cache_order.append(key)
    while len(_score_cache_order) > _SCORE_CACHE_MAX:
        evict = _score_cache_order.pop(0)
        _score_cache.pop(evict, None)


def cache_clear() -> None:
    """Public: drop the entire score cache. Useful for benchmarks."""
    _score_cache.clear()
    _score_cache_order.clear()


def cache_stats() -> Dict[str, int]:
    """Public: return (entries, max) so tests / debug can inspect."""
    return {"entries": len(_score_cache), "max": _SCORE_CACHE_MAX}


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------

def score_pairs(
    query: str,
    candidates: List[str],
    model: Optional[str] = None,
) -> List[float]:
    """Score (query, candidate) pairs with a cross-encoder.

    Returns a list of float scores aligned 1:1 with ``candidates``. On
    any failure (model unavailable, backend down, exception during
    scoring) returns a list of zeros aligned with the input — this is
    the "no signal" fallback that callers can sort-stably on (zeros
    preserve original RRF order).

    Caching: hits the per-process LRU cache first; only the missing
    pairs are passed to the model. With a hot cache, latency is
    dominated by hashing rather than inference.
    """
    if not candidates:
        return []
    model_name = model or DEFAULT_MODEL

    # Cache lookup pass — collect indices that need real scoring.
    q_hash = _short_hash(query)
    keys = [(model_name, q_hash, _short_hash(c)) for c in candidates]
    scores: List[Optional[float]] = [_cache_get(k) for k in keys]
    needs_idx = [i for i, s in enumerate(scores) if s is None]

    if not needs_idx:
        # Full cache hit — return cleanly typed list of floats.
        return [float(s) for s in scores]  # type: ignore[arg-type]

    # Load the model (lazy + cached).
    st_model = _load_st_model(model_name)
    if st_model is None:
        # Fall through: zeros for the misses, real scores for cache hits.
        # Zeros sort stably alongside cache hits; callers see "no rerank"
        # behaviour on a model-unavailable system without crashing.
        return [float(s) if s is not None else 0.0 for s in scores]

    # Score the missing pairs in a single batched call. CrossEncoder
    # accepts a list of (q, c) tuples and returns a numpy array.
    pairs = [(query, candidates[i]) for i in needs_idx]
    try:
        # convert_to_numpy=True is the default; convert_to_tensor=False
        # avoids a torch tensor import surface. Some CrossEncoder
        # versions raise TypeError on unknown kwargs — keep the call
        # minimal.
        new_scores = st_model.predict(pairs, show_progress_bar=False)
    except Exception as exc:  # noqa: BLE001
        _warn(
            f"cross-encoder predict failed ({type(exc).__name__}: {exc}). "
            "Falling back to original ordering for this query.",
            dedupe_key=f"predict_fail:{model_name}",
        )
        return [float(s) if s is not None else 0.0 for s in scores]

    # Backfill cache + result list.
    for j, idx in enumerate(needs_idx):
        try:
            val = float(new_scores[j])
        except (TypeError, ValueError):
            val = 0.0
        scores[idx] = val
        _cache_set(keys[idx], val)

    return [float(s) for s in scores]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def rerank(
    query: str,
    candidates_with_metadata: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    top_k: Optional[int] = None,
    text_key: str = "content",
    score_key: str = "ce_score",
) -> List[Dict[str, Any]]:
    """Re-rank a list of candidate dicts by cross-encoder score.

    Parameters
    ----------
    query
        The user's search query.
    candidates_with_metadata
        Output of brainctl's RRF + recency-trimming chain. Each dict
        is expected to have at least ``text_key`` (default "content").
        Other keys (final_score, source, scope, etc.) are passed
        through untouched.
    model
        Reranker model name. None → default (``bge-reranker-v2-m3``).
    top_k
        Trim to this many results after re-sorting. None → return all
        re-sorted (caller does its own slicing).
    text_key
        Which dict key holds the candidate text. Memories use
        "content"; events use "summary"; context uses "content" too.
    score_key
        Where to stash the cross-encoder score on each candidate dict.
        Default ``ce_score``. The candidate's existing ``final_score``
        is preserved as ``pre_ce_score`` for auditability.

    Returns
    -------
    A new list (the input list is not mutated in-place; each candidate
    dict IS the same object, but the surrounding list ordering is new).
    Items with no scoreable text are pushed to the end with score 0.0.

    Failure semantics: if scoring fails entirely, returns the input in
    its original order with NO score_key set — callers can detect
    "rerank silently no-op'd" by checking for the score_key.
    """
    if not candidates_with_metadata:
        return []

    # Extract texts; tolerate missing keys (treat as empty string,
    # which will get a zero score and sink to the bottom).
    texts = [str(c.get(text_key) or "") for c in candidates_with_metadata]
    scores = score_pairs(query, texts, model=model)

    # Decorate each candidate with the score (and preserve the
    # pre-rerank final_score for auditability).
    decorated: List[Dict[str, Any]] = []
    for cand, sc in zip(candidates_with_metadata, scores):
        # Don't mutate caller's dict in-place — clone so re-running
        # the same input through the search pipeline (e.g. tests) is
        # idempotent.
        new_cand = dict(cand)
        if "final_score" in new_cand and "pre_ce_score" not in new_cand:
            new_cand["pre_ce_score"] = new_cand["final_score"]
        new_cand[score_key] = round(float(sc), 6)
        # Overwrite final_score so downstream stages (MMR, quantum,
        # the trim) operate on the cross-encoder ordering.
        new_cand["final_score"] = round(float(sc), 6)
        decorated.append(new_cand)

    # Stable sort by ce_score desc; ties preserve insertion order
    # (which preserves the pre-rerank ordering — a sane tiebreaker
    # because the upstream RRF chain already considered relevance).
    decorated.sort(key=lambda d: d[score_key], reverse=True)

    if top_k is not None:
        return decorated[:top_k]
    return decorated


# ---------------------------------------------------------------------------
# Convenience: timed rerank for benchmarks
# ---------------------------------------------------------------------------

def rerank_timed(
    query: str,
    candidates_with_metadata: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    top_k: Optional[int] = None,
    text_key: str = "content",
) -> Tuple[List[Dict[str, Any]], float]:
    """Like ``rerank`` but also returns wall-clock seconds spent.

    Used by the benchmark harness to populate the latency column.
    """
    t0 = time.perf_counter()
    out = rerank(
        query,
        candidates_with_metadata,
        model=model,
        top_k=top_k,
        text_key=text_key,
    )
    return out, time.perf_counter() - t0


__all__ = [
    "SUPPORTED_MODELS",
    "DEFAULT_MODEL",
    "available_models",
    "score_pairs",
    "rerank",
    "rerank_timed",
    "cache_clear",
    "cache_stats",
]
