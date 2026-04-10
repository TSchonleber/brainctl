#!/usr/bin/env python3
"""brainctl quickstart — the absolute minimum to get started.

Run:  python examples/quickstart.py
"""
import os, tempfile
from agentmemory import Brain

# Use a temp path so this example doesn't touch your real brain.db
brain = Brain(os.path.join(tempfile.gettempdir(), "quickstart_brain.db"), agent_id="quickstart")

# Store memories with different categories
brain.remember("Auth module uses JWT with 24h expiry", category="convention")
brain.remember("Always check rate limits before bulk API calls", category="lesson", confidence=0.9)
brain.remember("User prefers dark mode and compact layout", category="preference")

# Search (uses FTS5 full-text search with porter stemming)
results = brain.search("JWT auth")
print(f"Search 'JWT auth': {len(results)} result(s)")
for r in results:
    print(f"  [{r['category']}] {r['content']}")

# Build a knowledge graph
brain.entity("Alice", "person", observations=["Senior engineer", "Owns auth module"])
brain.entity("Acme", "organization", observations=["Series B startup", "50 engineers"])
brain.relate("Alice", "works_at", "Acme")

# Log events and decisions
brain.log("Completed auth module review", event_type="result", project="api-v2")
brain.decide("Keep JWT expiry at 24h", "Balance of security and UX", project="api-v2")

# Check stats
print(f"\nStats: {brain.stats()}")

# Diagnostics
dx = brain.doctor()
print(f"Health: {'healthy' if dx['healthy'] else 'issues found'} | DB: {dx['db_size_mb']} MB")
