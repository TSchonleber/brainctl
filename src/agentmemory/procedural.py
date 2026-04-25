"""Procedural memory service layer.

Canonical procedures live in dedicated tables and are bridged back to the
generic ``memories`` table through ``procedures.memory_id`` so the legacy
memory/search surfaces still have a human-readable synopsis row.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

PROCEDURE_STATUSES = {
    "active",
    "candidate",
    "stale",
    "needs_review",
    "superseded",
    "retired",
}

PROCEDURE_KINDS = {
    "workflow",
    "runbook",
    "playbook",
    "troubleshooting",
    "rollback",
    "recipe",
    "routine",
}

_STEP_RE = re.compile(r"^\s*(?:\d+[\).\:-]|[-*•])\s+(?P<step>.+?)\s*$")
_IF_THEN_RE = re.compile(r"\bif\s+(.+?)\s+then\s+(.+)", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"\b(rollback|roll back|revert|undo)\b", re.IGNORECASE)
_HOW_TO_RE = re.compile(r"^\s*how\s+(?:to|do|does|can|should)\s+", re.IGNORECASE)
_TOOL_RE = re.compile(r"\b(?:run|use|with|via|invoke)\s+([A-Za-z0-9_./:-]+)")
_LIST_SPLIT_RE = re.compile(r"\b(?:first|then|next|after that|finally|lastly)\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"[•*\-]\s+")
_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "then",
    "to",
    "use",
    "using",
    "when",
    "with",
}


@dataclass(slots=True)
class ProcedureRecord:
    procedure_id: int
    memory_id: int
    title: str
    goal: str
    procedure_kind: str
    status: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=True)


def _json_loads_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_loads_obj(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tokenize(text: str) -> list[str]:
    return [
        tok
        for tok in _TOKEN_RE.findall((text or "").lower())
        if tok not in _STOPWORDS and len(tok) > 1
    ]


def _sentence_split(text: str) -> list[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\r?\n+", text.strip())
    return [p.strip(" -\t\r\n") for p in parts if p.strip(" -\t\r\n")]


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80] or "procedure"


def _procedure_key(title: str, goal: str, scope: str) -> str:
    stem = f"{_slugify(title or goal)}:{scope or 'global'}:{goal or title}"
    digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:10]
    return f"{_slugify(title or goal)}-{digest}"


def _normalize_step_item(step: Any) -> dict[str, Any]:
    if isinstance(step, str):
        return {"action": step.strip()}
    if isinstance(step, dict):
        action = (step.get("action") or step.get("step") or "").strip()
        out = {
            "action": action,
            "rationale": (step.get("rationale") or "").strip() or None,
            "tool_name": (step.get("tool_name") or step.get("tool") or "").strip() or None,
            "expected_output": (step.get("expected_output") or "").strip() or None,
            "stop_condition": (step.get("stop_condition") or "").strip() or None,
            "retry_policy": (step.get("retry_policy") or "").strip() or None,
            "rollback_hint": (step.get("rollback_hint") or "").strip() or None,
        }
        return {k: v for k, v in out.items() if v is not None or k == "action"}
    return {"action": str(step).strip()}


def _normalize_steps(steps: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in steps:
        step = _normalize_step_item(raw)
        if step.get("action"):
            out.append(step)
    return out


def _extract_tools(text: str, steps: list[dict[str, Any]]) -> list[str]:
    tools: list[str] = []
    for step in steps:
        if step.get("tool_name"):
            tools.append(step["tool_name"])
        for match in _TOOL_RE.findall(step.get("action") or ""):
            tools.append(match)
    for match in _TOOL_RE.findall(text or ""):
        tools.append(match)
    seen: set[str] = set()
    deduped: list[str] = []
    for tool in tools:
        key = tool.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(tool)
    return deduped


def _guess_kind(text: str) -> str:
    lower = (text or "").lower()
    if _ROLLBACK_RE.search(lower):
        return "rollback"
    if any(word in lower for word in ("troubleshoot", "debug", "fix ", "error", "failure", "incident")):
        return "troubleshooting"
    if any(word in lower for word in ("playbook", "runbook")):
        return "runbook"
    if any(word in lower for word in ("routine", "repeat", "recurring")):
        return "routine"
    if any(word in lower for word in ("recipe", "tool use", "tool-use")):
        return "recipe"
    return "workflow"


def looks_procedural(text: str) -> bool:
    if not text or len(text.strip()) < 12:
        return False
    lowered = text.lower()
    if _HOW_TO_RE.search(text):
        return True
    if _IF_THEN_RE.search(text):
        return True
    if _ROLLBACK_RE.search(text):
        return True
    if any(_STEP_RE.match(line) for line in text.splitlines()):
        return True
    hints = (
        "steps",
        "first",
        "then",
        "finally",
        "run ",
        "deploy",
        "rollback",
        "revert",
        "restart",
        "apply migrations",
        "troubleshoot",
        "before ",
        "after ",
    )
    return sum(1 for hint in hints if hint in lowered) >= 2


def parse_procedural_text(
    text: str,
    *,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    procedure_kind: Optional[str] = None,
    scope: str = "global",
) -> dict[str, Any]:
    """Deterministically coerce free text into a structured procedure payload."""

    original = (text or "").strip()
    lines = [ln.strip() for ln in original.splitlines() if ln.strip()]
    steps: list[dict[str, Any]] = []
    triggers: list[str] = []
    preconditions: list[str] = []
    rollback_steps: list[str] = []
    failure_modes: list[str] = []
    success_criteria: list[str] = []

    for line in lines:
        match = _STEP_RE.match(line)
        if match:
            body = match.group("step").strip()
            steps.append({"action": body})
            if _ROLLBACK_RE.search(body):
                rollback_steps.append(body)
        if "if " in line.lower():
            m = _IF_THEN_RE.search(line)
            if m:
                triggers.append(m.group(1).strip())
                steps.append({"action": m.group(2).strip()})
            else:
                triggers.append(line)
        if any(token in line.lower() for token in ("before ", "requires ", "ensure ", "must ", "need to ")):
            preconditions.append(line)
        if any(token in line.lower() for token in ("failure", "error", "incident", "stuck", "syntax error")):
            failure_modes.append(line)
        if any(token in line.lower() for token in ("success", "done when", "healthy", "green", "validated")):
            success_criteria.append(line)

    if not steps and original:
        split_chunks = [chunk.strip(" .") for chunk in _LIST_SPLIT_RE.split(original) if chunk.strip(" .")]
        if len(split_chunks) > 1:
            steps = [{"action": chunk} for chunk in split_chunks]

    if not steps and original:
        sentences = _sentence_split(original)
        if len(sentences) > 1:
            steps = [{"action": sentence} for sentence in sentences]

    if not steps and original:
        steps = [{"action": original}]

    steps = _normalize_steps(steps)
    tools = _extract_tools(original, steps)
    kind = procedure_kind or _guess_kind(original)

    if not goal:
        for sentence in _sentence_split(original):
            cleaned = _HOW_TO_RE.sub("", sentence).strip(" .:-")
            if cleaned:
                goal = cleaned[0].upper() + cleaned[1:] if len(cleaned) > 1 else cleaned
                break
    goal = goal or (steps[0]["action"] if steps else "Complete the procedure safely")

    if not title:
        title = goal
        if len(title) > 96:
            title = title[:93].rstrip() + "..."

    expected_outcomes: list[str] = []
    if success_criteria:
        expected_outcomes.extend(success_criteria)
    elif "deploy" in original.lower():
        expected_outcomes.append("Deployment completes and target environment is healthy.")
    elif "rollback" in original.lower():
        expected_outcomes.append("System returns to the last known good state.")
    elif "migrat" in original.lower():
        expected_outcomes.append("Schema changes apply cleanly and services remain healthy.")

    if not rollback_steps and kind == "rollback":
        rollback_steps = [step["action"] for step in steps]
    elif not rollback_steps:
        rollback_steps = [line for line in lines if _ROLLBACK_RE.search(line)]

    search_text = compose_search_text(
        {
            "title": title,
            "goal": goal,
            "description": original,
            "procedure_kind": kind,
            "trigger_conditions": triggers,
            "preconditions": preconditions,
            "steps_json": steps,
            "tools_json": tools,
            "failure_modes_json": failure_modes,
            "rollback_steps_json": rollback_steps,
            "success_criteria_json": success_criteria,
            "expected_outcomes": expected_outcomes,
            "applicability_scope": scope,
        }
    )
    return {
        "title": title,
        "goal": goal,
        "description": original,
        "procedure_kind": kind,
        "trigger_conditions": triggers,
        "preconditions": preconditions,
        "steps_json": steps,
        "tools_json": tools,
        "failure_modes_json": failure_modes,
        "rollback_steps_json": rollback_steps,
        "success_criteria_json": success_criteria,
        "expected_outcomes": expected_outcomes,
        "applicability_scope": scope,
        "status": "active",
        "automation_ready": 1 if tools else 0,
        "determinism": 0.7 if len(steps) > 1 else 0.45,
        "constraints_json": [],
        "repair_strategies_json": rollback_steps or failure_modes,
        "tool_policy_json": tools,
        "task_family": kind,
        "search_text": search_text,
    }


def compose_search_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "title",
        "goal",
        "description",
        "task_family",
        "procedure_kind",
        "applicability_scope",
        "expected_outcomes",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(v) for v in value)

    for key in (
        "trigger_conditions",
        "preconditions",
        "tools_json",
        "failure_modes_json",
        "rollback_steps_json",
        "success_criteria_json",
        "constraints_json",
        "repair_strategies_json",
        "tool_policy_json",
    ):
        values = payload.get(key)
        if isinstance(values, list):
            parts.extend(str(v) for v in values)

    for step in _normalize_steps(payload.get("steps_json") or []):
        parts.extend(str(v) for v in step.values() if v)

    text = " ".join(part for part in parts if part)
    return re.sub(r"\s+", " ", text).strip()


def compose_synopsis(payload: dict[str, Any]) -> str:
    title = payload.get("title") or payload.get("goal") or "Procedure"
    goal = payload.get("goal") or title
    steps = _normalize_steps(payload.get("steps_json") or [])
    lead = f"{title}. Goal: {goal}."
    if steps:
        preview = " ".join(
            f"{idx + 1}. {step['action']}"
            for idx, step in enumerate(steps[:4])
            if step.get("action")
        )
        lead += f" Steps: {preview}."
    rollback = _json_loads_list(payload.get("rollback_steps_json"))
    if rollback:
        lead += f" Rollback: {rollback[0]}"
        if len(rollback) > 1:
            lead += f"; then {rollback[1]}"
        lead += "."
    tools = _json_loads_list(payload.get("tools_json"))
    if tools:
        lead += f" Tools: {', '.join(str(t) for t in tools[:5])}."
    return re.sub(r"\s+", " ", lead).strip()


def ensure_procedure_schema(conn: sqlite3.Connection) -> None:
    """Best-effort local guard so procedural APIs work on legacy DBs too."""

    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS procedures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE,
            procedure_key TEXT UNIQUE,
            title TEXT,
            goal TEXT NOT NULL,
            description TEXT,
            task_family TEXT,
            procedure_kind TEXT NOT NULL DEFAULT 'workflow',
            trigger_conditions TEXT,
            preconditions TEXT,
            constraints_json TEXT,
            steps_json TEXT NOT NULL,
            tools_json TEXT,
            failure_modes_json TEXT,
            rollback_steps_json TEXT,
            success_criteria_json TEXT,
            repair_strategies_json TEXT,
            tool_policy_json TEXT,
            expected_outcomes TEXT,
            applicability_scope TEXT NOT NULL DEFAULT 'global',
            temporal_class TEXT DEFAULT 'durable',
            status TEXT NOT NULL DEFAULT 'active',
            automation_ready INTEGER NOT NULL DEFAULT 0,
            determinism REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.5,
            utility_score REAL NOT NULL DEFAULT 0.5,
            generality_score REAL NOT NULL DEFAULT 0.5,
            support_count INTEGER NOT NULL DEFAULT 0,
            execution_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            last_executed_at TEXT,
            last_validated_at TEXT,
            stale_after_days INTEGER NOT NULL DEFAULT 90,
            supersedes_procedure_id INTEGER REFERENCES procedures(id),
            retired_at TEXT,
            search_text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_procedures_kind ON procedures(procedure_kind);
        CREATE INDEX IF NOT EXISTS idx_procedures_status ON procedures(status);
        CREATE INDEX IF NOT EXISTS idx_procedures_last_validated ON procedures(last_validated_at);
        CREATE INDEX IF NOT EXISTS idx_procedures_execution_count ON procedures(execution_count DESC);
        CREATE INDEX IF NOT EXISTS idx_procedures_scope ON procedures(applicability_scope);
        CREATE INDEX IF NOT EXISTS idx_procedures_memory_id ON procedures(memory_id);
        CREATE INDEX IF NOT EXISTS idx_procedures_supersedes ON procedures(supersedes_procedure_id);

        CREATE TABLE IF NOT EXISTS procedure_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
            step_order INTEGER NOT NULL,
            action TEXT NOT NULL,
            rationale TEXT,
            tool_name TEXT,
            expected_output TEXT,
            stop_condition TEXT,
            retry_policy TEXT,
            rollback_hint TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_procedure_steps_procedure_order
            ON procedure_steps(procedure_id, step_order);

        CREATE TABLE IF NOT EXISTS procedure_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
            memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
            event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
            decision_id INTEGER REFERENCES decisions(id) ON DELETE CASCADE,
            entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
            source_role TEXT NOT NULL DEFAULT 'evidence',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_procedure_sources_procedure ON procedure_sources(procedure_id);
        CREATE INDEX IF NOT EXISTS idx_procedure_sources_memory ON procedure_sources(memory_id);
        CREATE INDEX IF NOT EXISTS idx_procedure_sources_event ON procedure_sources(event_id);
        CREATE INDEX IF NOT EXISTS idx_procedure_sources_decision ON procedure_sources(decision_id);

        CREATE TABLE IF NOT EXISTS procedure_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
            agent_id TEXT REFERENCES agents(id),
            task_family TEXT,
            task_signature TEXT,
            input_summary TEXT,
            outcome_summary TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            usefulness_score REAL,
            errors_seen TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_procedure_runs_procedure_created
            ON procedure_runs(procedure_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS procedure_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_signature TEXT NOT NULL UNIQUE,
            task_family TEXT,
            normalized_signature TEXT NOT NULL,
            support_count INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT,
            mean_success REAL NOT NULL DEFAULT 0.0,
            promoted_procedure_id INTEGER REFERENCES procedures(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_procedure_candidates_family
            ON procedure_candidates(task_family);
        CREATE INDEX IF NOT EXISTS idx_procedure_candidates_support
            ON procedure_candidates(support_count DESC);

        CREATE VIRTUAL TABLE IF NOT EXISTS procedures_fts USING fts5(
            title,
            goal,
            description,
            task_family,
            search_text,
            content=procedures,
            content_rowid=id,
            tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS procedures_fts_insert AFTER INSERT ON procedures BEGIN
            INSERT INTO procedures_fts(rowid, title, goal, description, task_family, search_text)
            VALUES (new.id, new.title, new.goal, new.description, new.task_family, new.search_text);
        END;
        CREATE TRIGGER IF NOT EXISTS procedures_fts_update AFTER UPDATE ON procedures BEGIN
            INSERT INTO procedures_fts(
                procedures_fts, rowid, title, goal, description, task_family, search_text
            )
            VALUES (
                'delete', old.id, old.title, old.goal, old.description, old.task_family, old.search_text
            );
            INSERT INTO procedures_fts(rowid, title, goal, description, task_family, search_text)
            VALUES (new.id, new.title, new.goal, new.description, new.task_family, new.search_text);
        END;
        CREATE TRIGGER IF NOT EXISTS procedures_fts_delete AFTER DELETE ON procedures BEGIN
            INSERT INTO procedures_fts(
                procedures_fts, rowid, title, goal, description, task_family, search_text
            )
            VALUES (
                'delete', old.id, old.title, old.goal, old.description, old.task_family, old.search_text
            );
        END;
        """
    )


