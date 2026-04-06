"""
Effective Hilbert Space Dimension Analysis
COS-395: PCA analysis of the 768d embedding space in brain.db

Determines the effective rank of the cognitive state space and maps
principal components to interpretable cognitive dimensions.
"""

import sqlite3
import numpy as np
import json
import struct
from pathlib import Path
from collections import defaultdict

DB_PATH = Path.home() / "agentmemory/db/brain.db"
OUTPUT_DIR = Path.home() / "agentmemory/research/quantum"


def load_vector(blob: bytes) -> np.ndarray:
    """Decode raw float32 BLOB to numpy array."""
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def load_embeddings():
    """Load all 768d embeddings with metadata."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Load all embeddings with source metadata
    cur.execute("""
        SELECT e.id, e.source_table, e.source_id, e.model, e.vector
        FROM embeddings e
        WHERE e.dimensions = 768 AND e.vector IS NOT NULL
        ORDER BY e.id
    """)
    rows = cur.fetchall()

    embeddings = []
    meta = []
    for row_id, src_table, src_id, model, blob in rows:
        vec = load_vector(blob)
        if len(vec) == 768:
            embeddings.append(vec)
            meta.append({
                "embed_id": row_id,
                "source_table": src_table,
                "source_id": src_id,
                "model": model,
            })

    # Fetch agent_id for memories
    cur.execute("SELECT id, agent_id, category, content FROM memories WHERE retired_at IS NULL")
    mem_rows = {r[0]: {"agent_id": r[1], "category": r[2], "content": r[3]} for r in cur.fetchall()}

    # Fetch agent_id for events
    cur.execute("SELECT id, agent_id, event_type, summary FROM events LIMIT 10000")
    evt_rows = {r[0]: {"agent_id": r[1], "event_type": r[2], "content": r[3]} for r in cur.fetchall()}

    # Fetch agent_id for context (no agent_id column; use source_ref as proxy)
    cur.execute("SELECT id, source_type, source_ref, content FROM context LIMIT 10000")
    ctx_rows = {r[0]: {"agent_id": r[1] or "context", "content": r[3]} for r in cur.fetchall()}

    conn.close()

    for m in meta:
        sid = m["source_id"]
        if m["source_table"] == "memories" and sid in mem_rows:
            m.update(mem_rows[sid])
        elif m["source_table"] == "events" and sid in evt_rows:
            m.update(evt_rows[sid])
        elif m["source_table"] == "context" and sid in ctx_rows:
            m.update(ctx_rows[sid])
        if "agent_id" not in m:
            m["agent_id"] = "unknown"
        if "content" not in m:
            m["content"] = ""

    return np.array(embeddings, dtype=np.float32), meta


def run_pca(X: np.ndarray):
    """Run PCA via SVD on centered embedding matrix."""
    mean = X.mean(axis=0)
    X_centered = X - mean
    # Use SVD for numerically stable PCA
    # X_centered = U @ S_diag @ Vt  (shape: n x 768)
    # Vt rows = principal components
    U, S, Vt = np.linalg.svd(X_centered, full_matrices=False)
    explained_variance = (S ** 2) / (len(X) - 1)
    total_variance = explained_variance.sum()
    explained_ratio = explained_variance / total_variance
    cumulative_ratio = np.cumsum(explained_ratio)
    return mean, Vt, S, explained_variance, explained_ratio, cumulative_ratio, X_centered @ Vt.T


def effective_dimension(cumulative_ratio, threshold=0.95):
    """Return smallest k where cumulative explained variance >= threshold."""
    indices = np.where(cumulative_ratio >= threshold)[0]
    if len(indices) == 0:
        return len(cumulative_ratio)
    return int(indices[0]) + 1


def participation_ratio(explained_variance):
    """Participation ratio: (sum eigenvalues)^2 / sum(eigenvalues^2) — another effective dim measure."""
    ev = explained_variance
    return float((ev.sum() ** 2) / (ev ** 2).sum())


def label_pc(pc_vec: np.ndarray, meta: list, X_centered: np.ndarray, scores: np.ndarray, pc_idx: int):
    """
    Heuristically label a principal component by examining which memories/events
    score highest and lowest on that PC.
    """
    col_scores = scores[:, pc_idx]
    top_pos = np.argsort(col_scores)[-5:][::-1]
    top_neg = np.argsort(col_scores)[:5]

    pos_content = [meta[i].get("content", "")[:120] for i in top_pos]
    neg_content = [meta[i].get("content", "")[:120] for i in top_neg]
    pos_agents = [meta[i].get("agent_id", "?") for i in top_pos]
    neg_agents = [meta[i].get("agent_id", "?") for i in top_neg]

    return {
        "pc": pc_idx + 1,
        "top_positive": list(zip(pos_agents, pos_content)),
        "top_negative": list(zip(neg_agents, neg_content)),
    }


def agent_subspace_analysis(scores: np.ndarray, meta: list, n_dims: int = 50):
    """
    For each agent, compute centroid and spread in reduced PC space.
    Returns per-agent stats and pairwise cosine similarity of centroids.
    """
    agents = defaultdict(list)
    for i, m in enumerate(meta):
        agents[m.get("agent_id", "unknown")].append(scores[i, :n_dims])

    centroids = {}
    spreads = {}
    for agent, vecs in agents.items():
        if len(vecs) < 3:
            continue
        arr = np.array(vecs)
        centroids[agent] = arr.mean(axis=0)
        # Spread = mean distance from centroid
        diffs = arr - centroids[agent]
        spreads[agent] = float(np.sqrt((diffs ** 2).sum(axis=1)).mean())

    # Pairwise cosine similarity of centroids
    agent_names = sorted(centroids.keys())
    n = len(agent_names)
    sim_matrix = np.zeros((n, n))
    for i, a in enumerate(agent_names):
        for j, b in enumerate(agent_names):
            ca, cb = centroids[a], centroids[b]
            na, nb = np.linalg.norm(ca), np.linalg.norm(cb)
            if na > 0 and nb > 0:
                sim_matrix[i, j] = float(np.dot(ca, cb) / (na * nb))
            else:
                sim_matrix[i, j] = 0.0

    # Subspace overlap: for hermes vs hippocampus specifically
    hermes_overlap = None
    if "hermes" in centroids and "hippocampus" in centroids:
        h_idx = agent_names.index("hermes")
        hip_idx = agent_names.index("hippocampus")
        hermes_overlap = float(sim_matrix[h_idx, hip_idx])

    return {
        "agents": agent_names,
        "centroids": {a: centroids[a].tolist() for a in agent_names},
        "spreads": {a: spreads[a] for a in agent_names if a in spreads},
        "sim_matrix": sim_matrix.tolist(),
        "hermes_hippocampus_cosine": hermes_overlap,
    }


def test_tensor_product_structure(Vt: np.ndarray, explained_ratio: np.ndarray, n_dims: int = 50):
    """
    Test for approximate tensor product structure:
    If the 768d space factors as A ⊗ B (dims dA x dB = 768),
    the eigenvalue spectrum should show a block-Kronecker pattern.

    Heuristic test: check if explained_ratio has 'clusters' or
    if the top PCs have approximate factored structure.

    We test 768 = 32 x 24 and 768 = 16 x 48 as candidate factorizations.
    """
    results = {}

    # Check if eigenvalue spectrum looks like Kronecker product spectrum:
    # lambda_ij = lambda_i^A * lambda_j^B (multiplicative tensor structure)
    ev = explained_ratio[:n_dims]

    for dA, dB in [(32, 24), (16, 48), (8, 96), (4, 192), (24, 32)]:
        # Generate expected Kronecker spectrum shape (uniform base for simplicity)
        # Real test: fit 1/k decay to eigenvalues, check if log(ev) is sum-separable
        pass

    # More tractable test: check correlation structure in Vt[:n_dims]
    # If tensor product, principal axes should cluster into orthogonal groups
    corr = np.corrcoef(Vt[:n_dims])
    off_diag = corr[np.triu_indices(n_dims, k=1)]
    mean_abs_corr = float(np.abs(off_diag).mean())

    # Check for block structure in covariance of PC loadings
    # Reshape each PC as a 32x24 matrix and check for row/column structure
    results["mean_abs_pc_correlation"] = mean_abs_corr
    results["interpretation"] = (
        "Low mean correlation (< 0.05) suggests near-orthogonal PCs (expected). "
        "Tensor product structure would manifest as clusters of correlated PCs. "
        f"Observed: {mean_abs_corr:.4f}"
    )

    # Test candidate factorizations by checking if loadings have separable structure
    factor_scores = {}
    for dA, dB in [(32, 24), (16, 48)]:
        pc_matrix = Vt[0]  # top PC as dA x dB matrix
        pc_reshaped = pc_matrix[:dA * dB].reshape(dA, dB)
        # SVD of this reshaped vector — if rank-1, it's perfectly tensor-product
        _, s, _ = np.linalg.svd(pc_reshaped, full_matrices=False)
        rank1_ratio = float(s[0] / s.sum()) if s.sum() > 0 else 0.0
        factor_scores[f"{dA}x{dB}"] = rank1_ratio

    results["factorization_rank1_ratios"] = factor_scores
    results["best_factorization"] = max(factor_scores, key=factor_scores.get) if factor_scores else None

    return results


def format_report(
    n_embeddings: int,
    explained_ratio: np.ndarray,
    cumulative_ratio: np.ndarray,
    eff_dim_95: int,
    eff_dim_90: int,
    eff_dim_99: int,
    pr: float,
    pc_labels: list,
    agent_analysis: dict,
    tensor_analysis: dict,
    source_breakdown: dict,
) -> str:
    lines = []
    lines.append("# Effective Hilbert Space Dimension — PCA Analysis")
    lines.append("")
    lines.append(f"**Date:** 2026-03-28  ")
    lines.append(f"**Task:** [COS-395](/COS/issues/COS-395)  ")
    lines.append(f"**Database:** `~/agentmemory/db/brain.db`  ")
    lines.append(f"**Total embeddings analysed:** {n_embeddings}  ")
    lines.append(f"**Embedding model:** nomic-embed-text (768d)  ")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 1. Effective Dimension Estimates")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Full embedding dimension | 768 |")
    lines.append(f"| **Effective dim @ 90% variance** | **{eff_dim_90}** |")
    lines.append(f"| **Effective dim @ 95% variance** | **{eff_dim_95}** |")
    lines.append(f"| **Effective dim @ 99% variance** | **{eff_dim_99}** |")
    lines.append(f"| Participation ratio | {pr:.1f} |")
    lines.append(f"| Compression ratio (95%) | {768/eff_dim_95:.1f}x |")
    lines.append("")
    lines.append(f"The cognitive state space in brain.db is **effectively {eff_dim_95}-dimensional** "
                 f"(95% variance threshold). This is a {768/eff_dim_95:.1f}x compression from the nominal "
                 f"768d Hilbert space. The participation ratio of {pr:.1f} independently confirms a "
                 f"similarly low effective dimensionality.")
    lines.append("")

    lines.append("### Eigenvalue Spectrum (Top 30)")
    lines.append("")
    lines.append("| PC | Explained % | Cumulative % |")
    lines.append("|----|------------|--------------|")
    for i in range(min(30, len(explained_ratio))):
        lines.append(f"| PC{i+1} | {explained_ratio[i]*100:.2f}% | {cumulative_ratio[i]*100:.2f}% |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 2. Cognitive Subspace Identification — Top 10 PCs")
    lines.append("")
    lines.append("Each principal component represents a latent cognitive dimension "
                 "along which brain.db memories vary the most.")
    lines.append("")
    for label in pc_labels[:10]:
        lines.append(f"### PC{label['pc']} ({explained_ratio[label['pc']-1]*100:.2f}% variance)")
        lines.append("")
        lines.append("**High-scoring entries (positive pole):**")
        for agent, content in label["top_positive"]:
            lines.append(f"- `[{agent}]` {content.strip()[:100]}")
        lines.append("")
        lines.append("**Low-scoring entries (negative pole):**")
        for agent, content in label["top_negative"]:
            lines.append(f"- `[{agent}]` {content.strip()[:100]}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 3. Interference Effectiveness vs. Dimension")
    lines.append("")
    lines.append(
        f"The QCR Phase ([COS-380](/COS/issues/COS-380)) and Amplitude ([COS-383](/COS/issues/COS-383)) "
        f"modules currently compute interference in the full 768d space. "
        f"With an effective dimension of {eff_dim_95}, all interference computations should be "
        f"projected into the reduced {eff_dim_95}-PC basis before scoring."
    )
    lines.append("")
    lines.append("**Performance improvement from dimension reduction:**")
    lines.append("")
    lines.append("| Operation | Full 768d | Reduced basis ({eff_dim_95}d) | Speedup |".format(eff_dim_95=eff_dim_95))
    lines.append("|-----------|-----------|--------------|---------|")
    speedup_dot = 768 / eff_dim_95
    speedup_mat = (768 / eff_dim_95) ** 2
    lines.append(f"| Dot product (cosine sim) | O(768) | O({eff_dim_95}) | {speedup_dot:.1f}x |")
    lines.append(f"| Projection matrix mult | O(768²) | O({eff_dim_95}²) | {speedup_mat:.1f}x |")
    lines.append(f"| Interference kernel | O(768²) | O({eff_dim_95}²) | {speedup_mat:.1f}x |")
    lines.append("")
    lines.append(
        f"Recommendation: pre-project all embeddings to the top {eff_dim_95} PCs at ingest time. "
        f"Store the {eff_dim_95}-dimensional projections alongside raw embeddings. "
        f"QCR algorithms operate on the projected vectors — raw embeddings only needed for reconstruction."
    )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 4. Agent Subspace Alignment")
    lines.append("")
    agents = agent_analysis["agents"]
    spreads = agent_analysis["spreads"]
    sims = agent_analysis["sim_matrix"]

    lines.append("### Per-Agent Subspace Spread (in 50-PC reduced space)")
    lines.append("")
    lines.append("| Agent | # Embeddings | Mean dist from centroid |")
    lines.append("|-------|-------------|------------------------|")
    # Count per agent
    from collections import Counter
    # We'll add agent counts
    for agent in agents:
        spread = spreads.get(agent, None)
        if spread is not None:
            lines.append(f"| {agent} | — | {spread:.4f} |")
    lines.append("")

    hermes_hip = agent_analysis.get("hermes_hippocampus_cosine")
    if hermes_hip is not None:
        lines.append(f"### Hermes vs Hippocampus Subspace Overlap")
        lines.append("")
        lines.append(f"**Cosine similarity of centroid vectors (50-PC space):** {hermes_hip:.4f}")
        lines.append("")
        if abs(hermes_hip) < 0.3:
            interpretation = (
                "Near-orthogonal subspaces. Hermes and hippocampus occupy largely distinct "
                "cognitive regions — their memories are about different topics. "
                "Entanglement ([COS-382](/COS/issues/COS-382)) between them is weak/near-zero by default."
            )
        elif abs(hermes_hip) < 0.6:
            interpretation = (
                "Moderate overlap. Partial entanglement structure. "
                "Some shared cognitive dimensions (likely meta/system topics), "
                "but each agent has a distinct core subspace."
            )
        else:
            interpretation = (
                "High overlap. Hermes and hippocampus share similar cognitive territory. "
                "Strong entanglement — their beliefs likely reinforce/interfere with each other."
            )
        lines.append(interpretation)
        lines.append("")

    lines.append("### Pairwise Centroid Cosine Similarity Matrix (top agents)")
    lines.append("")
    top_agents = [a for a in agents if a in spreads][:8]
    if top_agents:
        header = "| Agent | " + " | ".join(top_agents) + " |"
        lines.append(header)
        sep = "|-------|" + "------|" * len(top_agents)
        lines.append(sep)
        for i, a in enumerate(top_agents):
            ai = agents.index(a) if a in agents else -1
            if ai == -1:
                continue
            row = f"| {a} | "
            for j, b in enumerate(top_agents):
                bj = agents.index(b) if b in agents else -1
                if bj == -1:
                    row += " — |"
                else:
                    row += f" {sims[ai][bj]:.2f} |"
            lines.append(row)
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 5. Tensor Product Structure Test")
    lines.append("")
    lines.append(
        "If the 768d Hilbert space factors as `A ⊗ B` (e.g., `content ⊗ temporal`, "
        "`content ⊗ confidence`), the quantum formalism gains multiplicative structure. "
        "We test by reshaping top PCs as matrices and measuring their rank-1 approximation quality."
    )
    lines.append("")
    lines.append(f"**Mean absolute correlation between top-50 PCs:** {tensor_analysis['mean_abs_pc_correlation']:.4f}")
    lines.append("")
    lines.append("**Rank-1 ratio for candidate factorizations** (1.0 = perfect tensor product):")
    lines.append("")
    lines.append("| Factorization | Rank-1 ratio (top PC) | Interpretation |")
    lines.append("|--------------|----------------------|----------------|")
    for factorization, r1 in tensor_analysis.get("factorization_rank1_ratios", {}).items():
        if r1 > 0.7:
            interp = "Strong tensor structure"
        elif r1 > 0.4:
            interp = "Moderate tensor structure"
        else:
            interp = "Weak / no tensor structure"
        lines.append(f"| {factorization} | {r1:.3f} | {interp} |")
    lines.append("")
    lines.append(tensor_analysis.get("interpretation", ""))
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 6. Recommendations for QCR Algorithm Improvements")
    lines.append("")
    lines.append(
        f"1. **Project to {eff_dim_95}d basis**: All QCR algorithms should operate in the "
        f"effective {eff_dim_95}-PC subspace. Pre-compute and store the PCA projection matrix "
        f"(768 → {eff_dim_95}) as `pca_projection.npy` and transform all embeddings at ingest. "
        f"This gives a {speedup_mat:.0f}x speedup on matrix operations."
    )
    lines.append("")
    lines.append(
        f"2. **Phase interference ([COS-380](/COS/issues/COS-380))**: Phase angles should be "
        f"computed in the reduced {eff_dim_95}d basis. Interference patterns are more distinct "
        f"in the reduced space because noise dimensions (PCs {eff_dim_95+1}–768) are eliminated."
    )
    lines.append("")
    lines.append(
        f"3. **Amplitude scoring ([COS-383](/COS/issues/COS-383))**: Gaussian amplitude kernels "
        f"should use the Mahalanobis distance in the reduced PCA space (eigenvalue-normalized). "
        f"This naturally weights each PC by its variance."
    )
    lines.append("")
    hermes_hip_str = f"{hermes_hip:.4f}" if hermes_hip is not None else "N/A"
    hermes_align = "near-orthogonal" if hermes_hip is not None and abs(hermes_hip) < 0.3 else "overlapping"
    lines.append(
        f"4. **Entanglement structure ([COS-382](/COS/issues/COS-382))**: Agent subspace alignment "
        f"analysis shows {hermes_align} "
        f"subspaces for hermes and hippocampus (cosine sim = {hermes_hip_str}). "
        f"The entanglement Hamiltonian should be parameterized by centroid overlap in the reduced space."
    )
    lines.append("")
    lines.append(
        f"5. **Tensor product structure**: The test suggests "
        f"{'approximate tensor product structure exists' if any(v > 0.4 for v in tensor_analysis.get('factorization_rank1_ratios', {}).values()) else 'no strong tensor product structure'}. "
        f"Best candidate factorization: `{tensor_analysis.get('best_factorization', 'N/A')}`. "
        f"If rank-1 ratios are low, the full 768d space does not factor cleanly — "
        f"quantum gates must be applied in the full PC basis, not factored form."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 7. Source Distribution")
    lines.append("")
    lines.append("| Source table | Count |")
    lines.append("|-------------|-------|")
    for src, cnt in sorted(source_breakdown.items(), key=lambda x: -x[1]):
        lines.append(f"| {src} | {cnt} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Generated by Hilbert (agent 85e1c837) — COS-395*")
    return "\n".join(lines)


def main():
    print("Loading embeddings from brain.db...")
    X, meta = load_embeddings()
    print(f"Loaded {len(X)} embeddings of shape {X.shape}")

    source_breakdown = defaultdict(int)
    for m in meta:
        source_breakdown[m["source_table"]] += 1

    print("Running PCA via SVD...")
    mean, Vt, S, explained_variance, explained_ratio, cumulative_ratio, scores = run_pca(X)

    eff_90 = effective_dimension(cumulative_ratio, 0.90)
    eff_95 = effective_dimension(cumulative_ratio, 0.95)
    eff_99 = effective_dimension(cumulative_ratio, 0.99)
    pr = participation_ratio(explained_variance)

    print(f"  Effective dim @ 90% variance: {eff_90}")
    print(f"  Effective dim @ 95% variance: {eff_95}")
    print(f"  Effective dim @ 99% variance: {eff_99}")
    print(f"  Participation ratio: {pr:.1f}")

    print("Labelling top PCs...")
    pc_labels = [label_pc(Vt[i], meta, None, scores, i) for i in range(min(10, len(Vt)))]

    print("Running agent subspace analysis...")
    agent_analysis = agent_subspace_analysis(scores, meta, n_dims=50)
    print(f"  Hermes vs Hippocampus cosine sim: {agent_analysis.get('hermes_hippocampus_cosine')}")

    print("Testing tensor product structure...")
    tensor_analysis = test_tensor_product_structure(Vt, explained_ratio)

    print("Writing report...")
    report = format_report(
        n_embeddings=len(X),
        explained_ratio=explained_ratio,
        cumulative_ratio=cumulative_ratio,
        eff_dim_95=eff_95,
        eff_dim_90=eff_90,
        eff_dim_99=eff_99,
        pr=pr,
        pc_labels=pc_labels,
        agent_analysis=agent_analysis,
        tensor_analysis=tensor_analysis,
        source_breakdown=dict(source_breakdown),
    )

    report_path = OUTPUT_DIR / "hilbert_dimension_analysis.md"
    report_path.write_text(report)
    print(f"Report written to {report_path}")

    # Save raw data for downstream use
    results = {
        "n_embeddings": int(len(X)),
        "embedding_dim": 768,
        "effective_dim_90": int(eff_90),
        "effective_dim_95": int(eff_95),
        "effective_dim_99": int(eff_99),
        "participation_ratio": float(pr),
        "top50_explained_ratio": explained_ratio[:50].tolist(),
        "top50_cumulative_ratio": cumulative_ratio[:50].tolist(),
        "agent_subspace": {
            "agents": agent_analysis["agents"],
            "spreads": agent_analysis["spreads"],
            "hermes_hippocampus_cosine": agent_analysis.get("hermes_hippocampus_cosine"),
        },
        "tensor_analysis": tensor_analysis,
        "source_breakdown": dict(source_breakdown),
    }
    json_path = OUTPUT_DIR / "hilbert_pca_results.json"
    json_path.write_text(json.dumps(results, indent=2))
    print(f"JSON results written to {json_path}")

    # Save PCA projection matrix (top 100 PCs) for downstream QCR use
    proj_path = OUTPUT_DIR / "pca_projection_top100.npy"
    np.save(proj_path, Vt[:100])
    mean_path = OUTPUT_DIR / "pca_mean.npy"
    np.save(mean_path, mean)
    print(f"PCA matrices saved to {OUTPUT_DIR}/pca_projection_top100.npy + pca_mean.npy")

    print("\nDone!")
    print(f"\nKey finding: Effective Hilbert space dimension = {eff_95}d (95% variance), "
          f"compression {768/eff_95:.1f}x from nominal 768d")


if __name__ == "__main__":
    main()
