"""brainctl UI — lightweight web dashboard served from Python's http.server."""
import json, os, re, sqlite3, webbrowser, argparse, threading, urllib.request, urllib.error, subprocess, time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from agentmemory.paths import get_brain_home, get_db_path

DB_PATH = str(get_db_path())
STATIC = Path(__file__).parent / "static"
DEFAULT_AGENT_ROSTER_URL = os.environ.get("BRAINCTL_AGENT_ROSTER_URL")
DEFAULT_AGENT_ROSTER_FILE = os.environ.get("BRAINCTL_AGENT_ROSTER_FILE")
DEFAULT_AGENT_ROSTER_TOKEN = os.environ.get("BRAINCTL_AGENT_ROSTER_TOKEN")
# External agent registry (optional — set env vars to enable)
DEFAULT_EXTERNAL_API_URL = os.environ.get("BRAINCTL_EXTERNAL_API_URL", "")
DEFAULT_EXTERNAL_KEY_FILE = Path(os.environ.get("BRAINCTL_EXTERNAL_KEY_FILE", str(get_brain_home() / "api-key.json")))
DEFAULT_QUIET_HOURS_SCRIPT = get_brain_home() / "bin" / "quiet-hours-start.py"
REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_CACHE = {"checked_at": 0.0, "payload": None}
UPDATE_TTL_SECONDS = 300


def get_db(db_path=None):
    p = db_path or DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


def _json_loads(raw, default):
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _git_output(*args):
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=6,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _resolve_default_branch():
    symref = _git_output("ls-remote", "--symref", "origin", "HEAD")
    if symref:
        for line in symref.splitlines():
            if line.startswith("ref:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    return parts[1].split("/")[-1]
    return "main"


def get_update_status(force=False):
    now = time.time()
    if not force and UPDATE_CACHE["payload"] and (now - UPDATE_CACHE["checked_at"]) < UPDATE_TTL_SECONDS:
        return UPDATE_CACHE["payload"]

    local_commit = _git_output("rev-parse", "HEAD")
    local_short = _git_output("rev-parse", "--short", "HEAD")
    current_branch = _git_output("rev-parse", "--abbrev-ref", "HEAD")
    remote_url = _git_output("remote", "get-url", "origin")
    default_branch = _resolve_default_branch() if remote_url else None
    remote_commit = None
    remote_short = None
    update_available = False
    error = None

    if remote_url and default_branch:
        ls_remote = _git_output("ls-remote", "origin", f"refs/heads/{default_branch}")
        if ls_remote:
            remote_commit = ls_remote.split()[0]
            remote_short = remote_commit[:7]
            update_available = bool(local_commit and remote_commit and local_commit != remote_commit)
        else:
            error = "remote_unreachable"
    else:
        error = "not_a_git_checkout"

    payload = {
        "local_commit": local_short,
        "local_branch": current_branch,
        "remote_commit": remote_short,
        "default_branch": default_branch,
        "remote_url": remote_url,
        "update_available": update_available,
        "checked_at": int(now),
        "error": error,
    }
    UPDATE_CACHE["checked_at"] = now
    UPDATE_CACHE["payload"] = payload
    return payload


def _resolve_external_company_id():
    """Resolve external registry company/org ID from env or config."""
    if os.environ.get("BRAINCTL_EXTERNAL_COMPANY_ID"):
        return os.environ["BRAINCTL_EXTERNAL_COMPANY_ID"]
    # Legacy fallback for external registries
    if os.environ.get("EXTERNAL_COMPANY_ID"):
        return os.environ["EXTERNAL_COMPANY_ID"]
    if DEFAULT_QUIET_HOURS_SCRIPT.exists():
        match = re.search(r'COMPANY_ID\s*=\s*"([^"]+)"', DEFAULT_QUIET_HOURS_SCRIPT.read_text())
        if match:
            return match.group(1)
    return None


def _resolve_external_api_key():
    """Resolve external registry API key from env or key file."""
    if os.environ.get("BRAINCTL_EXTERNAL_API_KEY"):
        return os.environ["BRAINCTL_EXTERNAL_API_KEY"]
    if os.environ.get("EXTERNAL_API_KEY"):
        return os.environ["EXTERNAL_API_KEY"]
    if DEFAULT_EXTERNAL_KEY_FILE.exists():
        try:
            payload = json.loads(DEFAULT_EXTERNAL_KEY_FILE.read_text())
            return payload.get("token")
        except Exception:
            return None
    return None


def _resolve_external_registry_config():
    """Build config for an external agent registry API (optional)."""
    api_url = DEFAULT_EXTERNAL_API_URL
    api_key = _resolve_external_api_key()
    company_id = _resolve_external_company_id()
    if not api_url or not api_key or not company_id:
        return None
    return {"api_url": api_url.rstrip("/"), "api_key": api_key, "company_id": company_id}


def _fetch_json(url, bearer_token=None):
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode())


