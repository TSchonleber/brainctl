"""brainctl MCP tools — cross-agent entity reconciliation."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from mcp.types import Tool

DB_PATH = Path(os.environ.get("BRAIN_DB", str(Path.home() / "agentmemory" / "db" / "brain.db")))


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


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _load_json_list(s: str | None) -> list:
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _load_json_dict(s: str | None) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _name_similarity(a: str, b: str) -> tuple[float, str]:
    """Return (confidence, reason) for the best name match found."""
    al, bl = a.lower(), b.lower()
    # Exact (case-insensitive)
    if al == bl:
        return 1.0, "exact_match"
    # Substring containment
    if al in bl or bl in al:
        # Confidence relative to shorter-vs-longer length ratio
        shorter = min(len(al), len(bl))
        longer = max(len(al), len(bl))
        conf = 0.85 + 0.1 * (shorter / longer)
        return min(conf, 0.99), "name_substring"
    # Common prefix / suffix (≥ 4 chars)
    prefix_len = 0
    for ca, cb in zip(al, bl):
        if ca == cb:
            prefix_len += 1
        else:
            break
    suffix_len = 0
    for ca, cb in zip(reversed(al), reversed(bl)):
        if ca == cb:
            suffix_len += 1
        else:
            break
    max_overlap = max(prefix_len, suffix_len)
    shorter = min(len(al), len(bl))
    if shorter > 0 and max_overlap >= 4 and max_overlap / shorter >= 0.6:
        return 0.75 + 0.1 * (max_overlap / shorter), "common_prefix_suffix"
    # SequenceMatcher
    ratio = SequenceMatcher(None, al, bl).ratio()
    if ratio > 0:
        return ratio, "fuzzy_name"
    return 0.0, "no_match"


def _observations_overlap(obs_a: list, obs_b: list) -> float:
    """Return fraction of overlapping observations (0-1)."""
    if not obs_a or not obs_b:
        return 0.0
    set_a = {o.lower().strip() for o in obs_a if isinstance(o, str)}
    set_b = {o.lower().strip() for o in obs_b if isinstance(o, str)}
    if not set_a or not set_b:
        return 0.0
    inter = set_a & set_b
    union = set_a | set_b
    return len(inter) / len(union)


# ---------------------------------------------------------------------------
# Tool: entity_duplicates_scan
# ---------------------------------------------------------------------------

def tool_entity_duplicates_scan(
    agent_id: str = "mcp-client",
    entity_type: str | None = None,
    similarity_threshold: float = 0.8,
    limit: int = 50,
    **kw,
) -> dict:
    """Find potential duplicate entities using name similarity and observation overlap."""
    try:
        conn = _db()
        sql = "SELECT id, name, entity_type, observations, properties, agent_id, created_at FROM entities WHERE retired_at IS NULL"
        params: list[Any] = []
        if entity_type:
            sql += " AND entity_type = ?"
            params.append(entity_type)
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
        conn.close()

        entities = [dict(r) for r in rows]
        # Parse observations once
        for e in entities:
            e["_obs"] = _load_json_list(e.get("observations"))

        # O(n²) pairwise comparison — limited by limit param
        groups: list[dict] = []
        seen_ids: set[frozenset] = set()

        for i, ea in enumerate(entities):
            for eb in entities[i + 1:]:
                if ea["entity_type"] != eb["entity_type"]:
                    continue  # only compare same type
                pair_key = frozenset({ea["id"], eb["id"]})
                if pair_key in seen_ids:
                    continue

                name_conf, reason = _name_similarity(ea["name"], eb["name"])
                obs_overlap = _observations_overlap(ea["_obs"], eb["_obs"])

                # Boost confidence if observations overlap
                confidence = name_conf
                if obs_overlap >= 0.3:
                    confidence = min(0.99, confidence + 0.05 * obs_overlap)
                    if reason == "fuzzy_name":
                        reason = "fuzzy_name+obs_overlap"

                if confidence >= similarity_threshold:
                    seen_ids.add(pair_key)
                    # Primary = the older entity (lower id)
                    primary, duplicate = (ea, eb) if ea["id"] < eb["id"] else (eb, ea)
                    groups.append({
                        "primary": {k: v for k, v in primary.items() if not k.startswith("_")},
                        "duplicates": [{k: v for k, v in duplicate.items() if not k.startswith("_")}],
                        "confidence": round(confidence, 4),
                        "reason": reason,
                        "obs_overlap": round(obs_overlap, 4),
                    })
                    if len(groups) >= limit:
                        break
            if len(groups) >= limit:
                break

        return {
            "ok": True,
            "threshold": similarity_threshold,
            "entity_type": entity_type,
            "duplicate_groups": groups,
            "total_candidates": len(groups),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: entity_merge
# ---------------------------------------------------------------------------

def tool_entity_merge(
    agent_id: str = "mcp-client",
    primary_id: int = 0,
    duplicate_ids: list | None = None,
    dry_run: bool = True,
    **kw,
) -> dict:
    """Merge one or more duplicate entities into a primary entity."""
    if not primary_id:
        return {"ok": False, "error": "primary_id is required"}
    if not duplicate_ids:
        return {"ok": False, "error": "duplicate_ids must be a non-empty list"}

    try:
        conn = _db()

        # Load primary entity
        primary = conn.execute(
            "SELECT * FROM entities WHERE id = ? AND retired_at IS NULL", (primary_id,)
        ).fetchone()
        if not primary:
            conn.close()
            return {"ok": False, "error": f"Primary entity {primary_id} not found or retired"}

        primary = dict(primary)
        primary_obs = _load_json_list(primary.get("observations"))
        primary_props = _load_json_dict(primary.get("properties"))

        merged_obs: list[str] = list(primary_obs)
        merged_props: dict = dict(primary_props)

        dup_rows: list[dict] = []
        for dup_id in duplicate_ids:
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ? AND retired_at IS NULL", (dup_id,)
            ).fetchone()
            if not row:
                conn.close()
                return {"ok": False, "error": f"Duplicate entity {dup_id} not found or already retired"}
            dup_rows.append(dict(row))

        # Collect all observations (deduplicated, order-preserving)
        seen_obs = {o.lower().strip() for o in merged_obs}
        for dup in dup_rows:
            for obs in _load_json_list(dup.get("observations")):
                key = obs.lower().strip()
                if key not in seen_obs:
                    merged_obs.append(obs)
                    seen_obs.add(key)

        # Merge properties: primary wins on conflict
        for dup in dup_rows:
            for k, v in _load_json_dict(dup.get("properties")).items():
                if k not in merged_props:
                    merged_props[k] = v

        # Collect edge redirections needed
        edges_to_redirect: list[dict] = []  # {dup_edge_id, source_table, source_id, target_table, target_id, relation_type, weight}
        for dup in dup_rows:
            dup_id = dup["id"]
            # Edges where duplicate is source
            src_edges = conn.execute(
                "SELECT id, source_table, source_id, target_table, target_id, relation_type, weight "
                "FROM knowledge_edges WHERE source_table = 'entities' AND source_id = ?",
                (dup_id,),
            ).fetchall()
            for e in src_edges:
                edges_to_redirect.append({"direction": "source", **dict(e)})

            # Edges where duplicate is target
            tgt_edges = conn.execute(
                "SELECT id, source_table, source_id, target_table, target_id, relation_type, weight "
                "FROM knowledge_edges WHERE target_table = 'entities' AND target_id = ?",
                (dup_id,),
            ).fetchall()
            for e in tgt_edges:
                edges_to_redirect.append({"direction": "target", **dict(e)})

        if dry_run:
            conn.close()
            return {
                "ok": True,
                "dry_run": True,
                "primary_id": primary_id,
                "duplicate_ids": duplicate_ids,
                "merged_count": len(dup_rows),
                "observations_combined": len(merged_obs),
                "observations_added": len(merged_obs) - len(primary_obs),
                "edges_to_redirect": len(edges_to_redirect),
                "properties_merged": dict(merged_props),
                "observations_preview": merged_obs[:10],
            }

        # === WRITE ===
        edges_redirected = 0
        for edge in edges_to_redirect:
            eid = edge["id"]
            if edge["direction"] == "source":
                # New source would be primary_id
                new_src_id = primary_id
                conflict = conn.execute(
                    "SELECT id, weight FROM knowledge_edges "
                    "WHERE source_table='entities' AND source_id=? "
                    "AND target_table=? AND target_id=? AND relation_type=?",
                    (new_src_id, edge["target_table"], edge["target_id"], edge["relation_type"]),
                ).fetchone()
                if conflict:
                    # Keep higher weight, delete duplicate's edge
                    if edge["weight"] > conflict["weight"]:
                        conn.execute(
                            "UPDATE knowledge_edges SET weight=? WHERE id=?",
                            (edge["weight"], conflict["id"]),
                        )
                    conn.execute("DELETE FROM knowledge_edges WHERE id=?", (eid,))
                else:
                    conn.execute(
                        "UPDATE knowledge_edges SET source_id=? WHERE id=?",
                        (new_src_id, eid),
                    )
            else:
                # New target would be primary_id
                new_tgt_id = primary_id
                conflict = conn.execute(
                    "SELECT id, weight FROM knowledge_edges "
                    "WHERE source_table=? AND source_id=? "
                    "AND target_table='entities' AND target_id=? AND relation_type=?",
                    (edge["source_table"], edge["source_id"], new_tgt_id, edge["relation_type"]),
                ).fetchone()
                if conflict:
                    if edge["weight"] > conflict["weight"]:
                        conn.execute(
                            "UPDATE knowledge_edges SET weight=? WHERE id=?",
                            (edge["weight"], conflict["id"]),
                        )
                    conn.execute("DELETE FROM knowledge_edges WHERE id=?", (eid,))
                else:
                    conn.execute(
                        "UPDATE knowledge_edges SET target_id=? WHERE id=?",
                        (new_tgt_id, eid),
                    )
            edges_redirected += 1

        # Update primary entity with merged data
        conn.execute(
            "UPDATE entities SET observations=?, properties=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
            (json.dumps(merged_obs), json.dumps(merged_props), primary_id),
        )

        # Soft-delete duplicates
        for dup in dup_rows:
            conn.execute(
                "UPDATE entities SET retired_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (dup["id"],),
            )

        # Ensure agent exists for the event log
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES (?, ?, 'mcp', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (agent_id, agent_id),
        )

        # Log memory_merged event
        meta = json.dumps({
            "primary_id": primary_id,
            "duplicate_ids": duplicate_ids,
            "merged_count": len(dup_rows),
            "observations_combined": len(merged_obs),
            "edges_redirected": edges_redirected,
        })
        conn.execute(
            "INSERT INTO events (agent_id, event_type, summary, metadata, importance, created_at) "
            "VALUES (?, 'memory_merged', ?, ?, 0.7, strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (
                agent_id,
                f"Merged {len(dup_rows)} duplicate(s) into entity #{primary_id} ({primary['name']})",
                meta,
            ),
        )

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "dry_run": False,
            "primary_id": primary_id,
            "merged_count": len(dup_rows),
            "observations_combined": len(merged_obs),
            "edges_redirected": edges_redirected,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: entity_aliases
# ---------------------------------------------------------------------------

def tool_entity_aliases(
    agent_id: str = "mcp-client",
    entity_id: int = 0,
    **kw,
) -> dict:
    """List known aliases for an entity (from properties and alias_of edges)."""
    if not entity_id:
        return {"ok": False, "error": "entity_id is required"}
    try:
        conn = _db()
        row = conn.execute(
            "SELECT id, name, properties FROM entities WHERE id = ? AND retired_at IS NULL",
            (entity_id,),
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": f"Entity {entity_id} not found or retired"}

        props = _load_json_dict(row["properties"])
        stored_aliases: list[str] = props.get("aliases", [])
        if not isinstance(stored_aliases, list):
            stored_aliases = []

        # Find alias_of edges pointing to or from this entity
        edge_aliases: list[dict] = []
        # Edges where this entity is the subject (source) and relation is alias_of
        out_edges = conn.execute(
            "SELECT ke.id, e.name, e.id as linked_id FROM knowledge_edges ke "
            "JOIN entities e ON ke.target_id = e.id AND ke.target_table = 'entities' "
            "WHERE ke.source_table = 'entities' AND ke.source_id = ? AND ke.relation_type = 'alias_of'",
            (entity_id,),
        ).fetchall()
        for e in out_edges:
            edge_aliases.append({"entity_id": e["linked_id"], "name": e["name"], "direction": "alias_of"})

        # Edges where this entity is the target of an alias_of
        in_edges = conn.execute(
            "SELECT ke.id, e.name, e.id as linked_id FROM knowledge_edges ke "
            "JOIN entities e ON ke.source_id = e.id AND ke.source_table = 'entities' "
            "WHERE ke.target_table = 'entities' AND ke.target_id = ? AND ke.relation_type = 'alias_of'",
            (entity_id,),
        ).fetchall()
        for e in in_edges:
            edge_aliases.append({"entity_id": e["linked_id"], "name": e["name"], "direction": "aliased_by"})

        conn.close()
        return {
            "ok": True,
            "entity_id": entity_id,
            "entity_name": row["name"],
            "stored_aliases": stored_aliases,
            "edge_aliases": edge_aliases,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def tool_entity_add_alias(
    agent_id: str = "mcp-client",
    entity_id: int = 0,
    alias_name: str = "",
    **kw,
) -> dict:
    """Record that alias_name is another name for entity_id."""
    if not entity_id:
        return {"ok": False, "error": "entity_id is required"}
    if not alias_name or not alias_name.strip():
        return {"ok": False, "error": "alias_name must not be empty"}

    alias_name = alias_name.strip()
    try:
        conn = _db()
        row = conn.execute(
            "SELECT id, name, properties FROM entities WHERE id = ? AND retired_at IS NULL",
            (entity_id,),
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": f"Entity {entity_id} not found or retired"}

        # Store alias in properties["aliases"]
        props = _load_json_dict(row["properties"])
        aliases: list[str] = props.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        if alias_name not in aliases:
            aliases.append(alias_name)
        props["aliases"] = aliases

        conn.execute(
            "UPDATE entities SET properties=?, updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
            (json.dumps(props), entity_id),
        )

        # If an entity with alias_name exists, also create a knowledge_edge alias_of
        edge_created = False
        alias_entity = conn.execute(
            "SELECT id FROM entities WHERE name = ? AND retired_at IS NULL LIMIT 1",
            (alias_name,),
        ).fetchone()
        if alias_entity:
            alias_eid = alias_entity["id"]
            # Ensure calling agent exists
            conn.execute(
                "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
                "VALUES (?, ?, 'mcp', 'active', strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
                (agent_id, agent_id),
            )
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_edges "
                    "(source_table, source_id, target_table, target_id, relation_type, agent_id, created_at) "
                    "VALUES ('entities', ?, 'entities', ?, 'alias_of', ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
                    (entity_id, alias_eid, agent_id),
                )
                edge_created = True
            except Exception:
                pass  # already exists or constraint violation

        conn.commit()
        conn.close()
        return {
            "ok": True,
            "entity_id": entity_id,
            "entity_name": row["name"],
            "alias_name": alias_name,
            "aliases": aliases,
            "edge_created": edge_created,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: entity_cross_agent_view
# ---------------------------------------------------------------------------

def tool_entity_cross_agent_view(
    agent_id: str = "mcp-client",
    entity_name: str = "",
    **kw,
) -> dict:
    """Show how different agents perceive the same entity (by name)."""
    if not entity_name or not entity_name.strip():
        return {"ok": False, "error": "entity_name is required"}
    entity_name = entity_name.strip()
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT id, name, entity_type, observations, properties, agent_id, created_at, scope "
            "FROM entities WHERE name = ? AND retired_at IS NULL ORDER BY created_at",
            (entity_name,),
        ).fetchall()

        agents_view: list[dict] = []
        all_obs_seen: set[str] = set()
        all_observations: list[str] = []
        all_properties: dict = {}

        for r in rows:
            obs = _load_json_list(r["observations"])
            props = _load_json_dict(r["properties"])
            agents_view.append({
                "agent_id": r["agent_id"],
                "entity_id": r["id"],
                "scope": r["scope"],
                "entity_type": r["entity_type"],
                "observations": obs,
                "properties": props,
                "created_at": r["created_at"],
            })
            # Merge for combined view
            for o in obs:
                key = o.lower().strip()
                if key not in all_obs_seen:
                    all_observations.append(o)
                    all_obs_seen.add(key)
            for k, v in props.items():
                if k not in all_properties:
                    all_properties[k] = v

        conn.close()
        return {
            "ok": True,
            "entity_name": entity_name,
            "agent_count": len(agents_view),
            "agents": agents_view,
            "merged_view": {
                "all_observations": all_observations,
                "all_properties": all_properties,
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: entity_reconcile_report
# ---------------------------------------------------------------------------

def tool_entity_reconcile_report(
    agent_id: str = "mcp-client",
    entity_type: str | None = None,
    **kw,
) -> dict:
    """Full audit: entity counts, duplication rate, top merge candidates."""
    try:
        conn = _db()

        sql_base = "SELECT id, name, entity_type, agent_id, observations FROM entities WHERE retired_at IS NULL"
        params: list[Any] = []
        if entity_type:
            sql_base += " AND entity_type = ?"
            params.append(entity_type)

        rows = conn.execute(sql_base, params).fetchall()
        total_entities = len(rows)

        # Count unique names (case-insensitive)
        name_map: dict[str, list[dict]] = {}
        for r in rows:
            key = r["name"].lower().strip()
            name_map.setdefault(key, []).append(dict(r))

        unique_names = len(name_map)
        # Names with >1 entity
        dup_names = {k: v for k, v in name_map.items() if len(v) > 1}
        duplication_rate = len(dup_names) / unique_names if unique_names > 0 else 0.0

        # Find top scan candidates using duplicate scan (threshold=0.75)
        conn.close()

        scan_result = tool_entity_duplicates_scan(
            agent_id=agent_id,
            entity_type=entity_type,
            similarity_threshold=0.75,
            limit=20,
        )
        top_duplicates = scan_result.get("duplicate_groups", []) if scan_result.get("ok") else []

        return {
            "ok": True,
            "entity_type": entity_type,
            "total_entities": total_entities,
            "unique_names": unique_names,
            "duplication_rate": round(duplication_rate, 4),
            "exact_duplicate_name_groups": len(dup_names),
            "top_duplicates": top_duplicates[:10],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tool descriptors
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="entity_duplicates_scan",
        description=(
            "Scan for potential duplicate entities using name similarity (exact, substring, "
            "fuzzy) and observation overlap. Returns grouped duplicate candidates with "
            "confidence scores and match reasons. Use before merging to identify targets."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "entity_type": {
                    "type": "string",
                    "description": "Restrict scan to a specific entity type (e.g. 'person')",
                },
                "similarity_threshold": {
                    "type": "number",
                    "description": "Minimum similarity confidence to report (default 0.8)",
                    "default": 0.8,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of duplicate groups to return (default 50)",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="entity_merge",
        description=(
            "Merge one or more duplicate entities into a primary canonical entity. "
            "Combines all observations (deduplicated), merges properties (primary wins), "
            "redirects all knowledge_edges, soft-deletes duplicates, and logs a memory_merged event. "
            "Use dry_run=true (default) to preview the merge without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "primary_id": {
                    "type": "integer",
                    "description": "ID of the entity to keep as the canonical record",
                },
                "duplicate_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of entity IDs to merge into primary and retire",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true (default), preview only — do not write",
                    "default": True,
                },
            },
            "required": ["primary_id", "duplicate_ids"],
        },
    ),
    Tool(
        name="entity_aliases",
        description=(
            "List all known aliases for an entity — both names stored in entity properties "
            "and alias_of relationships recorded in knowledge_edges."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "entity_id": {
                    "type": "integer",
                    "description": "ID of the entity to retrieve aliases for",
                },
            },
            "required": ["entity_id"],
        },
    ),
    Tool(
        name="entity_add_alias",
        description=(
            "Record that alias_name is another name for the given entity. "
            "Stores the alias in entity properties and, if an entity with that name exists, "
            "creates an alias_of edge in knowledge_edges."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "entity_id": {
                    "type": "integer",
                    "description": "ID of the entity to add an alias to",
                },
                "alias_name": {
                    "type": "string",
                    "description": "The alternative name (alias) for this entity",
                },
            },
            "required": ["entity_id", "alias_name"],
        },
    ),
    Tool(
        name="entity_cross_agent_view",
        description=(
            "Show how different agents perceive the same entity by name. "
            "Returns per-agent observations and properties, plus a merged view combining all."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "entity_name": {
                    "type": "string",
                    "description": "Exact entity name to look up across all agents",
                },
            },
            "required": ["entity_name"],
        },
    ),
    Tool(
        name="entity_reconcile_report",
        description=(
            "Full audit of entity state: total entities, unique names, duplication rate, "
            "exact duplicate name groups, and top merge candidates. "
            "Useful for periodic hygiene checks across multi-agent deployments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Calling agent ID"},
                "entity_type": {
                    "type": "string",
                    "description": "Restrict report to a specific entity type",
                },
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

DISPATCH: dict = {
    "entity_duplicates_scan": tool_entity_duplicates_scan,
    "entity_merge": tool_entity_merge,
    "entity_aliases": tool_entity_aliases,
    "entity_add_alias": tool_entity_add_alias,
    "entity_cross_agent_view": tool_entity_cross_agent_view,
    "entity_reconcile_report": tool_entity_reconcile_report,
}
