"""Managed Solana wallet for brainctl signed exports.

Ships in 2.3.2 to remove the "install Solana CLI first" hurdle from the
signed-export flow introduced in 2.3.0. A non-crypto user (or an AI
agent walking a user through setup) can now do:

    brainctl wallet new --yes        # creates ~/.brainctl/wallet.json
    brainctl wallet address          # pipeable: $(brainctl wallet address)
    brainctl wallet show             # full info incl. SOL balance
    brainctl export --sign -o foo.json   # auto-uses the managed wallet

Design constraints (preference memory #1691, spec for 2.3.2):

  * **Non-custodial.** Keystore lives on the user's disk. brainctl
    never transmits the key, never backs it up to a server. The only
    network call originating from a key is the user's own opt-in
    ``--pin-onchain`` memo transaction.
  * **No token gating.** Anyone can create + use a wallet for free.
  * **Lazy solders import.** ``import solders`` happens inside the
    functions that need it. Module import is solders-free so the rest
    of brainctl keeps working when the optional ``[signing]`` extra
    isn't installed.
  * **Atomic, 0600 keystore writes.** ``os.open(O_CREAT|O_WRONLY|
    O_EXCL, 0o600)`` so the file is never momentarily world-readable.
    Parent dir ``~/.brainctl/`` is created 0700. Skipped silently on
    Windows where chmod semantics don't match.
  * **Interactive-prompt safety.** Every prompt has a ``--yes`` /
    ``force=`` non-interactive escape hatch. When stdin isn't a TTY
    and ``--yes`` wasn't passed, prompts error out with a clear
    "use --yes to confirm" rather than hanging an agent.

The pure-impl functions (``wallet_new_impl``, ``wallet_show_impl``,
etc.) all return ``dict`` and never call ``sys.exit`` / ``input()`` /
``print``. The argparse handlers are thin wrappers that turn those
dicts into CLI output + exit codes. The MCP tools call the same impl
functions directly so the two surfaces can't drift.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default on-disk location for the managed wallet. Overridable via env
# (``BRAINCTL_WALLET_PATH``) or per-invocation via ``--path`` / the
# ``path`` arg on each impl function.
DEFAULT_WALLET_DIR = "~/.brainctl"
DEFAULT_WALLET_FILENAME = "wallet.json"

# Reuse the signing module's default RPC so wallet balance reads the
# same endpoint as ``--pin-onchain``.
def _default_rpc_url() -> str:
    from agentmemory import signing  # late import to keep this module light
    return signing.DEFAULT_RPC_URL


# Lamports per SOL (base unit conversion factor). Hard-coded constant
# from the Solana protocol; will not change.
LAMPORTS_PER_SOL = 1_000_000_000


# Safety warning surfaced to the user any time we hand them an address
# or write a keystore. Single source of truth so CLI and MCP say the
# same thing.
SAFETY_WARNING = (
    "SAFETY: This keystore is your wallet's private key. Anyone with "
    "this file can sign as you. brainctl never transmits, copies, or "
    "backs up this key — back it up yourself with `brainctl wallet "
    "export <path>` and store it somewhere safe."
)


# ---------------------------------------------------------------------------
# Path / filesystem helpers
# ---------------------------------------------------------------------------

def resolve_wallet_path(cli_arg: Optional[str] = None) -> Path:
    """Resolve the managed wallet path from CLI arg, env, or default.

    Precedence (highest first):
      1. ``cli_arg`` — explicit ``--path`` flag
      2. ``BRAINCTL_WALLET_PATH`` env var
      3. ``~/.brainctl/wallet.json``
    """
    if cli_arg:
        return Path(cli_arg).expanduser().resolve()
    env = os.environ.get("BRAINCTL_WALLET_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(DEFAULT_WALLET_DIR).expanduser() / DEFAULT_WALLET_FILENAME).resolve()


def _is_windows() -> bool:
    return os.name == "nt"


def _ensure_parent_dir(path: Path) -> None:
    """Create the parent directory with 0700 perms (silently on Windows)."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not _is_windows():
        try:
            os.chmod(parent, 0o700)
        except OSError:
            # Best-effort hardening — don't crash if filesystem refuses.
            pass


def _atomic_write_keystore(path: Path, secret_bytes: bytes) -> None:
    """Write the 64-byte keystore to ``path`` with 0600 perms, atomically.

    Uses ``os.open(O_CREAT|O_WRONLY|O_EXCL, 0o600)`` so the file is
    never momentarily readable by other users between create and
    chmod. ``O_EXCL`` means this raises if the file already exists —
    callers are responsible for unlinking first when ``--force`` was
    passed.

    On Windows we fall back to ``Path.write_text`` because the POSIX
    permission bits don't apply.
    """
    payload = json.dumps(list(secret_bytes), separators=(",", ":")).encode("ascii")
    if _is_windows():
        path.write_bytes(payload)
        return
    flags = os.O_CREAT | os.O_WRONLY | os.O_EXCL
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    # Defensive re-chmod in case the umask was inherited oddly.
    os.chmod(path, 0o600)


