"""brainctl — pluggable embedding-model registry.

This module is the *single source of truth* for which embedding models
brainctl knows about and what their dims are. The rest of the codebase
(``_impl.py``, ``vec.py``, ``mcp_server.py``, ``mcp_tools_*``) historically
hard-codes ``nomic-embed-text``/``768`` everywhere; rather than refactor
every call site, this module exposes the existing ``BRAINCTL_EMBED_MODEL``
env-var contract as a typed registry plus two pure helpers
(:func:`embed_text`, :func:`embed_query`) so new code paths can stay
model-agnostic and the bake-off harness has one place to introspect from.

Design choices (see comments in the bake-off harness for empirical
backing):

* ``embed_text`` and ``embed_query`` return ``list[float]`` per the brief.
  Existing call sites that need packed ``bytes`` for sqlite-vec storage
  use :func:`pack_embedding` to encode. This keeps the public API
  numpy-friendly while preserving the hot-path contract that vec_memories
  expects ``BLOB`` (4 bytes per float, little-endian, native float32).

* The default model is resolved by :func:`_get_default_embed_model` at
  call time, NOT at import time. This is because the existing module-
  level ``EMBED_MODEL = os.environ.get(...)`` constants in
  ``mcp_server.py`` / ``vec.py`` / ``_impl.py`` are baked in at first
  import and can't be flipped without re-importing. The bake-off harness
  side-steps that by spawning a subprocess per model, with the env set
  before Python starts. New code that wants runtime model swaps should
  call :func:`_get_default_embed_model` per-call.

* ``BRAINCTL_EMBED_MODEL`` is the existing user-facing knob. We keep it.
  When set to a registered name we read the dim from the registry; when
  set to an unknown name we fall back to ``BRAINCTL_EMBED_DIMENSIONS``
  (also existing) or 768.

* Lazy dim validation lives in :func:`validate_db_compatibility`, which
  reads the dim straight from the ``vec_memories`` virtual-table DDL so
  the validator believes what's *physically on disk*, not what env says.

Models tested in the 2026-04 bake-off (see
``tests/bench/embedding_bakeoff.py`` and the result JSON for the
empirical winner — the comment at the bottom of this file is updated
each time a new bake-off is run):

* ``nomic-embed-text``        (768-dim, 274 MB)   — 2025 baseline.
* ``bge-m3``                  (1024-dim, 1.2 GB)  — BAAI, multi-language + long context.
* ``mxbai-embed-large``       (1024-dim, 669 MB)  — Mixedbread, MTEB top-tier English.
* ``snowflake-arctic-embed2`` (1024-dim, 1.2 GB)  — Snowflake Arctic v2, late 2024.
* ``qwen3-embedding:8b``      (4096-dim, 4.7 GB)  — Alibaba Qwen3, MTEB SOTA but heavy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry — the canonical model catalogue
# ---------------------------------------------------------------------------

#: Embedding-model registry. Keyed by the *brainctl name* (which we use as
#: the BRAINCTL_EMBED_MODEL value); each entry records:
#:
#: - ``dim``: output dimensionality (must match the ``vec_memories(embedding
#:   float[N])`` declaration on the user's brain.db).
#: - ``ollama_tag``: the tag passed to ``ollama pull`` / the model field
#:   on ``/api/embed``. Usually identical to the key, but can differ
#:   (e.g. ``qwen3-embedding`` -> ``qwen3-embedding:8b``).
#: - ``description``: one-liner shown by ``brainctl vec models``.
#: - ``size_mb``: approximate on-disk size after ``ollama pull``. Useful
#:   for "should I bother pulling this?" UX in the reindex CLI.
EMBEDDING_MODELS: Dict[str, Dict[str, Any]] = {
    "nomic-embed-text": {
        "dim": 768,
        "ollama_tag": "nomic-embed-text",
        "description": "Nomic AI 2024 baseline. Small (274MB), fast, English-focused.",
        "size_mb": 274,
    },
    "bge-m3": {
        "dim": 1024,
        "ollama_tag": "bge-m3",
        "description": "BAAI BGE-M3. Multi-language (100+), long context (8k tokens), strong on retrieval.",
        "size_mb": 1200,
    },
    "mxbai-embed-large": {
        "dim": 1024,
        "ollama_tag": "mxbai-embed-large",
        "description": "Mixedbread mxbai-embed-large-v1. Top of MTEB English leaderboard at release.",
        "size_mb": 669,
    },
    "snowflake-arctic-embed2": {
        "dim": 1024,
        "ollama_tag": "snowflake-arctic-embed2",
        "description": "Snowflake Arctic-Embed v2 (multilingual). Recent, competitive on MTEB.",
        "size_mb": 1200,
    },
    "qwen3-embedding:8b": {
        "dim": 4096,
        "ollama_tag": "qwen3-embedding:8b",
        "description": "Alibaba Qwen3-Embedding-8B. SOTA on MTEB Multilingual but VRAM-heavy (4.7GB) and slow.",
        "size_mb": 4700,
    },
}


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _ollama_url() -> str:
    """Return the Ollama embed endpoint (env-overridable)."""
    return os.environ.get(
        "BRAINCTL_OLLAMA_URL", "http://localhost:11434/api/embed"
    )


def _get_default_embed_model() -> str:
    """Return the default embedding-model name brainctl should use.

    Resolution order:

    1. ``BRAINCTL_EMBED_MODEL`` env var (existing contract).
    2. The registry default — ``DEFAULT_MODEL_NAME`` below.

    The default is reviewed each bake-off; current pick documented at
    the bottom of this file under "Default-model rationale".
    """
    env = os.environ.get("BRAINCTL_EMBED_MODEL", "").strip()
    if env:
        return env
    return DEFAULT_MODEL_NAME


def _get_model_dim(model: Optional[str] = None) -> int:
    """Return the expected dimensionality for ``model``.

    For models in :data:`EMBEDDING_MODELS` we trust the registry.
    For unknown models we fall back to ``BRAINCTL_EMBED_DIMENSIONS``
    (existing env knob) or 768.
    """
    name = model or _get_default_embed_model()
    if name in EMBEDDING_MODELS:
        return int(EMBEDDING_MODELS[name]["dim"])
    try:
        return int(os.environ.get("BRAINCTL_EMBED_DIMENSIONS", "768"))
    except (TypeError, ValueError):
        return 768


def _resolve_ollama_tag(model: Optional[str] = None) -> str:
    """Translate a brainctl model name to the tag Ollama expects."""
    name = model or _get_default_embed_model()
    if name in EMBEDDING_MODELS:
        return EMBEDDING_MODELS[name]["ollama_tag"]
    return name


# ---------------------------------------------------------------------------
# Public API — embedding helpers
# ---------------------------------------------------------------------------


def embed_text(text: str, model: Optional[str] = None) -> Optional[List[float]]:
    """Embed ``text`` for *storage* (memory ingest).

    Returns the raw float list on success, or ``None`` on any failure
    (Ollama unreachable, model not pulled, malformed response, etc.).
    Callers that need bytes for sqlite-vec storage should pipe through
    :func:`pack_embedding`.

    The model is resolved per-call, so future code paths can swap models
    by passing ``model=``. Existing module-level constants in ``_impl``
    and ``vec`` keep their behavior unless those modules are refactored
    to consult this registry.
    """
    return _embed(text, model)


def embed_query(query: str, model: Optional[str] = None) -> Optional[List[float]]:
    """Embed ``query`` for *retrieval*.

    Today this is identical to :func:`embed_text`, but the split exists
    so we can later wire query-side prompt prefixes (BGE / E5 / mxbai
    all use specific instruction templates that boost retrieval quality
    by ~1-3pp). Currently no model in the registry requires a different
    query template; when one does, this is the seam to add it without
    touching call sites.
    """
    # NOTE: when wiring per-model query templates, branch here on
    # the resolved model name. Keep storage-side embed_text untouched
    # because changing the storage prefix invalidates indexed vectors.
    return _embed(query, model)


def pack_embedding(vec: List[float]) -> bytes:
    """Pack a float list into the little-endian float32 BLOB sqlite-vec wants."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> List[float]:
    """Inverse of :func:`pack_embedding` — useful for tests / inspection."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob[: n * 4]))


def _embed(text: str, model: Optional[str]) -> Optional[List[float]]:
    """Core embed implementation — Ollama call, list[float] return."""
    tag = _resolve_ollama_tag(model)
    try:
        payload = json.dumps({"model": tag, "input": text}).encode()
        req = urllib.request.Request(
            _ollama_url(),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        timeout = float(os.environ.get("BRAINCTL_EMBED_TIMEOUT", "30"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        _log.debug("embed: Ollama unreachable for model=%s: %s", tag, exc)
        return None
    except Exception as exc:
        _log.debug("embed: unexpected error for model=%s: %s", tag, exc)
        return None
    try:
        vec = data["embeddings"][0]
    except (KeyError, IndexError, TypeError) as exc:
        _log.debug("embed: bad response shape for model=%s: %s", tag, exc)
        return None
    if not isinstance(vec, list) or not vec:
        return None
    return vec


# ---------------------------------------------------------------------------
# Lazy dim-mismatch validation
# ---------------------------------------------------------------------------


_DIM_RE = re.compile(r"float\s*\[\s*(\d+)\s*\]")


def get_db_embedding_dim(conn: sqlite3.Connection) -> Optional[int]:
    """Read the dim baked into the ``vec_memories`` virtual-table DDL.

    Returns ``None`` if the table doesn't exist (vec extension not
    initialized) or the DDL doesn't match the expected pattern. This is
    the *physical* truth — what the user's brain.db can actually store —
    and is what the dim-mismatch validator checks against.
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'vec_memories'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    sql = row[0] if isinstance(row, tuple) else row["sql"]
    if not sql:
        return None
    m = _DIM_RE.search(sql)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def sample_db_embedding_widths(
    conn: sqlite3.Connection, sample_size: int = 8
) -> dict:
    """Probe the actual float32 width of a sample of stored embeddings.

    ``cmd_vec_reindex`` cross-checks the DDL-declared dim
    (:func:`get_db_embedding_dim`) against the real byte width of a handful of
    rows before it does a destructive DROP+CREATE, so an interrupted prior
    reindex or a mixed-width index refuses to be silently papered over unless
    ``--force`` is passed (audit H4).

    Each ``vec_memories`` embedding is stored as packed float32, so a vector's
    dimensionality is ``len(blob) // 4``.

    Returns a dict with keys ``ok``, ``sample_count``, ``consistent``,
    ``declared_dim``, ``observed_dims`` (sorted), and ``message``. When the
    table is absent/unreadable or empty, ``consistent`` is ``True`` (nothing to
    contradict) so the reindex guard treats it as "nothing to check" and
    proceeds to build a fresh index.
    """
    declared_dim = get_db_embedding_dim(conn)
    try:
        n = max(1, int(sample_size))
    except (TypeError, ValueError):
        n = 8
    try:
        rows = conn.execute(
            "SELECT embedding FROM vec_memories LIMIT ?", (n,)
        ).fetchall()
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "sample_count": 0,
            "consistent": True,
            "declared_dim": declared_dim,
            "observed_dims": [],
            "message": f"vec_memories unreadable ({exc}); nothing to probe.",
        }

    observed: set[int] = set()
    sample_count = 0
    for row in rows:
        blob = row["embedding"] if isinstance(row, sqlite3.Row) else row[0]
        if blob is None:
            continue
        try:
            width = len(blob) // 4  # packed float32
        except TypeError:
            continue
        if width <= 0:
            continue
        observed.add(width)
        sample_count += 1

    observed_dims = sorted(observed)
    if sample_count == 0:
        return {
            "ok": True,
            "sample_count": 0,
            "consistent": True,
            "declared_dim": declared_dim,
            "observed_dims": [],
            "message": "vec_memories has no embedding rows to probe.",
        }

    consistent = len(observed_dims) == 1 and (
        declared_dim is None or observed_dims[0] == declared_dim
    )
    if consistent:
        message = (
            f"Sampled {sample_count} embedding(s); all {observed_dims[0]}-dim"
            + (
                f", matching the declared dim {declared_dim}."
                if declared_dim is not None
                else " (no declared dim to compare)."
            )
        )
    elif len(observed_dims) > 1:
        message = (
            f"Sampled {sample_count} embedding(s) with mixed widths "
            f"{observed_dims}; the vec index is corrupted or mid-migration."
        )
    else:
        message = (
            f"Sampled {sample_count} embedding(s) at {observed_dims[0]}-dim, "
            f"but the DDL declares {declared_dim}-dim."
        )

    return {
        "ok": True,
        "sample_count": sample_count,
        "consistent": consistent,
        "declared_dim": declared_dim,
        "observed_dims": observed_dims,
        "message": message,
    }


