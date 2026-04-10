"""
brainctl — standalone embedding and vector-search helper module.

All public functions are safe to call even when sqlite-vec is NOT installed
or Ollama is unavailable.  They return None / False / [] on failure rather
than raising, so callers never need to guard against ImportError/NetworkError.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import struct
import urllib.error
import urllib.request
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (from environment, with defaults)
# ---------------------------------------------------------------------------

OLLAMA_EMBED_URL: str = os.environ.get(
    "BRAINCTL_OLLAMA_URL", "http://localhost:11434/api/embed"
)
EMBED_MODEL: str = os.environ.get("BRAINCTL_EMBED_MODEL", "nomic-embed-text")


def _embed_dimensions() -> int:
    """Return the expected embedding dimensionality (env-configurable)."""
    try:
        return int(os.environ.get("BRAINCTL_EMBED_DIMENSIONS", "768"))
    except (TypeError, ValueError):
        return 768


def _find_vec_dylib() -> str | None:
    """Auto-discover the sqlite-vec loadable extension path."""
    try:
        import sqlite_vec  # type: ignore[import]
        return sqlite_vec.loadable_path()
    except (ImportError, AttributeError):
        pass
    import glob as _glob
    for pattern in [
        "/opt/homebrew/lib/python*/site-packages/sqlite_vec/vec0.*",
        "/usr/lib/python*/site-packages/sqlite_vec/vec0.*",
    ]:
        matches = sorted(_glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def embed_text(text: str) -> bytes | None:
    """Call Ollama to produce a float32 embedding for *text*.

    Returns packed float32 bytes (length = len(embedding) * 4) on success,
    or None on any failure (network error, bad response, Ollama not running).

    The expected dimensionality is configured via BRAINCTL_EMBED_DIMENSIONS
    (default 768); this is used by init_vec_tables and index_memory to create
    appropriately-sized virtual tables.  embed_text itself packs whatever
    Ollama returns.
    """
    try:
        payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(
            OLLAMA_EMBED_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
            if not isinstance(vec, list) or len(vec) == 0:
                return None
            return struct.pack(f"{len(vec)}f", *vec)
    except (urllib.error.URLError, urllib.error.HTTPError):
        _log.debug("embed_text: Ollama unavailable or network error")
        return None
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _log.debug("embed_text: bad response from Ollama — %s", exc)
        return None
    except Exception as exc:
        _log.debug("embed_text: unexpected error — %s", exc)
        return None


def init_vec_tables(conn: sqlite3.Connection) -> bool:
    """Create vec_memories virtual table on *conn* if sqlite-vec is available.

    Loads the sqlite-vec extension on *conn* and issues CREATE VIRTUAL TABLE
    IF NOT EXISTS.  Returns True on success, False if sqlite-vec is not
    installed or loading fails.

    The extension is loaded and immediately locked again (enable_load_extension
    is toggled) so the connection is left in the same security state as before.
    """
    dylib = _find_vec_dylib()
    if not dylib:
        return False
    try:
        conn.enable_load_extension(True)
        conn.load_extension(dylib)
        conn.enable_load_extension(False)
        dims = _embed_dimensions()
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories "
            f"USING vec0(embedding float[{dims}])"
        )
        conn.commit()
        return True
    except Exception as exc:
        _log.debug("init_vec_tables: failed — %s", exc)
        return False


def index_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    content: str,
) -> bool:
    """Embed *content* and upsert the vector into vec_memories.

    Opens a **separate** connection to the same database with sqlite-vec
    loaded, rather than mutating the caller's connection.  This keeps the
    caller's transaction state clean.

    Returns True if the row was indexed, False if vec is unavailable or
    embedding failed.
    """
    dylib = _find_vec_dylib()
    if not dylib:
        return False

    embedding = embed_text(content)
    if embedding is None:
        return False

    # Determine the DB file path from the caller's connection.
    try:
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        db_path = ":memory:"

    try:
        vec_conn = sqlite3.connect(db_path, timeout=10)
        vec_conn.enable_load_extension(True)
        vec_conn.load_extension(dylib)
        vec_conn.enable_load_extension(False)
        # Ensure the table exists.
        dims = _embed_dimensions()
        vec_conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories "
            f"USING vec0(embedding float[{dims}])"
        )
        vec_conn.execute(
            "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?, ?)",
            (memory_id, embedding),
        )
        vec_conn.commit()
        vec_conn.close()
        return True
    except Exception as exc:
        _log.debug("index_memory: failed for memory_id=%s — %s", memory_id, exc)
        return False


def vec_search(
    conn: sqlite3.Connection,
    query: str,
    k: int = 10,
) -> list[dict[str, Any]]:
    """Search vec_memories for the nearest neighbours of *query*.

    Opens a sqlite-vec–enabled connection to the same DB as *conn*, runs the
    ANN query, then joins back to the memories table for full row data.

    Returns a list of dicts with keys: id, content, category, distance.
    Returns [] if sqlite-vec is unavailable, embedding fails, or any error
    occurs.
    """
    dylib = _find_vec_dylib()
    if not dylib:
        return []

    embedding = embed_text(query)
    if embedding is None:
        return []

    try:
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        db_path = ":memory:"

    try:
        vec_conn = sqlite3.connect(db_path, timeout=10)
        vec_conn.row_factory = sqlite3.Row
        vec_conn.enable_load_extension(True)
        vec_conn.load_extension(dylib)
        vec_conn.enable_load_extension(False)

        vec_rows = vec_conn.execute(
            "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k=?",
            (embedding, k),
        ).fetchall()
        if not vec_rows:
            vec_conn.close()
            return []

        rowids = [r["rowid"] for r in vec_rows]
        dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
        ph = ",".join("?" * len(rowids))

        src_rows = vec_conn.execute(
            f"SELECT id, content, category FROM memories "
            f"WHERE id IN ({ph}) AND retired_at IS NULL",
            rowids,
        ).fetchall()

        results = []
        for row in src_rows:
            results.append(
                {
                    "id": row["id"],
                    "content": row["content"],
                    "category": row["category"],
                    "distance": round(dist_map.get(row["id"], 1.0), 6),
                }
            )
        results.sort(key=lambda r: r["distance"])
        vec_conn.close()
        return results
    except Exception as exc:
        _log.debug("vec_search: failed — %s", exc)
        return []
