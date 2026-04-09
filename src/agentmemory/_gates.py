"""Shared write gate logic used by both _impl.py and mcp_server.py."""

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_write_decision_module():
    """Load the write_decision module from the built-in lib directory.

    Returns the module or None if loading fails.
    """
    # Try user-provided override first, then built-in
    user_path = Path.home() / "agentmemory" / "bin" / "lib" / "write_decision.py"
    builtin_path = Path(__file__).parent / "lib" / "write_decision.py"

    for path in (user_path, builtin_path):
        if path.exists():
            try:
                spec = importlib.util.spec_from_file_location("write_decision", str(path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception as exc:
                logger.debug("Failed to load write_decision from %s: %s", path, exc)
                continue

    return None


def run_write_gate(blob, confidence, category, scope, get_vec_db_fn, force=False):
    """Run the W(m) write worthiness gate.

    Args:
        blob: Embedding bytes for the candidate memory
        confidence: Memory confidence score
        category: Memory category string
        scope: Memory scope string
        get_vec_db_fn: Callable that returns a vec DB connection (or None)
        force: If True, skip the gate

    Returns:
        (worthiness_score, worthiness_reason, worthiness_components)
        - worthiness_score: float or None if gate didn't run
        - worthiness_reason: empty string = approved, non-empty = rejected
        - worthiness_components: dict breakdown
    """
    if force or not blob:
        return (None, "", {})

    wd = load_write_decision_module()
    if not wd:
        logger.debug("write_decision module not available — gate skipped")
        return (None, "", {})

    vdb = get_vec_db_fn()
    if not vdb:
        return (None, "", {})

    try:
        return wd.gate_write(
            candidate_blob=blob,
            confidence=confidence,
            temporal_class=None,
            category=category,
            scope=scope,
            db_vec=vdb,
            force=False,
        )
    except Exception as exc:
        logger.debug("Write gate execution failed: %s", exc)
        return (None, "", {})
    finally:
        vdb.close()
