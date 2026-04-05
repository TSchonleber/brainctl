# State-of-the-Art AI Memory & Cognition Systems
## Research Report — COS-78
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**Target:** brain.db — SQLite + FTS5 + sqlite-vec, serving ~178 agents

---

## 1. MemGPT / Letta — Virtual Context Management

### What It Is
MemGPT (Packer et al., 2023) treats the LLM context window as CPU registers and uses structured external memory as RAM/disk. The agent self-manages what stays in-context vs. what gets paged out. Letta is its production successor with a persistent-agent server architecture.

### How It Works
- **Main context**: In-window working memory (system + conversation)
- **External storage**: Archival (vector search) + recall (conversation history, recency-weighted)
- **Self-directed memory ops**: `core_memory_append`, `core_memory_replace`, `archival_memory_insert`, `archival_memory_search` — the agent calls these as tool functions
- **Memory hierarchy**: Persona + human summaries (core) → message history → archival (full vector store)

### What's Proven Effective
- Agents maintain coherent long-term relationships and facts across 1000s of turns
- Self-editing core memory beats static summaries — the agent knows what matters
- Explicit paging eliminates silent context truncation failures
- Letta's stateful server model allows agent "identity" to persist across invocations

### Limitations
- Memory operations cost tokens (each tool call + result)
- Requires the LLM to reliably trigger memory ops — weaker models miss writes
- Core memory size (~2K tokens) limits how much "active" state an agent holds

### Implementation Recommendations for brain.db
- **Two-tier memory**: `core_facts` table (small, always-fetched, agent-specific) + `archival` (vector search on demand)
- `core_facts` rows: `agent_id`, `key` (e.g. "persona", "user_model"), `value TEXT`, `updated_at`
- Agents should be able to PATCH their own core_facts via brainctl — equivalent to `core_memory_replace`
- FTS5 provides the "recall" tier (keyword search over history); sqlite-vec provides the "archival" tier (semantic search)
- **Critical**: expire/summarize old message history, never let the recall tier grow unbounded

---

## 2. Advanced RAG — Hybrid Search, Reranking, Contextual Retrieval

### What It Is
Retrieval-Augmented Generation extended beyond naive top-k cosine similarity. Production systems combine multiple retrieval signals and post-retrieval reranking to dramatically improve recall precision.

### How It Works
**Hybrid search (BM25 + vector)**:
- BM25 (term frequency) excels at exact keyword matches, proper nouns, IDs
- Dense vector search excels at semantic similarity, paraphrase matching
- Reciprocal Rank Fusion (RRF) merges results: `score = Σ 1/(k + rank_i)` where k≈60
- SQLite FTS5 is a native BM25 engine; sqlite-vec handles dense vectors → brain.db already has both

**Contextual retrieval (Anthropic, 2024)**:
- Before indexing, prepend a 1-2 sentence context to each chunk explaining its position in the source document
- Reduces retrieval failure rate by ~49% on benchmarks
- Cost: one LLM call per chunk at index time (amortized, not per-query)

**Reranking**:
- First-pass: fast BM25/ANN retrieval fetches top-50-100 candidates
- Second-pass: cross-encoder model (e.g. BGE-reranker, Cohere Rerank) scores all candidates against query
- Returns top-5-10 with dramatically better precision
- For brain.db: use a small local cross-encoder (sentence-transformers/ms-marco-MiniLM) via Python

**HyDE (Hypothetical Document Embeddings)**:
- Generate a hypothetical answer to the query, embed that answer, search for similar real documents
- Bridges the query-document distribution gap; useful for "what does X do?" style queries

### What's Proven Effective
- Hybrid BM25+vector outperforms pure vector by 10-20% on BEIR benchmark
- Contextual retrieval + reranking combined: ~67% reduction in top-20 retrieval failures
- HyDE effective when queries are short and documents are long-form

### Limitations
- Reranking adds latency (cross-encoder is slow without GPU)
- Contextual retrieval requires re-indexing all existing content when adopted
- HyDE can hallucinate in the hypothetical answer, poisoning the search

