"""CrewAI integration for brainctl.

Provides RAGStorage backed by brain.db for CrewAI's memory system.
brainctl handles both FTS5 search and optional vector search — no separate
vector DB required.

Install: pip install brainctl crewai

Usage:

    from crewai import Crew, Agent, Task
    from agentmemory.integrations.crewai import BrainctlStorage

    crew = Crew(
        agents=[agent1, agent2],
        tasks=[task1, task2],
        memory=True,
        short_term_memory=ShortTermMemory(storage=BrainctlStorage("short-term")),
        long_term_memory=LongTermMemory(storage=BrainctlStorage("long-term")),
        entity_memory=EntityMemory(storage=BrainctlStorage("entity")),
    )

Or with the unified memory interface (CrewAI 0.100+):

    from agentmemory.integrations.crewai import BrainctlStorage
    storage = BrainctlStorage("crew-memory")
    # Pass to crew or agent memory configuration
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    from crewai.memory.storage.rag_storage import RAGStorage
except ImportError as e:
    raise ImportError(
        "crewai is required for the CrewAI integration. "
        "Install it with: pip install crewai"
    ) from e

from agentmemory.brain import Brain


class BrainctlStorage(RAGStorage):
    """CrewAI RAGStorage backed by brainctl's brain.db.

    Stores CrewAI memory items as brainctl memories with metadata preserved
    in tags. Search uses FTS5 full-text search with optional vector similarity
    if sqlite-vec is available.

    Args:
        type: Memory type identifier (e.g., "short-term", "long-term", "entity").
        db_path: Path to brain.db. Defaults to ~/agentmemory/db/brain.db.
        agent_id: Agent attribution. Defaults to "crewai".
        embedder_config: CrewAI embedder configuration (ignored — brainctl handles its own embeddings).
        crew: CrewAI Crew reference (stored but not used).
    """

    def __init__(
        self,
        type: str,
        db_path: Optional[str] = None,
        agent_id: str = "crewai",
        embedder_config: Optional[Dict[str, Any]] = None,
        crew: Any = None,
    ) -> None:
        self.type = type
        self.brain = Brain(db_path=db_path, agent_id=agent_id)
        self._crew = crew

    def _initialize_app(self) -> None:
        """Initialize storage backend. Brain auto-initializes, so this is a no-op."""
        pass

    def _category_for_type(self) -> str:
        """Map CrewAI memory type to brainctl category."""
        mapping = {
            "short-term": "project",
            "long-term": "lesson",
            "entity": "integration",
            "crew-memory": "project",
        }
        return mapping.get(self.type, "project")

    def save(self, value: Any, metadata: Optional[Dict[str, Any]] = None, agent: Optional[str] = None) -> None:
        """Store a memory item in brain.db.

        Args:
            value: The text content to store.
            metadata: Optional metadata dict (preserved in memory tags).
            agent: Optional agent name for attribution.
        """
        content = str(value) if not isinstance(value, str) else value
        if not content.strip():
            return

        category = self._category_for_type()
        tags = None
        confidence = 0.8

        if metadata:
            # Extract useful fields from CrewAI metadata
            if "categories" in metadata:
                cats = metadata["categories"]
                if isinstance(cats, list) and cats:
                    category = cats[0] if cats[0] in (
                        "identity", "user", "environment", "convention", "project",
                        "decision", "lesson", "preference", "integration"
                    ) else category
            if "importance" in metadata:
                confidence = max(0.1, min(1.0, float(metadata["importance"])))
            # Store full metadata as tags for round-tripping
            tags = json.dumps({"crewai_type": self.type, "crewai_meta": metadata})

        self.brain.remember(content, category=category, tags=tags, confidence=confidence)

    def search(
        self,
        query: str,
        limit: int = 3,
        filter: Optional[Dict[str, Any]] = None,
        score_threshold: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Search brain.db for relevant memories.

        Returns list of dicts with keys: id, metadata, context, score.
        Uses FTS5 search with optional vector search fallback.
        """
        # Try vector search first (better semantic matching), fall back to FTS5
        results = self.brain.vsearch(query, limit=limit)
        if not results:
            results = self.brain.search(query, limit=limit)

        items = []
        for r in results:
            # Normalize score: FTS5 doesn't have distance, vec has distance (lower=better)
            if "distance" in r:
                score = max(0.0, 1.0 - r["distance"])
            else:
                score = r.get("confidence", 0.5)

            if score < score_threshold:
                continue

            meta = {
                "id": r.get("id"),
                "category": r.get("category"),
                "confidence": r.get("confidence"),
                "created_at": r.get("created_at"),
                "crewai_type": self.type,
            }
            items.append({
                "id": str(r.get("id", "")),
                "metadata": meta,
                "context": r.get("content", ""),
                "score": round(score, 4),
            })
        return items

    def reset(self) -> None:
        """Clear all memories stored by this storage instance.

        Only retires memories tagged with this CrewAI memory type to avoid
        destroying unrelated brainctl data.
        """
        db = self.brain._db()
        from agentmemory.brain import _now_ts
        now = _now_ts()
        # Only retire memories that have our crewai_type tag
        db.execute(
            "UPDATE memories SET retired_at = ? "
            "WHERE agent_id = ? AND retired_at IS NULL "
            "AND tags LIKE ?",
            (now, self.brain.agent_id, f'%"crewai_type": "{self.type}"%'),
        )
        db.commit()
        db.close()