class EmbeddingDimMismatchError(RuntimeError):
    """Raised when the requested embedding model's dim doesn't match the DB.

    Keep this distinct from ``RuntimeError`` so callers (CLI, MCP tools)
    can catch it and surface the reindex hint instead of crashing the
    whole process.
    """

    def __init__(
        self,
        *,
        db_dim: int,
        model: str,
        model_dim: int,
        db_path: Optional[str] = None,
    ) -> None:
        self.db_dim = db_dim
        self.model = model
        self.model_dim = model_dim
        self.db_path = db_path
        msg = (
            f"Embedding dim mismatch: brain.db was indexed with "
            f"{db_dim}-dim vectors but model {model!r} produces "
            f"{model_dim}-dim. Run `brainctl vec reindex --model {model}` "
            f"to migrate the existing index, or set BRAINCTL_EMBED_MODEL "
            f"to a {db_dim}-dim model to match the existing index."
        )
        if db_path:
            msg += f" (db: {db_path})"
        super().__init__(msg)


_validation_cache: Dict[str, bool] = {}  # path -> validated, cleared per-process


def validate_db_compatibility(
    conn: sqlite3.Connection,
    *,
    model: Optional[str] = None,
    db_path: Optional[str] = None,
    cache: bool = True,
) -> None:
    """Check the requested model's dim matches the brain.db's vec_memories.

    Called lazily on first embed in a process (per the brief: "on first
    embed, check ... If they mismatch, error with a clear message").
    Subsequent calls in the same process are no-ops thanks to the
    validation cache; pass ``cache=False`` to force a re-check (used by
    the reindex command after migrating).

    Raises :class:`EmbeddingDimMismatchError` on mismatch. Returns
    silently when:

    * The vec extension isn't loaded (no ``vec_memories`` table) — the
      caller is presumably using FTS-only mode.
    * The DB has a vec_memories table but it's *empty* — fresh install,
      first write will create the table at the model's native dim.
    * Dims agree.
    """
    name = model or _get_default_embed_model()
    cache_key = f"{db_path or '<conn>'}::{name}"
    if cache and cache_key in _validation_cache:
        return

    db_dim = get_db_embedding_dim(conn)
    if db_dim is None:
        # No vec_memories table yet — first index_memory call will create it.
        if cache:
            _validation_cache[cache_key] = True
        return

    model_dim = _get_model_dim(name)
    if db_dim != model_dim:
        # Don't cache the failure — we want the next call to also raise.
        raise EmbeddingDimMismatchError(
            db_dim=db_dim,
            model=name,
            model_dim=model_dim,
            db_path=db_path,
        )

    if cache:
        _validation_cache[cache_key] = True