def _extract_agent_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("agents", "items", "data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _normalize_external_agent(agent, source_name, source_context=None):
    if not isinstance(agent, dict):
        return None
    agent_id = (
        agent.get("id")
        or agent.get("agentId")
        or agent.get("agent_id")
        or agent.get("slug")
        or agent.get("name")
    )
    if not agent_id:
        return None

    display_name = (
        agent.get("display_name")
        or agent.get("displayName")
        or agent.get("name")
        or agent.get("title")
        or agent.get("label")
        or agent_id
    )
    runtime_label = (
        agent.get("runtime")
        or agent.get("framework")
        or agent.get("adapter")
        or agent.get("adapterType")
        or agent.get("provider")
        or agent.get("type")
        or source_name
    )
    runtime_config = agent.get("runtimeConfig") or {}
    attention_class = (
        agent.get("attention_class")
        or agent.get("attentionClass")
        or agent.get("priority_tier")
        or agent.get("priorityTier")
        or ("exec" if runtime_config.get("heartbeat", {}).get("enabled") else None)
        or "ic"
    )
    status = (
        agent.get("status")
        or agent.get("state")
        or ("archived" if agent.get("archivedAt") else "active")
    )
    last_seen_at = (
        agent.get("updatedAt")
        or agent.get("lastActiveAt")
        or agent.get("last_seen_at")
        or agent.get("lastSeenAt")
        or agent.get("createdAt")
    )
    adapter_info = json.dumps(
        {
            "source": source_name,
            "source_context": source_context or {},
            "raw": agent,
        }
    )
    return {
        "id": str(agent_id),
        "display_name": str(display_name),
        "agent_type": str(runtime_label),
        "adapter_info": adapter_info,
        "status": str(status),
        "last_seen_at": last_seen_at,
        "attention_class": str(attention_class),
    }


def _load_external_agent_records():
    records = []

    if DEFAULT_AGENT_ROSTER_FILE:
        path = Path(DEFAULT_AGENT_ROSTER_FILE).expanduser()
        if path.exists():
            try:
                payload = json.loads(path.read_text())
                for agent in _extract_agent_list(payload):
                    normalized = _normalize_external_agent(
                        agent,
                        "external_roster_file",
                        {"file": str(path)},
                    )
                    if normalized:
                        records.append(normalized)
            except Exception:
                pass

    if DEFAULT_AGENT_ROSTER_URL:
        try:
            payload = _fetch_json(DEFAULT_AGENT_ROSTER_URL, DEFAULT_AGENT_ROSTER_TOKEN)
            for agent in _extract_agent_list(payload):
                normalized = _normalize_external_agent(
                    agent,
                    "external_roster_url",
                    {"url": DEFAULT_AGENT_ROSTER_URL},
                )
                if normalized:
                    records.append(normalized)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            pass

    config = _resolve_external_registry_config()
    if config:
        try:
            payload = _fetch_json(
                f"{config['api_url']}/api/companies/{config['company_id']}/agents",
                config["api_key"],
            )
            for agent in _extract_agent_list(payload):
                normalized = _normalize_external_agent(
                    agent,
                    "external_registry_api",
                    {"company_id": config["company_id"], "api_url": config["api_url"]},
                )
                if normalized:
                    records.append(normalized)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            pass

    deduped = {}
    for record in records:
        deduped[record["id"]] = record
    return list(deduped.values())


def sync_external_agents(db):
    records = _load_external_agent_records()
    if not records:
        return 0

    synced = 0
    for agent in records:
        db.execute(
            """
            INSERT INTO agents (
                id, display_name, agent_type, adapter_info, status,
                last_seen_at, updated_at, attention_class, attention_budget_tier
            )
            VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')), datetime('now'), ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                agent_type = excluded.agent_type,
                adapter_info = excluded.adapter_info,
                status = excluded.status,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at,
                attention_class = excluded.attention_class,
                attention_budget_tier = excluded.attention_budget_tier
            """,
            (
                agent["id"],
                agent["display_name"],
                agent["agent_type"],
                agent["adapter_info"],
                agent["status"],
                agent["last_seen_at"],
                agent["attention_class"],
                1,
            ),
        )
        synced += 1

    if synced:
        db.commit()

    return synced


def _agent_graph_label(agent, duplicate_names):
    display_name = agent["display_name"] or agent["id"]
    if duplicate_names.get(display_name.lower(), 0) > 1:
        return f"{display_name} [{agent['id']}]"
    return display_name


def _find_agent_entity_candidates(db, agent):
    display_name = agent["display_name"] or agent["id"]
    candidates = db.execute(
        """
        SELECT *
        FROM entities
        WHERE retired_at IS NULL
          AND (
            json_extract(properties, '$.agent_id') = ?
            OR lower(name) = lower(?)
            OR lower(name) = lower(?)
            OR lower(json_extract(properties, '$.real_name')) = lower(?)
          )
        """,
        (
            agent["id"],
            agent["id"],
            display_name,
            display_name,
        ),
    ).fetchall()

    def candidate_score(row):
        props = _json_loads(row["properties"], {})
        source_penalty = 1 if props.get("source") == "agents_sync" else 0
        if row["name"].lower() == agent["id"].lower():
            match_rank = 0
        elif str(props.get("real_name", "")).lower() == display_name.lower():
            match_rank = 1
        elif str(props.get("agent_id", "")).lower() == agent["id"].lower():
            match_rank = 2
        elif row["name"].lower() == display_name.lower():
            match_rank = 3
        else:
            match_rank = 4
        return (source_penalty, match_rank, row["id"])

    return sorted(candidates, key=candidate_score)


def sync_agents_to_entities(db):
    """Backfill active agents into entities so graph and entity views stay aligned."""
    sync_external_agents(db)
    agent_rows = db.execute(
        """
        SELECT id, display_name, agent_type, status, attention_class, last_seen_at
        FROM agents
        WHERE COALESCE(status, 'active') != 'archived'
        """
    ).fetchall()

    synced = 0
    for agent in agent_rows:
        display_name = agent["display_name"] or agent["id"]
        candidates = _find_agent_entity_candidates(db, agent)
        canonical = candidates[0] if candidates else None
        merged_properties = {
            "agent_id": agent["id"],
            "display_name": display_name,
            "agent_type": agent["agent_type"],
            "status": agent["status"],
            "attention_class": agent["attention_class"],
            "last_seen_at": agent["last_seen_at"],
            "source": "agents_sync",
        }
        merged_observations = [
            f"Registered agent {display_name}",
            f"Runtime label: {agent['agent_type']}",
            f"Priority tier: {agent['attention_class']}",
        ]

        if canonical:
            props = _json_loads(canonical["properties"], {})
            props.update({k: v for k, v in merged_properties.items() if v is not None})
            observations = [
                item for item in _json_loads(canonical["observations"], [])
                if not (
                    isinstance(item, str)
                    and (item.startswith("Agent type: ") or item.startswith("Attention class: "))
                )
            ]
            for item in merged_observations:
                if item not in observations:
                    observations.append(item)
            db.execute(
                """
                UPDATE entities
                SET properties = ?, observations = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    json.dumps(props),
                    json.dumps(observations),
                    canonical["id"],
                ),
            )
            duplicate_ids = [row["id"] for row in candidates[1:] if row["id"] != canonical["id"]]
            if duplicate_ids:
                placeholders = ",".join("?" for _ in duplicate_ids)
                db.execute(
                    f"UPDATE entities SET retired_at = datetime('now'), updated_at = datetime('now') WHERE id IN ({placeholders})",
                    duplicate_ids,
                )
            synced += 1
            continue

        db.execute(
            """
            INSERT INTO entities (name, entity_type, properties, observations, agent_id, confidence, scope)
            VALUES (?, 'agent', ?, ?, ?, ?, 'global')
            """,
            (
                display_name,
                json.dumps(merged_properties),
                json.dumps(merged_observations),
                agent["id"],
                0.9,
            ),
        )
        synced += 1

    if synced:
        db.commit()

    return synced


def load_graph_entities(db):
    sync_agents_to_entities(db)
    nodes = []
    entity_names = {}
    name_to_node_id = {}
    agent_rows = db.execute(
        """
        SELECT id, display_name, agent_type, status, attention_class, last_seen_at
        FROM agents
        WHERE COALESCE(status, 'active') != 'archived'
        """
    ).fetchall()
    duplicate_names = {}
    for agent in agent_rows:
        display_name = agent["display_name"] or agent["id"]
        duplicate_names[display_name.lower()] = duplicate_names.get(display_name.lower(), 0) + 1

    for r in db.execute("SELECT * FROM entities WHERE retired_at IS NULL").fetchall():
        nid = f"entity_{r['id']}"
        props = _json_loads(r["properties"], {})
        label = r["name"]
        if r["entity_type"] == "agent" and props.get("agent_id"):
            label = _agent_graph_label(
                {
                    "id": props.get("agent_id"),
                    "display_name": props.get("display_name") or r["name"],
                },
                duplicate_names,
            )
        elif props.get("agent_id"):
            label = r["name"]
        else:
            label = props.get("display_name") or r["name"]
        obs = _json_loads(r["observations"], [])
        nodes.append(
            {
                "id": nid,
                "label": label,
                "type": r["entity_type"] or "unknown",
                "confidence": r["confidence"] or 0.5,
                "observations": obs[:5],
                "table": "entities",
                "entity_name": r["name"],
                "agent_id": props.get("agent_id"),
                "agent_type": props.get("agent_type"),
                "attention_class": props.get("attention_class"),
                "source": props.get("source", "entity"),
            }
        )
        entity_names[r["id"]] = label
        name_to_node_id[label.lower()] = nid
        name_to_node_id[r["name"].lower()] = nid
        if props.get("agent_id"):
            name_to_node_id[props["agent_id"].lower()] = nid

    for agent in agent_rows:
        if agent["id"].lower() in name_to_node_id:
            continue

        nid = f"agent_{agent['id']}"
        label = _agent_graph_label(agent, duplicate_names)
        observations = [
            f"Registered agent {agent['display_name'] or agent['id']}",
            f"Runtime label: {agent['agent_type']}",
            f"Priority tier: {agent['attention_class']}",
        ]
        nodes.append(
            {
                "id": nid,
                "label": label,
                "type": "agent",
                "confidence": 0.85,
                "observations": observations,
                "table": "agents",
                "entity_name": agent["display_name"] or agent["id"],
                "agent_id": agent["id"],
                "agent_type": agent["agent_type"],
                "attention_class": agent["attention_class"],
                "source": "agents_runtime",
            }
        )
        name_to_node_id[label.lower()] = nid
        name_to_node_id[(agent["display_name"] or agent["id"]).lower()] = nid
        name_to_node_id[agent["id"].lower()] = nid

    return nodes, entity_names, name_to_node_id


def _append_node(nodes, node_index, node):
    if node["id"] in node_index:
        return
    nodes.append(node)
    node_index.add(node["id"])


def _append_edge(edges, edge_index, source, target, label="", weight=1.0, kind="relation"):
    if not source or not target or source == target:
        return
    key = (source, target, kind, label)
    if key in edge_index:
        return
    edges.append(
        {
            "source": source,
            "target": target,
            "label": label,
            "weight": weight,
            "kind": kind,
        }
    )
    edge_index.add(key)


def api_memories(params, db):
    search = params.get("search", [""])[0]
    category = params.get("category", [""])[0]
    limit = int(params.get("limit", ["50"])[0])
    q = "SELECT * FROM memories WHERE retired_at IS NULL"
    args = []
    if search:
        q += " AND content LIKE ?"
        args.append(f"%{search}%")
    if category:
        q += " AND category = ?"
        args.append(category)
    q += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
    args.append(limit)
    return rows_to_list(db.execute(q, args).fetchall())


def api_entities(params, db):
    sync_agents_to_entities(db)
    etype = params.get("type", [""])[0]
    search = params.get("search", [""])[0]
    q = "SELECT * FROM entities WHERE retired_at IS NULL"
    args = []
    if etype:
        q += " AND entity_type = ?"
        args.append(etype)
    if search:
        q += " AND (name LIKE ? OR properties LIKE ?)"
        args.extend([f"%{search}%", f"%{search}%"])
    q += " ORDER BY confidence DESC"
    return rows_to_list(db.execute(q, args).fetchall())


def api_events(params, db):
    etype = params.get("type", [""])[0]
    project = params.get("project", [""])[0]
    limit = int(params.get("limit", ["50"])[0])
    q = "SELECT * FROM events WHERE 1=1"
    args = []
    if etype:
        q += " AND event_type = ?"
        args.append(etype)
    if project:
        q += " AND project = ?"
        args.append(project)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return rows_to_list(db.execute(q, args).fetchall())


def api_decisions(params, db):
    return rows_to_list(db.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT 100").fetchall())


def api_triggers(params, db):
    status = params.get("status", [""])[0]
    q = "SELECT * FROM memory_triggers WHERE 1=1"
    args = []
    if status:
        q += " AND status = ?"
        args.append(status)
    q += " ORDER BY priority DESC"
    return rows_to_list(db.execute(q, args).fetchall())


def api_health(params, db):
    sync_agents_to_entities(db)
    metrics = {}
    for tbl in ["memories", "entities", "events", "decisions", "memory_triggers", "knowledge_edges"]:
        try:
            metrics[tbl + "_total"] = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            metrics[tbl + "_total"] = 0
    try:
        metrics["active_memories"] = db.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NULL").fetchone()[0]
        metrics["retired_memories"] = db.execute("SELECT COUNT(*) FROM memories WHERE retired_at IS NOT NULL").fetchone()[0]
        metrics["avg_confidence"] = round(db.execute("SELECT AVG(confidence) FROM memories WHERE retired_at IS NULL").fetchone()[0] or 0, 2)
        metrics["active_entities"] = db.execute("SELECT COUNT(*) FROM entities WHERE retired_at IS NULL").fetchone()[0]
        metrics["active_triggers"] = db.execute("SELECT COUNT(*) FROM memory_triggers WHERE status='active'").fetchone()[0]
        metrics["categories"] = rows_to_list(db.execute("SELECT category, COUNT(*) as cnt FROM memories WHERE retired_at IS NULL GROUP BY category ORDER BY cnt DESC").fetchall())
        metrics["entity_types"] = rows_to_list(db.execute("SELECT entity_type, COUNT(*) as cnt FROM entities WHERE retired_at IS NULL GROUP BY entity_type ORDER BY cnt DESC").fetchall())
        metrics["db_size_kb"] = round(os.path.getsize(db_path_actual) / 1024, 1) if os.path.exists(db_path_actual) else 0
    except Exception:
        pass
    return metrics


def api_graph(params, db):
    nodes, entity_names, name_to_node_id = load_graph_entities(db)
    node_index = {node["id"] for node in nodes}
    edges = []
    edge_index = set()

    # Existing entity-to-entity relations.
    for r in db.execute(
        "SELECT * FROM knowledge_edges WHERE source_table='entities' AND target_table='entities'"
    ).fetchall():
        sid = f"entity_{r['source_id']}"
        tid = f"entity_{r['target_id']}"
        _append_edge(edges, edge_index, sid, tid, r["relation_type"] or "", r["weight"] or 1.0, "entity")

    # Recent thought/event nodes.
    recent_events = db.execute(
        """
        SELECT id, agent_id, event_type, summary, detail, project, importance, created_at, caused_by_event_id
        FROM events
        WHERE importance >= 0.3
        ORDER BY created_at DESC
        LIMIT 60
        """
    ).fetchall()
    for r in recent_events:
        node_id = f"event_{r['id']}"
        summary = r["summary"] or r["event_type"] or "event"
        _append_node(
            nodes,
            node_index,
            {
                "id": node_id,
                "label": summary[:80],
                "type": "event",
                "kind": "event",
                "confidence": r["importance"] or 0.3,
                "table": "events",
                "agent_id": r["agent_id"],
                "event_type": r["event_type"],
                "project": r["project"],
                "created_at": r["created_at"],
                "detail": r["detail"] or summary,
            },
        )
        _append_edge(edges, edge_index, node_id, name_to_node_id.get((r["agent_id"] or "").lower()), r["event_type"] or "thought", 0.6, "authored_by")
        if r["caused_by_event_id"]:
            _append_edge(edges, edge_index, node_id, f"event_{r['caused_by_event_id']}", "caused by", 0.35, "causal")

    # Decision nodes linked to agents and source events.
    recent_decisions = db.execute(
        """
        SELECT id, agent_id, title, rationale, project, reversible, source_event_id, created_at
        FROM decisions
        ORDER BY created_at DESC
        LIMIT 30
        """
    ).fetchall()
    for r in recent_decisions:
        node_id = f"decision_{r['id']}"
        _append_node(
            nodes,
            node_index,
            {
                "id": node_id,
                "label": (r["title"] or "decision")[:90],
                "type": "decision",
                "kind": "decision",
                "confidence": 0.95,
                "table": "decisions",
                "agent_id": r["agent_id"],
                "project": r["project"],
                "created_at": r["created_at"],
                "detail": r["rationale"],
                "reversible": bool(r["reversible"]),
            },
        )
        _append_edge(edges, edge_index, node_id, name_to_node_id.get((r["agent_id"] or "").lower()), "decided by", 0.8, "decision")
        if r["source_event_id"]:
            _append_edge(edges, edge_index, node_id, f"event_{r['source_event_id']}", "based on", 0.45, "evidence")

    # Memory nodes with provenance to events and authors.
    recent_memories = db.execute(
        """
        SELECT id, agent_id, content, category, confidence, created_at, source_event_id
        FROM memories
        WHERE retired_at IS NULL
        ORDER BY confidence DESC, created_at DESC
        LIMIT 45
        """
    ).fetchall()
    for r in recent_memories:
        node_id = f"memory_{r['id']}"
        content = r["content"] or ""
        _append_node(
            nodes,
            node_index,
            {
                "id": node_id,
                "label": content[:90],
                "type": "memory",
                "kind": "memory",
                "confidence": r["confidence"] or 0.3,
                "table": "memories",
                "agent_id": r["agent_id"],
                "category": r["category"],
                "created_at": r["created_at"],
                "detail": content,
            },
        )
        _append_edge(edges, edge_index, node_id, name_to_node_id.get((r["agent_id"] or "").lower()), "remembered by", 0.45, "memory")
        if r["source_event_id"]:
            _append_edge(edges, edge_index, node_id, f"event_{r['source_event_id']}", "distilled from", 0.4, "distilled")
        lowered = content.lower()
        for ename, target_id in name_to_node_id.items():
            if ename and lowered and ename in lowered:
                _append_edge(edges, edge_index, node_id, target_id, "mentions", 0.2, "mentions")
                break

    return {"nodes": nodes, "edges": edges}


def api_stats(params, db):
    stats = {}
    for tbl in ["memories", "entities", "events", "decisions", "memory_triggers", "knowledge_edges"]:
        try:
            stats[tbl] = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            stats[tbl] = 0
    return stats


def api_update(params, db):
    force = params.get("refresh", ["0"])[0] == "1"
    return get_update_status(force=force)


db_path_actual = DB_PATH

def api_cost(params, db):
    """Token cost dashboard for the web UI."""
    from datetime import datetime, timedelta, timezone
    report = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    today_row = db.execute(
        "SELECT COUNT(*) as queries, COALESCE(SUM(tokens_consumed), 0) as tokens "
        "FROM access_log WHERE created_at >= ?", (today + " 00:00:00",)
    ).fetchone()
    report["today"] = {"queries": today_row[0], "tokens": today_row[1]}

    week_row = db.execute(
        "SELECT COUNT(*) as queries, COALESCE(SUM(tokens_consumed), 0) as tokens "
        "FROM access_log WHERE created_at >= ?", (week_ago + " 00:00:00",)
    ).fetchone()
    report["last_7_days"] = {
        "queries": week_row[0], "tokens": week_row[1],
        "avg_per_query": round(week_row[1] / max(week_row[0], 1)),
    }

    top_agents = db.execute(
        "SELECT agent_id, COUNT(*) as queries, COALESCE(SUM(tokens_consumed), 0) as tokens "
        "FROM access_log WHERE created_at >= ? AND tokens_consumed IS NOT NULL "
        "GROUP BY agent_id ORDER BY tokens DESC LIMIT 5",
        (week_ago + " 00:00:00",)
    ).fetchall()
    report["top_agents"] = [{"agent": r[0], "queries": r[1], "tokens": r[2]} for r in top_agents]
    return report


def api_activity(params, db):
    """Live activity feed — returns recent events, new memories, new edges, and retirements since a timestamp."""
    since = (params.get("since", [""])[0] or "").strip()
    if not since:
        # Default: last 30 seconds
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")

    result = {"since": since, "events": [], "new_memories": [], "new_edges": [], "retirements": [], "affect": []}

    # Recent events
    try:
        rows = db.execute(
            "SELECT id, agent_id, event_type, summary, importance, created_at "
            "FROM events WHERE created_at > ? ORDER BY created_at DESC LIMIT 20", (since,)
        ).fetchall()
        result["events"] = rows_to_list(rows)
    except Exception:
        pass

    # New memories
    try:
        rows = db.execute(
            "SELECT id, agent_id, category, content, confidence, created_at "
            "FROM memories WHERE created_at > ? AND retired_at IS NULL ORDER BY created_at DESC LIMIT 10", (since,)
        ).fetchall()
        result["new_memories"] = rows_to_list(rows)
    except Exception:
        pass

    # New edges
    try:
        rows = db.execute(
            "SELECT id, source_table, source_id, target_table, target_id, relation_type, weight, created_at "
            "FROM knowledge_edges WHERE created_at > ? ORDER BY created_at DESC LIMIT 20", (since,)
        ).fetchall()
        result["new_edges"] = rows_to_list(rows)
    except Exception:
        pass

    # Recent retirements
    try:
        rows = db.execute(
            "SELECT id, content, category, retired_at "
            "FROM memories WHERE retired_at > ? ORDER BY retired_at DESC LIMIT 10", (since,)
        ).fetchall()
        result["retirements"] = rows_to_list(rows)
    except Exception:
        pass

    # Affect changes
    try:
        rows = db.execute(
            "SELECT id, agent_id, valence, arousal, dominance, affect_label, functional_state, safety_flag, created_at "
            "FROM affect_log WHERE created_at > ? ORDER BY created_at DESC LIMIT 10", (since,)
        ).fetchall()
        result["affect"] = rows_to_list(rows)
    except Exception:
        pass

    return result


ROUTES = {
    "/api/memories": api_memories,
    "/api/entities": api_entities,
    "/api/events": api_events,
    "/api/decisions": api_decisions,
    "/api/triggers": api_triggers,
    "/api/health": api_health,
    "/api/cost": api_cost,
    "/api/graph": api_graph,
    "/api/stats": api_stats,
    "/api/update": api_update,
    "/api/activity": api_activity,
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, db_path=None, **kw):
        self._db_path = db_path
        super().__init__(*a, directory=str(STATIC), **kw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ROUTES:
            params = parse_qs(parsed.query)
            try:
                db = get_db(self._db_path)
                data = ROUTES[parsed.path](params, db)
                db.close()
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())
        else:
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()

    def log_message(self, fmt, *args):
        pass  # Silence request logs


def serve(port=3939, db_path=None, open_browser=True):
    global db_path_actual, DB_PATH
    if db_path:
        db_path_actual = db_path
        DB_PATH = db_path

    def handler(*a, **kw):
        return Handler(*a, db_path=db_path_actual, **kw)

    server = HTTPServer(("127.0.0.1", port), handler)
    url = f"http://localhost:{port}"
    print(f"🧠 brainctl UI running at {url}")
    print(f"   Database: {db_path_actual}")
    print(f"   Press Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down")
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="brainctl web UI")
    parser.add_argument("--port", type=int, default=3939)
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(port=args.port, db_path=args.db, open_browser=not args.no_browser)
