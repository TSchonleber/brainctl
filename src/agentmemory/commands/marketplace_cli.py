"""CLI handlers for ``brainctl marketplace api ...``.

Thin layer over ``agentmemory.marketplace_api``. Each subcommand
turns its argparse Namespace into a single REST call (or a small
orchestration of calls) and prints the result either as a JSON blob
(``--json``) or a short human-readable summary.

The CLI is *deliberately* shaped to be agent-driven — every command
has structured JSON output, machine-readable exit codes, and minimal
prompts. The brainctl coordination layer can invoke this directly via
subprocess without needing to scrape stdout.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict

from agentmemory import marketplace_api as api


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit(payload: Dict[str, Any], *, as_json: bool, exit_code: int = 0) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        ok = payload.get("ok")
        if ok is False or payload.get("error"):
            print(f"FAIL: {payload.get('error', 'unknown')}", file=sys.stderr)
            if payload.get("detail"):
                print(f"      {payload['detail']}", file=sys.stderr)
        elif ok is True:
            print("OK")
        # Print the most useful side-fields in a stable order.
        for key in (
            "session_token", "expires_at", "listing_id", "offer_id",
            "mint_address", "tx_signature", "status",
            "price_usd_final", "currency", "unit_usd_price",
            "tx_base64", "envelope_arweave_id", "minted_cnft_address",
            "release_tx_signature",
        ):
            if key in payload and payload[key] is not None:
                val = payload[key]
                # Truncate long base64-encoded txs in human mode so
                # they don't flood the terminal.
                if isinstance(val, str) and len(val) > 88:
                    val = val[:80] + "…"
                print(f"  {key}: {val}")
    sys.exit(exit_code)


def _resolve_wallet_pubkey() -> str:
    """Get the user's brainctl wallet pubkey for auth purposes."""
    from agentmemory import signing
    from agentmemory.commands import wallet as _wallet

    managed = _wallet.resolve_wallet_path(None)
    if not managed.exists():
        print(
            "no wallet found at ~/.brainctl/wallet.json — run "
            "`brainctl wallet new --yes` first",
            file=sys.stderr,
        )
        sys.exit(1)
    keypair = signing.load_keystore(str(managed))
    return str(keypair.pubkey())


# ---------------------------------------------------------------------------
# login / logout
# ---------------------------------------------------------------------------

def cmd_login(args: Any) -> None:
    """Authenticate with the marketplace API (challenge-response)."""
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    try:
        token = api.ensure_session(api_base, pubkey)
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload}, as_json=as_json, exit_code=1)
    except Exception as e:
        _emit({"ok": False, "error": str(e)}, as_json=as_json, exit_code=1)
    _emit(
        {
            "ok": True,
            "api_base": api_base,
            "pubkey": pubkey,
            "session_token": token,
        },
        as_json=as_json,
    )


def cmd_logout(args: Any) -> None:
    """Drop the persisted session token."""
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    api.clear_session(api_base, pubkey)
    _emit({"ok": True, "api_base": api_base, "pubkey": pubkey}, as_json=as_json)


# ---------------------------------------------------------------------------
# browse / show
# ---------------------------------------------------------------------------

def cmd_browse(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    try:
        result = api.browse_listings(
            api_base,
            cluster=args.cluster,
            max_price_usd=getattr(args, "max_price_usd", None),
            category=getattr(args, "category", None),
            visibility=getattr(args, "visibility", None),
            seller_pubkey=getattr(args, "seller_pubkey", None),
            limit=getattr(args, "limit", 20),
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload}, as_json=as_json, exit_code=1)
    if as_json:
        _emit(result, as_json=True)
    # Human-readable browse table.
    listings = result.get("listings", [])
    if not listings:
        print("no open listings on this cluster.")
        sys.exit(0)
    for entry in listings:
        m = entry.get("manifest") or {}
        pricing = m.get("pricing") or {}
        print(
            f"  ${pricing.get('price_usd', '?'):>7.2f}  "
            f"{(m.get('visibility','?')+'')[:7]:7}  "
            f"{m.get('listing_id','?')}  "
            f"by {(m.get('seller_pubkey') or '?')[:8]}…  "
            f"{(m.get('preview',{}).get('description') or '')[:60]}"
        )
    sys.exit(0)


def cmd_show(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    token: str | None
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception:
        token = None  # public read is allowed
    try:
        result = api.get_listing(api_base, args.listing_id, cluster=args.cluster, session_token=token)
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload}, as_json=as_json, exit_code=1)
    _emit(result, as_json=as_json)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: Any) -> None:
    """Poll settlement status for a listing+buyer."""
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    try:
        if getattr(args, "wait", False):
            result = api.poll_settlement_until_released(
                api_base, args.listing_id, buyer_pubkey=pubkey,
                cluster=args.cluster,
                timeout_seconds=int(getattr(args, "timeout", 120)),
            )
        else:
            result = api.settlement_status(
                api_base, args.listing_id, buyer_pubkey=pubkey, cluster=args.cluster
            )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload}, as_json=as_json, exit_code=1)
    _emit(result, as_json=as_json)


