"""Tests for brainctl config system."""
import os
import tempfile
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BRAIN_DB", raising=False)
    monkeypatch.delenv("BRAINCTL_HOME", raising=False)
    monkeypatch.delenv("BRAINCTL_OLLAMA_URL", raising=False)
    monkeypatch.delenv("BRAINCTL_EMBED_MODEL", raising=False)
    monkeypatch.delenv("BRAINCTL_CONFIG", raising=False)


class TestConfigLoad:
    def test_defaults_returned_without_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCTL_CONFIG", str(tmp_path / "nonexistent.toml"))
        from agentmemory import config
        # reload to pick up monkeypatched env
        import importlib; importlib.reload(config)
        cfg = config.load()
        assert "db" in cfg
        assert "embedding" in cfg
        assert "maintenance" in cfg

    def test_env_overrides_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCTL_CONFIG", str(tmp_path / "nonexistent.toml"))
        monkeypatch.setenv("BRAINCTL_EMBED_MODEL", "custom-model")
        from agentmemory import config
        import importlib; importlib.reload(config)
        cfg = config.load()
        assert cfg["embedding"]["model"] == "custom-model"


class TestConfigInit:
    def test_init_creates_file(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.toml"
        monkeypatch.setenv("BRAINCTL_CONFIG", str(cfg_path))
        from agentmemory import config
        import importlib; importlib.reload(config)
        created, path = config.init_config_file()
        assert created is True
        assert cfg_path.exists()
        assert "brainctl" in cfg_path.read_text().lower()

    def test_init_no_overwrite_by_default(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("# existing")
        monkeypatch.setenv("BRAINCTL_CONFIG", str(cfg_path))
        from agentmemory import config
        import importlib; importlib.reload(config)
        created, path = config.init_config_file()
        assert created is False
        assert cfg_path.read_text() == "# existing"

    def test_show_returns_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAINCTL_CONFIG", str(tmp_path / "nonexistent.toml"))
        from agentmemory import config
        import importlib; importlib.reload(config)
        result = config.show()
        assert "_config_file" in result
        assert "_tomllib_available" in result
