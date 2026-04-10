"""brainctl configuration system.

Precedence: CLI flags > environment variables > config file > defaults.
Config file location: ~/.brainctl/config.toml (or $BRAINCTL_CONFIG).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # fallback
    except ImportError:
        tomllib = None  # type: ignore


CONFIG_DIR = Path.home() / ".brainctl"
CONFIG_FILE = Path(os.environ.get("BRAINCTL_CONFIG", str(CONFIG_DIR / "config.toml")))

DEFAULTS = {
    "db": {
        "path": str(Path.home() / "agentmemory" / "db" / "brain.db"),
    },
    "embedding": {
        "ollama_url": "http://localhost:11434/api/embed",
        "model": "nomic-embed-text",
        "dimensions": 768,
    },
    "maintenance": {
        "decay_interval_hours": 24,
        "compress_interval_hours": 72,
        "hard_cap": 10000,
        "hard_cap_target": 9000,
    },
}

_SAMPLE_CONFIG = """\
# brainctl configuration
# All values can be overridden by environment variables.
# Precedence: CLI flags > env vars > this file > defaults.

[db]
# path = "~/agentmemory/db/brain.db"  # or set BRAIN_DB env var

[embedding]
# ollama_url = "http://localhost:11434/api/embed"  # or BRAINCTL_OLLAMA_URL
# model = "nomic-embed-text"                       # or BRAINCTL_EMBED_MODEL
# dimensions = 768                                 # or BRAINCTL_EMBED_DIMENSIONS

[maintenance]
# decay_interval_hours = 24
# compress_interval_hours = 72
# hard_cap = 10000
# hard_cap_target = 9000
"""


def load() -> dict[str, Any]:
    """Load config from file, falling back to defaults."""
    config = {k: dict(v) for k, v in DEFAULTS.items()}

    if tomllib is None:
        # Apply env vars and return (no TOML parsing available)
        _apply_env(config)
        return config

    cfg_file = Path(os.environ.get("BRAINCTL_CONFIG", str(CONFIG_DIR / "config.toml")))
    if cfg_file.exists():
        try:
            with open(cfg_file, "rb") as f:
                file_config = tomllib.load(f)
            # Merge file config into defaults (shallow merge per section)
            for section, values in file_config.items():
                if section in config and isinstance(values, dict):
                    config[section].update(values)
                else:
                    config[section] = values
        except Exception:
            pass  # malformed config — fall back to defaults

    # Apply environment variable overrides
    _apply_env(config)

    return config


def _apply_env(config: dict[str, Any]) -> None:
    """Apply environment variable overrides to config in-place."""
    if "BRAIN_DB" in os.environ:
        config["db"]["path"] = os.environ["BRAIN_DB"]
    if "BRAINCTL_HOME" in os.environ:
        config["db"]["path"] = str(Path(os.environ["BRAINCTL_HOME"]) / "db" / "brain.db")
    if "BRAINCTL_OLLAMA_URL" in os.environ:
        config["embedding"]["ollama_url"] = os.environ["BRAINCTL_OLLAMA_URL"]
    if "BRAINCTL_EMBED_MODEL" in os.environ:
        config["embedding"]["model"] = os.environ["BRAINCTL_EMBED_MODEL"]
    if "BRAINCTL_EMBED_DIMENSIONS" in os.environ:
        config["embedding"]["dimensions"] = int(os.environ["BRAINCTL_EMBED_DIMENSIONS"])


def get(section: str, key: str, default=None):
    """Get a single config value."""
    return load().get(section, {}).get(key, default)


def init_config_file(force: bool = False) -> tuple[bool, str]:
    """Create the default config file. Returns (created: bool, path: str)."""
    cfg_file = Path(os.environ.get("BRAINCTL_CONFIG", str(CONFIG_DIR / "config.toml")))
    cfg_dir = cfg_file.parent
    if cfg_file.exists() and not force:
        return False, str(cfg_file)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(_SAMPLE_CONFIG)
    return True, str(cfg_file)


def show() -> dict:
    """Return current effective config with source annotations."""
    cfg_file = Path(os.environ.get("BRAINCTL_CONFIG", str(CONFIG_DIR / "config.toml")))
    cfg = load()
    cfg["_config_file"] = str(cfg_file)
    cfg["_config_file_exists"] = cfg_file.exists()
    cfg["_tomllib_available"] = tomllib is not None
    return cfg
