"""
Context Compression — Fit More Useful Info Into Limited Context Windows
=======================================================================
Concept: LLM context windows are finite. When injecting memories into an agent's
context, we need to maximize information density. This module provides algorithms
to select, rank, and compress memories to fit within a token budget.

Algorithms:
  1. Token-aware selection — pick highest-salience memories that fit in N tokens
  2. Redundancy pruning — remove memories whose information is already covered
  3. Hierarchical summarization — summarize clusters, inject summaries not originals
  4. Temporal compression — recent events as full text, older as 1-line summaries
  5. Scope filtering — only inject memories relevant to current agent/task scope
"""

import sqlite3
import json
import re
from datetime import datetime, timezone

DB_PATH = "/Users/r4vager/agentmemory/db/brain.db"

# Approximate token counts (rough: 1 token ≈ 4 chars for English)
CHARS_PER_TOKEN = 4

DEFAULT_TOKEN_BUDGET = 2000   # tokens allocated to memory injection
MAX_MEMORY_TOKENS   = 150     # max tokens per individual memory


def estimate_tokens(text: str) -> int:
    """Fast character-based token estimate."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


# ── 1. Token-Aware Selection ──────────────────────────────────────────────────

def select_within_budget(
    memories: list[dict],
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    salience_key: str = "salience",
) -> list[dict]:
    """
    Greedy selection of highest-salience memories that fit in token budget.
    Each memory is pre-truncated to MAX_MEMORY_TOKENS.
    memories must be sorted by salience descending.
    """
    selected = []
    used_tokens = 0

    for m in memories:
        content = truncate_to_tokens(m.get("content", ""), MAX_MEMORY_TOKENS)
        cost = estimate_tokens(content)

        if used_tokens + cost > token_budget:
            # Try to fit a shorter version
            remaining = token_budget - used_tokens
            if remaining < 20:
                break
            content = truncate_to_tokens(content, remaining)
            cost = estimate_tokens(content)

        m = dict(m)
        m["content"] = content
        m["token_cost"] = cost
        selected.append(m)
        used_tokens += cost

    return selected


# ── 2. Redundancy Pruning ─────────────────────────────────────────────────────

def prune_redundant(
    memories: list[dict],
    overlap_threshold: float = 0.6,
) -> list[dict]:
    """
    Remove memories whose content is largely covered by a higher-salience memory.
    Uses Jaccard similarity on token sets as a fast overlap proxy.
    """
    def token_set(text: str) -> set:
        tokens = re.findall(r'\b\w{4,}\b', text.lower())
        return set(tokens)

    kept = []
    covered_tokens: set = set()

    for m in memories:
        toks = token_set(m.get("content", ""))
        if not toks:
            kept.append(m)
            continue

        overlap = len(toks & covered_tokens) / len(toks)
        if overlap < overlap_threshold:
            kept.append(m)
            covered_tokens |= toks

    return kept


# ── 3. Hierarchical Summarization ────────────────────────────────────────────

def summarize_cluster_naive(memories: list[dict]) -> str:
    """
    Naive extractive summarization: take first sentence of each memory, dedupe.
    Replace with LLM call in production.
    """
    sentences = []
    seen = set()
    for m in memories:
        first_sent = m["content"].split(".")[0].strip()
        if first_sent and first_sent not in seen:
            seen.add(first_sent)
            sentences.append(first_sent)
    return ". ".join(sentences[:5]) + "."


def compress_to_cluster_summaries(
    memories: list[dict],
    cluster_size: int = 5,
    summarizer_fn=None,
) -> list[dict]:
    """
    Group memories into clusters and replace each cluster with a summary.
    Reduces N memories to N/cluster_size summary items.
    """
    if summarizer_fn is None:
        summarizer_fn = summarize_cluster_naive

    # Group by category as simple clusters
    by_category: dict[str, list] = {}
    for m in memories:
        by_category.setdefault(m.get("category", "unknown"), []).append(m)

    summaries = []
    for category, mems in by_category.items():
        for i in range(0, len(mems), cluster_size):
            chunk = mems[i:i + cluster_size]
            if len(chunk) == 1:
                summaries.append(chunk[0])
            else:
                summary_text = summarizer_fn(chunk)
                avg_sal = sum(m.get("salience", 0.5) for m in chunk) / len(chunk)
                summaries.append({
                    "id": None,
                    "content": summary_text,
                    "category": category,
                    "salience": round(avg_sal, 3),
                    "temporal_class": "medium",
                    "source_count": len(chunk),
                    "is_summary": True,
                })

    return summaries


# ── 4. Temporal Compression ──────────────────────────────────────────────────

def days_since(ts_str: str) -> float:
    if not ts_str:
        return 999.0
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def apply_temporal_compression(
    memories: list[dict],
    recent_days: float = 3.0,
    old_max_tokens: int = 40,
) -> list[dict]:
    """
    Recent memories: keep full text.
    Older memories: compress to first sentence (one-liner).
    """
    result = []
    for m in memories:
        age = days_since(m.get("last_recalled_at") or m.get("created_at", ""))
        if age <= recent_days:
            result.append(m)
        else:
            m = dict(m)
            m["content"] = truncate_to_tokens(m["content"].split(".")[0], old_max_tokens)
            m["temporally_compressed"] = True
            result.append(m)
    return result


# ── 5. Full Compression Pipeline ─────────────────────────────────────────────

def compress_memories_for_context(
    memories: list[dict],
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    use_cluster_summaries: bool = False,
    summarizer_fn=None,
    recent_days: float = 3.0,
) -> dict:
    """
    Full pipeline:
      1. Temporal compression (old memories → one-liners)
      2. Redundancy pruning
      3. Optional: cluster summarization
      4. Token-budget selection

    Returns: {"memories": [...], "token_estimate": int, "dropped": int}
    """
    original_count = len(memories)

    # Step 1: Temporal compression
    memories = apply_temporal_compression(memories, recent_days=recent_days)

    # Step 2: Redundancy pruning
    memories = prune_redundant(memories, overlap_threshold=0.55)

    # Step 3: Cluster summarization (optional, for very large sets)
    if use_cluster_summaries and len(memories) > 20:
        memories = compress_to_cluster_summaries(memories, summarizer_fn=summarizer_fn)

    # Step 4: Token-budget selection (memories already sorted by salience)
    selected = select_within_budget(memories, token_budget=token_budget)

    total_tokens = sum(m.get("token_cost", estimate_tokens(m["content"])) for m in selected)

    return {
        "memories": selected,
        "token_estimate": total_tokens,
        "selected": len(selected),
        "dropped": original_count - len(selected),
        "budget": token_budget,
    }


# ── Context Block Renderer ────────────────────────────────────────────────────

def render_context_block(compressed_result: dict) -> str:
    """
    Render the compressed memory set into a markdown context block
    suitable for injection into an agent's system prompt.
    """
    lines = ["## Relevant Memory Context\n"]
    for m in compressed_result["memories"]:
        prefix = ""
        if m.get("is_summary"):
            prefix = f"[Summary of {m['source_count']} {m['category']} memories] "
        elif m.get("temporally_compressed"):
            prefix = "[~] "  # tilde = compressed
        cat = m.get("category", "?")
        sal = m.get("salience", 0)
        lines.append(f"- **[{cat}|{sal:.2f}]** {prefix}{m['content']}")

    lines.append(f"\n_Context: {compressed_result['selected']} memories, "
                 f"~{compressed_result['token_estimate']} tokens, "
                 f"{compressed_result['dropped']} dropped_")
    return "\n".join(lines)


if __name__ == "__main__":
    # Demo with dummy memories
    sample = [
        {"id": 1, "content": "The consolidation cycle runs every 24 hours at midnight UTC.", "category": "environment", "confidence": 0.9, "salience": 0.85, "recalled_count": 5, "last_recalled_at": None, "created_at": "2026-03-27T00:00:00Z"},
        {"id": 2, "content": "Brain.db uses FTS5 and sqlite-vec for search.", "category": "environment", "confidence": 0.8, "salience": 0.75, "recalled_count": 3, "last_recalled_at": None, "created_at": "2026-03-25T00:00:00Z"},
        {"id": 3, "content": "Agent Prune handles memory hygiene and retirement.", "category": "identity", "confidence": 0.95, "salience": 0.70, "recalled_count": 8, "last_recalled_at": None, "created_at": "2026-03-20T00:00:00Z"},
    ]
    result = compress_memories_for_context(sample, token_budget=500)
    print(render_context_block(result))