def _file_mode_octal(path: Path) -> Optional[str]:
    """Return the file's mode as an octal string (``0o600``-style)."""
    try:
        m = stat.S_IMODE(path.stat().st_mode)
        return oct(m)
    except OSError:
        return None


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Keystore validation (no solders import)
# ---------------------------------------------------------------------------

def _validate_keystore_payload(raw: Any) -> Optional[str]:
    """Return None if ``raw`` looks like a valid Solana CLI keystore.

    Returns a human-readable error string on failure. This intentionally
    does NOT need solders — we just check the JSON shape. Callers that
    need a Keypair object should also call ``signing.load_keystore``.
    """
    if not isinstance(raw, list):
        return f"expected JSON array, got {type(raw).__name__}"
    if len(raw) != 64:
        return f"expected 64 ints, got {len(raw)}"
    if not all(isinstance(x, int) and 0 <= x <= 255 for x in raw):
        return "entries must be ints in 0-255"
    return None


# ---------------------------------------------------------------------------
# Pure impl functions — used by CLI handlers AND MCP tools
# ---------------------------------------------------------------------------

def wallet_new_impl(
    path: Optional[str] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Generate a fresh Ed25519 keypair and persist it as a Solana keystore.

    Returns a structured dict; never prints or exits. Callers decide
    how to surface ``ok=False`` with the ``error`` field.

    With ``force=True``, an existing keystore at ``path`` is unlinked
    before write. Without it, an existing file aborts with
    ``error="wallet exists"``. NEVER overwrites without explicit
    ``force`` — no exception, this is the safety contract for both
    CLI and MCP surfaces.
    """
    from agentmemory import signing
    try:
        signing._require_solders()
    except SystemExit as exc:
        return {
            "ok": False,
            "address": None,
            "path": None,
            "error": "solders not installed — pip install 'brainctl[signing]'",
            "exit_code": exc.code if isinstance(exc.code, int) else 1,
        }
    from solders.keypair import Keypair  # type: ignore

    target = resolve_wallet_path(path)
    if target.exists():
        if not force:
            return {
                "ok": False,
                "address": None,
                "path": str(target),
                "error": (
                    f"wallet already exists at {target}. "
                    "Pass force=true (or --force) to overwrite. "
                    "Back up the existing keystore first if it has SOL."
                ),
            }
        # Force path: unlink the old keystore. We don't move-aside or
        # back up — the user explicitly opted in and the new keystore
        # is about to land in the same spot.
        try:
            target.unlink()
        except OSError as e:
            return {"ok": False, "address": None, "path": str(target),
                    "error": f"could not remove existing keystore: {e}"}

    _ensure_parent_dir(target)
    kp = Keypair()
    secret_bytes = bytes(kp)  # 64 bytes: secret(32) || pubkey(32)
    try:
        _atomic_write_keystore(target, secret_bytes)
    except FileExistsError:
        # Race: someone created the file between our exists() check
        # and the O_EXCL open. Treat as the same "exists" error.
        return {
            "ok": False, "address": None, "path": str(target),
            "error": (
                f"wallet already exists at {target} (lost race with "
                "another writer). Pass force=true to overwrite."
            ),
        }
    except OSError as e:
        return {"ok": False, "address": None, "path": str(target),
                "error": f"could not write keystore: {e}"}

    return {
        "ok": True,
        "address": str(kp.pubkey()),
        "path": str(target),
        "mode": _file_mode_octal(target),
        "warning": SAFETY_WARNING,
        "error": None,
    }


def wallet_address_impl(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the keystore and derive its public address.

    Returns ``ok=False`` with a friendly error if the keystore is
    missing, malformed, or solders isn't installed. Otherwise returns
    ``{ok: True, address: "<base58>"}``.
    """
    from agentmemory import signing
    target = resolve_wallet_path(path)
    if not target.exists():
        return {
            "ok": False, "address": None, "path": str(target),
            "error": (
                f"no wallet at {target}. Run `brainctl wallet new` "
                "to create one."
            ),
        }
    try:
        kp = signing.load_keystore(str(target))
    except SystemExit:
        return {"ok": False, "address": None, "path": str(target),
                "error": "solders not installed — pip install 'brainctl[signing]'"}
    except FileNotFoundError as e:
        return {"ok": False, "address": None, "path": str(target),
                "error": str(e)}
    except (ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "address": None, "path": str(target),
                "error": f"invalid keystore: {e}"}
    return {"ok": True, "address": str(kp.pubkey()), "path": str(target),
            "error": None}


def wallet_balance_impl(
    path: Optional[str] = None,
    *,
    rpc_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch the wallet's SOL balance via JSON-RPC ``getBalance``.

    Returns ``{ok: True, address, lamports, sol, rpc_url}`` on success,
    or ``{ok: False, ..., error}`` on RPC / keystore failures. Callers
    that need a "0 SOL" check should look at ``lamports == 0``, not
    string-format the SOL value.
    """
    from agentmemory import signing
    addr_res = wallet_address_impl(path)
    if not addr_res["ok"]:
        return {**addr_res, "lamports": None, "sol": None, "rpc_url": rpc_url}
    address = addr_res["address"]
    url = rpc_url or _default_rpc_url()
    try:
        result = signing._rpc_call(url, "getBalance", [address])
    except Exception as e:
        return {
            "ok": False, "address": address, "path": addr_res["path"],
            "lamports": None, "sol": None, "rpc_url": url,
            "error": f"RPC call to {url} failed: {e}",
        }

    # getBalance returns {context: {...}, value: <lamports>}.
    lamports: Optional[int] = None
    if isinstance(result, dict) and "value" in result:
        lamports = int(result["value"])
    elif isinstance(result, int):
        lamports = int(result)
    if lamports is None:
        return {
            "ok": False, "address": address, "path": addr_res["path"],
            "lamports": None, "sol": None, "rpc_url": url,
            "error": f"unexpected getBalance response shape: {result!r}",
        }
    return {
        "ok": True, "address": address, "path": addr_res["path"],
        "lamports": lamports, "sol": lamports / LAMPORTS_PER_SOL,
        "rpc_url": url, "error": None,
    }


def wallet_show_impl(
    path: Optional[str] = None,
    *,
    rpc_url: Optional[str] = None,
    fetch_balance: bool = True,
) -> Dict[str, Any]:
    """Full wallet info: address, balance, keystore path, perms, mtime.

    ``fetch_balance=False`` skips the RPC call (useful for CI / offline
    diagnostics). When the wallet doesn't exist, returns
    ``{ok: True, exists: False, ...}`` rather than an error — the show
    command must be safe to call before ``wallet new``.
    """
    target = resolve_wallet_path(path)
    if not target.exists():
        return {
            "ok": True, "exists": False, "path": str(target),
            "address": None, "lamports": None, "sol": None, "mode": None,
            "mtime": None, "rpc_url": None, "balance_error": None,
            "error": None, "warning": (
                f"No wallet found at {target}. "
                "Run `brainctl wallet new` to create one."
            ),
        }

    addr_res = wallet_address_impl(path)
    payload: Dict[str, Any] = {
        "ok": addr_res["ok"], "exists": True, "path": str(target),
        "address": addr_res.get("address"),
        "mode": _file_mode_octal(target),
        "mtime": _file_mtime_iso(target),
        "lamports": None, "sol": None, "rpc_url": None,
        "balance_error": None,
        "error": addr_res.get("error"), "warning": None,
    }
    if not addr_res["ok"]:
        return payload

    if fetch_balance:
        bal = wallet_balance_impl(path, rpc_url=rpc_url)
        payload["lamports"] = bal.get("lamports")
        payload["sol"] = bal.get("sol")
        payload["rpc_url"] = bal.get("rpc_url")
        if not bal["ok"]:
            # A balance failure shouldn't kill `wallet show` — the
            # local info is still useful when the user is offline.
            payload["balance_error"] = bal.get("error")
    return payload


def wallet_export_impl(
    output_path: str,
    *,
    path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Copy the managed keystore to ``output_path`` for backup.

    The copy is chmod 0600 and lands at the user-supplied path. We do
    NOT auto-prefix or relocate — if the user wants their backup on a
    USB stick mounted at ``/Volumes/Backup``, that's their call.

    The output is the same secret bytes as the source, not a derived
    representation — the 64-int Solana CLI format is itself the full
    secret + pubkey, there's nothing to re-derive.
    """
    src = resolve_wallet_path(path)
    if not src.exists():
        return {
            "ok": False, "src": str(src), "dst": str(Path(output_path).expanduser()),
            "error": (
                f"no wallet at {src}. Run `brainctl wallet new` first "
                "(nothing to back up yet)."
            ),
        }

    dst = Path(output_path).expanduser()
    if dst.exists() and not force:
        return {
            "ok": False, "src": str(src), "dst": str(dst),
            "error": (
                f"output path already exists: {dst}. "
                "Pass force=true (or --force) to overwrite."
            ),
        }
    try:
        # Copy bytes verbatim, then chmod 0600 (matches `wallet new`
        # hardening; the backup file is just as sensitive as the
        # source).
        if dst.exists() and force:
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(dst))
        if not _is_windows():
            os.chmod(dst, 0o600)
    except OSError as e:
        return {"ok": False, "src": str(src), "dst": str(dst),
                "error": f"could not copy keystore: {e}"}
    return {
        "ok": True, "src": str(src), "dst": str(dst),
        "mode": _file_mode_octal(dst), "error": None,
        "warning": (
            "BACK THIS UP SAFELY. The backup file is the full private "
            "key — anyone with it can sign as you. Store it somewhere "
            "you wouldn't store a credit card."
        ),
    }


def wallet_export_key_impl(
    *,
    path: Optional[str] = None,
    output_path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Export the wallet's secret key as a base58 string that
    Phantom / Backpack / Solflare / Glow accept under "import
    private key".

    Why this exists: ``wallet_export_impl`` (the file backup) writes
    the Solana CLI keystore format — a 64-int JSON array. Wallet UIs
    don't accept that format; they want a base58-encoded 64-byte
    secret. This impl renders the same secret in the form those UIs
    will actually accept.

    Security: the returned string IS the full private key. Anyone
    with it can sign as the wallet's owner.

    Without ``output_path`` the secret is returned in the payload so
    the caller (CLI handler) can print it. With ``output_path`` it
    lands at that file at mode 0600 and the payload reports the
    write target but elides the secret from the JSON.
    """
    src = resolve_wallet_path(path)
    if not src.exists():
        return {
            "ok": False, "src": str(src),
            "error": (
                f"no wallet at {src}. Run `brainctl wallet new` first."
            ),
        }
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "src": str(src),
                "error": f"could not parse keystore: {e}"}
    if not isinstance(raw, list) or len(raw) != 64:
        return {
            "ok": False, "src": str(src),
            "error": (
                "keystore is not a Solana CLI 64-int array. Was the "
                "file created by `brainctl wallet new` or imported "
                "from `solana-keygen`?"
            ),
        }
    try:
        secret_bytes = bytes(raw)
    except (TypeError, ValueError) as e:
        return {"ok": False, "src": str(src),
                "error": f"keystore array is not bytes-coercible: {e}"}

    try:
        import base58
    except ImportError:
        return {
            "ok": False, "src": str(src),
            "error": (
                "base58 package is required. "
                "Install with `pip install 'brainctl[signing]'`."
            ),
        }
    private_key_b58 = base58.b58encode(secret_bytes).decode("ascii")
    address = base58.b58encode(secret_bytes[32:]).decode("ascii")

    if output_path:
        dst = Path(output_path).expanduser()
        if dst.exists() and not force:
            return {
                "ok": False, "src": str(src), "dst": str(dst),
                "error": (
                    f"output path already exists: {dst}. "
                    "Pass --force to overwrite."
                ),
            }
        try:
            if dst.exists() and force:
                dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(private_key_b58, encoding="utf-8")
            if not _is_windows():
                os.chmod(dst, 0o600)
        except OSError as e:
            return {"ok": False, "src": str(src), "dst": str(dst),
                    "error": f"could not write secret: {e}"}
        return {
            "ok": True, "src": str(src), "dst": str(dst),
            "address": address,
            "mode": _file_mode_octal(dst),
            # Elide from JSON output when writing to file. The user
            # asked for the file path — printing the secret in JSON
            # too would defeat the purpose of the write.
            "private_key_b58": None,
            "error": None,
        }

    return {
        "ok": True, "src": str(src),
        "address": address,
        "private_key_b58": private_key_b58,
        "error": None,
    }


def wallet_import_impl(
    input_path: str,
    *,
    path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Copy an external Solana CLI keystore into the managed location.

    Validates the input parses as a 64-int array BEFORE touching the
    target — we don't want to clobber a wallet with garbage.
    """
    src = Path(input_path).expanduser()
    if not src.exists():
        return {"ok": False, "src": str(src), "dst": None,
                "error": f"input keystore not found: {src}"}
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"ok": False, "src": str(src), "dst": None,
                "error": f"input is not valid JSON: {e}"}
    err = _validate_keystore_payload(raw)
    if err:
        return {"ok": False, "src": str(src), "dst": None,
                "error": f"invalid Solana keystore: {err}"}

    dst = resolve_wallet_path(path)
    if dst.exists() and not force:
        return {
            "ok": False, "src": str(src), "dst": str(dst),
            "error": (
                f"managed wallet already exists at {dst}. "
                "Pass force=true (or --force) to overwrite. "
                "Back up the existing keystore first if it has SOL."
            ),
        }

    _ensure_parent_dir(dst)
    try:
        if dst.exists() and force:
            dst.unlink()
        _atomic_write_keystore(dst, bytes(raw))
    except OSError as e:
        return {"ok": False, "src": str(src), "dst": str(dst),
                "error": f"could not write keystore: {e}"}

    # Round-trip sanity: derive the address so we can confirm the
    # import worked end-to-end.
    addr = wallet_address_impl(path)
    return {
        "ok": True, "src": str(src), "dst": str(dst),
        "address": addr.get("address"),
        "mode": _file_mode_octal(dst), "error": None,
    }


def wallet_rm_impl(path: Optional[str] = None) -> Dict[str, Any]:
    """Delete the managed keystore. Caller is responsible for confirmation.

    The CLI handler asks ``input("type 'delete' to confirm: ")`` (or
    skips on ``--yes``) before invoking this. The MCP tool requires
    explicit ``force=true``. The impl itself just unlinks.
    """
    target = resolve_wallet_path(path)
    if not target.exists():
        return {"ok": True, "removed": False, "path": str(target),
                "error": None,
                "note": "nothing to remove — wallet did not exist"}
    try:
        target.unlink()
    except OSError as e:
        return {"ok": False, "removed": False, "path": str(target),
                "error": f"could not remove keystore: {e}"}
    return {"ok": True, "removed": True, "path": str(target), "error": None}


# ---------------------------------------------------------------------------
# Onboarding flow — wraps wallet_new_impl + prints next-step guidance.
# ---------------------------------------------------------------------------

# Solscan / Solana Explorer base URLs for the friendly wallet link.
# We use Solscan because it's the most ad-light explorer and works
# without JS for the address page — friendlier to non-crypto users.
SOLSCAN_ADDR_URL = "https://solscan.io/account/{address}"


def wallet_onboard_impl(
    path: Optional[str] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Create a wallet + return the onboarding payload.

    Returns the structured "next steps" content the CLI / MCP handler
    will format. We do NOT auto-sign a sample bundle here — the
    interactive CLI handler offers that as a follow-up step. Agents
    calling onboard via MCP get just the wallet creation; the side
    effect of "we also wrote a bundle to disk" is two intents not one.
    """
    create = wallet_new_impl(path, force=force)
    if not create["ok"]:
        return {
            "ok": False, "address": None, "path": create.get("path"),
            "error": create["error"], "next_steps": [],
            "explorer_url": None,
        }
    address = create["address"]
    explorer_url = SOLSCAN_ADDR_URL.format(address=address)
    next_steps = [
        f"Your new wallet address is: {address}",
        f"View it on Solscan: {explorer_url}",
        (
            "To enable optional on-chain pinning of bundle hashes, "
            "send any small amount of SOL (~$1) to the address above. "
            "Each pin costs ~$0.001."
        ),
        (
            "Sign your first bundle: `brainctl export --sign -o "
            "first-bundle.json` (it auto-uses this wallet)."
        ),
        (
            "Pinning is optional. Offline signatures already make "
            "your bundles tamper-evident with no SOL required."
        ),
    ]
    return {
        "ok": True, "address": address, "path": create["path"],
        "mode": create.get("mode"), "warning": SAFETY_WARNING,
        "next_steps": next_steps, "explorer_url": explorer_url,
        "error": None,
    }


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def _emit_json(payload: Dict[str, Any], *, exit_code: int = 0) -> None:
    """Pretty-print JSON to stdout and exit."""
    print(json.dumps(payload, indent=2, default=str))
    sys.exit(exit_code)


def _confirm(prompt: str, *, yes: bool) -> bool:
    """Return True if the user (or ``--yes``) consents.

    Critical safety: when stdin isn't a TTY and ``--yes`` wasn't
    passed, we DO NOT hang waiting for input. We return False. The
    caller is responsible for printing a "use --yes to confirm" hint.
    Agents wrapping brainctl never get to see an interactive prompt
    and would otherwise stall indefinitely.
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def cmd_wallet_new(args: Any) -> None:
    yes = bool(getattr(args, "yes", False))
    force = bool(getattr(args, "force", False))
    as_json = bool(getattr(args, "json", False))
    path_arg = getattr(args, "path", None)

    target = resolve_wallet_path(path_arg)
    if target.exists() and force:
        # Overwriting a real wallet is destructive — confirm unless --yes.
        if not _confirm(
            f"Overwrite existing wallet at {target}? (y/N): ", yes=yes,
        ):
            payload = {
                "ok": False, "address": None, "path": str(target),
                "error": (
                    "refusing to overwrite without confirmation. "
                    "Re-run with --yes to skip this prompt "
                    "(non-interactive shell or agent context)."
                ),
            }
            if as_json:
                _emit_json(payload, exit_code=1)
            print(payload["error"], file=sys.stderr)
            sys.exit(1)

    res = wallet_new_impl(path_arg, force=force)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)

    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"Created wallet at {res['path']}")
    print(f"Address: {res['address']}")
    if res.get("mode"):
        print(f"  perms: {res['mode']}  (chmod 0600)")
    print()
    print(res["warning"])
    sys.exit(0)


