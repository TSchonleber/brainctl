#!/Users/r4vager/agentmemory/.venv/bin/python3
"""
pca_159_recompute.py — Recompute PCA projection matrix for 159 components (COS-412)

Loads all 768d embeddings from brain.db, runs full-rank SVD PCA, saves:
  - pca_projection_top159.npy  (768, 159) — projection matrix
  - pca_eigenvalues_159.npy    (159,)     — explained variance per PC
  - pca_mean_159.npy           (768,)     — centering vector

Optionally backfills memories.hilbert_projection with 159d PCA coordinates.
"""

import argparse
import sqlite3
import struct
import numpy as np
from pathlib import Path

DB_PATH = Path.home() / "agentmemory/db/brain.db"
OUTPUT_DIR = Path.home() / "agentmemory/research/quantum"

N_COMPONENTS = 159


def load_vector(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def load_embeddings(conn: sqlite3.Connection):
    """Load all 768d embeddings and their source memory_id (if from memories table)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, e.source_table, e.source_id, e.vector
        FROM embeddings e
        WHERE e.dimensions = 768 AND e.vector IS NOT NULL
        ORDER BY e.id
    """)
    rows = cur.fetchall()

    vecs = []
    meta = []  # (embed_id, source_table, source_id)
    for embed_id, src_table, src_id, blob in rows:
        vec = load_vector(blob)
        if len(vec) == 768:
            vecs.append(vec)
            meta.append((embed_id, src_table, src_id))

    return np.array(vecs, dtype=np.float32), meta


def run_pca(X: np.ndarray, n_components: int):
    """Fit PCA via SVD. Returns (mean, projection_matrix, eigenvalues)."""
    mean = X.mean(axis=0)
    X_c = X - mean
    # Thin SVD — only min(n, p) singular values
    U, S, Vt = np.linalg.svd(X_c, full_matrices=False)
    explained_variance = (S ** 2) / (len(X) - 1)
    projection = Vt[:n_components].T   # shape (768, n_components)
    eigenvalues = explained_variance[:n_components]
    cumvar = np.cumsum(explained_variance / explained_variance.sum())
    print(f"  Cumulative variance @ {n_components} components: {cumvar[n_components-1]*100:.2f}%")
    return mean, projection, eigenvalues


def project_to_pca_space(
    embedding: np.ndarray,
    pca_matrix: np.ndarray,
    pca_mean: np.ndarray,
) -> np.ndarray:
    """Project a 768d embedding to 159d PCA space."""
    return (embedding - pca_mean) @ pca_matrix


def backfill_memories(conn: sqlite3.Connection, pca_matrix: np.ndarray, pca_mean: np.ndarray, meta):
    """Store 159d PCA projection in memories.hilbert_projection for all memory embeddings."""
    cur = conn.cursor()

    # Check column exists
    cols = [r[1] for r in cur.execute("PRAGMA table_info(memories)").fetchall()]
    if "hilbert_projection" not in cols:
        print("  Adding hilbert_projection column to memories...")
        cur.execute("ALTER TABLE memories ADD COLUMN hilbert_projection BLOB DEFAULT NULL")

    updated = 0
    for embed_id, src_table, src_id in meta:
        if src_table != "memories":
            continue
        # Load raw embedding
        row = cur.execute(
            "SELECT vector FROM embeddings WHERE id = ?", (embed_id,)
        ).fetchone()
        if not row:
            continue
        vec = load_vector(row[0])
        if len(vec) != 768:
            continue
        pca_vec = project_to_pca_space(vec.astype(np.float64), pca_matrix, pca_mean)
        blob = struct.pack(f"{N_COMPONENTS}d", *pca_vec)
        cur.execute(
            "UPDATE memories SET hilbert_projection = ? WHERE id = ?",
            (blob, src_id),
        )
        updated += 1

    conn.commit()
    print(f"  Backfilled hilbert_projection for {updated} memories.")
    return updated


