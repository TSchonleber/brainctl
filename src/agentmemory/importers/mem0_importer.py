"""mem0 importer.

Parses a mem0 export JSON. mem0's export shapes vary across versions;
this importer handles the three shapes I've seen in the wild:

  1. Top-level list of memory dicts.
  2. {"memories": [...]}
  3. {"results": [{"memory": "...", "id": "...", "user_id": "...",
                   "metadata": {...}, "created_at": "..."}]}  ← Python SDK
                   default

Common fields mapped to brainctl:

  - ``memory`` or ``content`` or ``text`` → MemoryRecord.content
  - ``id`` → MemoryRecord.source_id (preserved)
  - ``user_id`` / ``agent_id`` → MemoryRecord.agent_id
  - ``created_at`` → MemoryRecord.created_at
  - ``metadata`` → MemoryRecord.source_metadata (round-tripped intact)
  - mem0 has no first-class category; we default to "user" since the
    mem0 product is user-memory-centric. Override with --category.
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


class Mem0Importer(BaseImporter):
    provider = "mem0"
    file_extensions = (".json",)

    def parse(self, source: Path) -> ImportResult:
        path = Path(source).expanduser()
        if not path.exists():
            raise ImporterError(f"source not found: {path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ImporterError(f"failed to parse mem0 export: {e}") from e

        # Coerce to a flat list of memory dicts regardless of which
        # shape mem0 produced.
        candidates: List[Dict[str, Any]]
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = (
                data.get("results")
                or data.get("memories")
                or data.get("data")
                or []
            )
            if not isinstance(candidates, list):
                raise ImporterError(
                    "mem0 export dict must contain 'results', 'memories', "
                    "or 'data' as a list"
                )
        else:
            raise ImporterError("mem0 export root must be a list or dict")

        out: List[MemoryRecord] = []
        warnings: List[str] = []
        skipped = 0

        for i, r in enumerate(candidates):
            if not isinstance(r, dict):
                warnings.append(f"record {i}: not an object")
                skipped += 1
                continue

            # mem0 SDK uses `memory`; legacy exports may use `text` or
            # `content`. Try all three.
            content = r.get("memory") or r.get("content") or r.get("text")
            if not content or not isinstance(content, str):
                warnings.append(f"record {i}: missing 'memory'/'content'/'text'")
                skipped += 1
                continue

            metadata = dict(r.get("metadata") or {})
            # Pull provider-specific fields into the metadata bag for
            # full round-trip context.
            for k in ("hash", "score", "categories", "app_id", "run_id"):
                if k in r and k not in metadata:
                    metadata[k] = r[k]

            out.append(MemoryRecord(
                content=content.strip(),
                # mem0 has no first-class category; default to "user".
                category="user",
                tags=_extract_tags(r),
                confidence=1.0,
                source_id=str(r.get("id") or "") or None,
                source_metadata=metadata,
                created_at=r.get("created_at") or r.get("updated_at"),
                agent_id=r.get("agent_id") or r.get("user_id"),
            ))

        return ImportResult(
            provider=self.provider,
            source_path=str(path),
            records=out,
            warnings=warnings,
            skipped=skipped,
        )


def _extract_tags(record: Dict[str, Any]) -> List[str]:
    """mem0 tags live in a few possible places: top-level 'categories',
    metadata.tags, or metadata.labels."""
    raw = (
        record.get("categories")
        or (record.get("metadata") or {}).get("tags")
        or (record.get("metadata") or {}).get("labels")
        or []
    )
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


register_importer("mem0", Mem0Importer)