def _insert_procedure_steps(conn: sqlite3.Connection, procedure_id: int, steps: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM procedure_steps WHERE procedure_id = ?", (procedure_id,))
    for idx, step in enumerate(steps, start=1):
        conn.execute(
            """
            INSERT INTO procedure_steps (
                procedure_id, step_order, action, rationale, tool_name,
                expected_output, stop_condition, retry_policy, rollback_hint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                procedure_id,
                idx,
                step.get("action"),
                step.get("rationale"),
                step.get("tool_name"),
                step.get("expected_output"),
                step.get("stop_condition"),
                step.get("retry_policy"),
                step.get("rollback_hint"),
            ),
        )


def _link_knowledge_edge(
    conn: sqlite3.Connection,
    *,
    procedure_id: int,
    target_table: str,
    target_id: int,
    relation_type: str,
    weight: float = 1.0,
    agent_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO knowledge_edges
            (source_table, source_id, target_table, target_id, relation_type, weight, agent_id, created_at)
        VALUES ('procedures', ?, ?, ?, ?, ?, ?, ?)
        """,
        (procedure_id, target_table, target_id, relation_type, weight, agent_id, now_iso()),
    )


def create_procedure(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    payload: dict[str, Any],
    category: str = "convention",
    scope: str = "global",
    confidence: float = 0.9,
    source_memory_ids: Optional[list[int]] = None,
    source_event_ids: Optional[list[int]] = None,
    source_decision_ids: Optional[list[int]] = None,
    source_entity_ids: Optional[list[int]] = None,
    memory_id: Optional[int] = None,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    source_memory_ids = source_memory_ids or []
    source_event_ids = source_event_ids or []
    source_decision_ids = source_decision_ids or []
    source_entity_ids = source_entity_ids or []

    data = dict(payload)
    if not data.get("steps_json"):
        data = parse_procedural_text(
            data.get("description") or data.get("goal") or "",
            title=data.get("title"),
            goal=data.get("goal"),
            procedure_kind=data.get("procedure_kind"),
            scope=scope,
        )
    steps = _normalize_steps(data.get("steps_json") or [])
    data["steps_json"] = steps or [{"action": data.get("goal") or "Review the procedure"}]
    data["trigger_conditions"] = list(data.get("trigger_conditions") or [])
    data["preconditions"] = list(data.get("preconditions") or [])
    data["tools_json"] = list(data.get("tools_json") or [])
    data["failure_modes_json"] = list(data.get("failure_modes_json") or [])
    data["rollback_steps_json"] = list(data.get("rollback_steps_json") or [])
    data["success_criteria_json"] = list(data.get("success_criteria_json") or [])
    data["constraints_json"] = list(data.get("constraints_json") or [])
    data["repair_strategies_json"] = list(data.get("repair_strategies_json") or [])
    data["tool_policy_json"] = list(data.get("tool_policy_json") or [])
    data["expected_outcomes"] = data.get("expected_outcomes") or []
    data["title"] = (data.get("title") or data.get("goal") or "Procedure").strip()
    data["goal"] = (data.get("goal") or data["title"]).strip()
    data["description"] = (data.get("description") or "").strip()
    data["procedure_kind"] = data.get("procedure_kind") or _guess_kind(
        " ".join([data["goal"], data["description"]])
    )
    if data["procedure_kind"] not in PROCEDURE_KINDS:
        data["procedure_kind"] = "workflow"
    data["status"] = data.get("status") or "active"
    if data["status"] not in PROCEDURE_STATUSES:
        data["status"] = "active"
    data["applicability_scope"] = data.get("applicability_scope") or scope or "global"
    data["task_family"] = data.get("task_family") or data["procedure_kind"]
    data["search_text"] = compose_search_text(data)
    synopsis = compose_synopsis(data)
    source_refs = {
        "memory_ids": source_memory_ids,
        "event_ids": source_event_ids,
        "decision_ids": source_decision_ids,
        "entity_ids": source_entity_ids,
    }

    created_at = now_iso()
    if memory_id is None:
        tags = data.get("tags")
        tags_json = _json_dumps(tags) if tags else None
        cur = conn.execute(
            """
            INSERT INTO memories (
                agent_id, category, scope, content, confidence, tags, memory_type,
                derived_from_ids, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'procedural', ?, ?, ?)
            """,
            (
                agent_id,
                category,
                scope,
                synopsis,
                confidence,
                tags_json,
                json.dumps(source_refs, ensure_ascii=True),
                created_at,
                created_at,
            ),
        )
        memory_id = int(cur.lastrowid)
    else:
        exists = conn.execute(
            "SELECT id, content, scope FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not exists:
            raise ValueError(f"memory_id {memory_id} does not exist")
        conn.execute(
            """
            UPDATE memories
               SET memory_type = 'procedural',
                   scope = COALESCE(scope, ?),
                   updated_at = ?,
                   derived_from_ids = COALESCE(derived_from_ids, ?)
             WHERE id = ?
            """,
            (scope, created_at, json.dumps(source_refs, ensure_ascii=True), memory_id),
        )
        maybe_existing = conn.execute(
            "SELECT id FROM procedures WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if maybe_existing:
            return get_procedure(conn, int(maybe_existing["id"]), include_sources=True)

    proc_key = data.get("procedure_key") or _procedure_key(
        data["title"], data["goal"], data["applicability_scope"]
    )
    cur = conn.execute(
        """
        INSERT INTO procedures (
            memory_id, procedure_key, title, goal, description, task_family,
            procedure_kind, trigger_conditions, preconditions, constraints_json,
            steps_json, tools_json, failure_modes_json, rollback_steps_json,
            success_criteria_json, repair_strategies_json, tool_policy_json,
            expected_outcomes, applicability_scope, temporal_class, status,
            automation_ready, determinism, confidence, utility_score,
            generality_score, support_count, execution_count, success_count,
            failure_count, last_used_at, last_executed_at, last_validated_at,
            stale_after_days, supersedes_procedure_id, retired_at, search_text,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, NULL, NULL, NULL, ?, ?, NULL, ?, ?, ?)
        """,
        (
            memory_id,
            proc_key,
            data["title"],
            data["goal"],
            data["description"],
            data["task_family"],
            data["procedure_kind"],
            _json_dumps(data["trigger_conditions"]),
            _json_dumps(data["preconditions"]),
            _json_dumps(data["constraints_json"]),
            _json_dumps(data["steps_json"]),
            _json_dumps(data["tools_json"]),
            _json_dumps(data["failure_modes_json"]),
            _json_dumps(data["rollback_steps_json"]),
            _json_dumps(data["success_criteria_json"]),
            _json_dumps(data["repair_strategies_json"]),
            _json_dumps(data["tool_policy_json"]),
            json.dumps(data["expected_outcomes"], ensure_ascii=True),
            data["applicability_scope"],
            data.get("temporal_class") or "durable",
            data["status"],
            int(bool(data.get("automation_ready", 0))),
            float(data.get("determinism", 0.5)),
            float(data.get("confidence", confidence)),
            float(data.get("utility_score", confidence)),
            float(data.get("generality_score", 0.5)),
            int(data.get("support_count", len(source_memory_ids) + len(source_event_ids) + len(source_decision_ids))),
            int(data.get("stale_after_days", 90)),
            data.get("supersedes_procedure_id"),
            data["search_text"],
            created_at,
            created_at,
        ),
    )
    procedure_id = int(cur.lastrowid)

    conn.execute(
        "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
        (synopsis, created_at, memory_id),
    )

    _insert_procedure_steps(conn, procedure_id, steps)

    for mid in source_memory_ids:
        conn.execute(
            """
            INSERT INTO procedure_sources (procedure_id, memory_id, source_role, created_at)
            VALUES (?, ?, 'derived_from_memory', ?)
            """,
            (procedure_id, mid, created_at),
        )
        _link_knowledge_edge(
            conn,
            procedure_id=procedure_id,
            target_table="memories",
            target_id=mid,
            relation_type="derived_from_memory",
            weight=1.0,
            agent_id=agent_id,
        )

    for eid in source_event_ids:
        conn.execute(
            """
            INSERT INTO procedure_sources (procedure_id, event_id, source_role, created_at)
            VALUES (?, ?, 'derived_from_event', ?)
            """,
            (procedure_id, eid, created_at),
        )
        rel = "rollback_for" if data["procedure_kind"] == "rollback" else "derived_from_event"
        _link_knowledge_edge(
            conn,
            procedure_id=procedure_id,
            target_table="events",
            target_id=eid,
            relation_type=rel,
            weight=0.9,
            agent_id=agent_id,
        )

    for did in source_decision_ids:
        conn.execute(
            """
            INSERT INTO procedure_sources (procedure_id, decision_id, source_role, created_at)
            VALUES (?, ?, 'derived_from_decision', ?)
            """,
            (procedure_id, did, created_at),
        )
        _link_knowledge_edge(
            conn,
            procedure_id=procedure_id,
            target_table="decisions",
            target_id=did,
            relation_type="derived_from_decision",
            weight=0.95,
            agent_id=agent_id,
        )

    for ent_id in source_entity_ids:
        conn.execute(
            """
            INSERT INTO procedure_sources (procedure_id, entity_id, source_role, created_at)
            VALUES (?, ?, 'applicable_to', ?)
            """,
            (procedure_id, ent_id, created_at),
        )
        _link_knowledge_edge(
            conn,
            procedure_id=procedure_id,
            target_table="entities",
            target_id=ent_id,
            relation_type="applicable_to",
            weight=0.8,
            agent_id=agent_id,
        )

    for tool in data["tools_json"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_edges
                (source_table, source_id, target_table, target_id, relation_type, weight, agent_id, created_at)
            SELECT 'procedures', ?, 'entities', e.id, 'requires_tool', 0.7, ?, ?
              FROM entities e
             WHERE lower(e.name) = lower(?)
            """,
            (procedure_id, agent_id, created_at, str(tool)),
        )

    if data.get("supersedes_procedure_id"):
        _link_knowledge_edge(
            conn,
            procedure_id=procedure_id,
            target_table="procedures",
            target_id=int(data["supersedes_procedure_id"]),
            relation_type="supersedes_procedure",
            weight=1.0,
            agent_id=agent_id,
        )
        conn.execute(
            "UPDATE procedures SET status = 'superseded', updated_at = ? WHERE id = ?",
            (created_at, int(data["supersedes_procedure_id"])),
        )

    return get_procedure(conn, procedure_id, include_sources=True)


def ensure_procedure_for_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: int,
    agent_id: str,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    existing = conn.execute(
        "SELECT id FROM procedures WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if existing:
        return get_procedure(conn, int(existing["id"]), include_sources=True)

    row = conn.execute(
        "SELECT id, content, category, scope, confidence FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"memory_id {memory_id} not found")

    payload = parse_procedural_text(
        row["content"],
        scope=row["scope"] or "global",
    )
    payload.setdefault("description", row["content"])
    payload.setdefault("confidence", row["confidence"] or 0.6)
    payload.setdefault("utility_score", row["confidence"] or 0.6)
    payload.setdefault("support_count", 1)
    return create_procedure(
        conn,
        agent_id=agent_id,
        payload=payload,
        category=row["category"] or "convention",
        scope=row["scope"] or "global",
        confidence=float(row["confidence"] or 0.8),
        source_memory_ids=[memory_id],
        memory_id=memory_id,
    )


def _procedure_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in (
        "trigger_conditions",
        "preconditions",
        "constraints_json",
        "steps_json",
        "tools_json",
        "failure_modes_json",
        "rollback_steps_json",
        "success_criteria_json",
        "repair_strategies_json",
        "tool_policy_json",
    ):
        out[key] = _json_loads_list(out.get(key))
    if isinstance(out.get("expected_outcomes"), str) and out["expected_outcomes"].startswith("["):
        out["expected_outcomes"] = _json_loads_list(out["expected_outcomes"])
    out["success_rate"] = round(
        float(out.get("success_count") or 0) / max(int(out.get("execution_count") or 0), 1),
        4,
    )
    return out


def get_procedure(
    conn: sqlite3.Connection,
    procedure_id: int,
    *,
    include_sources: bool = False,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    row = conn.execute(
        """
        SELECT p.*, m.content, m.category, m.scope, m.confidence AS memory_confidence,
               m.memory_type, m.created_at AS memory_created_at
          FROM procedures p
          JOIN memories m ON m.id = p.memory_id
         WHERE p.id = ?
        """,
        (procedure_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"procedure_id {procedure_id} not found")
    out = _procedure_row_to_dict(row)
    if include_sources:
        out["sources"] = [dict(r) for r in conn.execute(
            """
            SELECT memory_id, event_id, decision_id, entity_id, source_role, created_at
              FROM procedure_sources
             WHERE procedure_id = ?
             ORDER BY id
            """,
            (procedure_id,),
        ).fetchall()]
    out["steps"] = [dict(r) for r in conn.execute(
        """
        SELECT step_order, action, rationale, tool_name, expected_output,
               stop_condition, retry_policy, rollback_hint
          FROM procedure_steps
         WHERE procedure_id = ?
         ORDER BY step_order
        """,
        (procedure_id,),
    ).fetchall()]
    return out


def list_procedures(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_procedure_schema(conn)
    clauses = ["1=1"]
    params: list[Any] = []
    if status and status != "all":
        clauses.append("p.status = ?")
        params.append(status)
    if scope:
        clauses.append("(p.applicability_scope = 'global' OR p.applicability_scope = ?)")
        params.append(scope)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT p.*, m.content, m.category, m.scope, m.confidence AS memory_confidence
          FROM procedures p
          JOIN memories m ON m.id = p.memory_id
         WHERE {' AND '.join(clauses)}
         ORDER BY
            CASE p.status
                WHEN 'active' THEN 0
                WHEN 'candidate' THEN 1
                WHEN 'needs_review' THEN 2
                WHEN 'stale' THEN 3
                WHEN 'superseded' THEN 4
                ELSE 5
            END,
            COALESCE(p.last_validated_at, p.updated_at, p.created_at) DESC
         LIMIT ?
        """,
        params,
    ).fetchall()
    return [_procedure_row_to_dict(row) for row in rows]


def _days_old(timestamp: Optional[str]) -> float:
    if not timestamp:
        return 9999.0
    normalized = str(timestamp).replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def _score_procedure(
    query: str,
    proc: dict[str, Any],
    *,
    debug: bool = False,
) -> tuple[float, dict[str, float]]:
    tokens = set(_tokenize(query))
    phrase = query.lower().strip()

    title_tokens = set(_tokenize(proc.get("title") or ""))
    goal_tokens = set(_tokenize(proc.get("goal") or ""))
    desc_tokens = set(_tokenize(proc.get("description") or ""))
    trigger_tokens = set(_tokenize(" ".join(str(v) for v in proc.get("trigger_conditions", []))))
    pre_tokens = set(_tokenize(" ".join(str(v) for v in proc.get("preconditions", []))))
    tool_tokens = set(_tokenize(" ".join(str(v) for v in proc.get("tools_json", []))))
    step_tokens = set(_tokenize(" ".join(step.get("action", "") for step in proc.get("steps_json", []))))
    failure_tokens = set(_tokenize(" ".join(str(v) for v in proc.get("failure_modes_json", []))))
    rollback_tokens = set(_tokenize(" ".join(str(v) for v in proc.get("rollback_steps_json", []))))
    scope_tokens = set(_tokenize(proc.get("applicability_scope") or ""))

    overlap = lambda bag: len(tokens & bag) / max(len(tokens), 1)
    breakdown = {
        "goal_match": overlap(goal_tokens | desc_tokens) * 1.4,
        "title_match": overlap(title_tokens) * 1.6,
        "trigger_match": overlap(trigger_tokens) * 0.9,
        "precondition_match": overlap(pre_tokens) * 0.7,
        "step_overlap": overlap(step_tokens) * 1.3,
        "tool_overlap": overlap(tool_tokens) * 0.9,
        "failure_overlap": overlap(failure_tokens) * 0.7,
        "rollback_overlap": overlap(rollback_tokens) * 1.1,
        "scope_match": overlap(scope_tokens) * 0.4,
        "exact_phrase": 1.0 if phrase and phrase in (proc.get("search_text") or "").lower() else 0.0,
    }

    status = proc.get("status") or "active"
    status_multiplier = {
        "active": 1.15,
        "candidate": 0.95,
        "needs_review": 0.75,
        "stale": 0.68,
        "superseded": 0.35,
        "retired": 0.15,
    }.get(status, 1.0)
    validation_age = _days_old(proc.get("last_validated_at"))
    last_exec_age = _days_old(proc.get("last_executed_at"))
    validation_boost = max(0.0, 1.0 - min(validation_age / max(int(proc.get("stale_after_days") or 90), 1), 1.5))
    utility_boost = float(proc.get("utility_score") or 0.5)
    confidence_boost = float(proc.get("confidence") or 0.5)
    execution_count = int(proc.get("execution_count") or 0)
    success_count = int(proc.get("success_count") or 0)
    failure_count = int(proc.get("failure_count") or 0)
    success_rate = success_count / max(execution_count, 1)
    failure_penalty = min(failure_count / max(execution_count, 1), 1.0)
    support_bonus = min(int(proc.get("support_count") or 0) / 5.0, 1.0)
    freshness = max(0.0, 1.0 - min(last_exec_age / max(int(proc.get("stale_after_days") or 90), 1), 1.5))

    base = sum(breakdown.values())
    score = (
        base
        + validation_boost * 0.8
        + freshness * 0.4
        + success_rate * 0.8
        + support_bonus * 0.5
        + utility_boost * 0.3
        + confidence_boost * 0.4
        - failure_penalty * 0.9
    ) * status_multiplier
    directness = (
        breakdown["goal_match"]
        + breakdown["title_match"]
        + breakdown["trigger_match"]
        + breakdown["exact_phrase"]
    )
    if directness < 0.6 and breakdown["step_overlap"] > 0:
        score *= 0.72
    if (
        len(tokens) <= 4
        and directness < 0.45
        and (breakdown["goal_match"] + breakdown["title_match"]) < 0.25
        and breakdown["step_overlap"] >= 0.4
    ):
        score *= 0.35
    if debug:
        breakdown.update(
            {
                "validation_boost": round(validation_boost, 4),
                "freshness_boost": round(freshness, 4),
                "success_rate": round(success_rate, 4),
                "support_bonus": round(support_bonus, 4),
                "utility_boost": round(utility_boost, 4),
                "confidence_boost": round(confidence_boost, 4),
                "failure_penalty": round(failure_penalty, 4),
                "status_multiplier": round(status_multiplier, 4),
                "directness": round(directness, 4),
            }
        )
    return round(score, 6), breakdown


def search_procedures(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    scope: Optional[str] = None,
    status: Optional[str] = None,
    debug: bool = False,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    search = query.strip()
    if not search:
        return {"ok": True, "procedures": [], "debug": {"reason": "empty_query"}}

    tokens = _tokenize(search)
    fts_query = " OR ".join(tokens) if tokens else re.sub(r"[^\w\s]", " ", search).strip()
    clauses = ["1=1"]
    params: list[Any] = []
    if status and status != "all":
        clauses.append("p.status = ?")
        params.append(status)
    if scope:
        clauses.append("(p.applicability_scope = 'global' OR p.applicability_scope = ?)")
        params.append(scope)

    rows: list[sqlite3.Row]
    if fts_query:
        rows = conn.execute(
            f"""
            SELECT p.*, m.content, m.category, m.scope, m.confidence AS memory_confidence,
                   bm25(procedures_fts, 3.0, 2.0, 1.5, 1.0, 2.5) AS fts_rank
              FROM procedures_fts
              JOIN procedures p ON p.id = procedures_fts.rowid
              JOIN memories m ON m.id = p.memory_id
             WHERE procedures_fts MATCH ? AND {' AND '.join(clauses)}
             ORDER BY bm25(procedures_fts, 3.0, 2.0, 1.5, 1.0, 2.5)
             LIMIT ?
            """,
            [fts_query, *params, max(limit * 4, 12)],
        ).fetchall()
    else:
        rows = []

    if not rows:
        rows = conn.execute(
            f"""
            SELECT p.*, m.content, m.category, m.scope, m.confidence AS memory_confidence, NULL AS fts_rank
              FROM procedures p
              JOIN memories m ON m.id = p.memory_id
             WHERE {' AND '.join(clauses)}
               AND (
                    lower(p.goal) LIKE ? OR lower(COALESCE(p.description, '')) LIKE ?
                    OR lower(p.search_text) LIKE ? OR lower(m.content) LIKE ?
               )
             LIMIT ?
            """,
            [*params, f"%{search.lower()}%", f"%{search.lower()}%", f"%{search.lower()}%", f"%{search.lower()}%", max(limit * 4, 12)],
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        proc = _procedure_row_to_dict(row)
        score, breakdown = _score_procedure(search, proc, debug=debug)
        proc["final_score"] = score
        proc["fts_rank"] = row["fts_rank"]
        proc["type"] = "procedure"
        proc["why_retrieved"] = (
            "goal/title match" if breakdown.get("goal_match", 0.0) + breakdown.get("title_match", 0.0) >= 1.0
            else "procedural evidence match"
        )
        if debug:
            proc["score_breakdown"] = breakdown
        results.append(proc)

    results.sort(key=lambda item: item.get("final_score", 0.0), reverse=True)
    return {
        "ok": True,
        "procedures": results[:limit],
        "debug": {
            "query": search,
            "fts_query": fts_query,
            "candidate_count": len(results),
        },
    }


def update_procedure(
    conn: sqlite3.Connection,
    procedure_id: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    current = get_procedure(conn, procedure_id, include_sources=True)
    merged = dict(current)
    merged.update({k: v for k, v in changes.items() if v is not None})
    merged["steps_json"] = _normalize_steps(merged.get("steps_json") or current.get("steps_json") or [])
    merged["search_text"] = compose_search_text(merged)
    merged["updated_at"] = now_iso()

    conn.execute(
        """
        UPDATE procedures
           SET title = ?, goal = ?, description = ?, task_family = ?, procedure_kind = ?,
               trigger_conditions = ?, preconditions = ?, constraints_json = ?, steps_json = ?,
               tools_json = ?, failure_modes_json = ?, rollback_steps_json = ?,
               success_criteria_json = ?, repair_strategies_json = ?, tool_policy_json = ?,
               expected_outcomes = ?, applicability_scope = ?, status = ?, automation_ready = ?,
               determinism = ?, confidence = ?, utility_score = ?, generality_score = ?,
               support_count = ?, stale_after_days = ?, supersedes_procedure_id = ?,
               search_text = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            merged.get("title"),
            merged.get("goal"),
            merged.get("description"),
            merged.get("task_family"),
            merged.get("procedure_kind"),
            _json_dumps(merged.get("trigger_conditions")),
            _json_dumps(merged.get("preconditions")),
            _json_dumps(merged.get("constraints_json")),
            _json_dumps(merged.get("steps_json")),
            _json_dumps(merged.get("tools_json")),
            _json_dumps(merged.get("failure_modes_json")),
            _json_dumps(merged.get("rollback_steps_json")),
            _json_dumps(merged.get("success_criteria_json")),
            _json_dumps(merged.get("repair_strategies_json")),
            _json_dumps(merged.get("tool_policy_json")),
            json.dumps(merged.get("expected_outcomes") or [], ensure_ascii=True),
            merged.get("applicability_scope"),
            merged.get("status"),
            int(bool(merged.get("automation_ready", 0))),
            float(merged.get("determinism", 0.5)),
            float(merged.get("confidence", 0.5)),
            float(merged.get("utility_score", 0.5)),
            float(merged.get("generality_score", 0.5)),
            int(merged.get("support_count", 0)),
            int(merged.get("stale_after_days", 90)),
            merged.get("supersedes_procedure_id"),
            merged["search_text"],
            merged["updated_at"],
            procedure_id,
        ),
    )
    _insert_procedure_steps(conn, procedure_id, merged["steps_json"])
    conn.execute(
        "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
        (compose_synopsis(merged), merged["updated_at"], current["memory_id"]),
    )
    return get_procedure(conn, procedure_id, include_sources=True)


def _recompute_status(proc: dict[str, Any]) -> str:
    if proc.get("retired_at"):
        return "retired"
    if proc.get("status") == "superseded":
        return "superseded"
    stale_after_days = int(proc.get("stale_after_days") or 90)
    last_validated = proc.get("last_validated_at") or proc.get("updated_at") or proc.get("created_at")
    if last_validated and _days_old(last_validated) > stale_after_days:
        return "stale"
    failures = int(proc.get("failure_count") or 0)
    successes = int(proc.get("success_count") or 0)
    execution_count = int(proc.get("execution_count") or 0)
    if execution_count >= 3 and failures >= max(2, successes):
        return "needs_review"
    return "active"


def record_feedback(
    conn: sqlite3.Connection,
    *,
    procedure_id: int,
    agent_id: str,
    success: bool,
    usefulness_score: Optional[float] = None,
    outcome_summary: Optional[str] = None,
    errors_seen: Optional[str] = None,
    validated: bool = False,
    task_signature: Optional[str] = None,
    input_summary: Optional[str] = None,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    proc = get_procedure(conn, procedure_id, include_sources=False)
    now = now_iso()
    execution_count = int(proc.get("execution_count") or 0) + 1
    success_count = int(proc.get("success_count") or 0) + (1 if success else 0)
    failure_count = int(proc.get("failure_count") or 0) + (0 if success else 1)
    utility = usefulness_score if usefulness_score is not None else proc.get("utility_score") or 0.5
    utility = float(max(0.0, min(1.0, utility)))
    confidence = float(proc.get("confidence") or 0.5)
    confidence = confidence + (0.06 if success else -0.09)
    confidence = max(0.05, min(0.99, confidence))

    conn.execute(
        """
        INSERT INTO procedure_runs (
            procedure_id, agent_id, task_family, task_signature, input_summary,
            outcome_summary, success, usefulness_score, errors_seen, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            procedure_id,
            agent_id,
            proc.get("task_family"),
            task_signature,
            input_summary,
            outcome_summary,
            1 if success else 0,
            usefulness_score,
            errors_seen,
            now,
        ),
    )

    proc.update(
        {
            "execution_count": execution_count,
            "success_count": success_count,
            "failure_count": failure_count,
            "last_used_at": now,
            "last_executed_at": now,
            "last_validated_at": now if validated or success else proc.get("last_validated_at"),
            "utility_score": utility,
            "confidence": confidence,
        }
    )
    proc["status"] = _recompute_status(proc)
    conn.execute(
        """
        UPDATE procedures
           SET execution_count = ?, success_count = ?, failure_count = ?,
               last_used_at = ?, last_executed_at = ?, last_validated_at = ?,
               utility_score = ?, confidence = ?, status = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            execution_count,
            success_count,
            failure_count,
            now,
            now,
            proc.get("last_validated_at"),
            utility,
            confidence,
            proc["status"],
            now,
            procedure_id,
        ),
    )

    mem = conn.execute(
        "SELECT alpha, beta FROM memories WHERE id = ?",
        (proc["memory_id"],),
    ).fetchone()
    alpha = float(mem["alpha"] if mem and mem["alpha"] is not None else 1.0)
    beta = float(mem["beta"] if mem and mem["beta"] is not None else 1.0)
    if success:
        alpha += 1.0
    else:
        beta += 1.0
    posterior = alpha / max(alpha + beta, 1e-6)
    conn.execute(
        """
        UPDATE memories
           SET alpha = ?, beta = ?, confidence = ?, updated_at = ?
         WHERE id = ?
        """,
        (alpha, beta, posterior, now, proc["memory_id"]),
    )

    return get_procedure(conn, procedure_id, include_sources=True)


def _candidate_signature_from_text(text: str) -> str:
    tokens = _tokenize(text)[:8]
    if not tokens:
        return ""
    return " ".join(tokens)


def synthesize_procedure_candidates(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    dry_run: bool = False,
    min_support: int = 2,
    promote_support: int = 3,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    rows = conn.execute(
        """
        SELECT id, content, category, scope, confidence
          FROM memories
         WHERE retired_at IS NULL
           AND COALESCE(memory_type, 'episodic') = 'episodic'
           AND category IN ('lesson', 'integration', 'decision', 'convention')
         ORDER BY created_at DESC
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if not looks_procedural(row["content"]):
            continue
        signature = _candidate_signature_from_text(row["content"])
        if not signature:
            continue
        grouped.setdefault(signature, []).append(row)

    stats = {
        "scanned": len(rows),
        "candidates_updated": 0,
        "promoted": 0,
        "signatures": [],
    }
    now = now_iso()
    for signature, members in grouped.items():
        if len(members) < min_support:
            continue
        mean_success = sum(float(row["confidence"] or 0.5) for row in members) / len(members)
        evidence = {
            "memory_ids": [int(row["id"]) for row in members],
            "scope": members[0]["scope"],
            "category": members[0]["category"],
        }
        if not dry_run:
            conn.execute(
                """
                INSERT INTO procedure_candidates (
                    candidate_signature, task_family, normalized_signature,
                    support_count, evidence_json, mean_success, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_signature) DO UPDATE SET
                    support_count = excluded.support_count,
                    evidence_json = excluded.evidence_json,
                    mean_success = excluded.mean_success,
                    updated_at = excluded.updated_at
                """,
                (
                    signature,
                    _guess_kind(signature),
                    signature,
                    len(members),
                    json.dumps(evidence, ensure_ascii=True),
                    round(mean_success, 4),
                    now,
                ),
            )
        stats["candidates_updated"] += 1
        stats["signatures"].append({"signature": signature, "support": len(members)})

        should_promote = len(members) >= promote_support or (
            len(members) >= 2 and mean_success >= 0.75 and any(row["category"] in ("decision", "lesson") for row in members)
        )
        if should_promote:
            payload = parse_procedural_text(
                members[0]["content"],
                scope=members[0]["scope"] or "global",
            )
            payload["support_count"] = len(members)
            payload["confidence"] = round(mean_success, 4)
            payload["utility_score"] = round(mean_success, 4)
            if not dry_run:
                proc = create_procedure(
                    conn,
                    agent_id=agent_id,
                    payload=payload,
                    category=members[0]["category"] or "convention",
                    scope=members[0]["scope"] or "global",
                    confidence=round(mean_success, 4),
                    source_memory_ids=[int(row["id"]) for row in members],
                )
                conn.execute(
                    """
                    UPDATE procedure_candidates
                       SET promoted_procedure_id = ?, updated_at = ?
                     WHERE candidate_signature = ?
                    """,
                    (proc["id"], now, signature),
                )
            stats["promoted"] += 1
    return stats


def backfill_procedures(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    scope: Optional[str] = None,
    limit: int = 100,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    clauses = [
        "m.retired_at IS NULL",
        "COALESCE(m.memory_type, 'episodic') != 'procedural'",
        "m.category IN ('convention', 'lesson', 'integration', 'decision')",
        "NOT EXISTS (SELECT 1 FROM procedure_sources ps WHERE ps.memory_id = m.id)",
    ]
    params: list[Any] = []
    if scope:
        clauses.append("(m.scope = ? OR m.scope = 'global')")
        params.append(scope)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT m.id, m.content, m.category, m.scope, m.confidence
          FROM memories m
         WHERE {' AND '.join(clauses)}
         ORDER BY m.created_at DESC
         LIMIT ?
        """,
        params,
    ).fetchall()

    stats = {
        "ok": True,
        "scanned_memories": len(rows),
        "created_procedures": 0,
        "created_from_decisions": 0,
        "created_from_events": 0,
        "procedure_ids": [],
    }

    for row in rows:
        if not looks_procedural(row["content"]):
            continue
        stats["created_procedures"] += 1
        if dry_run:
            continue
        proc = ensure_procedure_for_memory(conn, memory_id=int(row["id"]), agent_id=agent_id)
        stats["procedure_ids"].append(proc["id"])

    decision_rows = conn.execute(
        """
        SELECT d.id, d.title, d.rationale, d.project
          FROM decisions d
         WHERE NOT EXISTS (
               SELECT 1 FROM procedure_sources ps WHERE ps.decision_id = d.id
         )
         ORDER BY d.created_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in decision_rows:
        combined = f"{row['title']}. {row['rationale']}"
        if not looks_procedural(combined):
            continue
        stats["created_from_decisions"] += 1
        if dry_run:
            continue
        payload = parse_procedural_text(combined, title=row["title"], scope=f"project:{row['project']}" if row["project"] else "global")
        proc = create_procedure(
            conn,
            agent_id=agent_id,
            payload=payload,
            category="decision",
            scope=f"project:{row['project']}" if row["project"] else "global",
            confidence=0.75,
            source_decision_ids=[int(row["id"])],
        )
        stats["procedure_ids"].append(proc["id"])

    event_rows = conn.execute(
        """
        SELECT e.id, e.summary, COALESCE(e.detail, '') AS detail, e.project
          FROM events e
         WHERE e.event_type IN ('error', 'warning', 'artifact', 'result')
           AND NOT EXISTS (
               SELECT 1 FROM procedure_sources ps WHERE ps.event_id = e.id
           )
         ORDER BY e.created_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in event_rows:
        combined = f"{row['summary']} {row['detail']}".strip()
        if not looks_procedural(combined):
            continue
        stats["created_from_events"] += 1
        if dry_run:
            continue
        payload = parse_procedural_text(
            combined,
            title=row["summary"],
            scope=f"project:{row['project']}" if row["project"] else "global",
        )
        proc = create_procedure(
            conn,
            agent_id=agent_id,
            payload=payload,
            category="lesson",
            scope=f"project:{row['project']}" if row["project"] else "global",
            confidence=0.7,
            source_event_ids=[int(row["id"])],
        )
        stats["procedure_ids"].append(proc["id"])

    candidate_stats = synthesize_procedure_candidates(
        conn,
        agent_id=agent_id,
        dry_run=dry_run,
    )
    stats["candidate_stats"] = candidate_stats
    return stats


def procedure_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_procedure_schema(conn)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM procedures GROUP BY status"
    ).fetchall()
    out = {row["status"]: row["cnt"] for row in rows}
    total = sum(out.values())
    candidate_count = conn.execute(
        "SELECT COUNT(*) FROM procedure_candidates"
    ).fetchone()[0]
    return {
        "ok": True,
        "total": total,
        "by_status": out,
        "candidates": candidate_count,
    }
