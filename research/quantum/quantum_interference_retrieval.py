"""
Quantum-Inspired Interference Retrieval for brain.db
=====================================================
COS-380 / COS-370 deliverable: Phase (Quantum Interference Engineer)
2026-03-28

Replaces the classical additive salience scorer with an amplitude-based
scorer that accounts for pairwise constructive/destructive interference
between candidate memories via knowledge_edges.

Classical scorer (current):
    score(m) = 0.45·sim + 0.25·recency + 0.20·confidence + 0.10·importance

Quantum scorer (this module):
    α_i  = cosine_sim(query, m_i)              # probability amplitude
    P_i  = α_i² + Σ_j I(m_i, m_j)             # interference-adjusted probability
    I_ij = φ(relation_type) · w_ij · α_i · α_j  # pairwise interference term

Where φ maps relation_type to {-1, -0.5, +0.3, +1}:
    semantic_similar, supports, co_referenced, topical_tag → +1.0 (constructive)
    contradicts                                             → -1.0 (destructive)
    supersedes                                             → -0.5 (partial destructive)
    causes, derived_from, causal_chain_member              → +0.3 (weak constructive)
    topical_scope, topical_project                         → +0.2 (weak constructive)

Usage (standalone):
    from quantum_interference_retrieval import QuantumRetriever
    retriever = QuantumRetriever(db_path="/Users/r4vager/agentmemory/db/brain.db")
    results = retriever.search("consolidation cycle agents", top_k=10)
    for r in results:
        print(r)

Usage (with embeddings for max accuracy):
    results = retriever.search_with_embedding(query_embedding, top_k=10)
"""

import sqlite3
import math
import json
import time
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

DB_PATH = Path.home() / "agentmemory" / "db" / "brain.db"

# --- Interference phase map ---
# Maps knowledge_edge relation_type to interference sign/weight multiplier.
# Constructive (+): amplitudes add, boosting co-retrieved memories.
# Destructive (-): amplitudes cancel, suppressing contradicted or superseded memories.
INTERFERENCE_PHASE = {
    "semantic_similar":      +1.0,
    "supports":              +1.0,
    "co_referenced":         +1.0,
    "topical_tag":           +1.0,
    "topical_scope":         +0.2,
    "topical_project":       +0.2,
    "causes":                +0.3,
    "derived_from":          +0.3,
    "causal_chain_member":   +0.3,
    "contradicts":           -1.0,
    "supersedes":            -0.5,
}
DEFAULT_PHASE = +0.1  # unknown relation types: weak constructive


@dataclass
class MemoryCandidate:
    id: int
    content: str
    category: str
    scope: str
    confidence: float
    recalled_count: int
    temporal_class: str
    created_at: str
    # Retrieval scores
    fts_score: float = 0.0         # BM25 from FTS5
    cosine_sim: float = 0.0        # vector similarity (if available)
    amplitude: float = 0.0         # α_i = primary retrieval amplitude
    classical_salience: float = 0.0
    interference_correction: float = 0.0
    quantum_probability: float = 0.0
    interference_details: list = field(default_factory=list)


@dataclass
class SearchResult:
    rank: int
    memory_id: int
    content: str
    category: str
    scope: str
    quantum_probability: float
    amplitude: float
    interference_correction: float
    classical_salience: float
    confidence: float
    recalled_count: int
    temporal_class: str
    # Diagnostics
    constructive_boosts: int = 0
    destructive_suppressions: int = 0

    def __str__(self):
        direction = "↑" if self.interference_correction > 0 else ("↓" if self.interference_correction < 0 else "→")
        return (
            f"[{self.rank}] mem#{self.memory_id} P={self.quantum_probability:.4f} "
            f"(α²={self.amplitude**2:.4f} {direction}{abs(self.interference_correction):.4f}) "
            f"conf={self.confidence:.2f} rc={self.recalled_count} [{self.temporal_class}]\n"
            f"    {self.content[:120]}..."
        )


