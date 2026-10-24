from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from benchmarks.convomem_bench import run_brainctl_convomem
from benchmarks.framework import PARTIAL


def test_convomem_degrades_to_partial_when_one_category_fails(tmp_path: Path):
    cache_dir = tmp_path / "convomem_cache"

    def _fake_discover(category: str, _cache_dir: Path):
        if category == "user_evidence":
            raise OSError("connection reset")
        return ["sample.json"]

    def _fake_download(url: str, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if "sample.json" not in url:
            return [{"path": "assistant_facts_evidence/sample.json"}]
        return {
            "evidence_items": [
                {
                    "question": "What does the assistant know?",
                    "message_evidences": [{"text": "The assistant knows the deployment plan."}],
                    "conversations": [
                        {
                            "messages": [
                                {"text": "The assistant knows the deployment plan."},
                                {"text": "Unrelated chatter."},
                            ]
                        }
                    ],
                }
            ]
        }

    with patch("benchmarks.convomem_bench._discover_files", side_effect=_fake_discover):
        with patch("benchmarks.convomem_bench._download_json", side_effect=_fake_download):
            run, rows = run_brainctl_convomem(
                categories=["assistant_facts_evidence", "user_evidence"],
                limit_per_category=1,
                top_k=5,
                cache_dir=cache_dir,
            )

    assert run.status == PARTIAL
    assert run.example_count == 1
    assert rows
    assert any("user_evidence" in caveat for caveat in run.caveats)

