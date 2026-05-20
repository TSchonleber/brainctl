"""brainctl MCP tools — connectome graph (Avenue 5 from research memo).

Phase 1 first-class representation of the inter-subsystem
communication graph. Walks the existing code base + design proposals
for the seed edges; Phase 2 will provide query tools for path-finding,
cycle detection, and impact analysis; Phase 3 auto-updates from
runtime observations.

See research/autonomous-research-avenues-2026-05-20.md §Avenue 5 for
the design rationale.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from agentmemory.lib.mcp_helpers import open_db
from agentmemory.paths import get_db_path

DB_PATH: Path = get_db_path()

VALID_NODE_CATEGORIES = {"subsystem", "table", "dial", "event_bus", "external"}
VALID_EDGE_TYPES = {
    "writes_to", "reads_from", "modulates", "gates",
    "depends_on", "broadcasts_to",
}


def _db() -> sqlite3.Connection:
    return open_db(str(DB_PATH))


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _require_schema(conn: sqlite3.Connection) -> str | None:
    for t in ("connectome_nodes", "connectome_edges"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone():
            return (f"connectome schema missing: {t} not found. "
                    "Run `brainctl migrate` (migration 073).")
    return None


def tool_connectome_status(**_kw: Any) -> dict[str, Any]:
    """Connectome graph health summary."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        node_count = conn.execute("SELECT COUNT(*) FROM connectome_nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM connectome_edges").fetchone()[0]
        by_category = _rows(conn.execute(
            "SELECT category, COUNT(*) AS n FROM connectome_nodes GROUP BY category"
        ).fetchall())
        by_edge_type = _rows(conn.execute(
            "SELECT edge_type, COUNT(*) AS n FROM connectome_edges GROUP BY edge_type"
        ).fetchall())
        top_degree = _rows(conn.execute(
            """
            SELECT n.name, n.category,
                   (SELECT COUNT(*) FROM connectome_edges WHERE source_id = n.id) AS out_degree,
                   (SELECT COUNT(*) FROM connectome_edges WHERE target_id = n.id) AS in_degree,
                   (SELECT COUNT(*) FROM connectome_edges
                       WHERE source_id = n.id OR target_id = n.id) AS total_degree
              FROM connectome_nodes n
             ORDER BY total_degree DESC LIMIT 10
            """
        ).fetchall())
    return {
        "ok": True,
        "node_count": node_count,
        "edge_count": edge_count,
        "nodes_by_category": by_category,
        "edges_by_type": by_edge_type,
        "top_degree": top_degree,
    }


