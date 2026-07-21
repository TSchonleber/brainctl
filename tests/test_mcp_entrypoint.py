"""Regression tests for the optional MCP console-script boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentmemory import mcp_entrypoint


def test_missing_mcp_extra_prints_actionable_install_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_mcp(_module_name: str):
        raise ModuleNotFoundError("No module named 'mcp'", name="mcp")

    monkeypatch.setattr(mcp_entrypoint, "import_module", missing_mcp)

    assert mcp_entrypoint.run() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "brainctl-mcp requires the MCP extra" in captured.err
    assert "pip install 'brainctl[mcp]'" in captured.err
    assert "pipx install --force 'brainctl[mcp]'" in captured.err
    assert "Traceback" not in captured.err


def test_unrelated_missing_dependency_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_unrelated_dependency(_module_name: str):
        raise ModuleNotFoundError(
            "No module named 'unexpected_dependency'",
            name="unexpected_dependency",
        )

    monkeypatch.setattr(
        mcp_entrypoint,
        "import_module",
        missing_unrelated_dependency,
    )

    with pytest.raises(ModuleNotFoundError, match="unexpected_dependency"):
        mcp_entrypoint.run()


def test_delegates_to_mcp_server_when_extra_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def load_server(module_name: str):
        calls.append(module_name)
        return SimpleNamespace(run=lambda: 23)

    monkeypatch.setattr(mcp_entrypoint, "import_module", load_server)

    assert mcp_entrypoint.run() == 23
    assert calls == ["agentmemory.mcp_server"]
