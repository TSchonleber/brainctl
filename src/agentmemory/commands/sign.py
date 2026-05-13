"""CLI handlers for ``brainctl export --sign`` and ``brainctl verify``.

Both commands shell out to ``agentmemory.signing``. Keeping the
parser-registration + handlers in their own module mirrors the
``commands/obsidian.py`` pattern and keeps ``_impl.py`` (already 16k+
lines) from growing further.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db_path() -> Path:
    """Resolve brain.db the same way _impl.py does (env aware)."""
    from agentmemory.paths import get_db_path
    return get_db_path()


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _emit(payload: Dict[str, Any], *, as_json: bool, exit_code: int = 0) -> None:
    """Print a result and exit with ``exit_code``.

    The ``--json`` flag emits a single JSON object on stdout. Default
    output is a short human-readable summary on stdout, with the same
    structured payload as JSON below it suppressed.
    """
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        # Compact human-readable rendering.
        if "ok" in payload and not payload["ok"] and payload.get("error"):
            print(f"FAIL: {payload['error']}", file=sys.stderr)
        elif payload.get("ok"):
            print("OK")
        # Useful side-fields (sorted for stability).
        for key in (
            "bundle_path", "bundle_hash", "signer_pubkey",
            "memories_count", "signed_at", "signature", "tx_signature",
            "slot", "block_time", "found", "checked_onchain",
            "minted", "mint_address", "mint_tx_signature",
            "mint_cluster", "arweave_metadata_uri",
            "arweave_ciphertext_uri", "bundle_key_path",
            "mint_warning",
        ):
            if key in payload and payload[key] is not None:
                print(f"  {key}: {payload[key]}")
    sys.exit(exit_code)


def _parse_ids(s: Optional[str]) -> Optional[List[int]]:
    if not s:
        return None
    out: List[int] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as e:
            raise ValueError(f"invalid id in --ids: {chunk!r}") from e
    return out


# ---------------------------------------------------------------------------
# export --sign
# ---------------------------------------------------------------------------

def _resolve_signer_keystore(
    cli_keystore: Optional[str],
    *,
    auto_setup: bool,
) -> Dict[str, Any]:
    """Resolve which keystore to sign with.

    Precedence (highest first):
      1. ``--keystore <path>`` (explicit user choice)
      2. ``~/.brainctl/wallet.json`` managed wallet (2.3.2+)
      3. ``$BRAINCTL_SIGNING_KEY_PATH`` env (2.3.0 legacy)
      4. ``--auto-setup-wallet`` → create a managed wallet on the fly
      5. friendly "no wallet found" error pointing at `wallet new`

    Returns ``{ok, keystore_path, source, error}`` where ``source`` is
    one of ``"cli"``, ``"managed"``, ``"env"``, ``"auto-created"`` —
    so the caller can surface a helpful breadcrumb in the output
    payload.
    """
    from agentmemory import signing
    from agentmemory.commands import wallet as _wallet

    # 1. explicit --keystore
    if cli_keystore:
        return {"ok": True, "keystore_path": cli_keystore,
                "source": "cli", "error": None}

    # 2. managed wallet (this is the 2.3.2 happy path for new users)
    managed = _wallet.resolve_wallet_path(None)
    if managed.exists():
        return {"ok": True, "keystore_path": str(managed),
                "source": "managed", "error": None}

    # 3. env fallback for 2.3.0 users who already wired this up
    env_path = os.environ.get("BRAINCTL_SIGNING_KEY_PATH")
    if env_path:
        return {"ok": True, "keystore_path": env_path,
                "source": "env", "error": None}

    # 4. agent-driven auto-setup
    if auto_setup:
        created = _wallet.wallet_new_impl(None, force=False)
        if not created["ok"]:
            return {"ok": False, "keystore_path": None,
                    "source": "auto-created",
                    "error": f"--auto-setup-wallet failed: {created['error']}"}
        return {"ok": True, "keystore_path": created["path"],
                "source": "auto-created", "error": None,
                "auto_created_address": created["address"]}

    # 5. nothing found, friendly error
    return {
        "ok": False, "keystore_path": None, "source": None,
        "error": (
            "No wallet found. Run `brainctl wallet new` to create one "
            "(takes 2 seconds, brainctl never sees the key). Or pass "
            "`--keystore <path>` to use an existing Solana CLI wallet, "
            "or `--auto-setup-wallet` to auto-create one inline."
        ),
    }


def cmd_export(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))

    if not getattr(args, "sign", False):
        # Future-proofing: leave room for an unsigned export mode later.
        _emit({
            "ok": False,
            "error": "brainctl export currently requires --sign. "
                     "(Plain unsigned exports are out of scope for 2.3.0.)",
        }, as_json=as_json, exit_code=2)

    # Late import — keeps the package import path solders-free.
    from agentmemory import signing

    db_path = _get_db_path()
    if not db_path.exists():
        _emit({"ok": False, "error": f"brain.db not found at {db_path}"},
              as_json=as_json, exit_code=1)

    auto_setup = bool(getattr(args, "auto_setup_wallet", False))
    resolved = _resolve_signer_keystore(
        getattr(args, "keystore", None), auto_setup=auto_setup,
    )
    if not resolved["ok"]:
        _emit({"ok": False, "error": resolved["error"]},
              as_json=as_json, exit_code=1)

    keystore_path = resolved["keystore_path"]
    keystore_source = resolved["source"]
    auto_created_address = resolved.get("auto_created_address")

    try:
        keypair = signing.load_keystore(keystore_path)
    except FileNotFoundError as e:
        _emit({"ok": False, "error": str(e)}, as_json=as_json, exit_code=1)
    except (ValueError, json.JSONDecodeError) as e:
        _emit({"ok": False, "error": f"invalid keystore: {e}"},
              as_json=as_json, exit_code=1)

    try:
        ids = _parse_ids(getattr(args, "ids", None))
    except ValueError as e:
        _emit({"ok": False, "error": str(e)}, as_json=as_json, exit_code=1)

    conn = _open_db(db_path)
    try:
        bundle = signing.build_bundle(
            conn,
            agent_id=getattr(args, "filter_agent", None),
            category=getattr(args, "category", None),
            scope=getattr(args, "scope", None),
            created_after=getattr(args, "created_after", None),
            created_before=getattr(args, "created_before", None),
            ids=ids,
        )
    finally:
        conn.close()

    signed = signing.sign_bundle(bundle, keypair)

    out_path_arg = getattr(args, "output", None)
    out_path = Path(out_path_arg).expanduser() if out_path_arg else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(signed, indent=2, default=str), encoding="utf-8")

    payload: Dict[str, Any] = {
        "ok": True,
        "bundle_path": str(out_path) if out_path else None,
        "bundle_hash": signed["bundle_hash_hex"],
        "signer_pubkey": signed["signer_pubkey_b58"],
        "memories_count": len(bundle["memories"]),
        "signed_at": signed["signed_at"],
        "pinned_onchain": False,
        "pin_skipped_reason": None,
        "signature": None,
        "slot": None,
        # Breadcrumb: which keystore source produced this bundle. Lets
        # the agent (or human) follow the trail back to the wallet.
        "keystore_source": keystore_source,
    }
    # Surface an "I just created a wallet for you" notice when
    # --auto-setup-wallet kicked in. Shown on stderr in human mode and
    # included in the JSON payload either way.
    if auto_created_address:
        payload["auto_created_wallet"] = {
            "address": auto_created_address,
            "path": keystore_path,
        }
        if not as_json:
            sys.stderr.write(
                "auto-created managed wallet at "
                f"{keystore_path} "
                f"(address: {auto_created_address}). "
                "Back it up with `brainctl wallet export <path>`.\n"
            )

    if getattr(args, "pin_onchain", False):
        rpc_url = getattr(args, "rpc_url", None) or signing.DEFAULT_RPC_URL
        # Pin cluster: derive from rpc_url. mainnet-beta if RPC is the
        # default or contains "mainnet"; otherwise devnet (so devnet
        # users default to no fee).
        pin_cluster = "mainnet-beta" if "mainnet" in rpc_url else "devnet"

        # Disclose protocol fee to the user before signing the tx.
        from agentmemory import protocol_fees as _pfees
        _fee_msg = _pfees.format_fee_disclosure(cluster=pin_cluster, op="pin")
        if _fee_msg and not as_json:
            sys.stderr.write(_fee_msg + "\n")

        # Pre-check the wallet's balance. If it's zero, the on-chain
        # pin would fail with an opaque "insufficient funds" RPC
        # error — we can do better by detecting this case up front
        # and printing an actionable message.
        #
        # IMPORTANT: a 0-SOL balance is foreseeable, not a failure.
        # The user's primary intent (sign the bundle) succeeded; the
        # pin was opportunistic. We exit 0 with pinned_onchain=False +
        # pin_skipped_reason="zero_balance". Genuine RPC / submit
        # failures (next branch) still exit 1 — they're unexpected.
        from agentmemory.commands import wallet as _wallet
        bal = _wallet.wallet_balance_impl(keystore_path, rpc_url=rpc_url)
        if bal["ok"] and bal["lamports"] == 0:
            payload["pin_skipped_reason"] = "zero_balance"
            msg = (
                f"Your wallet at {bal['address']} has 0 SOL. To pin "
                "on-chain (~$0.001 per pin), send any small amount "
                "of SOL to that address. Run `brainctl wallet "
                "balance` to check. Skipping the on-chain pin — the "
                "offline signature is still valid in your bundle file."
            )
            sys.stderr.write(msg + "\n")
            # Fall through to the no-pin happy path: the bundle has
            # already been signed and (if -o was passed) written.
        elif not bal["ok"]:
            # Couldn't read balance (RPC down, transient error). Don't
            # block the signing path — try the pin anyway and let it
            # report a real error if it fails.
            payload["pin_skipped_reason"] = None
            sys.stderr.write(
                f"warning: could not pre-check wallet balance "
                f"({bal['error']}); attempting pin anyway.\n"
            )
            pin = signing.pin_onchain(signed, keypair, rpc_url=rpc_url, cluster=pin_cluster)
            payload["pinned_onchain"] = bool(pin.get("ok"))
            payload["signature"] = pin.get("signature")
            payload["slot"] = pin.get("slot")
            if not pin.get("ok"):
                payload["error"] = f"on-chain pin failed: {pin.get('error')}"
                _emit(payload, as_json=as_json, exit_code=1)
        else:
            pin = signing.pin_onchain(signed, keypair, rpc_url=rpc_url, cluster=pin_cluster)
            payload["pinned_onchain"] = bool(pin.get("ok"))
            payload["signature"] = pin.get("signature")
            payload["slot"] = pin.get("slot")
            if not pin.get("ok"):
                payload["error"] = f"on-chain pin failed: {pin.get('error')}"
                # Bundle was still signed locally; don't drop that work — exit 1
                # so callers know the pin failed but the bundle is on disk.
                _emit(payload, as_json=as_json, exit_code=1)

    # --- Light Protocol compressed-token mint (v1 marketplace primitive) ---
    if getattr(args, "mint", False):
        from agentmemory import minting
        from agentmemory import protocol_fees as _pfees

        cluster = getattr(args, "cluster", minting.DEFAULT_CLUSTER)
        helius_api_key = getattr(args, "helius_api_key", None)
        mint_recipient = getattr(args, "mint_recipient", None) or signed["signer_pubkey_b58"]
        keys_dir_arg = getattr(args, "keys_dir", None)
        keys_dir = Path(keys_dir_arg).expanduser() if keys_dir_arg else None

        # Disclose protocol mint fee before invoking the mint.
        _mint_fee_msg = _pfees.format_fee_disclosure(cluster=cluster, op="mint")
        if _mint_fee_msg and not as_json:
            sys.stderr.write(_mint_fee_msg + "\n")

        mint_result = minting.mint_bundle(
            signed,
            owner_pubkey_b58=mint_recipient,
            cluster=cluster,
            helius_api_key=helius_api_key,
            keystore_path=keystore_path,
            keys_dir=keys_dir,
        )
        payload["minted"] = bool(mint_result.get("ok"))
        payload["mint_address"] = mint_result.get("mint")
        payload["mint_tx_signature"] = mint_result.get("tx_signature")
        payload["mint_cluster"] = mint_result.get("cluster")
        payload["arweave_metadata_uri"] = mint_result.get("arweave_metadata_uri")
        payload["arweave_ciphertext_uri"] = mint_result.get("arweave_ciphertext_uri")
        payload["bundle_key_path"] = mint_result.get("key_path")
        if mint_result.get("warning"):
            payload["mint_warning"] = mint_result["warning"]
        if not mint_result.get("ok"):
            payload["error"] = f"mint failed: {mint_result.get('error')}"
            # Local sign succeeded; surface the mint failure but don't
            # destroy the signing work. Exit 1 to flag the partial fail.
            _emit(payload, as_json=as_json, exit_code=1)

    if not out_path and not as_json:
        # No output file and not JSON mode — dump bundle to stdout so
        # the user gets *something* (mirrors `git format-patch -` UX).
        # In JSON mode we still want the summary payload, so suppress
        # the bundle dump there.
        sys.stderr.write(  # status to stderr to keep stdout pipeable
            f"signed {payload['memories_count']} memories "
            f"(hash={payload['bundle_hash'][:16]}..., "
            f"signer={payload['signer_pubkey'][:8]}...)\n"
        )
        print(json.dumps(signed, indent=2, default=str))
        sys.exit(0)

    _emit(payload, as_json=as_json, exit_code=0)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))
    bundle_path = Path(args.bundle_path).expanduser()

    if not bundle_path.exists():
        _emit({"ok": False, "error": f"bundle not found: {bundle_path}"},
              as_json=as_json, exit_code=1)

    try:
        signed = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _emit({"ok": False, "error": f"bundle is not valid JSON: {e}"},
              as_json=as_json, exit_code=1)

    from agentmemory import signing

    res = signing.verify_bundle(signed)
    payload: Dict[str, Any] = {
        "ok": bool(res.get("ok")),
        "signer_pubkey": res.get("signer_pubkey"),
        "signed_at": res.get("signed_at"),
        "bundle_hash": res.get("bundle_hash"),
        "memories_count": res.get("memories_count"),
        "error": res.get("error"),
        "checked_onchain": False,
        "found": None,
        "tx_signature": None,
        "block_time": None,
    }

    if not res.get("ok"):
        # Exit 1 for tamper / signature failures.
        _emit(payload, as_json=as_json, exit_code=1)

    if getattr(args, "check_onchain", False):
        rpc_url = getattr(args, "rpc_url", None) or signing.DEFAULT_RPC_URL
        on = signing.verify_onchain(
            res["bundle_hash"], res["signer_pubkey"], rpc_url=rpc_url,
        )
        payload["checked_onchain"] = True
        payload["found"] = bool(on.get("found"))
        payload["tx_signature"] = on.get("tx_signature")
        payload["block_time"] = on.get("block_time")
        if not on.get("found"):
            err = on.get("error") or "no matching memo-program tx found for this signer"
            payload["error"] = f"on-chain receipt missing: {err}"
            # Spec: exit 2 when --check-onchain was passed and the
            # receipt is missing.
            _emit(payload, as_json=as_json, exit_code=2)

    _emit(payload, as_json=as_json, exit_code=0)


# ---------------------------------------------------------------------------
# Parser registration (called from _impl.py's build_parser)
# ---------------------------------------------------------------------------

def register_parser(sub: Any) -> None:
    """Attach ``export`` and ``verify`` top-level subcommands."""
    # --- export ---
    p_exp = sub.add_parser(
        "export",
        help="Export memories as a signed JSON bundle (offline-verifiable, "
             "optionally pinned to Solana via the SPL memo program)",
        description=(
            "Export a filtered subset of brain.db memories as a JSON "
            "bundle, signed with your Solana keypair. The bundle stays "
            "on your machine; with --pin-onchain only the bundle's "
            "SHA-256 hash is published as a memo-program receipt."
        ),
    )
    p_exp.add_argument("--sign", action="store_true",
                       help="Sign the bundle (required in 2.3.0)")
    p_exp.add_argument("--keystore", default=None,
                       help="Path to a Solana CLI keystore (JSON array of 64 ints). "
                            "Default: ~/.brainctl/wallet.json (managed wallet, 2.3.2+); "
                            "falls back to $BRAINCTL_SIGNING_KEY_PATH for 2.3.0 users.")
    p_exp.add_argument("--auto-setup-wallet", dest="auto_setup_wallet",
                       action="store_true",
                       help="If no wallet exists, auto-create one at "
                            "~/.brainctl/wallet.json before signing (non-interactive). "
                            "Designed for agent-driven flows.")
    p_exp.add_argument("--filter-agent", dest="filter_agent", default=None,
                       help="Only export memories from this agent_id")
    p_exp.add_argument("--category", default=None,
                       help="Only export memories with this category")
    p_exp.add_argument("--scope", default=None,
                       help="Only export memories with this scope")
    p_exp.add_argument("--created-after", dest="created_after", default=None,
                       metavar="ISO_TS",
                       help="Only export memories created at/after this ISO timestamp")
    p_exp.add_argument("--created-before", dest="created_before", default=None,
                       metavar="ISO_TS",
                       help="Only export memories created at/before this ISO timestamp")
    p_exp.add_argument("--ids", default=None,
                       help="Comma-separated explicit memory IDs (overrides other filters)")
    p_exp.add_argument("--pin-onchain", dest="pin_onchain", action="store_true",
                       help="After signing, post the bundle hash as an SPL "
                            "memo-program transaction (~$0.001/pin)")
    p_exp.add_argument("--rpc-url", dest="rpc_url", default=None,
                       help="Solana RPC URL (default: mainnet-beta public RPC)")
    p_exp.add_argument("--mint", dest="mint", action="store_true",
                       help="Mint a Light Protocol compressed token "
                            "representing this signed bundle. Encrypted "
                            "content uploaded to Arweave, ownership token "
                            "minted to the signer wallet (or "
                            "--mint-recipient). Requires Node 20+ and the "
                            "tools/ helper (`cd tools && npm install`).")
    p_exp.add_argument("--cluster", default="devnet",
                       choices=["devnet", "mainnet-beta"],
                       help="Solana cluster for --mint (default: devnet). "
                            "mainnet-beta requires --helius-api-key and "
                            "real SOL for fees.")
    p_exp.add_argument("--helius-api-key", dest="helius_api_key",
                       default=None,
                       help="Helius API key for Photon RPC. Falls back to "
                            "$HELIUS_API_KEY. Required for mainnet-beta.")
    p_exp.add_argument("--mint-recipient", dest="mint_recipient",
                       default=None,
                       help="Base58 pubkey to receive the minted token "
                            "(default: signer wallet).")
    p_exp.add_argument("--keys-dir", dest="keys_dir", default=None,
                       help="Where to persist the per-bundle AES key on "
                            "disk (default: ~/.brainctl/keys).")
    p_exp.add_argument("-o", "--output", default=None,
                       help="Path to write the signed bundle JSON (default: stdout)")
    p_exp.add_argument("--json", action="store_true",
                       help="Emit a structured JSON summary on stdout")

    # --- verify ---
    p_ver = sub.add_parser(
        "verify",
        help="Verify a signed memory bundle (offline; --check-onchain "
             "additionally confirms the Solana memo-program receipt)",
    )
    p_ver.add_argument("bundle_path",
                       help="Path to the signed bundle JSON")
    p_ver.add_argument("--check-onchain", dest="check_onchain", action="store_true",
                       help="Also confirm a matching memo-program tx exists "
                            "for the signer (exit 2 if missing)")
    p_ver.add_argument("--rpc-url", dest="rpc_url", default=None,
                       help="Solana RPC URL (default: mainnet-beta public RPC)")
    p_ver.add_argument("--json", action="store_true",
                       help="Emit a structured JSON result on stdout")