### Implementation Recommendations for brain.db
```sql
-- Hybrid search: FTS5 for BM25, sqlite-vec for dense
-- RRF merge in Python/brainctl query layer

SELECT m.id, m.body,
  1.0/(60 + fts_rank) AS bm25_rrf,
  1.0/(60 + vec_rank) AS vec_rrf
FROM memories m
...
ORDER BY (bm25_rrf + vec_rrf) DESC LIMIT 20
```
- Contextual chunk enrichment: when storing new memories, have the writing agent include 1-sentence context prefix
- Reranker: implement as optional `--rerank` flag in `brainctl search` for high-stakes queries
- FTS5 porter tokenizer handles stemming automatically

---

## 3. Lost-in-the-Middle — Context Window Utilization

### What It Is
Liu et al. (2023) documented that LLMs systematically underutilize information in the middle of long contexts. Performance peaks when relevant info is at the start or end of the context window.

### How It Works
- Attention patterns in transformers show primacy and recency bias
- Middle-of-context information can be effectively invisible even when within the context window
- Effect worsens with longer contexts; significant at 4K+, severe at 16K+

### What's Proven Effective
- **Lost-in-middle-aware retrieval**: Never place the single most important retrieved chunk in the middle — front-load or back-load it
- **Re-ordering**: After retrieval, sort by relevance descending, then interleave: highest at start, second-highest at end, rest in middle
- **Summarization over stuffing**: Summarizing many chunks beats including them verbatim when context is long

### Implementation Recommendations for brain.db
- When `brainctl search` returns results for injection into agent context, sort: `[rank1, rank3, rank5, ..., rank4, rank2]` (most relevant first and last)
- Implement a `--context-order lost-in-middle` flag for agents that do multi-document retrieval
- Memory summaries (for long history) should always precede raw retrieved chunks

---

## 4. Reflexion — Self-Reflective Agents

### What It Is
Shinn et al. (2023). Agent maintains a "reflective memory" — after each episode, it generates a linguistic reflection on what went wrong, stores it, and includes it in future episodes.

### How It Works
1. Agent attempts task → gets outcome signal (success/failure/score)
2. On failure: LLM generates reflection: "I failed because X, next time I should Y"
3. Reflection stored in episodic memory
4. On next attempt: reflections prepended to system prompt
5. Agent self-corrects based on accumulated reflective experience

### What's Proven Effective
- 20-40% improvement on reasoning benchmarks (HotpotQA, HumanEval) with 3+ reflection iterations
- Outperforms chain-of-thought for tasks with feedback signals
- Works best with well-defined success/failure signals
- Memory of past failures prevents repeating the same mistakes

### Limitations
- Requires a feedback loop (explicit reward/failure signal per run)
- Reflections can be hallucinated (agent rationalizes rather than diagnoses)
- Memory grows indefinitely — needs compression or expiry

### Implementation Recommendations for brain.db
- Add `reflection` memory category in brain.db with `source_run_id`, `outcome` (success/fail), `lesson TEXT`
- `brainctl reflect add "<lesson>" --run <run_id> --outcome fail` — log after bad runs
- During heartbeat context injection: prepend last 3 relevant reflections to agent prompt
- Hermes or the agent itself can write reflections; low-cost, high-value
- **For Cortex specifically**: after each intelligence brief, note what patterns were wrong or missed

---

## 5. Cognitive Architectures — SOAR & ACT-R

### What It Is
Pre-neural symbolic AI architectures that modeled human cognition. SOAR (Laird, 2012) and ACT-R (Anderson, 2004) remain influential for agent memory design despite being rule-based.

### SOAR Memory Subsystems
- **Working memory**: Current problem state (context window analog)
- **Long-term declarative**: Facts about the world (semantic memory analog)
- **Long-term procedural**: Production rules, if-then patterns (tool/skill library analog)
- **Episodic memory**: Record of past experiences with temporal indexing

### ACT-R Memory Subsystems
- **Declarative memory**: Facts with activation levels (decay over time, boost on retrieval)
- **Procedural memory**: Production rules firing in serial
- **Activation formula**: `A(i) = B(i) + Σ W(j)×S(j,i) + ε`
  - B(i) = base-level activation (recency + frequency)
  - W = attention weights, S = associative strengths

### What's Proven Effective
- ACT-R activation formula maps directly to memory scoring: recent + frequently-accessed memories surface first
- SOAR's episodic memory with temporal indexing enables "what was I doing at time T?" queries
- Separation of declarative vs. procedural memory prevents mixing facts with behaviors

