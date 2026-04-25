from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
from benchmarks.retrieval_flow_optimizer import detect_flow_operators, optimize_ranked_documents, source_family


AGENT_ID = "legacy-compare-bench"
_SESSION_DOC_ID_RE = re.compile(r"^session[_-]?\d+$", re.IGNORECASE)


@dataclass
class SeededCorpus:
    root_dir: Path
    template_db_path: Path
    rowid_to_doc_id: dict[int, str]
    rowid_to_text: dict[int, str]

    def cleanup(self) -> None:
        shutil.rmtree(self.root_dir, ignore_errors=True)


def init_empty_db(db_path: Path) -> None:
    init_sql = ROOT / "src" / "agentmemory" / "db" / "init_schema.sql"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(init_sql.read_text(encoding="utf-8"))
        now = "2026-01-01T00:00:00Z"
        conn.execute(
            """
            INSERT OR IGNORE INTO agents (
                id, display_name, agent_type, status, created_at, updated_at
            ) VALUES (?, ?, 'bench', 'active', ?, ?)
            """,
            (AGENT_ID, AGENT_ID, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_config (key, value) VALUES ('enabled', '0')"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO neuromodulation_state (
                id, org_state, dopamine_signal, arousal_level,
                confidence_boost_rate, confidence_decay_rate, retrieval_breadth_multiplier,
                focus_level, temporal_lambda, context_window_depth
            ) VALUES (1, 'normal', 0.0, 0.3, 0.1, 0.02, 1.0, 0.3, 0.03, 50)
            """
        )
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    finally:
        conn.close()


def _search_brain(db_path: Path, query: str, top_k: int) -> list[dict]:
    brain = Brain(db_path=str(db_path), agent_id=AGENT_ID)
    try:
        return list(brain.search(query, limit=top_k))
    finally:
        brain.close()


def _search_cmd(
    db_path: Path,
    query: str,
    top_k: int,
    *,
    debug: bool = False,
    benchmark: bool = False,
    benchmark_ranking_mode: str = "full",
) -> list[dict]:
    import agentmemory._impl as _impl

    _impl.DB_PATH = db_path
    args = SimpleNamespace(
        query=query,
        limit=top_k,
        output="return",
        tables="memories",
        profile=None,
        no_recency=False,
        no_graph=True,
        budget=None,
        min_salience=None,
        mmr=False,
        mmr_lambda=0.7,
        explore=False,
        pagerank_boost=0.0,
        quantum=False,
        benchmark=benchmark,
        benchmark_ranking_mode=benchmark_ranking_mode,
        agent=AGENT_ID,
        format="json",
        oneline=False,
        verbose=False,
        debug=debug,
    )
    payload = _impl.cmd_search(args, db=None, db_path=str(db_path))
    memories = list((payload or {}).get("memories") or [])
    memories.sort(key=lambda row: row.get("final_score", row.get("rrf_score", 0.0)), reverse=True)
    return memories[:top_k]


def _is_whole_session_corpus(seeded: SeededCorpus) -> bool:
    if not seeded.rowid_to_text:
        return False
    session_rows = sum(
        1
        for rowid, text in seeded.rowid_to_text.items()
        if text.lstrip().startswith("Session ID:")
        or _SESSION_DOC_ID_RE.match(str(seeded.rowid_to_doc_id.get(rowid, "")))
    )
    return session_rows / max(len(seeded.rowid_to_text), 1) >= 0.8


def _has_compact_source_families(seeded: SeededCorpus, *, max_size: int = 6) -> bool:
    counts: dict[str, int] = {}
    for doc_id in seeded.rowid_to_doc_id.values():
        family = source_family(doc_id)
        counts[family] = counts.get(family, 0) + 1
    return any(2 <= count <= max_size for count in counts.values())


def seed_documents(
    documents: Iterable[tuple[str, str]],
    *,
    category: str = "benchmark",
) -> SeededCorpus:
    os.environ.setdefault("BRAINCTL_SILENT_MIGRATIONS", "1")
    tmp_dir = Path(tempfile.mkdtemp(prefix="brainctl-legacy-seeded-"))
    db_path = tmp_dir / "template_brain.db"
    try:
        init_empty_db(db_path)
        rowid_to_doc_id: dict[int, str] = {}
        rowid_to_text: dict[int, str] = {}
        brain = Brain(db_path=str(db_path), agent_id=AGENT_ID)
        try:
            for doc_id, text in documents:
                rowid = brain.remember(text, category=category)
                rowid_to_doc_id[int(rowid)] = doc_id
                rowid_to_text[int(rowid)] = text
        finally:
            brain.close()
        conn = sqlite3.connect(str(db_path))
        try:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()
        return SeededCorpus(
            root_dir=tmp_dir,
            template_db_path=db_path,
            rowid_to_doc_id=rowid_to_doc_id,
            rowid_to_text=rowid_to_text,
        )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def rank_seeded_documents(
    query: str,
    seeded: SeededCorpus,
    *,
    pipeline: str = "cmd",
    top_k: int = 10,
) -> list[str]:
    work_dir = Path(tempfile.mkdtemp(prefix="brainctl-legacy-query-"))
    db_path = work_dir / "brain.db"
    try:
        shutil.copy2(seeded.template_db_path, db_path)
        operators = detect_flow_operators(query)
        small_bounded_corpus = len(seeded.rowid_to_doc_id) <= max(top_k * 5, 50)
        whole_session_corpus = _is_whole_session_corpus(seeded)
        needs_expanded_pool = (
            operators.role_fact
            or (small_bounded_corpus and not whole_session_corpus)
            or (whole_session_corpus and operators.needs_breadth and _has_compact_source_families(seeded))
        )
        pool_k = max(top_k * 8, 50) if needs_expanded_pool else top_k

        if pipeline == "brain":
            results = _search_brain(db_path, query, pool_k)
        elif pipeline == "cmd":
            results = _search_cmd(db_path, query, pool_k)
        else:
            raise ValueError(f"Unknown pipeline {pipeline!r}")

        ranked, _trace = optimize_ranked_documents(
            query,
            results,
            seeded.rowid_to_doc_id,
            seeded.rowid_to_text,
            top_k=top_k,
        )
        return ranked
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def search_seeded_documents(
    query: str,
    seeded: SeededCorpus,
    *,
    pipeline: str = "cmd",
    top_k: int = 10,
    debug: bool = False,
) -> list[dict]:
    work_dir = Path(tempfile.mkdtemp(prefix="brainctl-legacy-query-"))
    db_path = work_dir / "brain.db"
    try:
        shutil.copy2(seeded.template_db_path, db_path)
        operators = detect_flow_operators(query)
        small_bounded_corpus = len(seeded.rowid_to_doc_id) <= max(top_k * 5, 50)
        whole_session_corpus = _is_whole_session_corpus(seeded)
        needs_expanded_pool = (
            operators.role_fact
            or (small_bounded_corpus and not whole_session_corpus)
            or (whole_session_corpus and operators.needs_breadth and _has_compact_source_families(seeded))
        )
        pool_k = max(top_k * 8, 50) if needs_expanded_pool else top_k
        if pipeline == "brain":
            results = _search_brain(db_path, query, pool_k)
        elif pipeline == "cmd":
            results = _search_cmd(db_path, query, pool_k, debug=debug)
        else:
            raise ValueError(f"Unknown pipeline {pipeline!r}")
        ranked, trace = optimize_ranked_documents(
            query,
            results,
            seeded.rowid_to_doc_id,
            seeded.rowid_to_text,
            top_k=top_k,
        )
        rows_by_doc: dict[str, dict] = {}
        for result in results:
            try:
                rowid = int(result["id"])
            except (KeyError, TypeError, ValueError):
                continue
            doc_id = seeded.rowid_to_doc_id.get(rowid, "")
            if doc_id:
                row = dict(result)
                row["doc_id"] = doc_id
                rows_by_doc[doc_id] = row
        out: list[dict] = []
        for rank, doc_id in enumerate(ranked, start=1):
            row = dict(rows_by_doc.get(doc_id) or {})
            row["doc_id"] = doc_id
            row.setdefault("content", seeded.rowid_to_text.get(_rowid_for_doc(seeded, doc_id), ""))
            row["retrieval_flow_rank"] = rank
            if debug and rank == 1:
                row["retrieval_flow_trace"] = trace
            out.append(row)
        return out
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _rowid_for_doc(seeded: SeededCorpus, doc_id: str) -> int:
    for rowid, mapped_doc_id in seeded.rowid_to_doc_id.items():
        if mapped_doc_id == doc_id:
            return rowid
    return 0


def rank_documents(
    query: str,
    documents: Iterable[tuple[str, str]],
    *,
    pipeline: str = "cmd",
    top_k: int = 10,
    category: str = "benchmark",
) -> list[str]:
    seeded = seed_documents(documents, category=category)
    try:
        return rank_seeded_documents(query, seeded, pipeline=pipeline, top_k=top_k)
    finally:
        seeded.cleanup()


def rank_documents_with_rows(
    query: str,
    documents: Iterable[tuple[str, str]],
    *,
    pipeline: str = "cmd",
    top_k: int = 10,
    category: str = "benchmark",
    debug: bool = False,
) -> list[dict]:
    seeded = seed_documents(documents, category=category)
    try:
        return search_seeded_documents(query, seeded, pipeline=pipeline, top_k=top_k, debug=debug)
    finally:
        seeded.cleanup()
