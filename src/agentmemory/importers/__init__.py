"""Provider import adapters.

Each importer parses a third-party memory provider's export format and
yields ``MemoryRecord`` instances that the ``brainctl import`` CLI
inserts into brain.db. By default imports land in a quarantine scope
``imported:<provider>[:<source>]`` so the agent's primary scope stays
clean until the user explicitly promotes individual memories.
"""
from agentmemory.importers.base import (
    BaseImporter,
    MemoryRecord,
    ImportResult,
    ImporterError,
    get_importer,
    list_importers,
)

__all__ = [
    "BaseImporter",
    "MemoryRecord",
    "ImportResult",
    "ImporterError",
    "get_importer",
    "list_importers",
]
