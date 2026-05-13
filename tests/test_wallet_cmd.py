"""Tests for the brainctl-managed wallet (commands/wallet.py + sign auto-discovery).

Covers the 2.3.2 managed-wallet UX:

  * ``wallet_new_impl`` produces a valid 64-int Solana keystore
  * file permissions are 0600 (skipped on Windows)
  * ``--force`` overwrites; without it errors loudly; non-TTY without
    ``--yes`` errors with "use --yes" hint instead of hanging
  * ``wallet_address_impl`` returns the correct address; CLI prints
    ONLY the address (no extra noise — pipe-friendly)
  * ``wallet_balance_impl`` mocks the RPC and returns the parsed value
  * ``wallet_import_impl`` validates the keystore format and rejects
    garbage (wrong type, wrong length, non-int entries, not JSON)
  * ``wallet_rm_impl`` removes the keystore; missing wallet is a no-op
  * ``wallet_export_impl`` copies bytes verbatim and chmod 0600 the copy
  * ``wallet_show_impl`` works before AND after ``wallet new``
  * ``wallet_onboard_impl`` returns next-steps incl. Solscan link
  * ``export --sign`` auto-uses the managed wallet when no
    ``--keystore`` is passed
  * ``export --sign --auto-setup-wallet`` creates a fresh wallet and
    signs in one go
  * ``export --sign --pin-onchain`` with mocked 0-SOL balance prints
    the friendly stderr message, exits 0, marks
    ``pin_skipped_reason="zero_balance"``

Every test pins ``BRAINCTL_WALLET_PATH`` to a tmp_path-derived file
so the test suite cannot pollute (or destroy) the real
``~/.brainctl/wallet.json`` on this machine.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Skip the entire module gracefully if solders isn't available.
solders = pytest.importorskip("solders", reason="brainctl[signing] required")
from solders.keypair import Keypair  # noqa: E402

from agentmemory import signing  # noqa: E402
from agentmemory.commands import wallet as wallet_mod  # noqa: E402
from agentmemory.commands import sign as sign_mod  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
INIT_SCHEMA = ROOT / "db" / "init_schema.sql"

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Autouse fixture: NEVER let the test suite touch the real wallet.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_wallet_path(tmp_path, monkeypatch):
    """Pin BRAINCTL_WALLET_PATH so every test is sandboxed.

    Without this, a single buggy test could clobber ~/.brainctl/wallet.json
    on the developer's machine. Subprocess-spawning tests get this via
    the env_extra dict passed to _run_cli (the env var inherits through
    monkeypatch.setenv into os.environ for the parent, and into the
    subprocess via env=os.environ.copy()).
    """
    target = tmp_path / "wallet-iso.json"
    monkeypatch.setenv("BRAINCTL_WALLET_PATH", str(target))
    # Defensive: also clear the legacy env so 2.3.0-style fallback can't
    # accidentally pick up a path from the developer's shell.
    monkeypatch.delenv("BRAINCTL_SIGNING_KEY_PATH", raising=False)
    yield target


def _make_db(tmp_path: Path) -> Path:
    """Create a temp brain.db with the production schema and 2 test rows."""
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(INIT_SCHEMA.read_text())
    ts = "2026-04-16T00:00:00"
    conn.execute(
        "INSERT INTO agents(id,display_name,agent_type,status,created_at,updated_at) "
        "VALUES (?, ?, 'test', 'active', ?, ?)",
        ("default", "default", ts, ts),
    )
    conn.execute(
        "INSERT INTO memories(agent_id,category,scope,content,created_at,updated_at) "
        "VALUES ('default', 'lesson', 'global', 'first', ?, ?)", (ts, ts),
    )
    conn.execute(
        "INSERT INTO memories(agent_id,category,scope,content,created_at,updated_at) "
        "VALUES ('default', 'lesson', 'global', 'second', ?, ?)", (ts, ts),
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# 1. wallet_new_impl
# ---------------------------------------------------------------------------

def test_wallet_new_creates_valid_solana_keystore(_isolate_wallet_path):
    res = wallet_mod.wallet_new_impl()
    assert res["ok"] is True
    assert res["address"]
    assert Path(res["path"]).exists()

    # Re-load with solders directly: it must parse as a 64-byte Keypair
    # and the derived pubkey must match what wallet_new_impl returned.
    kp = signing.load_keystore(res["path"])
    assert str(kp.pubkey()) == res["address"]


def test_wallet_new_writes_64_int_array(_isolate_wallet_path):
    res = wallet_mod.wallet_new_impl()
    raw = json.loads(Path(res["path"]).read_text())
    assert isinstance(raw, list)
    assert len(raw) == 64
    assert all(isinstance(x, int) and 0 <= x <= 255 for x in raw)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX permissions only")
def test_wallet_new_keystore_is_chmod_0600(_isolate_wallet_path):
    res = wallet_mod.wallet_new_impl()
    mode = stat.S_IMODE(Path(res["path"]).stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX permissions only")
def test_wallet_new_parent_dir_is_chmod_0700(_isolate_wallet_path, tmp_path):
    nested = tmp_path / "nested-dir" / "wallet.json"
    res = wallet_mod.wallet_new_impl(str(nested))
    assert res["ok"] is True
    parent_mode = stat.S_IMODE(nested.parent.stat().st_mode)
    assert parent_mode == 0o700, f"parent dir mode: {oct(parent_mode)}"


def test_wallet_new_refuses_to_overwrite_without_force(_isolate_wallet_path):
    first = wallet_mod.wallet_new_impl()
    assert first["ok"] is True
    second = wallet_mod.wallet_new_impl()
    assert second["ok"] is False
    assert "already exists" in second["error"]
    # The first wallet must still be intact (we did not clobber it).
    raw = json.loads(Path(first["path"]).read_text())
    kp = signing.load_keystore(first["path"])
    assert str(kp.pubkey()) == first["address"]


def test_wallet_new_force_overwrites(_isolate_wallet_path):
    first = wallet_mod.wallet_new_impl()
    second = wallet_mod.wallet_new_impl(force=True)
    assert second["ok"] is True
    # Different address — proves the keystore was actually replaced.
    assert second["address"] != first["address"]


# ---------------------------------------------------------------------------
# 2. wallet_address_impl — pipe-friendly output via the CLI handler
# ---------------------------------------------------------------------------

def test_wallet_address_returns_just_the_address(_isolate_wallet_path):
    new_res = wallet_mod.wallet_new_impl()
    addr_res = wallet_mod.wallet_address_impl()
    assert addr_res["ok"] is True
    assert addr_res["address"] == new_res["address"]


def test_wallet_address_cli_prints_only_address(tmp_path, capsys, _isolate_wallet_path):
    """The CLI handler must print exactly the address, nothing else.

    This is what makes ``$(brainctl wallet address)`` work cleanly.
    """
    res = wallet_mod.wallet_new_impl()
    expected_addr = res["address"]

    class _Args:
        path = None
    with pytest.raises(SystemExit) as exc:
        wallet_mod.cmd_wallet_address(_Args())
    assert exc.value.code == 0
    captured = capsys.readouterr()
    # stdout = address + a single trailing newline. No banner, no warning.
    assert captured.out.strip() == expected_addr
    assert captured.out.count("\n") == 1
    # stderr stays empty for the happy path.
    assert captured.err == ""


def test_wallet_address_missing_wallet_errors(_isolate_wallet_path):
    res = wallet_mod.wallet_address_impl()
    assert res["ok"] is False
    assert "no wallet" in res["error"].lower()


# ---------------------------------------------------------------------------
# 3. wallet_balance_impl — RPC mocked
# ---------------------------------------------------------------------------

def test_wallet_balance_with_mocked_rpc(_isolate_wallet_path, monkeypatch):
    wallet_mod.wallet_new_impl()
    # The 'getBalance' RPC returns {"context": {...}, "value": <lamports>}.
    captured = {}

    def fake_rpc(url, method, params, *, timeout=30.0):
        captured["url"] = url
        captured["method"] = method
        captured["params"] = params
        if method == "getBalance":
            return {"context": {"slot": 1}, "value": 1_500_000_000}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(signing, "_rpc_call", fake_rpc)
    res = wallet_mod.wallet_balance_impl(rpc_url="http://fake")
    assert res["ok"] is True
    assert res["lamports"] == 1_500_000_000
    assert res["sol"] == pytest.approx(1.5)
    assert captured["method"] == "getBalance"
    assert captured["url"] == "http://fake"


def test_wallet_balance_zero_sol(_isolate_wallet_path, monkeypatch):
    wallet_mod.wallet_new_impl()
    monkeypatch.setattr(
        signing, "_rpc_call",
        lambda u, m, p, *, timeout=30.0: {"context": {}, "value": 0},
    )
    res = wallet_mod.wallet_balance_impl(rpc_url="http://fake")
    assert res["ok"] is True
    assert res["lamports"] == 0
    assert res["sol"] == 0.0


def test_wallet_balance_rpc_failure_is_structured(_isolate_wallet_path, monkeypatch):
    wallet_mod.wallet_new_impl()
    monkeypatch.setattr(
        signing, "_rpc_call",
        lambda u, m, p, *, timeout=30.0: (_ for _ in ()).throw(RuntimeError("rpc down")),
    )
    res = wallet_mod.wallet_balance_impl(rpc_url="http://fake")
    assert res["ok"] is False
    assert "rpc down" in res["error"]


# ---------------------------------------------------------------------------
# 4. wallet_show_impl
# ---------------------------------------------------------------------------

def test_wallet_show_when_missing_returns_friendly_payload(_isolate_wallet_path):
    res = wallet_mod.wallet_show_impl(fetch_balance=False)
    # Important: must NOT raise / return ok=False just because the
    # wallet doesn't exist yet. show is the diagnostic — has to be safe.
    assert res["ok"] is True
    assert res["exists"] is False
    assert res["address"] is None
    assert "wallet new" in res["warning"]


def test_wallet_show_when_present_returns_full_info(_isolate_wallet_path):
    new_res = wallet_mod.wallet_new_impl()
    res = wallet_mod.wallet_show_impl(fetch_balance=False)
    assert res["ok"] is True
    assert res["exists"] is True
    assert res["address"] == new_res["address"]
    assert res["mtime"] is not None
    if not IS_WINDOWS:
        assert res["mode"] == "0o600"


def test_wallet_show_balance_failure_does_not_kill_command(_isolate_wallet_path, monkeypatch):
    """A balance-fetch failure must not break wallet_show.

    The local info (address, perms, mtime) is still useful when the
    user is offline.
    """
    wallet_mod.wallet_new_impl()
    monkeypatch.setattr(
        signing, "_rpc_call",
        lambda u, m, p, *, timeout=30.0: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    res = wallet_mod.wallet_show_impl(fetch_balance=True)
    assert res["ok"] is True
    assert res["exists"] is True
    assert res["address"] is not None
    assert res["balance_error"] is not None
    assert res["sol"] is None


# ---------------------------------------------------------------------------
# 5. wallet_export_impl
# ---------------------------------------------------------------------------

def test_wallet_export_copies_bytes_verbatim(_isolate_wallet_path, tmp_path):
    new_res = wallet_mod.wallet_new_impl()
    backup = tmp_path / "backup.json"
    res = wallet_mod.wallet_export_impl(str(backup))
    assert res["ok"] is True
    assert backup.exists()
    # Contents are byte-identical to the source.
    src_bytes = Path(new_res["path"]).read_bytes()
    dst_bytes = backup.read_bytes()
    assert src_bytes == dst_bytes
    if not IS_WINDOWS:
        mode = stat.S_IMODE(backup.stat().st_mode)
        assert mode == 0o600


def test_wallet_export_refuses_to_overwrite_without_force(_isolate_wallet_path, tmp_path):
    wallet_mod.wallet_new_impl()
    backup = tmp_path / "backup.json"
    backup.write_text("placeholder")
    res = wallet_mod.wallet_export_impl(str(backup))
    assert res["ok"] is False
    assert "already exists" in res["error"]
    # Placeholder content untouched.
    assert backup.read_text() == "placeholder"


# ---------------------------------------------------------------------------
# 5b. wallet_export_key_impl — base58 private key for Phantom-style import
# ---------------------------------------------------------------------------

def test_wallet_export_key_returns_phantom_compatible_secret(_isolate_wallet_path):
    """The base58 private key must round-trip through solders.Keypair —
    that's exactly what Phantom / Backpack / Solflare do under the hood
    when a user pastes the secret into 'Import private key'."""
    import base58
    from solders.keypair import Keypair as _Kp

    new_res = wallet_mod.wallet_new_impl()
    res = wallet_mod.wallet_export_key_impl()
    assert res["ok"] is True
    assert res["address"] == new_res["address"]

    b58 = res["private_key_b58"]
    assert isinstance(b58, str) and len(b58) > 80

    # The base58 string must decode to 64 bytes (secret_seed||pubkey)
    # and the derived address must match.
    decoded = base58.b58decode(b58)
    assert len(decoded) == 64
    rebuilt = _Kp.from_bytes(decoded)
    assert str(rebuilt.pubkey()) == new_res["address"]


def test_wallet_export_key_writes_to_file(_isolate_wallet_path, tmp_path):
    wallet_mod.wallet_new_impl()
    out = tmp_path / "secret.b58"
    res = wallet_mod.wallet_export_key_impl(output_path=str(out))
    assert res["ok"] is True
    assert out.exists()
    # File contains the base58 secret only (no whitespace, no JSON).
    body = out.read_text().strip()
    assert len(body) > 80
    import base58
    assert len(base58.b58decode(body)) == 64
    # JSON payload elides the secret when written to disk.
    assert res["private_key_b58"] is None
    if not IS_WINDOWS:
        mode = stat.S_IMODE(out.stat().st_mode)
        assert mode == 0o600


def test_wallet_export_key_refuses_existing_file_without_force(
    _isolate_wallet_path, tmp_path
):
    wallet_mod.wallet_new_impl()
    out = tmp_path / "secret.b58"
    out.write_text("placeholder")
    res = wallet_mod.wallet_export_key_impl(output_path=str(out))
    assert res["ok"] is False
    assert "already exists" in res["error"]
    assert out.read_text() == "placeholder"


def test_wallet_export_key_overwrites_with_force(_isolate_wallet_path, tmp_path):
    wallet_mod.wallet_new_impl()
    out = tmp_path / "secret.b58"
    out.write_text("placeholder")
    res = wallet_mod.wallet_export_key_impl(output_path=str(out), force=True)
    assert res["ok"] is True
    assert out.read_text() != "placeholder"


def test_wallet_export_key_fails_without_wallet(_isolate_wallet_path):
    res = wallet_mod.wallet_export_key_impl()
    assert res["ok"] is False
    assert "no wallet" in res["error"]


# ---------------------------------------------------------------------------
# 6. wallet_import_impl — validates input, rejects garbage
# ---------------------------------------------------------------------------

def test_wallet_import_round_trip(_isolate_wallet_path, tmp_path):
    """Generate a Keypair externally, import the keystore, re-derive address."""
    kp = Keypair()
    src = tmp_path / "external.json"
    src.write_text(json.dumps(list(bytes(kp))))
    res = wallet_mod.wallet_import_impl(str(src))
    assert res["ok"] is True
    assert res["address"] == str(kp.pubkey())
    if not IS_WINDOWS:
        assert res["mode"] == "0o600"


def test_wallet_import_rejects_wrong_length(_isolate_wallet_path, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2, 3]))
    res = wallet_mod.wallet_import_impl(str(bad))
    assert res["ok"] is False
    assert "64" in res["error"]


def test_wallet_import_rejects_non_array(_isolate_wallet_path, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "an array"}))
    res = wallet_mod.wallet_import_impl(str(bad))
    assert res["ok"] is False
    assert "expected JSON array" in res["error"]


def test_wallet_import_rejects_non_int_entries(_isolate_wallet_path, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(["not"] * 64))
    res = wallet_mod.wallet_import_impl(str(bad))
    assert res["ok"] is False
    assert "0-255" in res["error"]


def test_wallet_import_rejects_invalid_json(_isolate_wallet_path, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    res = wallet_mod.wallet_import_impl(str(bad))
    assert res["ok"] is False
    assert "not valid JSON" in res["error"]


def test_wallet_import_refuses_to_overwrite_without_force(_isolate_wallet_path, tmp_path):
    wallet_mod.wallet_new_impl()
    kp = Keypair()
    src = tmp_path / "external.json"
    src.write_text(json.dumps(list(bytes(kp))))
    res = wallet_mod.wallet_import_impl(str(src))
    assert res["ok"] is False
    assert "already exists" in res["error"]


# ---------------------------------------------------------------------------
# 7. wallet_rm_impl
# ---------------------------------------------------------------------------

def test_wallet_rm_removes_existing_keystore(_isolate_wallet_path):
    res = wallet_mod.wallet_new_impl()
    rm_res = wallet_mod.wallet_rm_impl()
    assert rm_res["ok"] is True
    assert rm_res["removed"] is True
    assert not Path(res["path"]).exists()


def test_wallet_rm_missing_wallet_is_noop(_isolate_wallet_path):
    rm_res = wallet_mod.wallet_rm_impl()
    assert rm_res["ok"] is True
    assert rm_res["removed"] is False


def test_wallet_rm_cli_requires_confirmation_in_non_tty(_isolate_wallet_path, monkeypatch, capsys):
    """Non-TTY without --yes must NOT delete; must print a hint."""
    wallet_mod.wallet_new_impl()
    # Force isatty to False to simulate an agent / CI shell.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    class _Args:
        yes = False
        json = False
        path = None

    with pytest.raises(SystemExit) as exc:
        wallet_mod.cmd_wallet_rm(_Args())
    # Exit non-zero so callers know we refused to delete.
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--yes" in err
    # And the wallet still exists.
    assert wallet_mod.resolve_wallet_path(None).exists()


def test_wallet_rm_cli_yes_deletes(_isolate_wallet_path, monkeypatch):
    wallet_mod.wallet_new_impl()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    class _Args:
        yes = True
        json = False
        path = None

    with pytest.raises(SystemExit) as exc:
        wallet_mod.cmd_wallet_rm(_Args())
    assert exc.value.code == 0
    assert not wallet_mod.resolve_wallet_path(None).exists()


# ---------------------------------------------------------------------------
# 8. wallet_onboard_impl
# ---------------------------------------------------------------------------

def test_wallet_onboard_creates_wallet_and_returns_next_steps(_isolate_wallet_path):
    res = wallet_mod.wallet_onboard_impl()
    assert res["ok"] is True
    assert res["address"]
    assert res["explorer_url"].startswith("https://solscan.io/")
    # Must mention how to fund the wallet AND how to sign without funding.
    joined = " ".join(res["next_steps"])
    assert "SOL" in joined
    assert "export --sign" in joined


def test_wallet_onboard_does_not_auto_sign(_isolate_wallet_path, tmp_path):
    """onboard must NOT leave a signed bundle on disk as a side effect.

    Per design: creating-a-wallet and signing-a-bundle are two intents.
    """
    res = wallet_mod.wallet_onboard_impl()
    assert res["ok"] is True
    # Look in the parent dir of the wallet for stray bundles.
    wallet_path = Path(res["path"])
    siblings = list(wallet_path.parent.glob("*.json"))
    # The only json file should be the wallet itself.
    assert siblings == [wallet_path], f"found stray files: {siblings}"


# ---------------------------------------------------------------------------
# 9. _resolve_signer_keystore — precedence ordering
# ---------------------------------------------------------------------------

def test_keystore_resolution_explicit_wins(_isolate_wallet_path, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINCTL_SIGNING_KEY_PATH", "/env/path.json")
    wallet_mod.wallet_new_impl()  # creates the managed wallet too
    res = sign_mod._resolve_signer_keystore(
        cli_keystore="/explicit/path.json", auto_setup=False,
    )
    assert res["ok"] is True
    assert res["source"] == "cli"
    assert res["keystore_path"] == "/explicit/path.json"


def test_keystore_resolution_managed_beats_env(_isolate_wallet_path, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINCTL_SIGNING_KEY_PATH", "/env/path.json")
    wallet_mod.wallet_new_impl()
    res = sign_mod._resolve_signer_keystore(cli_keystore=None, auto_setup=False)
    assert res["ok"] is True
    assert res["source"] == "managed"


def test_keystore_resolution_env_fallback(_isolate_wallet_path, tmp_path, monkeypatch):
    """No CLI, no managed wallet → env fallback (legacy 2.3.0 path)."""
    legacy = tmp_path / "legacy.json"
    monkeypatch.setenv("BRAINCTL_SIGNING_KEY_PATH", str(legacy))
    res = sign_mod._resolve_signer_keystore(cli_keystore=None, auto_setup=False)
    assert res["ok"] is True
    assert res["source"] == "env"
    assert res["keystore_path"] == str(legacy)


def test_keystore_resolution_no_wallet_friendly_error(_isolate_wallet_path):
    res = sign_mod._resolve_signer_keystore(cli_keystore=None, auto_setup=False)
    assert res["ok"] is False
    assert "wallet new" in res["error"]
    assert "--keystore" in res["error"]


def test_keystore_resolution_auto_setup_creates_wallet(_isolate_wallet_path):
    res = sign_mod._resolve_signer_keystore(cli_keystore=None, auto_setup=True)
    assert res["ok"] is True
    assert res["source"] == "auto-created"
    assert res["auto_created_address"]
    assert Path(res["keystore_path"]).exists()


# ---------------------------------------------------------------------------
# 10. End-to-end: export --sign auto-discovers the managed wallet
# ---------------------------------------------------------------------------

def _run_cli(argv, *, db_path, env_extra=None):
    env = {
        **os.environ,
        "BRAIN_DB": str(db_path),
        "PYTHONPATH": str(SRC),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agentmemory.cli", *argv],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_cli_export_sign_auto_uses_managed_wallet(tmp_path, _isolate_wallet_path):
    """No --keystore → falls through to ~/.brainctl/wallet.json."""
    db_p = _make_db(tmp_path)
    # Create the managed wallet via the impl (faster than spawning).
    wallet_mod.wallet_new_impl()

    out = tmp_path / "bundle.json"
    r = _run_cli(
        ["export", "--sign", "-o", str(out), "--json"],
        db_path=db_p,
        env_extra={"BRAINCTL_WALLET_PATH": os.environ["BRAINCTL_WALLET_PATH"]},
    )
    assert r.returncode == 0, f"export failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["keystore_source"] == "managed"
    assert payload["memories_count"] == 2
    assert out.exists()


def test_cli_export_sign_no_wallet_friendly_error(tmp_path, _isolate_wallet_path):
    """No --keystore, no managed wallet, no env → exit 1 with friendly hint."""
    db_p = _make_db(tmp_path)
    r = _run_cli(
        ["export", "--sign", "--json"],
        db_path=db_p,
        env_extra={
            "BRAINCTL_WALLET_PATH": os.environ["BRAINCTL_WALLET_PATH"],
            "BRAINCTL_SIGNING_KEY_PATH": "",
        },
    )
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert "wallet new" in payload["error"]


def test_cli_export_sign_auto_setup_wallet(tmp_path, _isolate_wallet_path):
    """--auto-setup-wallet creates a fresh wallet AND signs in one go."""
    db_p = _make_db(tmp_path)
    out = tmp_path / "bundle.json"

    r = _run_cli(
        ["export", "--sign", "--auto-setup-wallet", "-o", str(out), "--json"],
        db_path=db_p,
        env_extra={
            "BRAINCTL_WALLET_PATH": os.environ["BRAINCTL_WALLET_PATH"],
            "BRAINCTL_SIGNING_KEY_PATH": "",
        },
    )
    assert r.returncode == 0, f"auto-setup failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["keystore_source"] == "auto-created"
    assert payload["auto_created_wallet"]["address"]
    # The wallet file actually exists after the run.
    assert Path(payload["auto_created_wallet"]["path"]).exists()
    assert out.exists()


# ---------------------------------------------------------------------------
# 11. --pin-onchain with 0-SOL balance — friendly skip, exit 0
# ---------------------------------------------------------------------------

def test_pin_onchain_zero_balance_skips_gracefully(tmp_path, _isolate_wallet_path,
                                                   capsys, monkeypatch):
    """0-SOL wallet → skip the pin, exit 0, surface pin_skipped_reason.

    The signing module's RPC layer is patched so we never touch a
    real network. The bundle is written to disk and the offline
    signature is still valid — we just don't pin.
    """
    db_p = _make_db(tmp_path)
    wallet_mod.wallet_new_impl()

    # Mock the balance pre-check to return 0 lamports. The pin RPC
    # itself must NOT be called — if it is, we'll know because the
    # mock raises.
    def fake_rpc(url, method, params, *, timeout=30.0):
        if method == "getBalance":
            return {"context": {}, "value": 0}
        raise AssertionError(
            f"RPC method {method} called despite 0-SOL pre-check"
        )

    monkeypatch.setattr(signing, "_rpc_call", fake_rpc)
    # Run cmd_export in-process so the monkeypatch takes effect.
    out = tmp_path / "b.json"

    class _Args:
        sign = True
        keystore = None
        auto_setup_wallet = False
        filter_agent = None
        category = None
        scope = None
        created_after = None
        created_before = None
        ids = None
        pin_onchain = True
        rpc_url = "http://fake"
        output = str(out)
        json = True

    # cmd_export uses _get_db_path(), so override BRAIN_DB.
    monkeypatch.setenv("BRAIN_DB", str(db_p))
    with pytest.raises(SystemExit) as exc:
        sign_mod.cmd_export(_Args())
    # Exit 0 — the user's primary intent (sign the bundle) succeeded.
    assert exc.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["pinned_onchain"] is False
    assert payload["pin_skipped_reason"] == "zero_balance"
    # Bundle landed on disk, so the offline signature is recoverable.
    assert out.exists()
    # Friendly stderr message mentions both "0 SOL" and the address.
    assert "0 SOL" in captured.err
    assert payload["signer_pubkey"] in captured.err


# ---------------------------------------------------------------------------
# 12. MCP wallet tool surfaces (smoke — they route to the impl)
# ---------------------------------------------------------------------------

def test_mcp_wallet_show_routes_to_impl(_isolate_wallet_path):
    from agentmemory.mcp_server import tool_wallet_show
    res = tool_wallet_show(fetch_balance=False)
    # Exists=False since we haven't created one yet — but ok=True
    # because show is the diagnostic, not a "must exist" assertion.
    assert res["ok"] is True
    assert res["exists"] is False


def test_mcp_wallet_create_refuses_overwrite_without_force(_isolate_wallet_path):
    from agentmemory.mcp_server import tool_wallet_create
    first = tool_wallet_create()
    assert first["ok"] is True
    # Second call without force=True must REFUSE — agents need to
    # surface this and re-call only after explicit user consent.
    second = tool_wallet_create()
    assert second["ok"] is False
    assert "force" in second["error"].lower()


def test_mcp_wallet_create_force_overwrites(_isolate_wallet_path):
    from agentmemory.mcp_server import tool_wallet_create
    first = tool_wallet_create()
    second = tool_wallet_create(force=True)
    assert second["ok"] is True
    assert second["address"] != first["address"]
