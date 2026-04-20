from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_HOME = Path.home() / "agentmemory"


def get_brain_home() -> Path:
    return Path(os.environ.get("BRAINCTL_HOME", str(_DEFAULT_HOME))).expanduser()


def get_db_path() -> Path:
    # Go-forward canonical env var is BRAINCTL_DB (matches the family —
    # BRAINCTL_HOME, BRAINCTL_BLOBS_DIR, BRAINCTL_BACKUPS_DIR). BRAIN_DB is
    # the historical name kept as a deprecated alias so users with
    # existing plugin configs keep working. BRAINCTL_DB wins when both
    # are set. The goose / pi / opencode / gemini-cli plugins all ship
    # BRAINCTL_DB in their manifests; the claude-code / openclaw / cursor
    # / codex / eliza plugins still use BRAIN_DB and will be updated in
    # subsequent docs passes — no config change required for their users.
    default = str(get_brain_home() / "db" / "brain.db")
    return Path(
        os.environ.get("BRAINCTL_DB")
        or os.environ.get("BRAIN_DB")
        or default
    ).expanduser()


def get_blobs_dir() -> Path:
    return Path(os.environ.get("BRAINCTL_BLOBS_DIR", str(get_brain_home() / "blobs"))).expanduser()


def get_backups_dir() -> Path:
    return Path(os.environ.get("BRAINCTL_BACKUPS_DIR", str(get_brain_home() / "backups"))).expanduser()