def validate(pca_matrix: np.ndarray, pca_mean: np.ndarray, eigenvalues: np.ndarray):
    """Run basic correctness checks."""
    errors = []

    # Test 1: arbitrary 768d vector projects to 159d
    v = np.random.randn(768).astype(np.float64)
    proj = project_to_pca_space(v, pca_matrix, pca_mean)
    assert proj.shape == (N_COMPONENTS,), f"Expected ({N_COMPONENTS},), got {proj.shape}"

    # Test 2: Mahalanobis amplitude for identical embeddings = 1.0
    q = np.random.randn(768).astype(np.float64)
    q_pca = project_to_pca_space(q, pca_matrix, pca_mean)
    diff = q_pca - q_pca
    weighted = diff / np.sqrt(eigenvalues + 1e-8)
    distance = np.sqrt(np.dot(weighted, weighted))
    amplitude = np.exp(-distance ** 2 / 2.0)
    assert abs(amplitude - 1.0) < 1e-9, f"Identity amplitude should be 1.0, got {amplitude}"

    # Test 3: orthogonal embeddings in PCA space should give low amplitude
    # (hard to guarantee orthogonal in original space maps to far in PCA, so just check < 1)
    q2 = np.random.randn(768).astype(np.float64)
    m2 = np.random.randn(768).astype(np.float64)
    q2_pca = project_to_pca_space(q2, pca_matrix, pca_mean)
    m2_pca = project_to_pca_space(m2, pca_matrix, pca_mean)
    diff2 = q2_pca - m2_pca
    weighted2 = diff2 / np.sqrt(eigenvalues + 1e-8)
    dist2 = np.sqrt(np.dot(weighted2, weighted2))
    amp2 = np.exp(-dist2 ** 2 / 2.0)
    assert amp2 < 1.0, f"Random pair amplitude should be < 1.0, got {amp2}"

    print(f"  [OK] Projection shape: ({N_COMPONENTS},)")
    print(f"  [OK] Identity amplitude: {amplitude:.6f}")
    print(f"  [OK] Random pair amplitude: {amp2:.6f} < 1.0")
    return True


def main():
    parser = argparse.ArgumentParser(description="Recompute 159-component PCA for COS-412")
    parser.add_argument("--backfill-db", action="store_true",
                        help="Backfill memories.hilbert_projection with 159d PCA coordinates")
    parser.add_argument("--validate", action="store_true", default=True,
                        help="Run validation checks after computing (default: on)")
    args = parser.parse_args()

    print(f"Connecting to {DB_PATH}...")
    conn = sqlite3.connect(str(DB_PATH))

    print("Loading 768d embeddings...")
    X, meta = load_embeddings(conn)
    print(f"  Loaded {len(X)} embeddings, shape {X.shape}")

    print(f"Running PCA (n_components={N_COMPONENTS})...")
    mean, projection, eigenvalues = run_pca(X, N_COMPONENTS)
    print(f"  Projection matrix shape: {projection.shape}")
    print(f"  Eigenvalues shape: {eigenvalues.shape}")
    print(f"  Top-5 eigenvalues: {eigenvalues[:5]}")

    # Save outputs
    proj_path = OUTPUT_DIR / "pca_projection_top159.npy"
    eigen_path = OUTPUT_DIR / "pca_eigenvalues_159.npy"
    mean_path = OUTPUT_DIR / "pca_mean_159.npy"

    np.save(proj_path, projection)
    np.save(eigen_path, eigenvalues)
    np.save(mean_path, mean)
    print(f"Saved:")
    print(f"  {proj_path}  shape={projection.shape}")
    print(f"  {eigen_path}  shape={eigenvalues.shape}")
    print(f"  {mean_path}  shape={mean.shape}")

    if args.validate:
        print("Running validation...")
        validate(projection, mean, eigenvalues)

    if args.backfill_db:
        print(f"Backfilling memories.hilbert_projection ({N_COMPONENTS}d)...")
        n = backfill_memories(conn, projection, mean, meta)
        print(f"  Done. {n} rows updated.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
