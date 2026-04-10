"""brainctl MCP tools — message event bus, push & vector search."""
from __future__ import annotations
import json
import math
import os
import re
import sqlite3
import struct
import urllib.request
import urllib.error
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

# ---------------------------------------------------------------------------
# sqlite-vec / embedding constants
# ---------------------------------------------------------------------------

def _find_vec_dylib():
    """Auto-discover the sqlite-vec loadable extension path."""
    try:
        import sqlite_vec
        return sqlite_vec.loadable_path()
    except (ImportError, AttributeError):
        pass
    import glob as _glob
    for pattern in [
        '/opt/homebrew/lib/python*/site-packages/sqlite_vec/vec0.*',
        '/usr/lib/python*/site-packages/sqlite_vec/vec0.*',
    ]:
        matches = sorted(_glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


VEC_DYLIB = _find_vec_dylib()
OLLAMA_EMBED_URL = os.environ.get("BRAINCTL_OLLAMA_URL", "http://localhost:11434/api/embed")
EMBED_MODEL = os.environ.get("BRAINCTL_EMBED_MODEL", "nomic-embed-text")

# MEB defaults
_MEB_TTL_HOURS_DEFAULT = 72
_MEB_MAX_DEPTH_DEFAULT = 10_000

# FTS5 special characters — strip everything that isn't word chars or spaces
_FTS5_SPECIAL = re.compile(r'[.&|*"()\-@^?!]')

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _now_ts() -> str:
    return _now()


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5 special characters so MATCH never raises a syntax error."""
    cleaned = _FTS5_SPECIAL.sub(" ", query or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _days_since(created_at_str) -> float:
    if not created_at_str:
        return 0.0
    try:
        ts = created_at_str.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts, fmt)
                    break
                except ValueError:
                    continue
            else:
                return 0.0
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
            dt = dt.astimezone(timezone.utc)
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def _age_str(created_at_str: str | None) -> str:
    """Return a human-readable relative age string like '3 days ago'."""
    days = _days_since(created_at_str)
    if days < 1 / 24:
        return "just now"
    if days < 1:
        hours = int(days * 24)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days < 7:
        d = int(days)
        return f"{d} day{'s' if d != 1 else ''} ago"
    if days < 30:
        w = int(days / 7)
        return f"{w} week{'s' if w != 1 else ''} ago"
    if days < 365:
        m = int(days / 30)
        return f"{m} month{'s' if m != 1 else ''} ago"
    y = int(days / 365)
    return f"{y} year{'s' if y != 1 else ''} ago"


def _scope_lambda(scope: str | None) -> float:
    if scope and scope.startswith("project:"):
        return 0.03
    if scope and scope.startswith("agent:"):
        return 0.05
    return 0.01


def _temporal_weight(created_at_str, scope=None) -> float:
    return math.exp(-_scope_lambda(scope) * _days_since(created_at_str))


# ---------------------------------------------------------------------------
# MEB helpers
# ---------------------------------------------------------------------------


def _meb_config(db: sqlite3.Connection) -> dict:
    """Load MEB configuration from meb_config table, falling back to defaults."""
    try:
        rows = db.execute("SELECT key, value FROM meb_config").fetchall()
        cfg = {r["key"]: r["value"] for r in rows}
    except Exception:
        cfg = {}
    return {
        "ttl_hours":       int(cfg.get("ttl_hours",       _MEB_TTL_HOURS_DEFAULT)),
        "max_queue_depth": int(cfg.get("max_queue_depth", _MEB_MAX_DEPTH_DEFAULT)),
        "prune_on_read":   str(cfg.get("prune_on_read", "true")).lower() == "true",
    }


def _meb_prune(db: sqlite3.Connection, cfg: dict) -> int:
    """Delete TTL-expired events and enforce max queue depth. Returns rows deleted."""
    deleted = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=cfg["ttl_hours"])).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    cur = db.execute("DELETE FROM memory_events WHERE created_at < ?", (cutoff,))
    deleted += cur.rowcount

    count = db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    overflow = count - cfg["max_queue_depth"]
    if overflow > 0:
        cur = db.execute(
            "DELETE FROM memory_events WHERE id IN "
            "(SELECT id FROM memory_events ORDER BY id ASC LIMIT ?)",
            (overflow,),
        )
        deleted += cur.rowcount

    if deleted:
        db.commit()
    return deleted


# ---------------------------------------------------------------------------
# sqlite-vec helpers
# ---------------------------------------------------------------------------


def _get_vec_db() -> sqlite3.Connection | None:
    """Open DB with sqlite-vec loaded. Returns None if unavailable."""
    if not VEC_DYLIB:
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.enable_load_extension(True)
        conn.load_extension(VEC_DYLIB)
        conn.enable_load_extension(False)
        return conn
    except Exception:
        return None


def _embed_query_safe(text: str) -> bytes | None:
    """Embed query text via Ollama. Returns packed float32 bytes, or None on failure."""
    try:
        payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(
            OLLAMA_EMBED_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            vec = data["embeddings"][0]
            return struct.pack(f"{len(vec)}f", *vec)
    except Exception:
        return None


def _normalize(scores: list[float]) -> list[float]:
    if not scores:
        return scores
    mn, mx = min(scores), max(scores)
    if mx == mn:
        return [1.0] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]


def _rrf_fuse(fts_list: list[dict], vec_list: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion — returns merged list sorted by rrf_score descending."""
    scores: dict = {}
    sources: dict = {}
    rows: dict = {}
    for rank, row in enumerate(fts_list):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
        sources[rid] = "keyword"
        rows[rid] = row
    for rank, row in enumerate(vec_list):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
        sources[rid] = "both" if rid in sources else "semantic"
        if rid not in rows:
            rows[rid] = row
    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    out = []
    for rid in sorted_ids:
        r = rows[rid].copy()
        r["rrf_score"] = round(scores[rid], 6)
        r["source"] = sources[rid]
        out.append(r)
    return out


def _ensure_agent(conn, agent_id: str) -> None:
    """Insert agent row if it doesn't exist (satisfies FK constraint on events/memories)."""
    if not agent_id:
        return
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents "
            "(id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES (?, ?, 'mcp', 'active', ?, ?)",
            (agent_id, agent_id, _now_ts(), _now_ts()),
        )
    except Exception:
        pass  # agents table may not exist in minimal schemas


