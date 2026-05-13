"""Base classes + registry for provider import adapters."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

# Memory categories brainctl accepts. Importers map provider-specific
# types onto these. Unknown source types fall through to "project".
VALID_CATEGORIES = {
    "convention", "decision", "environment", "identity", "integration",
    "lesson", "preference", "project", "user",
}


@dataclass
class MemoryRecord:
    """Provider-agnostic memory shape the CLI inserts into brain.db.

    Importers MUST populate ``content`` and ``category`` (defaulting
    to ``"project"`` if the provider has no analog). ``scope`` is
    overridden by the CLI to ``"imported:<provider>[:<source>]"``
    unless the caller passes ``--no-quarantine``.
    """

    content: str
    category: str = "project"
    tags: List[str] = field(default_factory=list)
    confidence: float = 1.0
    source_id: Optional[str] = None
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    agent_id: Optional[str] = None

    def normalized_category(self) -> str:
        return self.category if self.category in VALID_CATEGORIES else "project"


@dataclass
class ImportResult:
    """Summary returned by an importer's ``parse`` method."""

    provider: str
    source_path: str
    records: List[MemoryRecord]
    warnings: List[str] = field(default_factory=list)
    skipped: int = 0


class ImporterError(Exception):
    """Raised when an importer can't parse the source."""


class BaseImporter(abc.ABC):
    """Interface every provider importer implements."""

    provider: str = ""        # e.g. "mem0", "json"
    file_extensions: tuple = ()  # informational; (".json",) for most

    @abc.abstractmethod
    def parse(self, source: Path) -> ImportResult:
        """Read ``source`` and yield an ImportResult. Raise
        ``ImporterError`` for unrecoverable parse failures.
        """
        raise NotImplementedError

    def iter_records(self, source: Path) -> Iterator[MemoryRecord]:
        """Convenience iterator wrapper around ``parse``."""
        for r in self.parse(source).records:
            yield r


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Callable[[], BaseImporter]] = {}


def register_importer(provider: str, factory: Callable[[], BaseImporter]) -> None:
    """Register an importer factory under a provider key."""
    _REGISTRY[provider] = factory


def get_importer(provider: str) -> BaseImporter:
    """Look up + instantiate the importer for a provider key."""
    factory = _REGISTRY.get(provider)
    if not factory:
        raise ImporterError(
            f"no importer registered for provider {provider!r}. "
            f"Available: {sorted(_REGISTRY)}"
        )
    return factory()


def list_importers() -> List[str]:
    """Return the registered provider keys (sorted)."""
    return sorted(_REGISTRY)


def _autoload() -> None:
    """Eagerly register every shipped importer. Called on module import."""
    from agentmemory.importers import json_importer, mem0_importer  # noqa: F401
    # Future: zep, cognee, letta, langchain modules register themselves
    # the same way (import-with-side-effect).


_autoload()
