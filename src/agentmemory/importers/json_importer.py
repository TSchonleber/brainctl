"""Generic JSON importer — accepts a list of records OR a wrapped
``{"memories": [...]}`` payload. Useful when the agent owner has a
custom export format and just wants a clean way to bring it in.

Expected record shape (all keys optional except ``content``):

    {
      "content": "User prefers dark mode",
      "category": "preference",      # one of brainctl's 9 categories
      "tags": ["ui", "ux"],
      "confidence": 1.0,
      "source_id": "external-id-123",
      "created_at": "2026-05-12T03:00:00Z",
      "agent_id": "my-agent",
      "metadata": {"...": "..."}
    }

Anything else in the record is preserved under ``source_metadata``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from agentmemory.importers.base import (
    BaseImporter,
    ImporterError,
    ImportResult,
    MemoryRecord,
    register_importer,
)


class JsonImporter(BaseImporter):
    provider = "json"
    file_extensions = (".json", ".jsonl")

    def parse(self, source: Path) -> ImportResult:
        path = Path(source).expanduser()
        if not path.exists():
            raise ImporterError(f"source not found: {path}")
        if not path.is_file():
            raise ImporterError(f"source is not a file: {path}")

        text = path.read_text(encoding="utf-8")
        records: List[Dict[str, Any]]
        warnings: List[str] = []

        # JSONL: one record per line. JSON: either a list or a dict
        # with a "memories" key.
        if path.suffix.lower() == ".jsonl":
            records = []
            for i, line in enumerate(text.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    snippet = line[:60].replace("\n", " ")
                    warnings.append(f"line {i}: {e} ({snippet!r})")
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise ImporterError(f"failed to parse JSON: {e}") from e
            if isinstance(parsed, list):
                records = parsed
            elif isinstance(parsed, dict):
                # Accept either {"memories": [...]} or {"data": [...]}.
                candidate = parsed.get("memories") or parsed.get("data") or parsed.get("records")
                if not isinstance(candidate, list):
                    raise ImporterError(
                        "JSON dict must contain 'memories', 'data', or "
                        "'records' as a list"
                    )
                records = candidate
            else:
                raise ImporterError("JSON root must be a list or dict")

        out: List[MemoryRecord] = []
        skipped = 0
        for i, r in enumerate(records):
            if not isinstance(r, dict):
                warnings.append(f"record {i}: not an object, skipped")
                skipped += 1
                continue
            content = r.get("content")
            if not content or not isinstance(content, str):
                warnings.append(f"record {i}: missing/empty 'content', skipped")
                skipped += 1
                continue
            tags = r.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            elif not isinstance(tags, list):
                tags = []

            # Capture provider extras under source_metadata for
            # round-tripping / debugging.
            consumed = {
                "content", "category", "tags", "confidence",
                "source_id", "created_at", "agent_id", "metadata",
            }
            metadata = dict(r.get("metadata") or {})
            for k, v in r.items():
                if k not in consumed:
                    metadata.setdefault(k, v)

            out.append(MemoryRecord(
                content=content.strip(),
                category=str(r.get("category") or "project"),
                tags=[str(t) for t in tags],
                confidence=float(r.get("confidence") or 1.0),
                source_id=r.get("source_id"),
                source_metadata=metadata,
                created_at=r.get("created_at"),
                agent_id=r.get("agent_id"),
            ))

        return ImportResult(
            provider=self.provider,
            source_path=str(path),
            records=out,
            warnings=warnings,
            skipped=skipped,
        )


register_importer("json", JsonImporter)