def tool_connectome_node_get(name: str, **_kw: Any) -> dict[str, Any]:
    """Get a node by name with its incoming + outgoing edges."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        node = conn.execute(
            "SELECT * FROM connectome_nodes WHERE name = ?", (name,)
        ).fetchone()
        if not node:
            return {"error": f"node {name!r} not found"}
        outgoing = _rows(conn.execute(
            """
            SELECT e.id, e.edge_type, e.weight, e.description, e.evidence_source,
                   t.name AS target, t.category AS target_category
              FROM connectome_edges e
              JOIN connectome_nodes t ON t.id = e.target_id
             WHERE e.source_id = ?
             ORDER BY e.weight DESC, t.name
            """, (node["id"],),
        ).fetchall())
        incoming = _rows(conn.execute(
            """
            SELECT e.id, e.edge_type, e.weight, e.description, e.evidence_source,
                   s.name AS source, s.category AS source_category
              FROM connectome_edges e
              JOIN connectome_nodes s ON s.id = e.source_id
             WHERE e.target_id = ?
             ORDER BY e.weight DESC, s.name
            """, (node["id"],),
        ).fetchall())
    return {
        "ok": True,
        "node": dict(node),
        "outgoing": outgoing,
        "incoming": incoming,
        "out_degree": len(outgoing),
        "in_degree": len(incoming),
    }


def tool_connectome_register_node(
    name: str, category: str, description: str | None = None,
    schema_version_introduced: int | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if category not in VALID_NODE_CATEGORIES:
        return {"error": f"invalid category {category!r}; expected one of {sorted(VALID_NODE_CATEGORIES)}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        conn.execute(
            """
            INSERT INTO connectome_nodes (name, category, description, schema_version_introduced)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              category = excluded.category,
              description = COALESCE(excluded.description, connectome_nodes.description),
              schema_version_introduced = COALESCE(excluded.schema_version_introduced, connectome_nodes.schema_version_introduced)
            """,
            (name, category, description, schema_version_introduced),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM connectome_nodes WHERE name = ?", (name,)
        ).fetchone()
    return {"ok": True, "node": dict(row) if row else None}


def tool_connectome_register_edge(
    source: str, target: str, edge_type: str,
    weight: float = 1.0, description: str | None = None,
    evidence_source: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if edge_type not in VALID_EDGE_TYPES:
        return {"error": f"invalid edge_type {edge_type!r}; expected one of {sorted(VALID_EDGE_TYPES)}"}
    if not 0.0 <= weight <= 1.0:
        return {"error": "weight must be in [0, 1]"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        src = conn.execute("SELECT id FROM connectome_nodes WHERE name = ?", (source,)).fetchone()
        tgt = conn.execute("SELECT id FROM connectome_nodes WHERE name = ?", (target,)).fetchone()
        if not src:
            return {"error": f"source node {source!r} not registered"}
        if not tgt:
            return {"error": f"target node {target!r} not registered"}
        conn.execute(
            """
            INSERT INTO connectome_edges
              (source_id, target_id, edge_type, weight, description, evidence_source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, target_id, edge_type) DO UPDATE SET
              weight = excluded.weight,
              description = COALESCE(excluded.description, connectome_edges.description),
              evidence_source = COALESCE(excluded.evidence_source, connectome_edges.evidence_source),
              last_observed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
            """,
            (src["id"], tgt["id"], edge_type, float(weight), description, evidence_source),
        )
        conn.commit()
    return {
        "ok": True, "source": source, "target": target, "edge_type": edge_type,
        "weight": float(weight),
    }


def tool_connectome_neighbors(
    name: str, direction: str = "out",
    edge_type: str | None = None, depth: int = 1,
    **_kw: Any,
) -> dict[str, Any]:
    """BFS-walk the connectome from `name` up to `depth` hops in
    `direction` ('in' | 'out' | 'both')."""
    if direction not in {"in", "out", "both"}:
        return {"error": "direction must be in {'in', 'out', 'both'}"}
    if depth < 1 or depth > 6:
        return {"error": "depth must be in [1, 6]"}
    if edge_type is not None and edge_type not in VALID_EDGE_TYPES:
        return {"error": f"invalid edge_type {edge_type!r}"}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        err = _require_schema(conn)
        if err:
            return {"error": err}
        root = conn.execute(
            "SELECT id FROM connectome_nodes WHERE name = ?", (name,)
        ).fetchone()
        if not root:
            return {"error": f"node {name!r} not found"}
        et_clause = ("AND edge_type = ?",) if edge_type else ("",)
        et_params = (edge_type,) if edge_type else ()
        visited = {root["id"]: 0}
        frontier = {root["id"]}
        paths: list[dict[str, Any]] = []
        for hop in range(1, depth + 1):
            next_frontier: set[int] = set()
            for nid in frontier:
                if direction in {"out", "both"}:
                    rows = conn.execute(
                        f"""
                        SELECT target_id AS other, edge_type, weight
                          FROM connectome_edges WHERE source_id = ? {et_clause[0]}
                        """,
                        (nid, *et_params),
                    ).fetchall()
                    for r in rows:
                        if r["other"] not in visited:
                            visited[r["other"]] = hop
                            next_frontier.add(r["other"])
                            paths.append({"from_id": nid, "to_id": r["other"],
                                          "edge_type": r["edge_type"],
                                          "weight": r["weight"], "hop": hop,
                                          "direction": "out"})
                if direction in {"in", "both"}:
                    rows = conn.execute(
                        f"""
                        SELECT source_id AS other, edge_type, weight
                          FROM connectome_edges WHERE target_id = ? {et_clause[0]}
                        """,
                        (nid, *et_params),
                    ).fetchall()
                    for r in rows:
                        if r["other"] not in visited:
                            visited[r["other"]] = hop
                            next_frontier.add(r["other"])
                            paths.append({"from_id": r["other"], "to_id": nid,
                                          "edge_type": r["edge_type"],
                                          "weight": r["weight"], "hop": hop,
                                          "direction": "in"})
            frontier = next_frontier
            if not frontier:
                break
        # Resolve IDs to names for the output.
        id_to_name = {
            r["id"]: r["name"] for r in conn.execute(
                f"SELECT id, name FROM connectome_nodes WHERE id IN ({','.join('?' * len(visited))})",
                tuple(visited.keys()),
            ).fetchall()
        }
        nodes_out = [{"name": id_to_name[nid], "hop": hop} for nid, hop in visited.items()]
        paths_out = [
            {**p, "from": id_to_name.get(p["from_id"]), "to": id_to_name.get(p["to_id"])}
            for p in paths
        ]
    return {
        "ok": True, "root": name, "direction": direction, "depth": depth,
        "edge_type_filter": edge_type, "nodes": nodes_out, "edges": paths_out,
    }


TOOLS: list[Tool] = [
    Tool(
        name="connectome_status",
        description=(
            "Connectome graph health: node/edge counts, breakdown by category + edge_type, "
            "and top-10 by total degree."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="connectome_node_get",
        description=(
            "Get one connectome node by name + all its incoming and outgoing edges, "
            "sorted by weight."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
    Tool(
        name="connectome_register_node",
        description=(
            "Idempotent UPSERT on connectome_nodes. category ∈ {subsystem, table, dial, "
            "event_bus, external}. Use when a new subsystem ships."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "category": {"type": "string", "enum": sorted(VALID_NODE_CATEGORIES)},
                "description": {"type": "string"},
                "schema_version_introduced": {"type": "integer"},
            },
            "required": ["name", "category"],
        },
    ),
    Tool(
        name="connectome_register_edge",
        description=(
            "Idempotent UPSERT on connectome_edges. edge_type ∈ {writes_to, reads_from, "
            "modulates, gates, depends_on, broadcasts_to}. weight in [0, 1]. UPSERT key is "
            "(source, target, edge_type)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "edge_type": {"type": "string", "enum": sorted(VALID_EDGE_TYPES)},
                "weight": {"type": "number", "default": 1.0},
                "description": {"type": "string"},
                "evidence_source": {"type": "string"},
            },
            "required": ["source", "target", "edge_type"],
        },
    ),
    Tool(
        name="connectome_neighbors",
        description=(
            "BFS-walk from a node up to `depth` hops in `direction` ('in', 'out', 'both'). "
            "Optional edge_type filter. Returns nodes (with hop count) + edges. Use for "
            "impact analysis ('what depends on X') and reachability queries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "direction": {"type": "string", "enum": ["in", "out", "both"], "default": "out"},
                "edge_type": {"type": "string", "enum": sorted(VALID_EDGE_TYPES)},
                "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 6},
            },
            "required": ["name"],
        },
    ),
]


_CONNECTOME_TOOLS = {
    "connectome_status": tool_connectome_status,
    "connectome_node_get": tool_connectome_node_get,
    "connectome_register_node": tool_connectome_register_node,
    "connectome_register_edge": tool_connectome_register_edge,
    "connectome_neighbors": tool_connectome_neighbors,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _CONNECTOME_TOOLS.items()
}


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