class QuantumRetriever:
    """
    Quantum-inspired retrieval engine for brain.db.

    The interference correction is computed over the top-N candidates
    returned by the classical FTS5 pipeline. Only candidate pairs with
    a knowledge_edge between them contribute to the interference term.

    At brain.db scale (150 active memories, 4718 edges):
    - Candidate pool: top-100 by FTS5
    - Interference matrix: O(k²) = O(10,000) operations for k=100
    - Edge lookup: single JOIN query covering all candidate pairs
    - Total overhead vs classical: ~5-15ms (negligible)
    """

    def __init__(self, db_path: Path = DB_PATH, interference_candidates: int = 50):
        self.db_path = str(db_path)
        self.interference_candidates = interference_candidates

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _recency_score(self, created_at: str) -> float:
        """Exponential recency decay: score = exp(-k * days_old), k=0.1."""
        import datetime
        try:
            ts = created_at.replace("Z", "+00:00")
            created = datetime.datetime.fromisoformat(ts)
            now = datetime.datetime.now(datetime.timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            days_old = (now - created).total_seconds() / 86400
            return math.exp(-0.1 * days_old)
        except Exception:
            return 0.5

    def _importance_score(self, recalled_count: int) -> float:
        """Log-normalized recall count."""
        return math.log1p(recalled_count) / math.log1p(1000)

    def _classical_salience(self, fts_score: float, candidate: MemoryCandidate) -> float:
        """Current brain.db salience formula."""
        sim = min(1.0, max(0.0, fts_score / 10.0))  # normalize BM25
        recency = self._recency_score(candidate.created_at)
        confidence = candidate.confidence
        importance = self._importance_score(candidate.recalled_count)
        return (0.45 * sim + 0.25 * recency + 0.20 * confidence + 0.10 * importance)

    def _build_fts5_query(self, query: str) -> str:
        """
        Convert natural language query to FTS5 OR syntax.
        FTS5 MATCH uses implicit AND for multi-word queries; we want OR for candidate retrieval
        to maximize recall in Phase 1 (re-ranking handles precision).
        """
        terms = [t.strip() for t in query.split() if len(t.strip()) > 2]
        if not terms:
            return query
        # FTS5 OR: any term matches
        return " OR ".join(terms)

    def _fetch_fts_candidates(self, conn: sqlite3.Connection, query: str, limit: int) -> list[MemoryCandidate]:
        """Retrieve top candidates by FTS5 BM25 scoring."""
        fts_query = self._build_fts5_query(query)
        sql = """
        SELECT
            m.id, m.content, m.category, m.scope, m.confidence,
            m.recalled_count, m.temporal_class, m.created_at,
            bm25(memories_fts) AS fts_score
        FROM memories m
        JOIN memories_fts mf ON m.id = mf.rowid
        WHERE memories_fts MATCH ?
          AND m.retired_at IS NULL
        ORDER BY fts_score
        LIMIT ?
        """
        # FTS5 BM25 returns negative values (lower = better match)
        try:
            rows = conn.execute(sql, (fts_query, limit)).fetchall()
        except sqlite3.OperationalError:
            # Fallback: LIKE search if FTS5 unavailable
            like_q = f"%{query.split()[0]}%"
            fallback_sql = """
            SELECT id, content, category, scope, confidence, recalled_count,
                   temporal_class, created_at, -1.0 AS fts_score
            FROM memories WHERE content LIKE ? AND retired_at IS NULL LIMIT ?
            """
            rows = conn.execute(fallback_sql, (like_q, limit)).fetchall()

        candidates = []
        for row in rows:
            c = MemoryCandidate(
                id=row["id"],
                content=row["content"],
                category=row["category"] or "",
                scope=row["scope"] or "",
                confidence=row["confidence"] or 0.5,
                recalled_count=row["recalled_count"] or 0,
                temporal_class=row["temporal_class"] or "medium",
                created_at=row["created_at"] or "",
                fts_score=abs(float(row["fts_score"])),  # BM25 magnitude
            )
            candidates.append(c)
        return candidates

    def _compute_amplitudes(self, candidates: list[MemoryCandidate]) -> None:
        """
        Assign probability amplitude α_i to each candidate.

        In the full quantum model, α_i = ⟨q|m_i⟩ (inner product with query embedding).
        Without embeddings, we use the normalized FTS5 BM25 score as a proxy.

        With embeddings: override amplitude with cosine_sim before calling this.
        """
        if not candidates:
            return
        max_fts = max(c.fts_score for c in candidates) or 1.0
        for c in candidates:
            if c.cosine_sim > 0:
                # Prefer embedding-based amplitude when available
                c.amplitude = c.cosine_sim
            else:
                # Normalize BM25 score to [0, 1] as amplitude proxy
                c.amplitude = c.fts_score / max_fts
            # Blend with confidence (confidence modulates coherence of the memory state)
            c.amplitude = c.amplitude * (0.7 + 0.3 * c.confidence)

    def _fetch_interference_edges(
        self,
        conn: sqlite3.Connection,
        candidate_ids: list[int],
    ) -> dict[tuple[int, int], tuple[str, float]]:
        """
        Fetch all knowledge edges between the candidate set.
        Returns {(source_id, target_id): (relation_type, weight)}.
        """
        if len(candidate_ids) < 2:
            return {}

        placeholders = ",".join("?" * len(candidate_ids))
        sql = f"""
        SELECT source_id, target_id, relation_type, weight
        FROM knowledge_edges
        WHERE source_table = 'memories'
          AND target_table = 'memories'
          AND source_id IN ({placeholders})
          AND target_id IN ({placeholders})
        """
        rows = conn.execute(sql, candidate_ids + candidate_ids).fetchall()

        edges = {}
        for row in rows:
            src, tgt = int(row["source_id"]), int(row["target_id"])
            edges[(src, tgt)] = (row["relation_type"], float(row["weight"]))
            # Edges are directional in brain.db; treat as bidirectional for interference
            if (tgt, src) not in edges:
                edges[(tgt, src)] = (row["relation_type"], float(row["weight"]))
        return edges

    def _apply_interference(
        self,
        candidates: list[MemoryCandidate],
        edges: dict[tuple[int, int], tuple[str, float]],
    ) -> None:
        """
        Compute pairwise interference corrections and apply to each candidate.

        Interference term:
            I(m_i, m_j) = φ(relation_type) · edge_weight · α_i · α_j

        Final quantum probability:
            P_i = clamp(α_i² + Σ_j I(m_i, m_j), 0, ∞)

        The sum Σ_j is over all candidates j≠i with a knowledge edge to i.
        """
        id_to_candidate = {c.id: c for c in candidates}

        for c in candidates:
            correction = 0.0
            details = []

            for other in candidates:
                if other.id == c.id:
                    continue
                edge = edges.get((c.id, other.id))
                if edge is None:
                    continue

                relation_type, edge_weight = edge
                phi = INTERFERENCE_PHASE.get(relation_type, DEFAULT_PHASE)

                # Interference term: φ · w · α_i · α_j
                interference = phi * edge_weight * c.amplitude * other.amplitude
                correction += interference

                details.append({
                    "other_id": other.id,
                    "relation": relation_type,
                    "phi": phi,
                    "edge_weight": edge_weight,
                    "term": round(interference, 6),
                })

            c.interference_correction = correction
            c.interference_details = details
            # P_i = α_i² + Σ_j I_ij  (probability, not amplitude)
            c.quantum_probability = max(0.0, c.amplitude ** 2 + correction)

    def search(
        self,
        query: str,
        top_k: int = 10,
        debug: bool = False,
    ) -> list[SearchResult]:
        """
        Main search entry point. Uses FTS5 for candidate retrieval,
        then applies quantum interference re-ranking.

        Args:
            query: Natural language search query
            top_k: Number of results to return
            debug: If True, prints interference diagnostics

        Returns:
            List of SearchResult sorted by quantum probability (descending)
        """
        conn = self._connect()
        try:
            # Step 1: Fetch FTS5 candidates
            candidates = self._fetch_fts_candidates(
                conn, query, limit=self.interference_candidates
            )
            if not candidates:
                return []

            # Step 2: Assign amplitudes
            self._compute_amplitudes(candidates)

            # Step 3: Compute classical salience (for comparison)
            for c in candidates:
                c.classical_salience = self._classical_salience(c.fts_score, c)

            # Step 4: Fetch interference edges between candidates
            candidate_ids = [c.id for c in candidates]
            edges = self._fetch_interference_edges(conn, candidate_ids)

            # Step 5: Apply quantum interference corrections
            self._apply_interference(candidates, edges)

            # Step 6: Sort by quantum probability (descending)
            candidates.sort(key=lambda c: c.quantum_probability, reverse=True)

            # Step 7: Build results
            results = []
            for rank, c in enumerate(candidates[:top_k], start=1):
                constructive = sum(
                    1 for d in c.interference_details if d["term"] > 0
                )
                destructive = sum(
                    1 for d in c.interference_details if d["term"] < 0
                )

                result = SearchResult(
                    rank=rank,
                    memory_id=c.id,
                    content=c.content,
                    category=c.category,
                    scope=c.scope,
                    quantum_probability=round(c.quantum_probability, 6),
                    amplitude=round(c.amplitude, 6),
                    interference_correction=round(c.interference_correction, 6),
                    classical_salience=round(c.classical_salience, 6),
                    confidence=c.confidence,
                    recalled_count=c.recalled_count,
                    temporal_class=c.temporal_class,
                    constructive_boosts=constructive,
                    destructive_suppressions=destructive,
                )
                results.append(result)

                if debug:
                    print(f"\n[{rank}] mem#{c.id} (P={c.quantum_probability:.4f})")
                    print(f"  amplitude={c.amplitude:.4f}  α²={c.amplitude**2:.4f}")
                    print(f"  correction={c.interference_correction:+.4f}  "
                          f"(+{constructive} constructive, -{destructive} destructive)")
                    print(f"  classical_salience={c.classical_salience:.4f}")
                    if c.interference_details:
                        print(f"  top interference terms:")
                        top_terms = sorted(
                            c.interference_details,
                            key=lambda d: abs(d["term"]),
                            reverse=True
                        )[:3]
                        for d in top_terms:
                            sign = "+" if d["term"] > 0 else ""
                            print(f"    {sign}{d['term']:.4f} via {d['relation']} "
                                  f"(w={d['edge_weight']:.2f}) from mem#{d['other_id']}")
                    print(f"  {c.content[:100]}...")

            return results

        finally:
            conn.close()

    def search_with_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        pre_filter_query: Optional[str] = None,
        debug: bool = False,
    ) -> list[SearchResult]:
        """
        Search using a pre-computed embedding vector for maximum amplitude accuracy.
        Uses cosine similarity against vec_memories as the primary amplitude source.

        Requires sqlite-vec extension. Falls back to FTS5 if unavailable.

        Args:
            query_embedding: 768-dimensional query embedding
            top_k: Number of results to return
            pre_filter_query: Optional FTS5 query to narrow candidate pool first
            debug: Verbose interference diagnostics
        """
        conn = self._connect()
        try:
            # Try sqlite-vec KNN search for candidates
            try:
                # Convert embedding to bytes (float32 array)
                import struct
                embedding_bytes = struct.pack(f"{len(query_embedding)}f", *query_embedding)

                sql = """
                SELECT
                    m.id, m.content, m.category, m.scope, m.confidence,
                    m.recalled_count, m.temporal_class, m.created_at,
                    vm.distance AS cosine_distance
                FROM vec_memories vm
                JOIN memories m ON vm.memory_id = m.id
                WHERE vm.embedding MATCH ?
                  AND k = ?
                  AND m.retired_at IS NULL
                ORDER BY vm.distance
                """
                rows = conn.execute(sql, (embedding_bytes, self.interference_candidates)).fetchall()

                candidates = []
                for row in rows:
                    c = MemoryCandidate(
                        id=row["id"],
                        content=row["content"],
                        category=row["category"] or "",
                        scope=row["scope"] or "",
                        confidence=row["confidence"] or 0.5,
                        recalled_count=row["recalled_count"] or 0,
                        temporal_class=row["temporal_class"] or "medium",
                        created_at=row["created_at"] or "",
                        fts_score=0.0,
                        # cosine_distance is L2/cosine distance; convert to similarity
                        cosine_sim=max(0.0, 1.0 - float(row["cosine_distance"])),
                    )
                    candidates.append(c)

            except sqlite3.OperationalError:
                # sqlite-vec not loaded; fall back to FTS5 with pre_filter
                if pre_filter_query:
                    return self.search(pre_filter_query, top_k=top_k, debug=debug)
                return []

            if not candidates:
                return []

            # Assign amplitudes (cosine_sim already set)
            self._compute_amplitudes(candidates)

            # Compute classical salience for comparison
            for c in candidates:
                # Use cosine_sim as sim proxy for classical formula
                recency = self._recency_score(c.created_at)
                importance = self._importance_score(c.recalled_count)
                c.classical_salience = (
                    0.45 * c.cosine_sim
                    + 0.25 * recency
                    + 0.20 * c.confidence
                    + 0.10 * importance
                )

            # Fetch interference edges and apply
            candidate_ids = [c.id for c in candidates]
            edges = self._fetch_interference_edges(conn, candidate_ids)
            self._apply_interference(candidates, edges)

            # Sort and return
            candidates.sort(key=lambda c: c.quantum_probability, reverse=True)
            results = []
            for rank, c in enumerate(candidates[:top_k], start=1):
                constructive = sum(1 for d in c.interference_details if d["term"] > 0)
                destructive = sum(1 for d in c.interference_details if d["term"] < 0)
                results.append(SearchResult(
                    rank=rank,
                    memory_id=c.id,
                    content=c.content,
                    category=c.category,
                    scope=c.scope,
                    quantum_probability=round(c.quantum_probability, 6),
                    amplitude=round(c.amplitude, 6),
                    interference_correction=round(c.interference_correction, 6),
                    classical_salience=round(c.classical_salience, 6),
                    confidence=c.confidence,
                    recalled_count=c.recalled_count,
                    temporal_class=c.temporal_class,
                    constructive_boosts=constructive,
                    destructive_suppressions=destructive,
                ))
                if debug:
                    print(f"\n[{rank}] mem#{c.id} P={c.quantum_probability:.4f} "
                          f"(cosine={c.cosine_sim:.4f} α²={c.amplitude**2:.4f} "
                          f"Δ={c.interference_correction:+.4f})")
                    print(f"  {c.content[:120]}...")
            return results

        finally:
            conn.close()

    def benchmark(self, query: str, top_k: int = 10) -> dict:
        """
        Compare quantum vs classical ranking for a given query.
        Returns ranking comparison showing how interference re-ordered results.
        """
        conn = self._connect()
        try:
            candidates = self._fetch_fts_candidates(conn, query, limit=self.interference_candidates)
            if not candidates:
                return {"error": "no results"}

            self._compute_amplitudes(candidates)
            for c in candidates:
                c.classical_salience = self._classical_salience(c.fts_score, c)

            candidate_ids = [c.id for c in candidates]
            edges = self._fetch_interference_edges(conn, candidate_ids)
            self._apply_interference(candidates, edges)

            # Classical ranking
            classical_order = sorted(candidates, key=lambda c: c.classical_salience, reverse=True)
            # Quantum ranking
            quantum_order = sorted(candidates, key=lambda c: c.quantum_probability, reverse=True)

            classical_top = [c.id for c in classical_order[:top_k]]
            quantum_top = [c.id for c in quantum_order[:top_k]]

            # Rank changes
            classical_rank = {c.id: i+1 for i, c in enumerate(classical_order[:top_k])}
            rank_changes = []
            for i, c in enumerate(quantum_order[:top_k]):
                classical_r = classical_rank.get(c.id, ">10")
                quantum_r = i + 1
                if classical_r != quantum_r:
                    rank_changes.append({
                        "memory_id": c.id,
                        "classical_rank": classical_r,
                        "quantum_rank": quantum_r,
                        "interference": round(c.interference_correction, 6),
                        "constructive": sum(1 for d in c.interference_details if d["term"] > 0),
                        "destructive": sum(1 for d in c.interference_details if d["term"] < 0),
                        "content_preview": c.content[:80],
                    })

            # Overall interference stats
            total_constructive = sum(
                sum(1 for d in c.interference_details if d["term"] > 0)
                for c in candidates
            )
            total_destructive = sum(
                sum(1 for d in c.interference_details if d["term"] < 0)
                for c in candidates
            )

            return {
                "query": query,
                "candidates_evaluated": len(candidates),
                "edges_found": len(edges),
                "rank_changes": len(rank_changes),
                "rank_change_details": rank_changes,
                "total_constructive_terms": total_constructive,
                "total_destructive_terms": total_destructive,
                "classical_top_k": classical_top,
                "quantum_top_k": quantum_top,
            }

        finally:
            conn.close()


