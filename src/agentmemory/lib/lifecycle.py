"""
Lifecycle hardening for ``brainctl-mcp`` stdio servers.

Why this exists
---------------
MCP stdio servers are spawned as long-lived child processes by clients
like Codex.app, Claude Code, Claude Desktop. In practice we observed two
failure modes that pile up zombie processes and intermittently hold
SQLite locks on ``brain.db``:

  1. Parent dies, child does not — ``stdio_server`` does not always
     terminate when stdin reaches EOF. Empirically, ``echo "" |
     brainctl-mcp`` stays alive for >6 s after stdin close. If the
     parent crashes, the child is reparented to launchd/init and
     loiters forever.

  2. Parent stays alive but stops talking — Codex.app's ``app-server``
     keeps idle MCP pipes connected indefinitely. ``getppid()`` still
     returns the parent, so a parent-death watchdog alone won't help.
     Each idle session costs FDs, RAM, and risks lock contention any
     time it does briefly attempt a write.

This module installs a single daemon watchdog thread that exits the
process cleanly under three conditions, all of which are safe for an
idempotent connection-per-call SQLite server:

  * parent process changed (we got reparented, parent crashed)
  * parent process is launchd/init (PID 1)
  * MCP request inactivity exceeded ``BRAINCTL_MCP_IDLE_TIMEOUT_SEC``

Plus it installs signal handlers that translate SIGTERM/SIGHUP into
``os._exit(0)``. We deliberately skip ``sys.exit`` / ``atexit`` here:
when the asyncio event loop is awaiting stdin inside ``stdio_server``,
``SystemExit`` does not propagate out reliably (verified empirically:
SIGTERM had no effect, SIGKILL was needed). The canonical server is
connection-per-call, so there is no persistent SQLite state to flush
and a hard exit gives MCP clients deterministic shutdown semantics.

Tunables (env vars)
-------------------
``BRAINCTL_MCP_IDLE_TIMEOUT_SEC``  default 0 (disabled). Accepts 0 or
    any value >= 60. Closes issue #108: stdio clients like Claude
    Desktop own the process lifecycle, so killing the server on idle
    leaves the client without memory tools until manual restart.
    The parent-death watchdog (below) still catches the orphan case,
    which is the failure this whole module exists to address. Set
    a positive value when running brainctl-mcp under a parent that
    keeps idle pipes alive indefinitely (e.g. some sandboxed sandboxes)
    and you want explicit per-process idle reaping.
``BRAINCTL_MCP_PARENT_POLL_SEC``   default 5. Min 1.
``BRAINCTL_MCP_DISABLE_WATCHDOG``  set to "1" to fully disable.

Design constraints
------------------
* Pure stdlib — no SQLite calls in the watchdog (the canonical server
  is connection-per-call; there is nothing to checkpoint).
* Idempotent — ``install_watchdog`` is safe to call multiple times.
* Thread-safe — single ``threading.Lock`` for installation guard.
* Daemon thread — does not delay normal interpreter shutdown.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_LAST_ACTIVITY: float = 0.0
_INITIAL_PPID: int = 0
_WATCHDOG_STARTED: bool = False
_SIGNALS_INSTALLED: bool = False
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def touch_activity() -> None:
    """Record an MCP request — call from the tool-dispatch path."""
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.monotonic()


def install_watchdog() -> bool:
    """Start the lifecycle watchdog as a daemon thread.

    Idempotent — second and later calls are no-ops. Returns True if
    the watchdog was started by this call, False if it was already
    running or disabled by env var.
    """
    global _INITIAL_PPID, _WATCHDOG_STARTED, _LAST_ACTIVITY
    with _LOCK:
        if _WATCHDOG_STARTED:
            return False
        if os.environ.get("BRAINCTL_MCP_DISABLE_WATCHDOG") == "1":
            return False
        _INITIAL_PPID = os.getppid()
        # Seed activity at install time so a brand-new server gets the
        # full idle window before any first tool call lands.
        _LAST_ACTIVITY = time.monotonic()
        t = threading.Thread(
            target=_watchdog_loop,
            name="brainctl-mcp-watchdog",
            daemon=True,
        )
        t.start()
        _WATCHDOG_STARTED = True
        return True


def install_signal_handlers() -> bool:
    """Translate SIGTERM/SIGHUP into a hard ``os._exit(0)``.

    Why ``os._exit`` and not ``sys.exit``: see the module docstring —
    in short, ``SystemExit`` does not propagate out of the asyncio
    ``stdio_server`` await reliably, so external SIGTERM was a no-op
    until we switched to ``os._exit``. The canonical server is
    connection-per-call, so skipping ``atexit`` is safe.

    SIGINT (Ctrl-C) is left alone — the default Python handler raises
    KeyboardInterrupt which the asyncio event loop will surface
    cleanly. Returns True if handlers were installed.
    """
    global _SIGNALS_INSTALLED
    with _LOCK:
        if _SIGNALS_INSTALLED:
            return False

    def _handler(signum, _frame):
        # Print first because os._exit skips stdio flushing.
        try:
            sys.stderr.write(
                f"[brainctl-mcp] caught signal {signum}, shutting down\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
        # Why os._exit instead of sys.exit:
        # When the asyncio event loop is awaiting stdin in stdio_server,
        # sys.exit's SystemExit doesn't always propagate out of the
        # await — observed empirically: SIGTERM did nothing, SIGKILL was
        # required. The canonical server is connection-per-call (no
        # persistent state to flush) so a hard exit is safe and gives
        # MCP clients deterministic shutdown semantics.
        os._exit(0)

    installed_any = False
    candidates = [signal.SIGTERM]
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        candidates.append(sighup)
    for sig in candidates:
        try:
            signal.signal(sig, _handler)
            installed_any = True
        except Exception:
            # Some signals are not settable from non-main threads or
            # on some platforms. Soft-fail.
            pass
    _SIGNALS_INSTALLED = installed_any
    return installed_any


# ---------------------------------------------------------------------------
# Introspection (used by tests + the cleanup CLI)
# ---------------------------------------------------------------------------


def watchdog_running() -> bool:
    return _WATCHDOG_STARTED


def initial_ppid() -> int:
    return _INITIAL_PPID


def last_activity_age_sec() -> float:
    if _LAST_ACTIVITY <= 0:
        return 0.0
    return max(0.0, time.monotonic() - _LAST_ACTIVITY)


def idle_timeout_sec() -> float:
    """Idle-timeout in seconds. 0 = disabled (default).

    Issue #108: stdio MCP clients (Claude Desktop, Codex, Cursor) own
    the process lifecycle and re-spawn the server on demand. An idle
    timeout that kills the server while the client is still attached
    leaves the agent without memory tools until the client manually
    re-spawns, with no upstream warning. We disable by default and
    let operators opt in via the env var when their parent process
    is the kind that keeps idle pipes alive forever.

    Accepts 0 (disabled) or any value >= 60. Values in 1..59 clamp up
    to 60 (a sub-minute idle window can kill the server mid-thought
    for slow LLMs).
    """
    raw = os.environ.get("BRAINCTL_MCP_IDLE_TIMEOUT_SEC")
    if raw is None or raw == "":
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    if v <= 0:
        return 0.0
    return max(60.0, v)


def parent_poll_sec() -> float:
    return _read_float_env("BRAINCTL_MCP_PARENT_POLL_SEC", default=5.0, minimum=1.0)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_float_env(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _exit_clean(reason: str, code: int = 0) -> None:
    """Send ourselves SIGTERM so signal handlers + atexit run, then
    fall back to ``os._exit`` after a short grace period."""
    try:
        sys.stderr.write(f"[brainctl-mcp watchdog] exiting: {reason}\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        pass
    # Give signal handler / atexit ~2 s to run, then hard-exit.
    time.sleep(2.0)
    os._exit(code)


def _watchdog_loop() -> None:
    poll = parent_poll_sec()
    idle = idle_timeout_sec()
    while True:
        time.sleep(poll)

        # 1. Parent-death detection. If we got reparented (ppid changed)
        #    or our new parent is launchd/init (1), bail.
        try:
            ppid = os.getppid()
        except OSError:
            ppid = 1
        if ppid == 1 or (_INITIAL_PPID != 0 and ppid != _INITIAL_PPID):
            _exit_clean(
                f"parent gone (initial ppid {_INITIAL_PPID} -> current {ppid})"
            )
            return

        # 2. Idle timeout. Only trips after at least one idle window has
        #    elapsed since install or the last touch_activity(). When
        #    idle == 0 the check is disabled entirely (issue #108).
        if idle > 0:
            age = last_activity_age_sec()
            if age > idle:
                _exit_clean(f"idle timeout: {age:.0f}s > {idle:.0f}s")
                return


# ---------------------------------------------------------------------------
# Test hooks (NOT public API — only for the unit tests in this repo)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Reset module state so successive unit tests get a clean slate.
    Do not call this from production code."""
    global _LAST_ACTIVITY, _INITIAL_PPID, _WATCHDOG_STARTED, _SIGNALS_INSTALLED
    with _LOCK:
        _LAST_ACTIVITY = 0.0
        _INITIAL_PPID = 0
        _WATCHDOG_STARTED = False
        _SIGNALS_INSTALLED = False
