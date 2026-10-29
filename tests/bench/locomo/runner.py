"""LOCOMO retrieval-only benchmark runner for brainctl.

LOCOMO (snap-research/locomo, ACL 2024) is a long-horizon conversational
memory benchmark: 10 multi-session conversations, ~5.9k turns total,
1986 QA pairs across 5 categories. Each QA carries gold "evidence" turn
IDs of the form `D{session}:{turn}` referencing the dia_id field on each
turn.

Brainctl claims to win on long-horizon retention; LOCOMO is a faithful
test of that claim *at the retrieval layer*. This runner skips the
LLM answer-generation + GPT-judge stage (no API budget required) and
reports pure retrieval quality: Hit@K, Recall@K, MRR against the gold
evidence set.

For each conversation:
  1. Spin up a fresh temp brain.db.
  2. Insert every turn as one memory, content tagged with `[key=D{i}:{j}]`.
  3. For each QA, search the question with limit=20, parse keys from
     results, score against gold evidence.
  4. Aggregate per-conversation, per-category, and overall.

Run:
    python3 -m tests.bench.locomo.runner --convo 0       # smoke (1 convo)
    python3 -m tests.bench.locomo.runner                 # all 10
    python3 -m tests.bench.locomo.runner --json out.json # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from agentmemory.brain import Brain  # noqa: E402

DATA_PATH = Path(__file__).parent / "locomo10.json"
KEY_RE = re.compile(r"\[key=(D\d+:\d+)\]")
KS = (1, 5, 10, 20)


def _build_cmd_search_fn(db_path: Path):
    """Wrap the full cmd_search hybrid pipeline (FTS5 + vector RRF + intent
    routing + adaptive salience). Mirrors tests/bench/eval._build_cmd_search_fn
    but parameterised for an arbitrary DB path that the caller already seeded.
    """
    import contextlib
    import io
    import types
    import gc
    import agentmemory._impl as _impl
    _impl.DB_PATH = db_path

    def search_fn(query: str, k: int):
        captured: list = []

        def _capture(data, compact=False):
            captured.append(data)

        args = types.SimpleNamespace(
            query=query, limit=k,
            tables="memories,events,context",
            # LOCOMO evidence skews to early sessions; recency reranking
            # actively buries the gold turns. Disable it for the bench.
            no_recency=True, no_graph=True,
            budget=None, min_salience=None,
            mmr=False, mmr_lambda=0.7, explore=False,
            profile=None, pagerank_boost=0.0,
            quantum=False, benchmark=False,
            agent="bench-agent", format="json",
            oneline=False, verbose=False,
        )
        saved_json = _impl.json_out
        saved_oneline = _impl.oneline_out
        _impl.json_out = _capture
        _impl.oneline_out = _capture
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    _impl.cmd_search(args)
                except Exception as exc:
                    search_fn.errors[type(exc).__name__] = search_fn.errors.get(type(exc).__name__, 0) + 1
                    return []
        finally:
            _impl.json_out = saved_json
            _impl.oneline_out = saved_oneline
            gc.collect()

        if not captured:
            return []
        payload = captured[0] if isinstance(captured[0], dict) else {}
        flat = []
        for bucket in ("memories", "events", "context", "entities", "decisions"):
            flat.extend(payload.get(bucket, []) or [])
        flat.sort(key=lambda r: r.get("final_score", 0.0), reverse=True)
        return flat[:k]

    search_fn.errors = {}
    return search_fn

# LOCOMO QA category labels (snap-research/locomo task_eval convention).
CATEGORY_LABELS = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}


@dataclass
class QAResult:
    question: str
    category: int
    gold: List[str]
    ranked_keys: List[str]
    hit: Dict[int, int] = field(default_factory=dict)      # K -> 0/1
    recall: Dict[int, float] = field(default_factory=dict)  # K -> [0,1]
    mrr: float = 0.0


def _key_for(result: dict) -> str:
    text = result.get("content") or ""
    m = KEY_RE.search(text)
    return m.group(1) if m else ""


def ingest_conversation(brain: Brain, convo: dict) -> int:
    """Insert every turn as one memory tagged with its dia_id."""
    n = 0
    conv = convo["conversation"]
    speaker_a = conv.get("speaker_a", "A")
    speaker_b = conv.get("speaker_b", "B")
    session_keys = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda s: int(s.split("_")[1]),
    )
    for sk in session_keys:
        date = conv.get(f"{sk}_date_time", "")
        for turn in conv[sk]:
            dia_id = turn.get("dia_id", "")
            if not dia_id:
                continue
            spk = turn.get("speaker") or ""
            text = turn.get("text") or ""
            content = f"[{spk} @ {date}] {text} [key={dia_id}]"
            brain.remember(content, category="observation")
            n += 1
    return n


def score_qa(qa: dict, search_fn, k_max: int = 20) -> QAResult:
    question = qa.get("question", "")
    gold = list(qa.get("evidence") or [])
    category = int(qa.get("category", 0))
    results = search_fn(question, k_max) if question else []
    ranked_keys = [k for k in (_key_for(r) for r in results) if k]
    qr = QAResult(question=question, category=category, gold=gold, ranked_keys=ranked_keys)
    gold_set = set(gold)
    for K in KS:
        window = ranked_keys[:K]
        inter = gold_set.intersection(window)
        qr.hit[K] = 1 if inter else 0
        qr.recall[K] = (len(inter) / len(gold_set)) if gold_set else 0.0
    qr.mrr = 0.0
    for i, key in enumerate(ranked_keys, start=1):
        if key in gold_set:
            qr.mrr = 1.0 / i
            break
    return qr


def run_convo(convo: dict, k_max: int = 20, backend: str = "brain") -> Dict[str, Any]:
    sample_id = convo.get("sample_id", "?")
    qa_pairs = [q for q in convo.get("qa", []) if q.get("evidence")]
    skipped = len(convo.get("qa", [])) - len(qa_pairs)

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "locomo.db"
        brain = Brain(db_path=str(db_path), agent_id=f"locomo-{sample_id}")
        t0 = time.perf_counter()
        n_turns = ingest_conversation(brain, convo)
        t_ingest = time.perf_counter() - t0

        if backend == "cmd":
            # Release Brain's writer connection + checkpoint WAL so cmd_search
            # can open its own connection without fighting for the write lock.
            try:
                conn = brain._get_conn()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                brain.close()
            except Exception:
                pass
            import gc
            gc.collect()
            search_fn = _build_cmd_search_fn(db_path)
        else:
            def _brain_search(q, k):
                return brain.search(q, limit=k)
            _brain_search.errors = {}
            search_fn = _brain_search

        rows: List[QAResult] = []
        t0 = time.perf_counter()
        for qa in qa_pairs:
            rows.append(score_qa(qa, search_fn, k_max=k_max))
        t_query = time.perf_counter() - t0
        if getattr(search_fn, "errors", None):
            print(f"  [warn] {sample_id} search errors: {dict(search_fn.errors)}", flush=True)

    n = len(rows) or 1
    summary = {
        "sample_id": sample_id,
        "n_turns": n_turns,
        "n_qa": len(rows),
        "n_qa_skipped_no_evidence": skipped,
        "t_ingest_s": round(t_ingest, 2),
        "t_query_s": round(t_query, 2),
    }
    for K in KS:
        summary[f"hit@{K}"] = round(sum(r.hit[K] for r in rows) / n, 4)
        summary[f"recall@{K}"] = round(sum(r.recall[K] for r in rows) / n, 4)
    summary["mrr"] = round(sum(r.mrr for r in rows) / n, 4)

    by_cat: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = by_cat.setdefault(str(r.category), {"count": 0, "hits": {K: 0 for K in KS},
                                                 "recall": {K: 0.0 for K in KS}, "mrr_sum": 0.0})
        b["count"] += 1
        for K in KS:
            b["hits"][K] += r.hit[K]
            b["recall"][K] += r.recall[K]
        b["mrr_sum"] += r.mrr
    for cat, b in by_cat.items():
        c = b["count"] or 1
        by_cat[cat] = {
            "label": CATEGORY_LABELS.get(int(cat), f"cat-{cat}"),
            "count": b["count"],
            **{f"hit@{K}": round(b["hits"][K] / c, 4) for K in KS},
            **{f"recall@{K}": round(b["recall"][K] / c, 4) for K in KS},
            "mrr": round(b["mrr_sum"] / c, 4),
        }
    summary["by_category"] = by_cat
    return summary


def aggregate(per_convo: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not per_convo:
        return {}
    weights = [c["n_qa"] for c in per_convo]
    total = sum(weights) or 1
    def wmean(field: str) -> float:
        return round(sum(c[field] * c["n_qa"] for c in per_convo) / total, 4)
    overall = {
        "n_convos": len(per_convo),
        "n_turns_total": sum(c["n_turns"] for c in per_convo),
        "n_qa_total": total,
        "mrr": wmean("mrr"),
    }
    for K in KS:
        overall[f"hit@{K}"] = wmean(f"hit@{K}")
        overall[f"recall@{K}"] = wmean(f"recall@{K}")

    cat_acc: Dict[str, Dict[str, Any]] = {}
    for c in per_convo:
        for cat, row in c["by_category"].items():
            acc = cat_acc.setdefault(cat, {"label": row["label"], "count": 0,
                                            "hits": {K: 0.0 for K in KS},
                                            "recall": {K: 0.0 for K in KS},
                                            "mrr_sum": 0.0})
            acc["count"] += row["count"]
            for K in KS:
                acc["hits"][K] += row[f"hit@{K}"] * row["count"]
                acc["recall"][K] += row[f"recall@{K}"] * row["count"]
            acc["mrr_sum"] += row["mrr"] * row["count"]
    by_cat = {}
    for cat, acc in cat_acc.items():
        n = acc["count"] or 1
        by_cat[cat] = {
            "label": acc["label"],
            "count": acc["count"],
            **{f"hit@{K}": round(acc["hits"][K] / n, 4) for K in KS},
            **{f"recall@{K}": round(acc["recall"][K] / n, 4) for K in KS},
            "mrr": round(acc["mrr_sum"] / n, 4),
        }
    overall["by_category"] = by_cat
    return overall


def _fmt_table(per_convo: List[Dict[str, Any]], overall: Dict[str, Any], backend: str = "brain") -> str:
    lines = []
    lines.append("=" * 88)
    label = ("Brain.search (FTS5-only)" if backend == "brain"
             else "cmd_search (hybrid FTS5+vector RRF + intent routing)")
    lines.append(f"LOCOMO retrieval-only benchmark — brainctl {label}")
    lines.append("=" * 88)
    lines.append(f"{'sample_id':<12} {'turns':>6} {'qa':>5} "
                 f"{'hit@1':>7} {'hit@5':>7} {'hit@10':>7} {'hit@20':>7} "
                 f"{'r@5':>6} {'r@10':>6} {'mrr':>6}")
    lines.append("-" * 88)
    for c in per_convo:
        lines.append(
            f"{str(c['sample_id']):<12} {c['n_turns']:>6} {c['n_qa']:>5} "
            f"{c['hit@1']:>7.4f} {c['hit@5']:>7.4f} {c['hit@10']:>7.4f} {c['hit@20']:>7.4f} "
            f"{c['recall@5']:>6.4f} {c['recall@10']:>6.4f} {c['mrr']:>6.4f}"
        )
    lines.append("-" * 88)
    o = overall
    lines.append(
        f"{'OVERALL':<12} {o['n_turns_total']:>6} {o['n_qa_total']:>5} "
        f"{o['hit@1']:>7.4f} {o['hit@5']:>7.4f} {o['hit@10']:>7.4f} {o['hit@20']:>7.4f} "
        f"{o['recall@5']:>6.4f} {o['recall@10']:>6.4f} {o['mrr']:>6.4f}"
    )
    lines.append("")
    lines.append("By category (overall, weighted by QA count)")
    lines.append("-" * 88)
    lines.append(f"{'cat':<14} {'count':>6} "
                 f"{'hit@1':>7} {'hit@5':>7} {'hit@10':>7} {'hit@20':>7} "
                 f"{'r@5':>6} {'r@10':>6} {'mrr':>6}")
    for cat, row in sorted(overall["by_category"].items(), key=lambda kv: int(kv[0])):
        lines.append(
            f"{row['label']:<14} {row['count']:>6} "
            f"{row['hit@1']:>7.4f} {row['hit@5']:>7.4f} {row['hit@10']:>7.4f} {row['hit@20']:>7.4f} "
            f"{row['recall@5']:>6.4f} {row['recall@10']:>6.4f} {row['mrr']:>6.4f}"
        )
    lines.append("=" * 88)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="LOCOMO retrieval-only benchmark for brainctl")
    p.add_argument("--data", default=str(DATA_PATH), help="Path to locomo10.json")
    p.add_argument("--convo", type=int, default=None,
                   help="Run only conversation index N (0-9). Default: all.")
    p.add_argument("--k-max", type=int, default=20, help="Max top-K to retrieve per question")
    p.add_argument("--backend", choices=("brain", "cmd"), default="brain",
                   help="Retrieval backend: 'brain' (FTS5-only Brain.search) or "
                        "'cmd' (full hybrid cmd_search: FTS5 + vector RRF + intent routing)")
    p.add_argument("--json", dest="json_out", default=None,
                   help="Write per-convo + overall results to this JSON file")
    args = p.parse_args(argv)

    data = json.loads(Path(args.data).read_text())
    if args.convo is not None:
        if not 0 <= args.convo < len(data):
            print(f"--convo must be in 0..{len(data)-1}", file=sys.stderr)
            return 2
        convos = [data[args.convo]]
    else:
        convos = data

    per_convo = []
    t_total = time.perf_counter()
    for c in convos:
        sid = c.get("sample_id", "?")
        print(f"[locomo] running {sid} ({sum(len(v) for k,v in c['conversation'].items() if isinstance(v, list))} turns, {len(c['qa'])} qa)...",
              flush=True)
        per_convo.append(run_convo(c, k_max=args.k_max, backend=args.backend))
    elapsed = time.perf_counter() - t_total
    overall = aggregate(per_convo)

    print()
    print(_fmt_table(per_convo, overall, backend=args.backend))
    print(f"\n[locomo] total wall time: {elapsed:.1f}s")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"per_convo": per_convo, "overall": overall, "elapsed_s": round(elapsed, 2)},
            indent=2,
        ))
        print(f"[locomo] wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