### Implementation Recommendations for brain.db
**ACT-R activation → brain.db scoring:**
```sql
-- Simplified ACT-R base-level activation
-- B(i) = ln(Σ t_j^(-d)) where t_j = time since j-th retrieval, d = decay (≈0.5)
-- Approximation for SQLite:
SELECT *,
  (0.5 * ln(retrieval_count + 1)) - (0.1 * (julianday('now') - julianday(last_retrieved_at)))
  AS activation_score
FROM memories
ORDER BY activation_score DESC
```
- `retrieval_count` and `last_retrieved_at` already tracked in brain.db schema — this is directly applicable
- Add `episodic_index` table: `agent_id, run_id, timestamp, event_summary` for temporal queries
- Procedural memory → `brainctl skills` or agent instruction files (already exists)

---

## 6. Embedding Strategies — Models, Chunking, Similarity at Scale

### What It Is
The mechanics of converting text to vectors, organizing those vectors, and searching them efficiently.

### Embedding Models (state of 2025)
| Model | Dims | Context | Best For |
|-------|------|---------|----------|
| text-embedding-3-small | 1536 | 8K | Cost/speed balance |
| text-embedding-3-large | 3072 | 8K | Max quality |
| BGE-m3 | 1024 | 8K | Multilingual + hybrid |
| nomic-embed-text-v1.5 | 768 | 8K | Local inference |
| mxbai-embed-large | 1024 | 512 | Local, very fast |

**For brain.db**: `nomic-embed-text-v1.5` or `mxbai-embed-large` for local/offline; `text-embedding-3-small` for cloud-backed. sqlite-vec works well with 768-1024 dims.

### Chunking Strategies
- **Fixed-size**: Simple, predictable. 256-512 tokens with 20-50 token overlap. Baseline.
- **Sentence-based**: Split on `.!?`. Better semantic coherence than fixed. Use NLTK/spaCy.
- **Semantic chunking**: Embed each sentence, split when cosine distance drops below threshold. Best quality, most compute.
- **Recursive character splitting**: LangChain default — split on `\n\n`, then `\n`, then ` `. Good balance.
- **Proposition chunking**: Decompose into atomic facts. Very high precision, very high cost.

**For brain.db**: Short-form memories (agent logs, facts) don't need chunking — store as atomic units. Long-form documents (research, plans) → recursive splitting at 512 tokens with 64-token overlap.

### Similarity Metrics
- **Cosine similarity**: Standard. Invariant to magnitude. Use for normalized embeddings.
- **Dot product**: Faster than cosine if vectors are already L2-normalized (identical result).
- **L2 distance**: Good for dense retrievers trained with L2 objective.
- sqlite-vec supports all three — use cosine/dot for text embeddings.

### Scale Considerations for 178 Agents
- At 178 agents with 1000 memories each = 178,000 vectors
- sqlite-vec with 768-dim float32 = ~548MB — fine for SQLite
- HNSW index in sqlite-vec handles 100K+ vectors with <10ms query time
- Full re-embed cost: ~$0.02 at text-embedding-3-small pricing for 178K entries

---

## 7. Memory-Augmented Transformers — External Memory Banks

### What It Is
Architectures where the transformer directly attends over an external memory bank (not just context). Key examples: REALM, RAG-Token, Atlas, Retro (Borgeaud et al., 2022).

### How It Works
**Retro (Retrieval Enhanced Transformers)**:
- At every layer, retrieves from a 2-trillion-token text corpus
- Uses "chunked cross-attention" — each input chunk attends to its retrieved neighbors
- Training includes retrieval; inference time retrieval is from same corpus

**MEMIT (Mass-Editing Memory In Transformers)**:
- Edits factual knowledge stored in MLP layers of the transformer
- Enables thousands of knowledge updates without fine-tuning
- Knowledge stored in specific layer weight matrices

### What's Proven Effective
- Retro achieves GPT-3 perplexity with 25× fewer parameters
- MEMIT can update 10,000+ facts simultaneously with <5% performance degradation
- External memory allows knowledge to be updated without model retraining