def cmd_wallet_address(args: Any) -> None:
    """Print just the address. Designed to be pipe-friendly.

    No header, no warning, no trailing newline besides the address
    itself, so ``$(brainctl wallet address)`` works without trimming.
    """
    path_arg = getattr(args, "path", None)
    res = wallet_address_impl(path_arg)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    # Single line, single field. Mirrors `git rev-parse HEAD` UX.
    print(res["address"])
    sys.exit(0)


def cmd_wallet_balance(args: Any) -> None:
    path_arg = getattr(args, "path", None)
    rpc_url = getattr(args, "rpc_url", None)
    as_json = bool(getattr(args, "json", False))
    res = wallet_balance_impl(path_arg, rpc_url=rpc_url)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"Address: {res['address']}")
    print(f"Balance: {res['sol']:.9f} SOL  ({res['lamports']} lamports)")
    print(f"  RPC: {res['rpc_url']}")
    sys.exit(0)


def cmd_wallet_show(args: Any) -> None:
    path_arg = getattr(args, "path", None)
    rpc_url = getattr(args, "rpc_url", None)
    as_json = bool(getattr(args, "json", False))
    skip_balance = bool(getattr(args, "no_balance", False))
    res = wallet_show_impl(path_arg, rpc_url=rpc_url, fetch_balance=not skip_balance)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    if not res["exists"]:
        print(res["warning"])
        sys.exit(0)
    print(f"Address: {res['address']}")
    print(f"Path:    {res['path']}")
    print(f"Perms:   {res['mode']}")
    print(f"Mtime:   {res['mtime']}")
    if res.get("balance_error"):
        print(f"Balance: (RPC failed: {res['balance_error']})")
    elif res.get("sol") is not None:
        print(f"Balance: {res['sol']:.9f} SOL  ({res['lamports']} lamports)")
        print(f"  RPC: {res['rpc_url']}")
    sys.exit(0)