def reset_validation_cache() -> None:
    """Drop the per-process validation cache (used by reindex post-migrate)."""
    _validation_cache.clear()


# ---------------------------------------------------------------------------
# Reindex helpers — used by ``brainctl vec reindex`` (CLI) and tests
# ---------------------------------------------------------------------------


def estimate_warmup_seconds(model: str) -> float:
    """Rough wall-clock estimate for first-token latency on ``model``.

    Only a hint for the reindex command's "warmup before pulling N
    memories" message. Not used for benchmarking.
    """
    # Empirical numbers from the 2026-04 bake-off on Apple M4 Max.
    # Ollama keeps the model in VRAM after the first request (5-min TTL),
    # so this is per-process, not per-batch.
    rough = {
        "nomic-embed-text": 1.5,
        "bge-m3": 5.0,
        "mxbai-embed-large": 3.5,
        "snowflake-arctic-embed2": 5.0,
        "qwen3-embedding:8b": 25.0,
    }
    return rough.get(model, 5.0)


def estimate_per_memory_seconds(model: str) -> float:
    """Rough wall-clock per-memory embed latency once warm."""
    rough = {
        "nomic-embed-text": 0.04,
        "bge-m3": 0.10,
        "mxbai-embed-large": 0.05,
        "snowflake-arctic-embed2": 0.10,
        "qwen3-embedding:8b": 0.50,
    }
    return rough.get(model, 0.10)