### Limitations
- Retro requires retrieval at train time — not applicable to frozen LLM agents
- MEMIT requires white-box model access — not usable with API-based models
- Architectural memory (Retro-style) vs. external tool memory (MemGPT-style) are different paradigms

### Implementation Recommendations for brain.db
- Retro/MEMIT patterns are not directly applicable to API-based Claude agents
- The relevant principle: **retrieval should be architecturally integrated, not bolted on**
- For brain.db: every agent heartbeat should automatically query for relevant memories (not optional), equivalent to "Retro always retrieves"
- Implement `brainctl context-inject --agent <id>` that runs at heartbeat start and prepends top-5 memories

---

## 8. Multi-Agent Shared Memory — CrewAI, AutoGen, LangGraph

### What It Is
Coordination patterns for memory sharing across agent networks.

### CrewAI Memory Types (2024-2025)
- **Short-term**: In-run shared context (dict passed between agents)
- **Long-term**: SQLite-backed persistent memory shared by all crew members
- **Entity memory**: Named entity extraction and tracking (person/place/org tracking across runs)
- **Contextual memory**: Combines all above for retrieval

### AutoGen Memory Patterns
- **Conversation buffer**: Full message history shared (expensive, context-window limited)
- **Transform messages**: Custom preprocessing to filter/compress before sharing
- **Teachability addon**: `TextAnalyzerAgent` extracts and stores facts from conversation
- `MemoStore` class: SQLite-backed, supports semantic retrieval via embeddings

### LangGraph State Management
- **State graph**: Typed shared state dict passed between graph nodes
- **Checkpointing**: SQLite/Postgres checkpointer saves state at each node execution
- **Memory store**: Separate `InMemoryStore`/`PostgresStore` for cross-thread persistence
- **Semantic routing**: Conditional edges based on memory retrieval results

### What's Proven Effective
- Shared entity memory prevents agents from asking for the same info repeatedly
- LangGraph's typed state with checkpointing is the most robust pattern for complex multi-agent flows
- AutoGen's `MemoStore` with semantic retrieval outperforms pure conversation-replay for long-running agents

### Limitations
- Naive shared memory creates write conflicts (no locking)
- Read-heavy workloads fine; write-heavy workloads need queuing
- Entity extraction quality depends on the LLM doing extraction

### Implementation Recommendations for brain.db
- **Write serialization**: brain.db uses SQLite WAL mode already. Good. Ensure agents don't simultaneously write to same memory slot — brainctl should use `INSERT OR REPLACE` with timestamps.
- **Entity tracking**: Add `entities` table: `name`, `type` (person/project/concept), `description`, `last_seen_run_id`. Agents write on encounter, query before introducing entities.
- **Shared working memory**: For coordinated tasks (like Cortex+Recall research), add a `shared_scratch` table keyed by `goal_id` — both agents can read/write intermediate results.
- **No locking needed**: SQLite WAL handles concurrent readers + single writer. Agents should use `BEGIN IMMEDIATE` for writes.
- **LangGraph lesson**: Typed, schema-enforced state prevents corruption. brain.db schema should be strict with NOT NULL and CHECK constraints.

---

## Summary: Priority Implementation Recommendations

Ranked by impact/effort ratio for the brain.db / 178-agent system:

| Priority | Pattern | Impact | Effort |
|----------|---------|--------|--------|
| 1 | Hybrid BM25+vector with RRF | High — better retrieval accuracy | Low — both engines already present |
| 2 | ACT-R activation scoring | High — surfaces relevant memories | Low — fields already in schema |
| 3 | Reflexion memory category | High — prevents repeated mistakes | Low — just a new memory category + write pattern |
| 4 | Contextual chunk enrichment | Medium — better semantic indexing | Medium — requires enrichment at write time |
| 5 | Entity tracking table | Medium — reduces redundant queries | Medium — new table + extraction logic |
| 6 | Lost-in-middle context ordering | Medium — better context utilization | Low — sort order change in retrieval |
| 7 | Shared scratch space (per-goal) | Medium — better multi-agent coordination | Low — new table, existing WAL handles concurrency |
| 8 | Local cross-encoder reranker | High quality gain | High — requires local model deployment |

---

*Delivered to ~/agentmemory/research/03_ai_memory_systems.md*