def cmd_wallet_export(args: Any) -> None:
    path_arg = getattr(args, "path", None)
    force = bool(getattr(args, "force", False))
    as_json = bool(getattr(args, "json", False))
    output_path = args.output_path
    res = wallet_export_impl(output_path, path=path_arg, force=force)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"Backed up wallet to {res['dst']}")
    if res.get("mode"):
        print(f"  perms: {res['mode']}")
    print()
    print(res["warning"])
    sys.exit(0)


def cmd_wallet_export_key(args: Any) -> None:
    """Print the wallet's base58 private key so it can be imported into
    Phantom / Backpack / Solflare / Glow / any standard Solana wallet UI.

    SECURITY: this is the full private key. Anyone with it controls the
    wallet. We print prominent stderr warnings and never to the
    `brainctl` daemon log. With `-o <path>` the secret is written
    directly to disk at mode 0600 and the JSON payload elides it so it
    can't leak via piping.
    """
    path_arg = getattr(args, "path", None)
    output_path = getattr(args, "output", None)
    force = bool(getattr(args, "force", False))
    as_json = bool(getattr(args, "json", False))

    res = wallet_export_key_impl(
        path=path_arg,
        output_path=output_path,
        force=force,
    )

    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)

    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)

    # Print SAFETY banner to stderr before the secret hits stdout so
    # the warning shows even when stdout is being piped or redirected.
    sys.stderr.write(
        "============================================================\n"
        "  SAFETY: the value below is your FULL Solana private key.\n"
        "  Anyone who copies it can sign as you, transfer your tokens,\n"
        "  and drain your wallet. brainctl can never recover it.\n"
        "  - NEVER paste it into a chat, AI tool, or screenshot.\n"
        "  - NEVER commit it to git or store it in a cloud note.\n"
        "  - Paste it into your wallet UI under:\n"
        "      Phantom:  Settings -> Add / Connect Wallet ->\n"
        "                Import Private Key\n"
        "      Backpack: + -> Import Wallet -> Private Key\n"
        "      Solflare: Settings -> Wallets -> + -> Import Private Key\n"
        "  - The secret has no recovery phrase (brainctl wallets are\n"
        "    not BIP39-mnemonic-derived). Losing the secret is final.\n"
        "============================================================\n"
    )
    sys.stderr.flush()

    if output_path:
        print(f"Wrote base58 private key to {res['dst']} (mode {res.get('mode')})")
        print(f"Address: {res['address']}")
        sys.exit(0)

    # No -o: print the secret on stdout so the user can copy it.
    print(res["private_key_b58"])
    sys.exit(0)


