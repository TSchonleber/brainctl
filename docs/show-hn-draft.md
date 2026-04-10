# Show HN Draft

## Title (80 char max)

Show HN: brainctl – persistent memory for AI agents, single SQLite file, no server

## URL

https://github.com/TSchonleber/brainctl

## Text

I built brainctl because every AI agent I work with forgets everything between sessions. Context windows are temporary. RAG retrieves documents, not learned experience. Existing memory systems (mem0, Zep, MemGPT) all require servers, API keys, or LLM calls.

brainctl is a single SQLite file. pip install brainctl, import Brain, and your agent has persistent memory — memories, events, entities, a knowledge graph, decisions with rationale, prospective memory triggers, and session handoffs. No server, no API keys, no LLM calls for any memory operation.

The core pattern is three lines:

    context = brain.orient()          # start: get handoff + events + triggers
    brain.remember("fact", category="lesson")  # work
    brain.wrap_up("what I did")       # end: state preserved for next session

Key technical decisions:
- FTS5 full-text search with porter stemming (optional vector search via sqlite-vec)
- Write gate with surprise scoring rejects redundant memories at ingest time
- Three-tier routing (skip/cache/full-index) inspired by D-MEM (Song & Xin 2025, arXiv 2603.14597)
- Half-life decay per category — integration details fade in a month, identity persists for a year
- Bayesian confidence scoring with alpha/beta tracking
- MCP server included (192 tools for Claude Desktop / VS Code)
- LangChain and CrewAI adapters included

Python 3.11+, MIT licensed, zero dependencies for core.

---

## Posting notes

- Post weekday morning US time (9-10am ET, Tuesday-Thursday is best)
- Don't post on weekends or holidays
- Reply to comments within the first 2 hours — HN rewards engaged authors
- If someone asks "how is this different from X" — direct comparison, don't be defensive
- If someone points out a flaw — acknowledge it honestly, that builds credibility on HN