def _log_access(conn, agent_id, action, target_table=None, target_id=None, query=None, result_count=None):
    try:
        conn.execute(
            "INSERT INTO access_log (agent_id, action, target_table, target_id, query, result_count) "
            "VALUES (?,?,?,?,?,?)",
            (agent_id, action, target_table, target_id, query, result_count),
        )
    except Exception:
        pass  # access_log may not exist in minimal test schemas


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_meb_tail(
    n: int = 20,
    since: int | None = None,
    agent: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    include_backfill: bool = False,
) -> dict:
    """Poll recent memory_events, optionally filtered by agent, category, or scope."""
    db = _db()
    try:
        cfg = _meb_config(db)
        if cfg["prune_on_read"]:
            _meb_prune(db, cfg)

        n = max(1, min(n or 20, 200))
        sql = (
            "SELECT me.*, m.content, m.confidence "
            "FROM memory_events me JOIN memories m ON me.memory_id = m.id "
            "WHERE 1=1"
        )
        params: list = []

        if not include_backfill:
            sql += " AND me.operation != 'backfill'"
        if since is not None:
            sql += " AND me.id > ?"
            params.append(since)
        if agent:
            sql += " AND me.agent_id = ?"
            params.append(agent)
        if category:
            sql += " AND me.category = ?"
            params.append(category)
        if scope:
            sql += " AND me.scope LIKE ?"
            params.append(f"{scope}%")

        sql += " ORDER BY me.id DESC LIMIT ?"
        params.append(n)

        rows = db.execute(sql, params).fetchall()
        results = list(reversed(_rows_to_list(rows)))
        for r in results:
            r["age"] = _age_str(r.get("created_at"))
        return {"ok": True, "events": results, "count": len(results)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_meb_stats() -> dict:
    """Queue depth, throughput, and propagation latency summary."""
    db = _db()
    try:
        cfg = _meb_config(db)

        total = db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        by_op = db.execute(
            "SELECT operation, COUNT(*) AS cnt FROM memory_events GROUP BY operation"
        ).fetchall()
        by_cat = db.execute(
            "SELECT category, COUNT(*) AS cnt FROM memory_events GROUP BY category ORDER BY cnt DESC"
        ).fetchall()

        oldest_row = db.execute("SELECT MIN(created_at) AS oldest FROM memory_events").fetchone()
        newest_row = db.execute("SELECT MAX(created_at) AS newest FROM memory_events").fetchone()
        oldest = oldest_row["oldest"] if oldest_row else None
        newest = newest_row["newest"] if newest_row else None

        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        recent_count = db.execute(
            "SELECT COUNT(*) FROM memory_events WHERE created_at >= ?", (one_hour_ago,)
        ).fetchone()[0]

        latency_rows = db.execute(
            """
            SELECT
                (julianday(me.created_at) - julianday(m.created_at)) * 86400.0 AS latency_secs
            FROM memory_events me
            JOIN memories m ON me.memory_id = m.id
            WHERE me.operation IN ('insert', 'update')
            ORDER BY me.id DESC
            LIMIT 100
            """
        ).fetchall()
        latencies = [r["latency_secs"] for r in latency_rows if r["latency_secs"] is not None]
        avg_latency_ms = round(sum(latencies) / len(latencies) * 1000, 2) if latencies else None
        max_latency_ms = round(max(latencies) * 1000, 2) if latencies else None

        return {
            "ok": True,
            "total_events":        total,
            "by_operation":        {r["operation"]: r["cnt"] for r in by_op},
            "by_category":         {r["category"]: r["cnt"] for r in by_cat},
            "oldest_event":        oldest,
            "newest_event":        newest,
            "events_last_hour":    recent_count,
            "avg_latency_ms":      avg_latency_ms,
            "max_latency_ms":      max_latency_ms,
            "latency_sample_size": len(latencies),
            "config":              cfg,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_meb_prune(
    ttl_hours: int | None = None,
    max_depth: int | None = None,
) -> dict:
    """Manually trigger TTL + max-depth cleanup of memory_events."""
    db = _db()
    try:
        cfg = _meb_config(db)
        if ttl_hours is not None:
            cfg["ttl_hours"] = int(ttl_hours)
        if max_depth is not None:
            cfg["max_queue_depth"] = int(max_depth)

        deleted = _meb_prune(db, cfg)
        remaining = db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        return {"ok": True, "deleted": deleted, "remaining": remaining}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_push(
    task: str,
    top_k: int = 5,
    agent: str = "unknown",
    project: str | None = None,
) -> dict:
    """Score + select top-K memories for a task description and return them for context injection."""
    if not task or not task.strip():
        return {"ok": False, "error": "task must not be empty"}

    db = _db()
    db_vec = None
    try:
        top_k = min(max(1, int(top_k)), 10)
        agent_id = agent or "unknown"
        _ensure_agent(db, agent_id)
        push_id = _uuid_mod.uuid4().hex[:12]

        # Sanitize for FTS5
        _raw_fts = _sanitize_fts_query(task)
        fts_query = re.sub(r'[,:+<>]', ' ', _raw_fts).strip()

        # Try hybrid (vec + FTS5) or fall back to FTS5-only
        db_vec = _get_vec_db()
        q_blob = _embed_query_safe(task) if db_vec else None
        hybrid = db_vec is not None and q_blob is not None
        fetch_limit = top_k * 6

        def _fts_mem():
            if not fts_query:
                return []
            rows = db.execute(
                "SELECT m.id, 'memory' as type, m.category, m.content, m.confidence, "
                "m.scope, m.created_at, f.rank as fts_rank "
                "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? AND m.retired_at IS NULL ORDER BY rank LIMIT ?",
                (fts_query, fetch_limit),
            ).fetchall()
            return _rows_to_list(rows)

        def _vec_mem():
            if not hybrid:
                return []
            try:
                vec_rows = db_vec.execute(
                    "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k=?",
                    (q_blob, fetch_limit),
                ).fetchall()
            except Exception:
                return []
            if not vec_rows:
                return []
            rowids = [r["rowid"] for r in vec_rows]
            dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
            ph = ",".join("?" * len(rowids))
            src_rows = db_vec.execute(
                f"SELECT id, 'memory' as type, category, content, confidence, scope, created_at "
                f"FROM memories WHERE id IN ({ph}) AND retired_at IS NULL",
                rowids,
            ).fetchall()
            out = [dict(r) | {"distance": round(dist_map.get(r["id"], 1.0), 4)} for r in src_rows]
            out.sort(key=lambda r: r["distance"])
            return out

        fts_list = _fts_mem()
        vec_list = _vec_mem()
        if hybrid:
            merged = _rrf_fuse(fts_list, vec_list)
        else:
            merged = [r | {"rrf_score": 0.0, "source": "keyword"} for r in fts_list]

        # Apply temporal weighting and select top_k
        for r in merged:
            tw = _temporal_weight(r.get("created_at"), r.get("scope"))
            r["temporal_weight"] = round(tw, 4)
            r["final_score"] = round(r.get("rrf_score", 0.0) * tw, 8)
        merged.sort(key=lambda r: r["final_score"], reverse=True)
        selected = merged[:top_k]

        # Snapshot recalled_count for later delta tracking
        memory_ids = [r["id"] for r in selected]
        recalled_snapshot: dict = {}
        if memory_ids:
            ph = ",".join("?" * len(memory_ids))
            snap_rows = db.execute(
                f"SELECT id, recalled_count FROM memories WHERE id IN ({ph})", memory_ids
            ).fetchall()
            recalled_snapshot = {r["id"]: r["recalled_count"] or 0 for r in snap_rows}

        # Record push event for utility tracking
        push_meta = json.dumps({
            "push_id": push_id,
            "task_desc": task[:200],
            "memory_ids": memory_ids,
            "recalled_at_push": recalled_snapshot,
            "top_k": top_k,
            "hybrid": hybrid,
        })
        push_event_cur = db.execute(
            "INSERT INTO events (agent_id, event_type, summary, detail, importance, project, created_at) "
            "VALUES (?, 'push_delivered', ?, ?, 0.2, ?, ?)",
            (
                agent_id,
                f"push:{push_id} delivered {len(memory_ids)} memories for task: {task[:80]}",
                push_meta,
                project,
                _now_ts(),
            ),
        )
        push_event_id = push_event_cur.lastrowid
        _log_access(db, agent_id, "push", "memories", None, task[:200], len(memory_ids))
        db.commit()

        return {
            "ok": True,
            "push_id": push_id,
            "push_event_id": push_event_id,
            "task": task,
            "memories_pushed": len(selected),
            "hybrid": hybrid,
            "memories": selected,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if db_vec:
            try:
                db_vec.close()
            except Exception:
                pass
        db.close()


def tool_push_report(push_id: str) -> dict:
    """Show utility report for a specific push_id: recalled_count delta since push."""
    if not push_id or not push_id.strip():
        return {"ok": False, "error": "push_id must not be empty"}

    db = _db()
    try:
        row = db.execute(
            "SELECT id, detail, created_at FROM events "
            "WHERE event_type='push_delivered' AND summary LIKE ?",
            (f"push:{push_id}%",),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"push_id {push_id!r} not found"}

        meta = json.loads(row["detail"] or "{}")
        memory_ids = meta.get("memory_ids", [])
        recalled_at_push = meta.get("recalled_at_push", {})

        if not memory_ids:
            return {"ok": True, "push_id": push_id, "pushed_at": row["created_at"], "memories": []}

        ph = ",".join("?" * len(memory_ids))
        current_rows = db.execute(
            f"SELECT id, content, recalled_count FROM memories WHERE id IN ({ph})", memory_ids
        ).fetchall()

        report = []
        for r in current_rows:
            snap = recalled_at_push.get(str(r["id"]), recalled_at_push.get(r["id"], 0))
            delta = (r["recalled_count"] or 0) - snap
            report.append({
                "memory_id":       r["id"],
                "content_snippet": (r["content"] or "")[:80],
                "recalled_at_push": snap,
                "recalled_now":    r["recalled_count"] or 0,
                "delta":           delta,
                "was_useful":      delta > 0,
            })

        total_useful = sum(1 for r in report if r["was_useful"])
        return {
            "ok": True,
            "push_id":         push_id,
            "pushed_at":       row["created_at"],
            "memories_pushed": len(memory_ids),
            "memories_useful": total_useful,
            "utility_rate":    round(total_useful / len(memory_ids), 2) if memory_ids else 0.0,
            "memories":        report,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def tool_vsearch(
    query: str,
    limit: int = 10,
    alpha: float = 0.5,
    tables: str = "memories,events,context",
    vec_only: bool = False,
    agent: str = "unknown",
) -> dict:
    """Vector similarity search across memories, events, and/or context tables.

    Requires sqlite-vec to be installed (brainctl[vec]).
    Falls back gracefully with an error if sqlite-vec is unavailable.
    """
    if not query or not query.strip():
        return {"ok": False, "error": "query must not be empty"}

    db_vec = _get_vec_db()
    if db_vec is None:
        return {"ok": False, "error": "sqlite-vec not available. Install brainctl[vec]"}

    try:
        limit = max(1, min(int(limit), 100))
        alpha = max(0.0, min(1.0, float(alpha)))
        table_list = [t.strip() for t in (tables or "memories,events,context").split(",")]

        q_blob = _embed_query_safe(query)
        if q_blob is None:
            db_vec.close()
            return {"ok": False, "error": "Failed to generate embedding — is Ollama running?"}

        results: dict[str, list] = {}
        candidate_limit = limit

        def _vsearch_table(vec_table, src_table, text_col, extra_cols, fts_table):
            fetch_n = limit * 3
            try:
                vec_rows = db_vec.execute(
                    f"SELECT rowid, distance FROM {vec_table} WHERE embedding MATCH ? AND k=?",
                    (q_blob, fetch_n),
                ).fetchall()
            except Exception:
                return []
            if not vec_rows:
                return []

            rowids = [r["rowid"] for r in vec_rows]
            dist_map = {r["rowid"]: r["distance"] for r in vec_rows}
            placeholder = ",".join("?" * len(rowids))
            retired_filter = " AND retired_at IS NULL" if src_table == "memories" else ""

            if vec_only or not fts_table:
                src_rows = db_vec.execute(
                    f"SELECT id, {text_col}{', ' + extra_cols if extra_cols else ''} "
                    f"FROM {src_table} WHERE id IN ({placeholder}){retired_filter}",
                    rowids,
                ).fetchall()
                out = []
                for row in src_rows:
                    d = dist_map.get(row["id"], 999.0)
                    out.append(dict(row) | {"distance": round(d, 4), "score": round(1.0 - d, 4)})
                out.sort(key=lambda r: r["distance"])
                return out[:candidate_limit]

            # Hybrid: FTS5 + vector
            _fts_q = _sanitize_fts_query(query)
            if _fts_q:
                fts_rows = db_vec.execute(
                    f"SELECT f.rowid, f.rank FROM {fts_table} f "
                    f"WHERE {fts_table} MATCH ? AND f.rowid IN ({placeholder})",
                    [_fts_q] + rowids,
                ).fetchall()
            else:
                fts_rows = []
            fts_map = {r["rowid"]: r["rank"] for r in fts_rows}

            src_rows = db_vec.execute(
                f"SELECT id, {text_col}{', ' + extra_cols if extra_cols else ''} "
                f"FROM {src_table} WHERE id IN ({placeholder}){retired_filter}",
                rowids,
            ).fetchall()

            candidates = []
            for row in src_rows:
                rid = row["id"]
                d = dist_map.get(rid, 1.0)
                fts_rank = fts_map.get(rid, 0.0)
                candidates.append({"row": dict(row), "distance": d, "fts_rank": fts_rank})

            vec_scores = _normalize([1.0 - c["distance"] for c in candidates])
            fts_scores = _normalize([-c["fts_rank"] for c in candidates])

            out = []
            for i, c in enumerate(candidates):
                hybrid_score = alpha * fts_scores[i] + (1.0 - alpha) * vec_scores[i]
                out.append(c["row"] | {
                    "distance": round(c["distance"], 4),
                    "fts_rank": round(c["fts_rank"], 4),
                    "score": round(hybrid_score, 4),
                })
            out.sort(key=lambda r: r["score"], reverse=True)
            return out[:candidate_limit]

        if "memories" in table_list:
            results["memories"] = _vsearch_table(
                "vec_memories", "memories", "content",
                "category, scope, confidence, created_at, recalled_count, temporal_class, last_recalled_at",
                "memories_fts",
            )
        if "events" in table_list:
            results["events"] = _vsearch_table(
                "vec_events", "events", "summary",
                "event_type, importance, project, created_at",
                "events_fts",
            )
        if "context" in table_list:
            results["context"] = _vsearch_table(
                "vec_context", "context", "content",
                "source_type, source_ref, summary, project, created_at",
                "context_fts",
            )

        mode = "vec-only" if vec_only else f"hybrid(alpha={alpha})"
        total = sum(len(v) for v in results.values())

        db_main = _db()
        try:
            _log_access(db_main, agent or "unknown", "vsearch", query=query, result_count=total)
            # Update recalled_count for every surfaced memory
            for r in results.get("memories", []):
                db_main.execute(
                    "UPDATE memories SET recalled_count = recalled_count + 1, "
                    "last_recalled_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
                    "confidence = MIN(1.0, confidence + 0.15 * (1.0 - confidence)) "
                    "WHERE id = ?",
                    (r["id"],),
                )
            db_main.commit()
        finally:
            db_main.close()

        return {"ok": True, "mode": mode, "query": query, **results}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            db_vec.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="meb_tail",
        description=(
            "Poll recent Memory Event Bus events. Returns the latest N memory_events joined with "
            "their memory content, optionally filtered by agent, category, or scope prefix. "
            "Use the returned event IDs as 'since' cursor for polling."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of events to return (default 20, max 200)",
                    "default": 20,
                },
                "since": {
                    "type": "integer",
                    "description": "Only return events with id > this value (cursor-based polling)",
                },
                "agent": {
                    "type": "string",
                    "description": "Filter by agent_id",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by memory category",
                },
                "scope": {
                    "type": "string",
                    "description": "Filter by scope prefix (e.g. 'project:foo')",
                },
                "include_backfill": {
                    "type": "boolean",
                    "description": "Include backfill events (default false)",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="meb_stats",
        description=(
            "Return Memory Event Bus statistics: total event count, breakdown by operation and "
            "category, oldest/newest timestamps, events in the last hour, and propagation latency "
            "(time from memory creation to event emission)."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="meb_prune",
        description=(
            "Manually trigger TTL + max-depth cleanup of memory_events. "
            "Removes events older than ttl_hours and trims the queue to max_depth, "
            "evicting the oldest events first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ttl_hours": {
                    "type": "integer",
                    "description": "Override TTL: delete events older than this many hours",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Override max queue depth",
                },
            },
        },
    ),
    Tool(
        name="push",
        description=(
            "Score and select the top-K most relevant memories for a task description using hybrid "
            "RRF scoring (FTS5 keyword + vector similarity when available) weighted by recency. "
            "Records a push_delivered event for later utility tracking via push_report. "
            "Returns memories ready to inject into agent context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description to find relevant memories for",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default 5, max 10)",
                    "default": 5,
                },
                "agent": {
                    "type": "string",
                    "description": "Agent ID making the request",
                    "default": "unknown",
                },
                "project": {
                    "type": "string",
                    "description": "Optional project scope filter",
                },
            },
            "required": ["task"],
        },
    ),
    Tool(
        name="push_report",
        description=(
            "Show the utility report for a push: how many of the pushed memories were subsequently "
            "recalled (recalled_count delta since the push event). Identifies which memories were "
            "actually useful for the task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "push_id": {
                    "type": "string",
                    "description": "The push_id returned by the push tool",
                },
            },
            "required": ["push_id"],
        },
    ),
    Tool(
        name="vsearch",
        description=(
            "Vector similarity search across memories, events, and/or context tables. "
            "Requires sqlite-vec to be installed (brainctl[vec]). "
            "Uses hybrid scoring: alpha controls the FTS5/keyword weight (0=pure vector, 1=pure keyword). "
            "Returns results sorted by combined score descending."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per table (default 10)",
                    "default": 10,
                },
                "alpha": {
                    "type": "number",
                    "description": "FTS5 weight in hybrid score: 0.0=pure vector, 1.0=pure keyword (default 0.5)",
                    "default": 0.5,
                },
                "tables": {
                    "type": "string",
                    "description": "Comma-separated list of tables to search: memories, events, context (default: all)",
                    "default": "memories,events,context",
                },
                "vec_only": {
                    "type": "boolean",
                    "description": "Use pure vector search without FTS5 hybrid",
                    "default": False,
                },
                "agent": {
                    "type": "string",
                    "description": "Agent ID for access logging",
                    "default": "unknown",
                },
            },
            "required": ["query"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

DISPATCH: dict = {
    "meb_tail":    lambda agent_id=None, **kw: tool_meb_tail(
        n=kw.get("n", 20),
        since=kw.get("since"),
        agent=kw.get("agent"),
        category=kw.get("category"),
        scope=kw.get("scope"),
        include_backfill=kw.get("include_backfill", False),
    ),
    "meb_stats":   lambda agent_id=None, **kw: tool_meb_stats(),
    "meb_prune":   lambda agent_id=None, **kw: tool_meb_prune(
        ttl_hours=kw.get("ttl_hours"),
        max_depth=kw.get("max_depth"),
    ),
    "push":        lambda agent_id=None, **kw: tool_push(
        task=kw["task"],
        top_k=kw.get("top_k", 5),
        agent=kw.get("agent", "unknown"),
        project=kw.get("project"),
    ),
    "push_report": lambda agent_id=None, **kw: tool_push_report(push_id=kw["push_id"]),
    "vsearch":     lambda agent_id=None, **kw: tool_vsearch(
        query=kw["query"],
        limit=kw.get("limit", 10),
        alpha=kw.get("alpha", 0.5),
        tables=kw.get("tables", "memories,events,context"),
        vec_only=kw.get("vec_only", False),
        agent=kw.get("agent", "unknown"),
    ),
}