def cmd_wallet_import(args: Any) -> None:
    yes = bool(getattr(args, "yes", False))
    force = bool(getattr(args, "force", False))
    as_json = bool(getattr(args, "json", False))
    path_arg = getattr(args, "path", None)
    input_path = args.input_path

    target = resolve_wallet_path(path_arg)
    if target.exists() and force:
        if not _confirm(
            f"Overwrite existing wallet at {target}? (y/N): ", yes=yes,
        ):
            payload = {
                "ok": False, "src": str(Path(input_path).expanduser()),
                "dst": str(target),
                "error": (
                    "refusing to overwrite without confirmation. "
                    "Re-run with --yes to skip this prompt."
                ),
            }
            if as_json:
                _emit_json(payload, exit_code=1)
            print(payload["error"], file=sys.stderr)
            sys.exit(1)

    res = wallet_import_impl(input_path, path=path_arg, force=force)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"Imported wallet from {res['src']} -> {res['dst']}")
    if res.get("address"):
        print(f"Address: {res['address']}")
    sys.exit(0)


def cmd_wallet_rm(args: Any) -> None:
    yes = bool(getattr(args, "yes", False))
    as_json = bool(getattr(args, "json", False))
    path_arg = getattr(args, "path", None)

    target = resolve_wallet_path(path_arg)
    if target.exists():
        # Stronger phrasing than y/N because this is unrecoverable.
        # Still respect --yes for automation.
        if not _confirm(
            f"Permanently delete wallet at {target}? "
            "Lost if not backed up. (y/N): ",
            yes=yes,
        ):
            payload = {
                "ok": False, "removed": False, "path": str(target),
                "error": (
                    "refusing to delete without confirmation. "
                    "Re-run with --yes to skip this prompt."
                ),
            }
            if as_json:
                _emit_json(payload, exit_code=1)
            print(payload["error"], file=sys.stderr)
            sys.exit(1)

    res = wallet_rm_impl(path_arg)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    if res.get("removed"):
        print(f"Removed wallet at {res['path']}")
    else:
        print(res.get("note", "nothing removed"))
    sys.exit(0)


