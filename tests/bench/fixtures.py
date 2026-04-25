"""Deterministic synthetic fixtures for the search-quality benchmark.

A small corpus of ~30 memories + ~10 entities + ~10 events covering a mix
of entity, procedural, decision, historical/timeline, and troubleshooting
content.

Queries are graded 3 (primary answer), 2 (strongly related), 1 (tangential).
The grading is per-memory ID; scores default to 0 for unmentioned rows so
nDCG_at_k can treat them as irrelevant. Relevance maps use stable
`content_key` strings rather than integer IDs so the runner can reseed and
still resolve them after inserts.

Nothing in this file imports agentmemory; it's a pure data module so the
runner can be imported cheaply in tests and in `bin/brainctl-bench`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Memory:
    key: str                          # stable slug, used for relevance lookup
    content: str
    category: str = "project"
    confidence: float = 0.9


@dataclass
class Event:
    key: str
    summary: str
    event_type: str = "observation"
    project: str = "bench"
    importance: float = 0.5


@dataclass
class EntityFixture:
    name: str
    entity_type: str
    observations: List[str] = field(default_factory=list)


@dataclass
class ProcedureFixture:
    key: str
    title: str
    goal: str
    description: str
    procedure_kind: str = "workflow"
    steps: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    failure_modes: List[str] = field(default_factory=list)
    rollback_steps: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    status: str = "active"
    scope: str = "global"
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    stale_after_days: int = 90


@dataclass
class Query:
    text: str
    category: str                     # entity|temporal|procedural|decision|troubleshooting|negative
    # Map of {"mem:<key>" | "evt:<key>" | "ent:<name>" | "proc:<key>": relevance grade 1-3}
    relevance: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

MEMORIES: List[Memory] = [
    # --- User preferences (entity/preference queries) ----------------------
    Memory("pref-dark-mode",
           "Alice prefers dark mode in all UIs and tools she uses daily.",
           category="preference"),
    Memory("pref-tabs",
           "Bob uses four-space indentation and dislikes tabs for Python code.",
           category="preference"),
    Memory("pref-coffee",
           "Carol drinks decaf coffee after 3pm and never soda in meetings.",
           category="preference"),

    # --- Project facts -----------------------------------------------------
    Memory("proj-python-version",
           "The brainctl project runs on Python 3.12 with strict type hints.",
           category="project"),
    Memory("proj-db-engine",
           "brainctl stores all memories in SQLite with FTS5 and sqlite-vec.",
           category="project"),
    Memory("proj-embedding",
           "Embeddings come from Ollama nomic-embed-text at 768 dimensions.",
           category="project"),
    Memory("proj-agents",
           "The system supports per-agent scopes identified by agent_id strings.",
           category="project"),

    # --- Conventions / how-tos (procedural queries) ------------------------
    Memory("how-deploy",
           "To deploy to staging, run make deploy-staging after passing tests.",
           category="convention"),
    Memory("how-rollback",
           "Rollback procedure: git revert the merge commit then re-run deploy.",
           category="convention"),
    Memory("how-migrate",
           "Apply pending migrations with brainctl migrate before rebooting services.",
           category="convention"),
    Memory("how-test",
           "Run the test suite with pytest -xvs from the repo root.",
           category="convention"),

    # --- Decisions (decision queries) --------------------------------------
    Memory("dec-sqlite",
           "We chose SQLite over Postgres because it ships with Python and needs zero ops.",
           category="decision"),
    Memory("dec-rrf",
           "We picked Reciprocal Rank Fusion for hybrid search because it beat weighted sums on our corpus.",
           category="decision"),
    Memory("dec-wm-gate",
           "The W(m) worthiness gate exists to prevent low-surprise memories from bloating the corpus.",
           category="decision"),
    Memory("dec-nomic",
           "We standardized on nomic-embed-text for local embeddings because it fits 768-dim vectors in RAM.",
           category="decision"),

    # --- Lessons / troubleshooting (error queries) -------------------------
    Memory("lesson-fts-escape",
           "FTS5 MATCH queries fail on unescaped punctuation; sanitize with _sanitize_fts_query before searching.",
           category="lesson"),
    Memory("lesson-vec-ext",
           "If sqlite-vec extension is missing, vector search silently falls back to FTS5 only.",
           category="lesson"),
    Memory("lesson-wal",
           "SQLite WAL mode is mandatory for concurrent reads during consolidation cycles.",
           category="lesson"),
    Memory("lesson-ollama",
           "When Ollama is not running, embedding calls hang indefinitely unless a timeout is set.",
           category="lesson"),

    # --- Identity / user background ----------------------------------------
    Memory("user-alice-role",
           "Alice is the primary maintainer of the retrieval pipeline.",
           category="user"),
    Memory("user-bob-role",
           "Bob owns the consolidation daemon and dream cycles.",
           category="user"),
    Memory("user-carol-role",
           "Carol is the security reviewer and approves PII-touching changes.",
           category="user"),

    # --- Environment / integration -----------------------------------------
    Memory("env-ollama-port",
           "Ollama runs on localhost port 11434 by default.",
           category="environment"),
    Memory("env-brain-db-path",
           "The active brain.db path is resolved via $BRAIN_DB or get_db_path().",
           category="environment"),
    Memory("int-mcp-stdio",
           "MCP server uses stdio transport and is started with python3 bin/brainctl-mcp.",
           category="integration"),
    Memory("int-langchain",
           "LangChain retriever adapter lives under src/agentmemory/integrations.",
           category="integration"),

    # --- Distractors (improve recall discriminability) ---------------------
    Memory("distract-1",
           "The office kitchen restocks snacks every Monday morning.",
           category="environment", confidence=0.4),
    Memory("distract-2",
           "Yesterday the rain delayed the bus but nobody missed standup.",
           category="environment", confidence=0.3),
    Memory("distract-3",
           "A quick brown fox jumps over the lazy dog repeatedly.",
           category="lesson", confidence=0.2),
    Memory("distract-4",
           "Lorem ipsum dolor sit amet consectetur adipiscing elit.",
           category="lesson", confidence=0.2),
]


EVENTS: List[Event] = [
    Event("evt-deploy-v1", "Deployed brainctl v1.0 to staging after green CI",
          event_type="artifact", project="brainctl"),
    Event("evt-deploy-v2", "Deployed brainctl v2.0 with the hybrid search rollout",
          event_type="artifact", project="brainctl"),
    Event("evt-migration-031", "Applied migration 031_dmem_write_tiers in production",
          event_type="artifact", project="brainctl"),
    Event("evt-rollback", "Rolled back bad release by reverting merge commit 0xdead",
          event_type="warning", project="brainctl"),
    Event("evt-error-fts",
          "Hit sqlite3.OperationalError: fts5 syntax error near '.' during search",
          event_type="error", project="brainctl"),
    Event("evt-ollama-outage",
          "Ollama container crashed overnight and blocked all embeddings",
          event_type="error", project="brainctl"),
    Event("evt-handoff",
          "Alice handed off retrieval reranking work to Bob before vacation",
          event_type="handoff", project="brainctl"),
    Event("evt-decision-rrf",
          "Team agreed to adopt Reciprocal Rank Fusion after benchmarks",
          event_type="decision", project="brainctl"),
]


PROCEDURES: List[ProcedureFixture] = [
    ProcedureFixture(
        key="deploy-staging",
        title="Staging deploy runbook",
        goal="Deploy the current branch to staging safely",
        description="Canonical staging deployment sequence. [key=proc:deploy-staging]",
        procedure_kind="runbook",
        steps=[
            "Run the full test suite and confirm CI is green.",
            "Apply pending database migrations with brainctl migrate.",
            "Deploy the release to staging.",
            "Verify health checks and smoke tests after rollout.",
        ],
        tools=["pytest", "brainctl", "deployctl"],
        rollback_steps=["Redeploy the previous staging release.", "Verify health checks return to green."],
        success_criteria=["Staging health checks are green.", "Smoke tests pass after deploy."],
        execution_count=8,
        success_count=7,
        failure_count=1,
    ),
    ProcedureFixture(
        key="rollback-release",
        title="Rollback bad release",
        goal="Roll back a bad release without extending downtime",
        description="Rollback playbook for failed deploys. [key=proc:rollback-release]",
        procedure_kind="rollback",
        steps=[
            "Pause further deploys and identify the last known good release.",
            "Redeploy the previous release artifact.",
            "Re-run health checks and confirm error rates recover.",
            "Open a follow-up incident note with the failing release id.",
        ],
        tools=["deployctl", "healthcheck"],
        failure_modes=["Health checks still failing after rollback."],
        rollback_steps=["Escalate to on-call and keep the platform on the last known good release."],
        success_criteria=["Previous release is serving traffic cleanly."],
        execution_count=6,
        success_count=6,
    ),
    ProcedureFixture(
        key="apply-migrations",
        title="Apply pending migrations",
        goal="Apply schema migrations before restarting dependent services",
        description="Migration runbook used during deploys. [key=proc:apply-migrations]",
        procedure_kind="workflow",
        steps=[
            "Inspect the pending migration list.",
            "Run brainctl migrate against the target database.",
            "Restart the dependent service after migrations complete.",
        ],
        tools=["brainctl"],
        success_criteria=["Schema version matches the newest applied migration."],
        execution_count=5,
        success_count=5,
    ),
    ProcedureFixture(
        key="fts-punctuation",
        title="Troubleshoot FTS5 punctuation errors",
        goal="Fix FTS5 syntax errors caused by punctuation in queries",
        description="Troubleshooting playbook for punctuation-sensitive FTS5 queries. [key=proc:fts-punctuation]",
        procedure_kind="troubleshooting",
        steps=[
            "Reproduce the failing query and capture the sqlite3 error message.",
            "Sanitize punctuation with _sanitize_fts_query before sending the query to MATCH.",
            "Re-run the search and verify the syntax error no longer occurs.",
        ],
        tools=["sqlite3", "brainctl"],
        failure_modes=["Unsafe punctuation reaches MATCH unchanged."],
        rollback_steps=["Fallback to a LIKE query while the sanitizer fix is being rolled out."],
        success_criteria=["Search completes without an FTS5 syntax error."],
        execution_count=4,
        success_count=4,
    ),
    ProcedureFixture(
        key="deploy-staging-legacy",
        title="Legacy staging deploy",
        goal="Old staging deploy sequence kept for audit history",
        description="Deprecated staging deployment sequence. [key=proc:deploy-staging-legacy]",
        procedure_kind="runbook",
        steps=[
            "Deploy directly to staging.",
            "Run tests after the deploy completes.",
        ],
        status="stale",
        execution_count=2,
        success_count=1,
        failure_count=1,
        stale_after_days=14,
    ),
]


ENTITIES: List[EntityFixture] = [
    EntityFixture("Alice", "person", ["Owns retrieval pipeline", "Prefers dark mode"]),
    EntityFixture("Bob",   "person", ["Owns consolidation daemon", "Writes Python"]),
    EntityFixture("Carol", "person", ["Security reviewer", "PII gatekeeper"]),
    EntityFixture("brainctl", "project", ["SQLite-backed agent memory system"]),
    EntityFixture("Ollama", "service", ["Local embedding provider on 11434"]),
    EntityFixture("SQLite", "tool",   ["Embedded relational database"]),
]


# ---------------------------------------------------------------------------
# Graded query set
# ---------------------------------------------------------------------------
# Grades: 3 = primary answer, 2 = strongly related, 1 = tangential.
# Keys are prefixed "mem:", "evt:", or "ent:" to disambiguate.

QUERIES: List[Query] = [
    # Entity / preference lookups
    Query("What does Alice prefer?", "entity", {
        "mem:pref-dark-mode": 3,
        "mem:user-alice-role": 2,
        "ent:Alice": 2,
    }),
    Query("Who owns the consolidation daemon?", "entity", {
        "mem:user-bob-role": 3,
        "ent:Bob": 2,
    }),
    Query("Who is the security reviewer?", "entity", {
        "mem:user-carol-role": 3,
        "ent:Carol": 2,
        "mem:pref-coffee": 1,
    }),
    Query("What does Bob use for indentation?", "entity", {
        "mem:pref-tabs": 3,
        "mem:user-bob-role": 1,
    }),

    # Project facts
    Query("What Python version does brainctl use?", "entity", {
        "mem:proj-python-version": 3,
    }),
    Query("What embedding model do we use?", "entity", {
        "mem:proj-embedding": 3,
        "mem:dec-nomic": 3,
        "mem:env-ollama-port": 1,
    }),
    Query("Where is memory data stored?", "entity", {
        "mem:proj-db-engine": 3,
        "mem:dec-sqlite": 2,
        "mem:env-brain-db-path": 2,
    }),

    # Procedural / how-to
    Query("How do I deploy to staging?", "procedural", {
        "proc:deploy-staging": 3,
        "mem:how-deploy": 3,
        "proc:deploy-staging-legacy": 1,
        "evt:evt-deploy-v1": 1,
    }),
    Query("How do I roll back a bad release?", "procedural", {
        "proc:rollback-release": 3,
        "mem:how-rollback": 3,
        "evt:evt-rollback": 2,
    }),
    Query("How do I run the tests?", "procedural", {
        "mem:how-test": 3,
    }),
    Query("How do I apply migrations?", "procedural", {
        "proc:apply-migrations": 3,
        "mem:how-migrate": 3,
        "evt:evt-migration-031": 2,
    }),

    # Decision rationale
    Query("Why did we choose SQLite?", "decision", {
        "mem:dec-sqlite": 3,
        "mem:proj-db-engine": 2,
    }),
    Query("Why do we use RRF for search?", "decision", {
        "mem:dec-rrf": 3,
        "evt:evt-decision-rrf": 2,
    }),
    Query("Why does the W(m) gate exist?", "decision", {
        "mem:dec-wm-gate": 3,
    }),

    # Temporal / historical
    Query("When did we deploy v2.0?", "temporal", {
        "evt:evt-deploy-v2": 3,
        "evt:evt-deploy-v1": 1,
    }),
    Query("When did Ollama crash?", "temporal", {
        "evt:evt-ollama-outage": 3,
        "mem:lesson-ollama": 2,
    }),

    # Troubleshooting
    Query("FTS5 syntax error on punctuation", "troubleshooting", {
        "proc:fts-punctuation": 3,
        "mem:lesson-fts-escape": 3,
        "evt:evt-error-fts": 3,
    }),
    Query("sqlite-vec extension missing fallback", "troubleshooting", {
        "mem:lesson-vec-ext": 3,
    }),

    # Negative control — nothing in the corpus should match
    Query("Summary of yesterday's basketball game", "negative", {}),
    Query("How do I calibrate the lunar sensor array?", "negative", {}),

    # Ambiguous — multiple tangential results, no single primary
    Query("dark mode indentation coffee", "ambiguous", {
        "mem:pref-dark-mode": 1,
        "mem:pref-tabs": 1,
        "mem:pref-coffee": 1,
    }),
    Query("Which staging deploy should I use?", "ambiguous", {
        "proc:deploy-staging": 2,
        "proc:deploy-staging-legacy": 1,
        "mem:how-deploy": 1,
    }),
]


def key_for_result(result: dict) -> str:
    """Translate a search result dict into the stable fixture key.

    Runner embeds the fixture key into `content` / `summary` as a trailing
    marker of the form `[key=foo-bar]` so we can re-derive it after FTS5
    roundtrip. Falls back to None when the marker is missing.
    """
    probes = [
        result.get("content"),
        result.get("summary"),
        result.get("name"),
        result.get("title"),
        result.get("goal"),
        result.get("description"),
    ]
    for text in probes:
        if text and "[key=" in text:
            tail = text.split("[key=", 1)[1]
            return tail.split("]", 1)[0].strip()
    # Entity-name results have no marker
    if result.get("type") == "entity" and result.get("name"):
        return f"ent:{result['name']}"
    return ""