def run_demo():
    """
    Run a quick demo against live brain.db showing interference effects.
    """
    retriever = QuantumRetriever()
    queries = [
        "consolidation cycle memory decay",
        "agent checkout task assignment",
        "knowledge graph edges retrieval",
    ]

    print("=" * 70)
    print("QUANTUM INTERFERENCE RETRIEVAL — brain.db Demo")
    print(f"DB: {DB_PATH}")
    print("=" * 70)

    for query in queries:
        print(f"\nQuery: '{query}'")
        print("-" * 60)

        bench = retriever.benchmark(query, top_k=10)
        print(f"  Candidates evaluated: {bench['candidates_evaluated']}")
        print(f"  Interference edges found: {bench['edges_found']}")
        print(f"  Rank changes vs classical: {bench['rank_changes']}")
        print(f"  Constructive terms: {bench['total_constructive_terms']}")
        print(f"  Destructive terms: {bench['total_destructive_terms']}")

        if bench["rank_changes"] > 0:
            print("\n  Notable rank changes:")
            for rc in bench["rank_change_details"][:3]:
                direction = "↑" if rc["quantum_rank"] < rc["classical_rank"] else "↓"
                print(f"    {direction} mem#{rc['memory_id']}: "
                      f"classical #{rc['classical_rank']} → quantum #{rc['quantum_rank']} "
                      f"(Δ={rc['interference']:+.4f}, "
                      f"+{rc['constructive']}/-{rc['destructive']})")
                print(f"      {rc['content_preview']}...")


if __name__ == "__main__":
    run_demo()