def cmd_wallet_onboard(args: Any) -> None:
    yes = bool(getattr(args, "yes", False))
    force = bool(getattr(args, "force", False))
    as_json = bool(getattr(args, "json", False))
    path_arg = getattr(args, "path", None)

    target = resolve_wallet_path(path_arg)
    if target.exists() and not force:
        # Onboard on top of an existing wallet is a no-op; we just
        # re-print the next-steps so the user sees the explorer link.
        addr = wallet_address_impl(path_arg)
        payload = {
            "ok": True, "already_existed": True, "address": addr.get("address"),
            "path": str(target),
            "explorer_url": (
                SOLSCAN_ADDR_URL.format(address=addr["address"])
                if addr["ok"] else None
            ),
            "next_steps": [
                f"Wallet already exists at {target}.",
                (
                    "Sign a bundle with: "
                    "`brainctl export --sign -o first-bundle.json`."
                ),
                (
                    "If you've lost the previous wallet's key, run "
                    "`brainctl wallet new --force` to start over "
                    "(destructive)."
                ),
            ],
        }
        if as_json:
            _emit_json(payload, exit_code=0)
        for line in payload["next_steps"]:
            print(line)
        sys.exit(0)

    if target.exists() and force:
        if not _confirm(
            f"Overwrite existing wallet at {target}? (y/N): ", yes=yes,
        ):
            payload = {
                "ok": False, "error": (
                    "refusing to overwrite without confirmation. "
                    "Re-run with --yes."
                ),
            }
            if as_json:
                _emit_json(payload, exit_code=1)
            print(payload["error"], file=sys.stderr)
            sys.exit(1)

    res = wallet_onboard_impl(path_arg, force=force)
    if as_json:
        _emit_json(res, exit_code=0 if res["ok"] else 1)
    if not res["ok"]:
        print(f"FAIL: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print()
    print(f"Created wallet at {res['path']}")
    print(f"Address: {res['address']}")
    print()
    print(res["warning"])
    print()
    print("Next steps:")
    for step in res["next_steps"]:
        print(f"  - {step}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Parser registration (called from _impl.py's build_parser)
# ---------------------------------------------------------------------------

def register_parser(sub: Any) -> None:
    """Attach the ``wallet`` subcommand suite to brainctl's top-level parser.

    Called by ``_impl.py:build_parser`` next to the existing
    ``obsidian`` / ``sign`` registrations.
    """
    p = sub.add_parser(
        "wallet",
        help="Manage a local Solana wallet for signing memory bundles "
             "(non-custodial; brainctl never sees the key).",
        description=(
            "brainctl-managed Solana wallet. Lives at ~/.brainctl/wallet.json "
            "(override with $BRAINCTL_WALLET_PATH or --path). The keystore "
            "stays on your disk; brainctl never transmits or backs it up. "
            "Used as the default signer for `brainctl export --sign`."
        ),
    )
    wsub = p.add_subparsers(dest="wallet_cmd", required=False)

    # Common options shared by every subcommand. We add them per-parser
    # rather than on the parent so each subcommand's --help shows them.
    def _common(sp):
        sp.add_argument("--path", default=None,
                        help="Override keystore path (default: $BRAINCTL_WALLET_PATH "
                             "or ~/.brainctl/wallet.json)")
        sp.add_argument("--json", action="store_true",
                        help="Emit a structured JSON result on stdout")

    # --- new ---
    p_new = wsub.add_parser(
        "new",
        help="Create a fresh Ed25519 keypair (Solana wallet)",
        description=(
            "Generates a new Solana keypair and writes it to "
            "~/.brainctl/wallet.json with chmod 0600. The key is "
            "generated locally and never transmitted."
        ),
    )
    _common(p_new)
    p_new.add_argument("--force", action="store_true",
                       help="Overwrite an existing wallet (destructive)")
    p_new.add_argument("--yes", action="store_true",
                       help="Skip the overwrite confirmation prompt "
                            "(required for non-interactive use)")

    # --- address ---
    p_addr = wsub.add_parser(
        "address",
        help="Print the wallet address (pipeable: $(brainctl wallet address))",
    )
    p_addr.add_argument("--path", default=None,
                        help="Override keystore path")

    # --- balance ---
    p_bal = wsub.add_parser(
        "balance",
        help="Fetch the wallet's SOL balance via JSON-RPC",
    )
    _common(p_bal)
    p_bal.add_argument("--rpc-url", dest="rpc_url", default=None,
                       help="Solana RPC URL (default: mainnet-beta public RPC)")

    # --- show ---
    p_show = wsub.add_parser(
        "show",
        help="Show full wallet info (address, balance, path, perms, mtime)",
    )
    _common(p_show)
    p_show.add_argument("--rpc-url", dest="rpc_url", default=None,
                        help="Solana RPC URL (default: mainnet-beta public RPC)")
    p_show.add_argument("--no-balance", dest="no_balance", action="store_true",
                        help="Skip the RPC balance fetch (offline-safe)")

    # --- export ---
    p_exp = wsub.add_parser(
        "export",
        help="Copy the keystore to a backup location (chmod 0600 the copy)",
    )
    _common(p_exp)
    p_exp.add_argument("output_path",
                       help="Destination path for the backup keystore")
    p_exp.add_argument("--force", action="store_true",
                       help="Overwrite the output file if it already exists")

    # --- export-key ---
    p_xkey = wsub.add_parser(
        "export-key",
        help=("Print/save the base58 private key for Phantom / Backpack / "
              "Solflare / Glow import"),
        description=(
            "Render the managed wallet's secret key in the base58 format "
            "every standard Solana wallet UI accepts under 'import private "
            "key'. brainctl wallets are not BIP39-mnemonic-derived, so the "
            "base58 secret IS the recovery — there is no separate seed "
            "phrase. Without -o the secret is printed to stdout (with "
            "stderr SAFETY warnings). With -o it lands at the path at "
            "mode 0600 and is elided from the JSON output."
        ),
    )
    _common(p_xkey)
    p_xkey.add_argument("-o", "--output", default=None,
                        help="Write the base58 secret to this file "
                             "(chmod 0600) instead of printing it to "
                             "stdout. Recommended for piping into a "
                             "password manager.")
    p_xkey.add_argument("--force", action="store_true",
                        help="Overwrite -o output file if it exists")
    p_xkey.set_defaults(func=cmd_wallet_export_key)

    # --- import ---
    p_imp = wsub.add_parser(
        "import",
        help="Import an existing Solana CLI keystore JSON into the managed location",
    )
    _common(p_imp)
    p_imp.add_argument("input_path",
                       help="Path to an existing Solana CLI keystore JSON "
                            "(64-int array)")
    p_imp.add_argument("--force", action="store_true",
                       help="Overwrite an existing managed wallet")
    p_imp.add_argument("--yes", action="store_true",
                       help="Skip the overwrite confirmation prompt")

    # --- rm ---
    p_rm = wsub.add_parser(
        "rm",
        help="Permanently delete the managed keystore (destructive)",
    )
    _common(p_rm)
    p_rm.add_argument("--yes", action="store_true",
                      help="Skip the deletion confirmation prompt "
                           "(required for non-interactive use)")

    # --- onboard ---
    p_onb = wsub.add_parser(
        "onboard",
        help="Guided first-time setup: creates wallet + prints next steps",
        description=(
            "One-shot onboarding for non-crypto users. Creates a "
            "wallet (if missing), prints the address, links to the "
            "Solana Explorer, and tells you how to fund the wallet "
            "to enable optional on-chain pinning."
        ),
    )
    _common(p_onb)
    p_onb.add_argument("--yes", action="store_true",
                       help="Skip interactive prompts")
    p_onb.add_argument("--force", action="store_true",
                       help="Overwrite an existing wallet")


# ---------------------------------------------------------------------------
# Subcommand dispatch (called from _impl.py main())
# ---------------------------------------------------------------------------

WALLET_DISPATCH = {
    "new":         cmd_wallet_new,
    "address":     cmd_wallet_address,
    "balance":     cmd_wallet_balance,
    "show":        cmd_wallet_show,
    "export":      cmd_wallet_export,
    "export-key":  cmd_wallet_export_key,
    "import":      cmd_wallet_import,
    "rm":          cmd_wallet_rm,
    "onboard":     cmd_wallet_onboard,
}


def cmd_wallet(args: Any) -> None:
    """Top-level wallet dispatch. Called from _impl.py main()."""
    sub = getattr(args, "wallet_cmd", None)
    fn = WALLET_DISPATCH.get(sub)
    if fn is None:
        # No subcommand → mirror `wallet show` (most useful default).
        cmd_wallet_show(args)
        return
    fn(args)


__all__ = [
    # Constants
    "DEFAULT_WALLET_DIR", "DEFAULT_WALLET_FILENAME", "SAFETY_WARNING",
    # Path helpers
    "resolve_wallet_path",
    # Pure impl
    "wallet_new_impl", "wallet_address_impl", "wallet_balance_impl",
    "wallet_show_impl", "wallet_export_impl", "wallet_export_key_impl",
    "wallet_import_impl", "wallet_rm_impl", "wallet_onboard_impl",
    # CLI / parser
    "cmd_wallet", "register_parser",
]
