"""Unit tests for the brainctl-mcp lifecycle hardening
(``agentmemory.lib.lifecycle``).

What we're protecting against
-----------------------------
The lifecycle module exists to prevent zombie ``brainctl-mcp``
processes from accumulating when MCP clients (Codex.app, Claude Code,
Claude Desktop) crash or hold idle pipes. We empirically observed 25
stale child processes piled up under a single Codex ``app-server`` —
each one a latent SQLite-lock contention risk on ``brain.db``.

These tests pin the watchdog's:
  * idempotent install
  * env-var disable knob
  * clamping of tunables to safe minima
  * activity-tracking counter
  * idle-timeout trigger (mocked, so the test runs in milliseconds)
  * parent-PID change trigger (mocked)
  * signal-handler installation
"""
from __future__ import annotations

import os
import signal
import time

import pytest

from agentmemory.lib import lifecycle


@pytest.fixture(autouse=True)
def _reset_state():
    """Every test starts from a fresh module state — the watchdog is a
    daemon thread that we can't safely restart, but we can pretend it
    was never installed for the purposes of unit testing the install
    contract."""
    lifecycle._reset_for_tests()
    yield
    lifecycle._reset_for_tests()


# ---------------------------------------------------------------------------
# install_watchdog contract
# ---------------------------------------------------------------------------


def test_install_watchdog_is_idempotent():
    """First call returns True, subsequent calls return False."""
    assert lifecycle.install_watchdog() is True
    assert lifecycle.watchdog_running() is True
    assert lifecycle.install_watchdog() is False
    assert lifecycle.install_watchdog() is False


def test_install_watchdog_disabled_by_env(monkeypatch):
    """BRAINCTL_MCP_DISABLE_WATCHDOG=1 must be a hard kill switch."""
    monkeypatch.setenv("BRAINCTL_MCP_DISABLE_WATCHDOG", "1")
    assert lifecycle.install_watchdog() is False
    assert lifecycle.watchdog_running() is False


def test_install_watchdog_records_initial_ppid():
    """The watchdog needs to remember the parent PID at install time so
    it can detect re-parenting later. This is the whole point of the
    parent-death detector."""
    expected = os.getppid()
    lifecycle.install_watchdog()
    assert lifecycle.initial_ppid() == expected


def test_install_watchdog_seeds_activity_clock():
    """Without seeding, an MCP server that's never received a tool call
    would get killed by the idle timer the moment its first idle window
    elapses — even though the parent never had a chance to send a
    request. Seeding prevents that footgun."""
    lifecycle.install_watchdog()
    # The clock should be very recent — within a second.
    assert lifecycle.last_activity_age_sec() < 1.0


# ---------------------------------------------------------------------------
# touch_activity
# ---------------------------------------------------------------------------


def test_touch_activity_resets_idle_age():
    """Each tool call must reset the idle timer."""
    lifecycle.install_watchdog()
    time.sleep(0.05)
    assert lifecycle.last_activity_age_sec() > 0.0
    lifecycle.touch_activity()
    # After touch, age should round to ~0.
    assert lifecycle.last_activity_age_sec() < 0.05


def test_touch_activity_works_without_install():
    """Activity tracking must be a no-op precondition — calling
    ``touch_activity`` before the watchdog runs (e.g. tests, or a
    server that explicitly disables the watchdog) must not crash."""
    lifecycle.touch_activity()
    # And subsequent install must still seed cleanly.
    assert lifecycle.install_watchdog() is True


# ---------------------------------------------------------------------------
# Tunable parsing — env vars must be clamped to safe minima so a typo
# can't result in e.g. "watchdog kills server every 0 seconds".
# ---------------------------------------------------------------------------


def test_idle_timeout_default_is_one_hour():
    assert lifecycle.idle_timeout_sec() == 3600.0


def test_idle_timeout_clamps_to_minimum(monkeypatch):
    monkeypatch.setenv("BRAINCTL_MCP_IDLE_TIMEOUT_SEC", "5")
    # Floor is 60 seconds — anything lower would be operationally
    # dangerous (a slow client could be killed mid-thought).
    assert lifecycle.idle_timeout_sec() == 60.0


def test_idle_timeout_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("BRAINCTL_MCP_IDLE_TIMEOUT_SEC", "not-a-number")
    assert lifecycle.idle_timeout_sec() == 3600.0


def test_parent_poll_default_is_five_seconds():
    assert lifecycle.parent_poll_sec() == 5.0


def test_parent_poll_clamps_to_minimum(monkeypatch):
    monkeypatch.setenv("BRAINCTL_MCP_PARENT_POLL_SEC", "0")
    # Anything below 1 s would burn CPU.
    assert lifecycle.parent_poll_sec() == 1.0


# ---------------------------------------------------------------------------
# Watchdog loop logic — we test the loop's decision logic directly
# instead of waiting for real wall-clock conditions. This keeps tests
# in the millisecond range while still pinning the behavior.
# ---------------------------------------------------------------------------