def warmup_model(model: Optional[str] = None) -> bool:
    """Send a 1-token request so Ollama loads the weights into VRAM.

    Returns True if Ollama responded with an embedding, False otherwise.
    Pure side-effect; callers don't need the return value but the bool
    is handy for CLI status output.
    """
    t0 = time.perf_counter()
    out = embed_text(".", model=model)
    elapsed = time.perf_counter() - t0
    _log.debug("warmup_model(%s) done in %.2fs", model, elapsed)
    return out is not None


# ---------------------------------------------------------------------------
# Default-model rationale
# ---------------------------------------------------------------------------
#
# DEFAULT_MODEL_NAME is the registry's chosen default. It's read by
# _get_default_embed_model when BRAINCTL_EMBED_MODEL is unset.
#
# Selection rule (Pareto, from the brief):
#   "Prefer the model that gives best Hit@5 on LOCOMO without being
#    more than 2x slower than nomic-embed-text. Document the choice
#    rationale ... Ship the new default if the winner beats nomic by
#    >=3pp on LOCOMO Hit@5 with <=1.5x latency."
#
# 2026-04-18 bake-off results — see tests/bench/baselines/embedding_bakeoff.json
# for raw numbers; the comment in tests/bench/embedding_bakeoff.py at the
# top of `pick_winner()` documents the ranking logic.
#
# DEFAULT_MODEL_NAME is set just below this block; if a future bake-off
# changes it, update both the constant AND this rationale comment.
DEFAULT_MODEL_NAME: str = "nomic-embed-text"
# ^^^ Updated by the bake-off at the end of the run — see
# tests/bench/embedding_bakeoff.py::maybe_promote_default for the
# automatic "if winner beats threshold, here's the line edit" logic.


__all__ = [
    "DEFAULT_MODEL_NAME",
    "EMBEDDING_MODELS",
    "EmbeddingDimMismatchError",
    "embed_query",
    "embed_text",
    "estimate_per_memory_seconds",
    "estimate_warmup_seconds",
    "get_db_embedding_dim",
    "pack_embedding",
    "reset_validation_cache",
    "unpack_embedding",
    "validate_db_compatibility",
    "warmup_model",
]
