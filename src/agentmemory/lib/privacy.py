"""Privacy helpers — `<private>…</private>` tag redaction.

Inspired by claude-mem's privacy tag. Any content wrapped in
`<private>` tags is stripped before being stored in the brain. If the
entire payload is private, callers should skip the write altogether.

Usage:

    from agentmemory.lib.privacy import redact_private, is_all_private

    safe = redact_private(user_text)
    if is_all_private(user_text):
        return  # nothing to store
"""
from __future__ import annotations

import re

_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)
_PRIVATE_ONLY_RE = re.compile(r"^\s*<private>.*?</private>\s*$", re.DOTALL | re.IGNORECASE)


def redact_private(text: str | None) -> str:
    """Strip `<private>…</private>` blocks from text.

    Returns the cleaned string with redacted blocks replaced by a single
    space so surrounding words remain separated. Empty or None input
    returns an empty string.
    """
    if not text:
        return ""
    cleaned = _PRIVATE_RE.sub(" ", text)
    # Collapse runs of whitespace that redaction may have introduced.
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def has_private(text: str | None) -> bool:
    """Return True if `text` contains at least one `<private>…</private>` block."""
    if not text:
        return False
    return bool(_PRIVATE_RE.search(text))


def is_all_private(text: str | None) -> bool:
    """Return True if `text` is entirely wrapped in a single `<private>` block
    (ignoring surrounding whitespace). Used to short-circuit writes that would
    store nothing after redaction.
    """
    if not text:
        return False
    return bool(_PRIVATE_ONLY_RE.match(text)) or not redact_private(text)


__all__ = ["redact_private", "has_private", "is_all_private"]
