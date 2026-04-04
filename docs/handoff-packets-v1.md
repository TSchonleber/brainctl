# Handoff Packets v1

## Goal
Let Hermes resume work cleanly after a session reset without dragging the full transcript forever.

brainctl should store a temporary handoff packet, Hermes should restore from it, then durable facts should be promoted and the raw handoff should be consumed.

## Problem this solves
- Tuesday conversation ends
- session resets overnight
- Thursday user wants to continue naturally
- Hermes needs working-state continuity without keeping a bloated live session

## Design principles
- handoff packets are temporary working memory
- long-term memory is for durable facts only
- raw handoffs should be consumed or expired
- retrieval should prefer the latest unconsumed handoff for the same scope
- project scope matters more than global scope

## Proposed storage model
Use a dedicated table instead of overloading generic memories.

### New table: handoff_packets

Columns:
- id INTEGER PRIMARY KEY
- agent_id TEXT NOT NULL REFERENCES agents(id)
- session_id TEXT
- chat_id TEXT
- thread_id TEXT
- user_id TEXT
- project TEXT
- scope TEXT NOT NULL
- status TEXT NOT NULL
  - pending
  - consumed
  - expired
  - pinned
- title TEXT
- goal TEXT
- current_state TEXT
- open_loops TEXT
- next_step TEXT
- recent_tail TEXT
- decisions_json TEXT
- entities_json TEXT
- tasks_json TEXT
- facts_json TEXT
- source_event_id INTEGER REFERENCES events(id)
- consumed_at TEXT
- expires_at TEXT
- created_at TEXT NOT NULL
- updated_at TEXT NOT NULL

Indexes:
- (status, created_at DESC)
- (chat_id, thread_id, status, created_at DESC)
- (project, status, created_at DESC)
- (session_id)

## Packet contents
Keep packets structured and short.

### Required fields
- title
- goal
- current_state
- open_loops
- next_step

### Optional structured fields
- decisions_json
- entities_json
- tasks_json
- facts_json
- recent_tail

## Resume flow
1. Hermes session resets
2. Hermes creates a handoff packet
3. brainctl stores it with status=pending
4. user returns later
5. Hermes fetches latest relevant pending handoff
6. Hermes injects a compact resume context into the model
7. Hermes promotes durable facts into memories/entities/decisions/events
8. brainctl marks handoff consumed
9. cleanup later expires or deletes old consumed packets

## Matching logic
Preferred matching order:
1. same chat_id + same thread_id
2. same chat_id
3. same project
4. same user_id + agent_id
5. recent global fallback

## Promotion rules
Promote only durable information.

Promote to memories:
- stable preferences
- recurring conventions
- durable project facts

Promote to decisions:
- explicit choices and rationale

Promote to entities:
- people, projects, tools, systems with durable facts

Promote to events:
- major milestone or handoff completion

Do not promote:
- temporary phrasing
- low-value chit-chat
- raw transcript chunks unless needed as context records

## Expiry rules
- pending handoff: default 30 days
- consumed handoff: default 7 days
- pinned handoff: no expiry
- expired handoff can be deleted by cleanup job later

## CLI ideas
- brainctl handoff add
- brainctl handoff latest
- brainctl handoff list
- brainctl handoff consume
- brainctl handoff expire
- brainctl handoff pin

## MCP ideas
- handoff_add
- handoff_latest
- handoff_consume

## Hermes integration shape
Short term:
- Hermes creates packet on reset
- Hermes resumes from latest packet manually or automatically

Later:
- Hermes auto-extracts durable facts after resume
- Hermes can generate packets directly over MCP

## What we are not doing in v1
- no transcript replay engine
- no dream synthesis
- no theory-of-mind logic
- no multi-packet merging heuristics beyond simple latest-match retrieval

## Why dedicated table is better than generic memories
- avoids memory landfill
- clean lifecycle tracking
- easier retrieval
- clearer distinction between temporary working state and long-term memory
