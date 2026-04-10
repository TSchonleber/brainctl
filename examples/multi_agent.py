#!/usr/bin/env python3
"""brainctl multi-agent — two agents sharing knowledge through a single brain.db.

Demonstrates cross-agent knowledge discovery: one agent stores facts,
another discovers them via search. Both read from the same database.

Run:  python examples/multi_agent.py
"""
import os, tempfile
from agentmemory import Brain

db_path = os.path.join(tempfile.gettempdir(), "multi_agent_brain.db")

# Two Brain instances pointing at the SAME database, different agent IDs
researcher = Brain(db_path, agent_id="researcher")
deployer = Brain(db_path, agent_id="deployer")

# ── RESEARCHER stores knowledge ─────────────────────────────────────
print("=== Researcher Phase ===")
researcher.log("Starting code review", event_type="session_start", project="api-v2")
researcher.remember("Auth module uses bcrypt with cost=12", category="convention")
researcher.remember("Database connection pool capped at 20 connections", category="environment")
researcher.remember("All API responses must include X-Request-Id header", category="convention")
researcher.entity("api-v2", "project", observations=["REST API", "Python 3.12", "PostgreSQL"])
researcher.entity("AuthModule", "tool", observations=["bcrypt cost=12", "JWT tokens", "24h expiry"])
researcher.relate("api-v2", "uses", "AuthModule")
researcher.decide("Keep bcrypt cost at 12", "Good balance of security vs latency at current scale", project="api-v2")
researcher.log("Completed code review", event_type="result", project="api-v2")
print(f"  Researcher stored: {researcher.stats()['active_memories']} memories")

# ── DEPLOYER discovers researcher's knowledge ────────────────────────
print("\n=== Deployer Phase ===")
deployer.log("Starting deployment prep", event_type="session_start", project="api-v2")

# Search sees ALL agents' memories by default — no isolation
results = deployer.search("api-v2")
print(f"  Deployer found {len(results)} memories (including researcher's)")
for r in results:
    print(f"    [{r['category']}] {r['content'][:70]}")

# Deployer adds its own knowledge
deployer.remember("Staging deploy takes ~3 minutes via GitHub Actions", category="environment")
deployer.remember("Production requires 2-person approval in Slack #deploys", category="convention")
deployer.entity("StagingEnv", "service", observations=["us-east-1", "t3.large", "3 min deploy"])
deployer.relate("api-v2", "deployed_to", "StagingEnv")
deployer.log("Deployed api-v2 to staging", event_type="result", project="api-v2")

# ── CROSS-AGENT STATS ────────────────────────────────────────────────
print("\n=== Cross-Agent View ===")
# Both agents see the same totals (shared DB)
r_stats = researcher.stats()
d_stats = deployer.stats()
print(f"  Total memories: {r_stats['active_memories']} (both agents see the same count)")
print(f"  Total entities: {r_stats['entities']}")
print(f"  Total events: {r_stats['events']}")
print(f"  Knowledge edges: {r_stats['knowledge_edges']}")

# Deployer can also find researcher's entities
auth_results = deployer.search("bcrypt cost")
print(f"\n  Deployer searching 'bcrypt cost': {len(auth_results)} result(s)")
if auth_results:
    print(f"    Found: {auth_results[0]['content']}")

print("\nNote: For agent-scoped search, use CLI: brainctl -a researcher memory search ...")
print("      For cross-agent borrowing in MCP: memory_search(borrow_from='researcher')")
