COSTCLOCK AI — MEMORY RESEARCH BRIEFING
=========================================
3 waves | 19 deliverables | 178-agent target

1. WHAT WE LEARNED ABOUT MEMORY
- Brain consolidates via replay (sleep), not instant storage — we need offline batch cycles
- Every recall modifies the memory — confidence must evolve on access
- Forgetting is adaptive — pruning unused memories IMPROVES cognition
- Salience gates encoding — critical task outcomes should weight higher
- Memories are content-addressable patterns — embeddings ARE artificial engrams
- Hybrid BM25+vector beats pure vector by 10-20% (AI systems survey)
- Reflexion (storing failure lessons) gives 20-40% reasoning boost
- Lost-in-middle: never put key info in middle of context window

2. WHAT WE BUILT (8 algorithms, all research prototypes)
- Spaced Repetition: decay + recall boost on access
- Semantic Forgetting: demote/promote by access patterns
- Knowledge Graph: PageRank + BFS over memory edges (1,933 edges)
- Salience Routing: 4-factor weighted scoring (FTS+vec)
- Consolidation Cycle: "sleep" orchestrator wrapping all passes
- Contradiction Detection: negation + supersession audit
- Emergence Detection: topic trending + store health
- Context Compression: token-budget selection + dedup

STATUS: All coded. NONE in production. No cron job. sqlite-vec not installed.

3. TOP 10 IMPROVEMENTS (ranked by impact)
1. Nightly sleep cycle — wire consolidation to cron [1 day]
2. Event-to-memory distillation — only 14 memories from 123 events [2d]
3. Hybrid BM25+vector search — both engines exist, just merge [2d]
4. Episodic/semantic split — facts shouldn't decay like events [3d]
5. Proactive memory push — pre-fetch context at checkout [2d]
6. Spreading activation — graph-based associative recall [2d]
7. Reflexion memory — store & reuse failure lessons [1d]
8. Write contention fix — version col + optimistic locking [2d]
9. Provenance & trust chains — track verification, cascade retractions [3d]
10. Situation models — answer "what's happening?" not just "what is?" [5d]

4. RESEARCH FRONTIERS
DELIVERED: Associative memory, episodic/semantic bifurcation, provenance, write contention, situation models, proactive push
BACKLOG: Metacognition, predictive cognition, collective intelligence, temporal reasoning, adversarial robustness
MOST PROMISING UNEXPLORED:
- Agent-to-agent real-time knowledge transfer (pub/sub)
- Memory-to-goal feedback (patterns auto-generate goals)
- Causal event graph (answer "why?" not just "what?")
- Continuous LLM compression (kill garbage accumulation)

5. WHAT'S NAIVE RIGHT NOW
- 89% of experience is lost (14 memories / 123 events)
- All 8 algorithms sit unused — no consolidation running
- sqlite-vec = dead code (not installed)
- Facts decay like events (no type distinction)
- No provenance — can't tell verified from hallucinated
- Single-writer SQLite will bottleneck at scale
- Agents only find what they know to ask for
- Timestamp inconsistency already causing sort bugs

10x VERSION WOULD HAVE:
- Continuous LLM consolidation, not nightly
- Embedding-first writes (auto-embed async)
- Hybrid retrieval + reranking as default
- Causal+temporal graph over all events
- Agent self-models as queryable memories
- Memory as policy engine (decisions queried, not just recalled)
- Push-based anticipatory context with feedback learning
- Federated shards for horizontal write scaling