# ---------------------------------------------------------------------------
# settle (build the tx — caller signs + submits)
# ---------------------------------------------------------------------------

def cmd_settle(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception as e:
        _emit({"ok": False, "error": f"auth_failed: {e}"}, as_json=as_json, exit_code=1)
        return  # unreachable — _emit calls sys.exit
    try:
        result = api.build_settle_tx(
            api_base,
            args.listing_id,
            buyer_x25519_pubkey=args.buyer_x25519_pubkey,
            cluster=args.cluster,
            session_token=token,
            offer_id=getattr(args, "offer_id", None),
            currency=getattr(args, "currency", None),
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload}, as_json=as_json, exit_code=1)
        return

    if not getattr(args, "submit", False):
        # Build-only path. Caller signs + submits themselves.
        _emit(result, as_json=as_json)
        return

    # --submit: sign with the user's wallet + submit to RPC.
    from agentmemory.commands import wallet as _wallet
    managed = _wallet.resolve_wallet_path(None)
    submit_result = api.sign_and_submit_settle_tx(
        tx_base64=result["tx_base64"],
        keystore_path=str(managed),
        cluster=args.cluster,
    )
    merged = {**result, **submit_result}
    if not submit_result.get("ok"):
        _emit(merged, as_json=as_json, exit_code=1)
        return
    _emit(merged, as_json=as_json)


# ---------------------------------------------------------------------------
# Parser registration (called from _impl.py's build_parser)
# ---------------------------------------------------------------------------

def register_parser(sub: Any) -> None:
    """Attach ``marketplace`` top-level subcommand + its api/* tree."""
    p = sub.add_parser(
        "marketplace",
        help="Memory marketplace operations (list / browse / negotiate / settle)",
        description=(
            "The brainctl memory marketplace runs at brainctl.org. "
            "Listings are chain-canonical (Solana memos + Arweave "
            "manifests); negotiations are signed proofs; settlement is "
            "trustless. This CLI wraps the REST API at /api/marketplace."
        ),
    )
    api_sub = p.add_subparsers(dest="marketplace_command", required=True)

    # ----- api login / logout -----
    p_api = api_sub.add_parser(
        "api",
        help="REST API operations (browse, list, offer, accept, settle, …)",
    )
    api_op = p_api.add_subparsers(dest="marketplace_api_op", required=True)

    p_login = api_op.add_parser("login", help="Authenticate via wallet signature")
    p_login.add_argument("--json", action="store_true")
    p_login.set_defaults(func=cmd_login)

    p_logout = api_op.add_parser("logout", help="Drop the persisted session")
    p_logout.add_argument("--json", action="store_true")
    p_logout.set_defaults(func=cmd_logout)

    # ----- browse / show -----
    p_browse = api_op.add_parser("browse", help="List open marketplace listings")
    p_browse.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_browse.add_argument("--max-price-usd", dest="max_price_usd", type=float, default=None)
    p_browse.add_argument("--category", default=None)
    p_browse.add_argument("--visibility", default=None,
                          choices=["auction", "private"])
    p_browse.add_argument("--seller-pubkey", dest="seller_pubkey", default=None)
    p_browse.add_argument("--limit", type=int, default=20)
    p_browse.add_argument("--json", action="store_true")
    p_browse.set_defaults(func=cmd_browse)

    p_show = api_op.add_parser("show", help="Show a single listing's detail")
    p_show.add_argument("listing_id")
    p_show.add_argument("--cluster", default="mainnet-beta",
                        choices=["mainnet-beta", "devnet"])
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    # ----- status (settlement polling) -----
    p_status = api_op.add_parser(
        "status",
        help="Poll settlement status for a listing (waits for the seller's release if --wait)",
    )
    p_status.add_argument("listing_id")
    p_status.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_status.add_argument("--wait", action="store_true",
                          help="Poll until status=released or --timeout elapses")
    p_status.add_argument("--timeout", type=int, default=120,
                          help="Max seconds to wait when --wait is set (default 120)")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    # ----- settle -----
    p_settle = api_op.add_parser(
        "settle",
        help=(
            "Build a settlement transaction for an accepted offer (or a "
            "static auction-mode buy). Returns the partially-signed tx "
            "the caller signs + submits."
        ),
    )
    p_settle.add_argument("listing_id")
    p_settle.add_argument("--buyer-x25519-pubkey", dest="buyer_x25519_pubkey",
                          required=True,
                          help="Buyer's X25519 pubkey (base58) for the SealedBox release")
    p_settle.add_argument("--offer-id", dest="offer_id", default=None,
                          help="Required for negotiated buys; omit for static auction price")
    p_settle.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_settle.add_argument("--currency", default=None,
                          choices=["SOL", "BRNDB"],
                          help="Override payment currency (default: server picks SOL until BRNDB launches)")
    p_settle.add_argument("--submit", action="store_true",
                          help="Sign + submit the tx in one shot (uses ~/.brainctl/wallet.json). "
                               "Without --submit, the base64 tx is returned for manual signing.")
    p_settle.add_argument("--json", action="store_true")
    p_settle.set_defaults(func=cmd_settle)


__all__ = ["register_parser"]
