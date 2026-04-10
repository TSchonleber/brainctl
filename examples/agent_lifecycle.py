#!/usr/bin/env python3
"""brainctl agent lifecycle — orient, work, wrap up.

Demonstrates the drop-in session pattern: brain.orient() at the start,
brain.wrap_up() at the end, with memory/entity/decision work in between.

Run:  python examples/agent_lifecycle.py
"""
import os, tempfile
from agentmemory import Brain

db_path = os.path.join(tempfile.gettempdir(), "lifecycle_brain.db")

# ── SESSION 1 ────────────────────────────────────────────────────────
print("=== Session 1 ===\n")
brain = Brain(db_path, agent_id="lifecycle-demo")

# One call to start — gets handoff, events, triggers, relevant memories
context = brain.orient(project="api-v2")
print(f"Orient: {context['stats']}")
if context["handoff"]:
    print(f"  Resuming: {context['handoff']['goal']}")
    brain.resume()  # consume it
else:
    print("  No prior handoff — fresh start")

# Set a trigger for future sessions
brain.trigger("deploy failure", "deploy,failure,rollback,502",
              "Check rollback procedure and notify oncall", priority="critical")

# Work: discover things, build knowledge graph
brain.remember("API rate-limits at 100 req/15s with Retry-After header",
               category="integration", confidence=0.9)
brain.remember("Team convention: all timestamps must be UTC ISO 8601",
               category="convention")
brain.entity("api-v2", "project", observations=["REST API", "Python 3.12"])
brain.entity("RateLimitAPI", "service", observations=["100 req/15s", "Retry-After"])
brain.relate("api-v2", "depends_on", "RateLimitAPI")
brain.decide("Use Retry-After header for rate limit backoff",
             "Server controls timing — more reliable than fixed exponential",
             project="api-v2")
print("  Stored 2 memories, 2 entities, 1 relation, 1 decision")

# Check triggers against what we're seeing
matches = brain.check_triggers("staging deploy returned 502 errors")
if matches:
    print(f"  TRIGGER FIRED: {matches[0]['action']}")

# One call to finish — logs session_end + creates handoff
result = brain.wrap_up(
    summary="Documented rate limiting behavior and auth conventions",
    goal="Ship api-v2 with rate-limited external service integration",
    open_loops="Retry logic not implemented. Load testing not started.",
    next_step="Implement exponential backoff using Retry-After header",
    project="api-v2",
)
print(f"  Wrapped up: event #{result['event_id']}, handoff #{result['handoff_id']}")

# ── SESSION 2 (simulated new agent picking up) ───────────────────────
print("\n=== Session 2 ===\n")
brain2 = Brain(db_path, agent_id="lifecycle-demo")

context2 = brain2.orient(project="api-v2")
if context2["handoff"]:
    print(f"Resuming: {context2['handoff']['goal']}")
    print(f"  State: {context2['handoff']['current_state'][:80]}...")
    print(f"  Next: {context2['handoff']['next_step'][:80]}...")
    brain2.resume(project="api-v2")  # consume
else:
    print("No handoff found")

print(f"Triggers active: {len(context2['triggers'])}")
print(f"Recent events: {len(context2['recent_events'])}")
print(f"Stats: {context2['stats']}")
