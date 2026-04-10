#!/usr/bin/env python3
"""brainctl agent lifecycle — bootstrap, orient, work, record, handoff.

Demonstrates the Orient -> Work -> Record pattern from COGNITIVE_PROTOCOL.md
with session bookending and handoff for continuity.

Run:  python examples/agent_lifecycle.py
"""
import os, tempfile
from agentmemory import Brain

db_path = os.path.join(tempfile.gettempdir(), "lifecycle_brain.db")
brain = Brain(db_path, agent_id="lifecycle-demo")

# ── PHASE 1: BOOTSTRAP ──────────────────────────────────────────────
print("=== Phase 1: Bootstrap ===")
brain.log("Session started", event_type="session_start", project="api-v2")
brain.entity("api-v2", "project", observations=["REST API", "Python 3.12", "PostgreSQL"])
brain.entity("lifecycle-demo", "agent", observations=["Integration specialist"])
brain.relate("lifecycle-demo", "works_on", "api-v2")
print("  Registered agent and project entities")

# Set a trigger for future sessions
brain.trigger(
    condition="deployment failure detected",
    keywords="deploy,failure,rollback,502",
    action="Check rollback procedure and notify oncall",
    priority="critical",
)
print("  Set prospective memory trigger for deploy failures")

# ── PHASE 2: ORIENT ─────────────────────────────────────────────────
print("\n=== Phase 2: Orient ===")
existing = brain.search("api-v2 conventions")
print(f"  Found {len(existing)} existing memories about api-v2")
stats = brain.stats()
print(f"  Brain state: {stats['active_memories']} active memories, {stats['events']} events")

# ── PHASE 3: WORK ───────────────────────────────────────────────────
print("\n=== Phase 3: Work ===")

# Discover things while working
brain.remember("API rate-limits at 100 req/15s with Retry-After header", category="integration", confidence=0.9)
brain.remember("Team convention: all timestamps must be UTC ISO 8601", category="convention")
brain.remember("PostgreSQL connection pool max=20, timeout=30s", category="environment")
print("  Stored 3 discoveries as memories")

# Record a decision with rationale
brain.decide(
    "Use Retry-After header for rate limit backoff",
    "More reliable than fixed exponential backoff — server controls the timing",
    project="api-v2",
)
print("  Recorded decision: use Retry-After header")

# Build knowledge graph
brain.entity("RateLimitAPI", "service", observations=["100 req/15s", "Retry-After header", "us-east-1"])
brain.relate("api-v2", "depends_on", "RateLimitAPI")
print("  Linked api-v2 -> depends_on -> RateLimitAPI")

# Check if any triggers match what we're seeing
matches = brain.check_triggers("the staging deploy returned 502 errors")
if matches:
    print(f"  TRIGGER FIRED: {matches[0]['action']}")

# ── PHASE 4: RECORD ─────────────────────────────────────────────────
print("\n=== Phase 4: Record ===")
brain.log("Completed API integration analysis — rate limiting documented",
          event_type="result", project="api-v2", importance=0.8)
brain.affect_log("Satisfied with the rate limit discovery — clear path forward")
brain.log("Session ended", event_type="session_end", project="api-v2")
print("  Logged result event, affect state, and session end")

# ── PHASE 5: HANDOFF ────────────────────────────────────────────────
print("\n=== Phase 5: Handoff ===")
hid = brain.handoff(
    goal="Finish api-v2 integration with rate-limited external service",
    current_state="Rate limiting behavior documented. Auth module complete. Connection pool configured.",
    open_loops="Retry logic not yet implemented. Load testing not started.",
    next_step="Implement exponential backoff using Retry-After header. Then run load test at 80% capacity.",
    project="api-v2",
    title="API v2 Integration Sprint",
)
print(f"  Created handoff packet #{hid}")

# Simulate a new session resuming from the handoff
print("\n=== New Session: Resume ===")
brain2 = Brain(db_path, agent_id="lifecycle-demo")
packet = brain2.resume(project="api-v2")
if packet:
    print(f"  Resumed: {packet['goal']}")
    print(f"  State: {packet['current_state'][:80]}...")
    print(f"  Next: {packet['next_step'][:80]}...")
else:
    print("  No pending handoff found")

# Final stats
print(f"\nFinal stats: {brain2.stats()}")
dx = brain2.doctor()
print(f"Health: {'healthy' if dx['healthy'] else 'ISSUES: ' + str(dx['issues'])}")