def _patch_loop_to_run_once(monkeypatch):
    """Replace ``time.sleep`` so the watchdog loop runs exactly one
    iteration and a second sleep raises StopIteration to break out."""
    calls = {"n": 0}

    def fake_sleep(_secs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise StopIteration  # break the while-True loop

    monkeypatch.setattr(lifecycle.time, "sleep", fake_sleep)
    return calls


def test_watchdog_exits_on_orphan(monkeypatch):
    """If our parent becomes PID 1 (launchd/init), we got reparented —
    bail out. This is the pure-orphan case: parent process crashed."""
    _patch_loop_to_run_once(monkeypatch)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 1)

    exit_called = {}
    def fake_exit(reason, code=0):
        exit_called["reason"] = reason
        raise SystemExit(code)
    monkeypatch.setattr(lifecycle, "_exit_clean", fake_exit)

    lifecycle._INITIAL_PPID = 12345  # was parented to 12345, now to 1
    with pytest.raises(SystemExit):
        lifecycle._watchdog_loop()
    assert "parent gone" in exit_called["reason"]


def test_watchdog_exits_on_reparent(monkeypatch):
    """The Codex case where ppid actually changes — same fix path."""
    _patch_loop_to_run_once(monkeypatch)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 99999)

    exit_called = {}
    def fake_exit(reason, code=0):
        exit_called["reason"] = reason
        raise SystemExit(code)
    monkeypatch.setattr(lifecycle, "_exit_clean", fake_exit)

    lifecycle._INITIAL_PPID = 12345
    with pytest.raises(SystemExit):
        lifecycle._watchdog_loop()
    assert "12345" in exit_called["reason"]
    assert "99999" in exit_called["reason"]


def test_watchdog_exits_on_idle_timeout(monkeypatch):
    """The Codex idle-pipe case: parent stays alive, never sends
    requests, server should self-terminate."""
    _patch_loop_to_run_once(monkeypatch)

    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 12345)
    monkeypatch.setattr(lifecycle, "idle_timeout_sec", lambda: 60.0)
    monkeypatch.setattr(lifecycle, "parent_poll_sec", lambda: 1.0)
    monkeypatch.setattr(lifecycle, "last_activity_age_sec", lambda: 9999.0)

    exit_called = {}
    def fake_exit(reason, code=0):
        exit_called["reason"] = reason
        raise SystemExit(code)
    monkeypatch.setattr(lifecycle, "_exit_clean", fake_exit)

    lifecycle._INITIAL_PPID = 12345
    with pytest.raises(SystemExit):
        lifecycle._watchdog_loop()
    assert "idle timeout" in exit_called["reason"]


def test_watchdog_does_not_exit_when_healthy(monkeypatch):
    """Healthy state: parent unchanged, recent activity. The loop
    should iterate without calling _exit_clean."""
    _patch_loop_to_run_once(monkeypatch)

    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 12345)
    monkeypatch.setattr(lifecycle, "idle_timeout_sec", lambda: 3600.0)
    monkeypatch.setattr(lifecycle, "parent_poll_sec", lambda: 1.0)
    monkeypatch.setattr(lifecycle, "last_activity_age_sec", lambda: 1.0)

    exit_called = {"called": False}
    def fake_exit(_reason, _code=0):
        exit_called["called"] = True
        raise SystemExit(0)
    monkeypatch.setattr(lifecycle, "_exit_clean", fake_exit)

    lifecycle._INITIAL_PPID = 12345
    with pytest.raises(StopIteration):
        # StopIteration comes from our patched sleep — meaning the loop
        # iterated once cleanly without calling _exit_clean, then tried
        # to sleep a second time.
        lifecycle._watchdog_loop()
    assert exit_called["called"] is False


# ---------------------------------------------------------------------------
# Signal handler installation
# ---------------------------------------------------------------------------


def test_install_signal_handlers_is_idempotent():
    assert lifecycle.install_signal_handlers() is True
    # Second call returns False (already installed) — but actual
    # signal.signal calls are safe to repeat, so this is just a
    # bookkeeping nicety.
    assert lifecycle.install_signal_handlers() is False


def test_signal_handler_calls_os_exit(monkeypatch):
    """SIGTERM/SIGHUP must trigger os._exit(0).

    We use os._exit (not sys.exit) on purpose: when the asyncio event
    loop is awaiting stdin in stdio_server, sys.exit's SystemExit does
    NOT always propagate out — empirically observed: the process kept
    running until SIGKILL. Since the canonical server is
    connection-per-call, there is no persistent state to flush, so a
    hard exit gives MCP clients deterministic shutdown semantics."""
    handlers = {}

    def fake_signal(sig, handler):
        handlers[sig] = handler
        return None

    monkeypatch.setattr(lifecycle.signal, "signal", fake_signal)

    # Replace os._exit with something we can observe — without it the
    # test process would actually die.
    exit_calls = []
    monkeypatch.setattr(lifecycle.os, "_exit", lambda code: exit_calls.append(code))

    lifecycle.install_signal_handlers()
    assert signal.SIGTERM in handlers

    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert exit_calls == [0]


def test_install_signal_handlers_skips_missing_sighup(monkeypatch):
    """On Windows ``signal.SIGHUP`` is absent. Looking it up inside the
    iterable raised ``AttributeError`` before the try/except could run,
    so the call crashed at import-time of the MCP server."""
    handlers = {}

    def fake_signal(sig, handler):
        handlers[sig] = handler
        return None

    monkeypatch.setattr(lifecycle.signal, "signal", fake_signal)
    monkeypatch.delattr(lifecycle.signal, "SIGHUP", raising=False)

    assert lifecycle.install_signal_handlers() is True
    assert signal.SIGTERM in handlers
