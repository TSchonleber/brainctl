from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_HOME = Path.home() / "agentmemory"


def get_brain_home() -> Path:
    return Path(os.environ.get("BRAINCTL_HOME", str(_DEFAULT_HOME))).expanduser()


def get_db_path() -> Path:
    return Path(os.environ.get("BRAIN_DB", str(get_brain_home() / "db" / "brain.db"))).expanduser()


def get_blobs_dir() -> Path:
    return Path(os.environ.get("BRAINCTL_BLOBS_DIR", str(get_brain_home() / "blobs"))).expanduser()


def get_backups_dir() -> Path:
    return Path(os.environ.get("BRAINCTL_BACKUPS_DIR", str(get_brain_home() / "backups"))).expanduser()
