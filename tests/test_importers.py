"""Tests for agentmemory.importers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmemory import importers
from agentmemory.importers.base import (
    BaseImporter,
    ImporterError,
    ImportResult,
    MemoryRecord,
)


class TestRegistry:
    def test_list_importers_has_mem0_and_json(self):
        ps = importers.list_importers()
        assert "mem0" in ps
        assert "json" in ps

    def test_get_unknown_raises(self):
        with pytest.raises(ImporterError):
            importers.get_importer("not-a-real-provider")


class TestJsonImporter:
    def test_list_payload(self, tmp_path: Path):
        p = tmp_path / "memories.json"
        p.write_text(json.dumps([
            {"content": "First memory", "category": "preference", "tags": ["ui"]},
            {"content": "Second memory", "category": "decision"},
        ]))
        result = importers.get_importer("json").parse(p)
        assert result.provider == "json"
        assert len(result.records) == 2
        assert result.records[0].content == "First memory"
        assert result.records[0].category == "preference"
        assert result.records[0].tags == ["ui"]
        assert result.skipped == 0

    def test_wrapped_payload(self, tmp_path: Path):
        p = tmp_path / "wrapped.json"
        p.write_text(json.dumps({"memories": [
            {"content": "wrapped a"},
            {"content": "wrapped b"},
        ]}))
        result = importers.get_importer("json").parse(p)
        assert len(result.records) == 2

    def test_jsonl(self, tmp_path: Path):
        p = tmp_path / "memories.jsonl"
        p.write_text(
            json.dumps({"content": "line one"}) + "\n"
            + json.dumps({"content": "line two"}) + "\n"
            + "\n"
            + "not-json-trailing"
        )
        result = importers.get_importer("json").parse(p)
        assert len(result.records) == 2
        assert any("not-json" in w for w in result.warnings)

    def test_skips_records_without_content(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps([
            {"content": "ok"},
            {"category": "decision"},  # no content
            {},
        ]))
        result = importers.get_importer("json").parse(p)
        assert len(result.records) == 1
        assert result.skipped == 2

    def test_round_trips_extra_fields(self, tmp_path: Path):
        p = tmp_path / "extras.json"
        p.write_text(json.dumps([
            {
                "content": "with extras",
                "category": "user",
                "metadata": {"k": "v"},
                "custom_field": "custom-value",
                "another": 42,
            }
        ]))
        result = importers.get_importer("json").parse(p)
        rec = result.records[0]
        assert rec.source_metadata.get("k") == "v"
        assert rec.source_metadata.get("custom_field") == "custom-value"
        assert rec.source_metadata.get("another") == 42

    def test_rejects_unparseable(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{ not valid")
        with pytest.raises(ImporterError):
            importers.get_importer("json").parse(p)

    def test_rejects_root_string(self, tmp_path: Path):
        p = tmp_path / "scalar.json"
        p.write_text(json.dumps("just a string"))
        with pytest.raises(ImporterError):
            importers.get_importer("json").parse(p)


class TestMem0Importer:
    def test_sdk_results_shape(self, tmp_path: Path):
        p = tmp_path / "mem0.json"
        p.write_text(json.dumps({
            "results": [
                {
                    "id": "abc-123",
                    "memory": "User prefers dark mode",
                    "user_id": "alice",
                    "metadata": {"app": "web"},
                    "created_at": "2026-05-12T03:00:00Z",
                    "score": 0.9,
                },
                {
                    "id": "def-456",
                    "memory": "Likes type-safe Python",
                    "user_id": "alice",
                    "metadata": {},
                },
            ]
        }))
        result = importers.get_importer("mem0").parse(p)
        assert len(result.records) == 2
        first = result.records[0]
        assert first.content == "User prefers dark mode"
        assert first.category == "user"
        assert first.source_id == "abc-123"
        assert first.agent_id == "alice"
        assert first.created_at == "2026-05-12T03:00:00Z"
        # mem0-only fields preserved in metadata
        assert first.source_metadata.get("score") == 0.9
        assert first.source_metadata.get("app") == "web"

    def test_legacy_top_level_list(self, tmp_path: Path):
        p = tmp_path / "mem0-legacy.json"
        p.write_text(json.dumps([
            {"text": "legacy memory", "id": "1"},
            {"content": "another via content field", "id": "2"},
        ]))
        result = importers.get_importer("mem0").parse(p)
        assert len(result.records) == 2
        assert result.records[0].content == "legacy memory"
        assert result.records[1].content == "another via content field"

    def test_skips_records_without_memory(self, tmp_path: Path):
        p = tmp_path / "mem0-bad.json"
        p.write_text(json.dumps({
            "results": [
                {"memory": "ok"},
                {"id": "no-memory"},
                {"text": ""},
            ]
        }))
        result = importers.get_importer("mem0").parse(p)
        assert len(result.records) == 1
        assert result.skipped == 2

    def test_tags_from_categories_list(self, tmp_path: Path):
        p = tmp_path / "mem0-cats.json"
        p.write_text(json.dumps({
            "results": [
                {"memory": "tagged",
                 "categories": ["preference", "ui"]}
            ]
        }))
        result = importers.get_importer("mem0").parse(p)
        assert result.records[0].tags == ["preference", "ui"]

    def test_tags_from_metadata_labels(self, tmp_path: Path):
        p = tmp_path / "mem0-labels.json"
        p.write_text(json.dumps({
            "results": [
                {"memory": "labeled",
                 "metadata": {"labels": ["a", "b"]}}
            ]
        }))
        result = importers.get_importer("mem0").parse(p)
        assert result.records[0].tags == ["a", "b"]

    def test_rejects_unparseable(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not json")
        with pytest.raises(ImporterError):
            importers.get_importer("mem0").parse(p)


class TestMemoryRecordNormalization:
    def test_known_category_passes_through(self):
        rec = MemoryRecord(content="x", category="preference")
        assert rec.normalized_category() == "preference"

    def test_unknown_category_falls_back(self):
        rec = MemoryRecord(content="x", category="totally-made-up")
        assert rec.normalized_category() == "project"
