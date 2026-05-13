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
from pathlib import Path
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
    """Poll settlement status for a listing+buyer. With --auto-decrypt,
    also fetches + decrypts the bundle once released.
    """
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
        return

    # If released + --auto-decrypt, run the buyer-side pipeline.
    if getattr(args, "auto_decrypt", False) and result.get("status") == "released":
        from agentmemory import marketplace_buy as buy_helpers
        from agentmemory.commands import wallet as _wallet

        envelope_id = result.get("envelope_arweave_id")
        if not envelope_id:
            _emit({**result, "decrypt_error": "no envelope_arweave_id in status response"},
                  as_json=as_json, exit_code=1)
            return

        # Fetch the listing to get the encrypted_bundle_uri.
        try:
            detail = api.get_listing(api_base, args.listing_id, cluster=args.cluster)
            encrypted_uri = detail["listing"]["manifest"]["encrypted_bundle_uri"]
        except Exception as e:
            _emit({**result, "decrypt_error": f"could not resolve encrypted_bundle_uri: {e}"},
                  as_json=as_json, exit_code=1)
            return

        managed = _wallet.resolve_wallet_path(None)
        try:
            decoded = buy_helpers.post_release_pipeline(
                envelope_arweave_id=envelope_id,
                encrypted_bundle_uri=encrypted_uri,
                keystore_path=str(managed),
            )
        except Exception as e:
            _emit({**result, "decrypt_error": str(e)}, as_json=as_json, exit_code=1)
            return

        bundle = decoded["bundle"]
        out_path = getattr(args, "output", None)
        if out_path:
            Path(out_path).expanduser().write_text(
                json.dumps(bundle, indent=2, default=str), encoding="utf-8"
            )
        ingest_summary = None
        if getattr(args, "ingest", False):
            ingest_summary = buy_helpers.ingest_into_quarantine(
                bundle, listing_id=args.listing_id
            )

        _emit(
            {
                **result,
                "decrypted": True,
                "memories_count": len(bundle.get("memories", [])),
                "bundle_signer_pubkey": bundle.get("memories", [{}])[0].get("agent_id") if bundle.get("memories") else None,
                "output_path": out_path,
                "ingest_summary": ingest_summary,
            },
            as_json=as_json,
        )
        return

    _emit(result, as_json=as_json)


# ---------------------------------------------------------------------------
# settle (build the tx — caller signs + submits)
# ---------------------------------------------------------------------------

def cmd_list(args: Any) -> None:
    """Publish a signed memory bundle as a marketplace listing.

    Orchestration:
      1. Load the signed bundle from --bundle (output of brainctl export
         --sign).
      2. Build a listing manifest (USD-pegged price, visibility, expires_at)
         using agentmemory.marketplace.build_listing_manifest.
      3. Sign the manifest's canonical-JSON hash with the user's wallet.
      4. Upload the signed manifest to Arweave via the Node helper.
      5. Post the list memo to Solana via the Node helper.
      6. Register the listing with brainctl.org/api/marketplace/listings.
    """
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()

    # Auth first — saves us from doing all the on-chain work only to
    # discover the API rejects us.
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception as e:
        _emit({"ok": False, "error": f"auth_failed: {e}"}, as_json=as_json, exit_code=1)
        return

    # Load signed bundle.
    bundle_path = Path(args.bundle).expanduser()
    if not bundle_path.exists():
        _emit({"ok": False, "error": "bundle_not_found", "detail": str(bundle_path)},
              as_json=as_json, exit_code=1)
        return
    try:
        signed = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _emit({"ok": False, "error": f"bad_bundle_json: {e}"}, as_json=as_json, exit_code=1)
        return

    bundle_hash_hex = signed.get("bundle_hash_hex")
    signer_pubkey = signed.get("signer_pubkey_b58")
    if not bundle_hash_hex or not signer_pubkey:
        _emit({"ok": False, "error": "bundle_missing_fields",
               "detail": "needs bundle_hash_hex + signer_pubkey_b58"},
              as_json=as_json, exit_code=1)
        return
    if signer_pubkey != pubkey:
        _emit({"ok": False, "error": "bundle_signer_mismatch",
               "detail": f"bundle was signed by {signer_pubkey[:8]}…, "
                         f"your wallet is {pubkey[:8]}…"},
              as_json=as_json, exit_code=1)
        return

    # Build preview from bundle memories.
    from agentmemory import marketplace as mp
    from agentmemory import signing
    preview = mp.build_preview_from_bundle(
        signed["bundle"], description=args.description
    )

    # Build listing manifest (currency=USD, capped at $10k).
    manifest = mp.build_listing_manifest(
        bundle_hash=bundle_hash_hex,
        seller_pubkey_b58=pubkey,
        price_usd=float(args.price_usd),
        duration_hours=float(args.duration_hours),
        encrypted_bundle_uri=args.encrypted_bundle_uri,
        metadata_uri=args.metadata_uri,
        preview=preview,
        visibility=args.visibility,
        treasury_pubkey=args.treasury_pubkey if hasattr(args, "treasury_pubkey") else None,
    )

    # Sign the manifest hash.
    from agentmemory.commands import wallet as _wallet
    keystore = str(_wallet.resolve_wallet_path(None))
    keypair = signing.load_keystore(keystore)
    h = mp.listing_hash(manifest)
    sig = keypair.sign_message(h)
    manifest["signature_b58"] = str(sig)

    # Upload to Arweave via the Node helper.
    upload = api.upload_manifest_to_arweave(
        manifest=manifest,
        schema="brainctl-marketplace/v1/listing",
        cluster=args.cluster,
    )
    if not upload.get("ok"):
        _emit({"ok": False, **upload}, as_json=as_json, exit_code=1)
        return
    listing_arweave_id = upload["arweave_id"]

    # Post the list memo (with protocol fee disclosure + bundled fee tx).
    list_memo = mp.format_list_memo(listing_arweave_id, bundle_hash_hex)
    from agentmemory import protocol_fees as _pfees
    _fee_msg = _pfees.format_fee_disclosure(cluster=args.cluster, op="list")
    if _fee_msg and not as_json:
        sys.stderr.write(_fee_msg + "\n")
    post = api.post_marketplace_memo(memo=list_memo, cluster=args.cluster, op="list")
    if not post.get("ok"):
        _emit({"ok": False, **post,
               "arweave_id": listing_arweave_id,
               "hint": "manifest is on Arweave but the list memo didn't post — "
                       "retry with the same listing_arweave_id"},
              as_json=as_json, exit_code=1)
        return
    list_tx = post["tx_signature"]

    # Register with the API.
    try:
        result = api.create_listing(
            api_base,
            manifest=manifest,
            listing_arweave_id=listing_arweave_id,
            list_tx_signature=list_tx,
            cluster=args.cluster,
            session_token=token,
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload,
               "arweave_id": listing_arweave_id, "list_tx": list_tx,
               "hint": "memo + manifest are on chain — API rejected registration. "
                       "Check the error detail."},
              as_json=as_json, exit_code=1)
        return

    _emit({
        **result,
        "arweave_id": listing_arweave_id,
        "list_tx_signature": list_tx,
        "price_usd": float(args.price_usd),
        "visibility": args.visibility,
    }, as_json=as_json)


# ---------------------------------------------------------------------------
# Negotiation: offer / counter / accept / reject / withdraw / offers
# ---------------------------------------------------------------------------

def _derive_x25519_pub_b58(keystore_path: str) -> str:
    """Read the wallet keystore + return its X25519 pubkey in base58."""
    from agentmemory import marketplace as mp
    from agentmemory import signing
    import base58
    kp = signing.load_keystore(keystore_path)
    secret = bytes(kp)[:32]
    x_seed = mp.ed25519_to_x25519_seed(secret)
    from nacl.public import PrivateKey
    x_priv = PrivateKey(x_seed)
    return base58.b58encode(bytes(x_priv.public_key)).decode("ascii")


def _post_memo_or_die(
    memo: str,
    cluster: str,
    as_json: bool,
    *,
    op: str = "marketplace_op",
) -> str:
    from agentmemory import protocol_fees as _pfees
    _fee_msg = _pfees.format_fee_disclosure(cluster=cluster, op=op)
    if _fee_msg and not as_json:
        sys.stderr.write(_fee_msg + "\n")
    post = api.post_marketplace_memo(memo=memo, cluster=cluster, op=op)
    if not post.get("ok"):
        _emit({"ok": False, **post}, as_json=as_json, exit_code=1)
    return post["tx_signature"]


def _sign_manifest_and_upload(
    manifest: Dict[str, Any],
    schema: str,
    keystore_path: str,
    cluster: str,
    as_json: bool,
) -> Dict[str, Any]:
    """Add signature_b58 to manifest, upload to Arweave, return upload result."""
    from agentmemory import marketplace as mp
    from agentmemory import signing
    keypair = signing.load_keystore(keystore_path)
    h = mp.manifest_hash(manifest)
    sig = keypair.sign_message(h)
    manifest["signature_b58"] = str(sig)
    upload = api.upload_manifest_to_arweave(
        manifest=manifest, schema=schema, cluster=cluster
    )
    if not upload.get("ok"):
        _emit({"ok": False, **upload}, as_json=as_json, exit_code=1)
    return upload


def cmd_offers(args: Any) -> None:
    """List open offers on a listing. Auction offers are public;
    private-mode offers are returned only if you're the seller or the
    offerer.
    """
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    token: str | None
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception:
        token = None
    try:
        result = api.list_offers(
            api_base, args.listing_id, cluster=args.cluster, session_token=token
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload}, as_json=as_json, exit_code=1)
        return
    if as_json:
        _emit(result, as_json=True)
    offers = result.get("offers", [])
    if not offers:
        print("no visible offers on this listing.")
        sys.exit(0)
    for o in offers:
        m = (o or {}).get("manifest") or {}
        print(
            f"  ${m.get('offered_price_usd', '?'):>7.2f}  "
            f"{m.get('offer_id','?'):>26}  "
            f"by {(m.get('buyer_pubkey') or '?')[:8]}…  "
            f"{(m.get('visibility','?')+'')[:7]:7}  "
            f"expires {m.get('expires_at','?')[:19]}"
        )
    sys.exit(0)


def cmd_offer(args: Any) -> None:
    """Buyer: submit an offer on a listing."""
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception as e:
        _emit({"ok": False, "error": f"auth_failed: {e}"}, as_json=as_json, exit_code=1)
        return

    from agentmemory import marketplace as mp
    from agentmemory.commands import wallet as _wallet
    keystore = str(_wallet.resolve_wallet_path(None))
    x25519_b58 = _derive_x25519_pub_b58(keystore)

    try:
        manifest = mp.build_offer_manifest(
            listing_id=args.listing_id,
            buyer_pubkey_b58=pubkey,
            buyer_x25519_pubkey_b58=x25519_b58,
            offered_price_usd=float(args.price_usd),
            visibility=args.visibility,
            message=args.message,
            ttl_hours=float(args.expires_hours),
        )
    except ValueError as e:
        _emit({"ok": False, "error": "bad_offer", "detail": str(e)},
              as_json=as_json, exit_code=1)
        return

    upload = _sign_manifest_and_upload(
        manifest, f"{mp.MARKETPLACE_SCHEMA}/offer", keystore, args.cluster, as_json
    )
    offer_arweave_id = upload["arweave_id"]

    offer_memo = mp.format_offer_memo(args.listing_id, offer_arweave_id, pubkey)
    offer_tx = _post_memo_or_die(offer_memo, args.cluster, as_json, op="offer")

    try:
        result = api.create_offer(
            api_base,
            args.listing_id,
            manifest=manifest,
            offer_arweave_id=offer_arweave_id,
            offer_tx_signature=offer_tx,
            cluster=args.cluster,
            session_token=token,
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload,
               "offer_arweave_id": offer_arweave_id, "offer_tx": offer_tx,
               "hint": "manifest + memo are on chain; API rejected registration."},
              as_json=as_json, exit_code=1)
        return

    _emit({
        **result,
        "offer_arweave_id": offer_arweave_id,
        "offer_tx_signature": offer_tx,
        "offer_id": manifest["offer_id"],
        "offered_price_usd": float(args.price_usd),
    }, as_json=as_json)


def cmd_counter(args: Any) -> None:
    """Counter an existing offer (seller or another counter-er)."""
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception as e:
        _emit({"ok": False, "error": f"auth_failed: {e}"}, as_json=as_json, exit_code=1)
        return

    from agentmemory import marketplace as mp
    from agentmemory.commands import wallet as _wallet
    keystore = str(_wallet.resolve_wallet_path(None))

    try:
        manifest = mp.build_counter_manifest(
            parent_offer_id=args.offer_id,
            from_pubkey_b58=pubkey,
            counter_price_usd=float(args.price_usd),
            message=args.message,
            ttl_hours=float(args.expires_hours),
        )
    except ValueError as e:
        _emit({"ok": False, "error": "bad_counter", "detail": str(e)},
              as_json=as_json, exit_code=1)
        return

    upload = _sign_manifest_and_upload(
        manifest, f"{mp.MARKETPLACE_SCHEMA}/counter", keystore, args.cluster, as_json
    )
    counter_arweave_id = upload["arweave_id"]

    counter_memo = mp.format_counter_memo(args.offer_id, counter_arweave_id, pubkey)
    counter_tx = _post_memo_or_die(counter_memo, args.cluster, as_json, op="counter")

    try:
        result = api.counter_offer(
            api_base,
            args.offer_id,
            manifest=manifest,
            counter_arweave_id=counter_arweave_id,
            counter_tx_signature=counter_tx,
            cluster=args.cluster,
            session_token=token,
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload,
               "counter_arweave_id": counter_arweave_id, "counter_tx": counter_tx},
              as_json=as_json, exit_code=1)
        return

    _emit({
        **result,
        "counter_arweave_id": counter_arweave_id,
        "counter_tx_signature": counter_tx,
        "counter_price_usd": float(args.price_usd),
    }, as_json=as_json)


def _cmd_offer_action(args: Any, action: str) -> None:
    """Shared body for accept / reject / withdraw — no manifest, just memo+API."""
    as_json = bool(getattr(args, "json", False))
    api_base = api.api_base_from_env()
    pubkey = _resolve_wallet_pubkey()
    try:
        token = api.ensure_session(api_base, pubkey)
    except Exception as e:
        _emit({"ok": False, "error": f"auth_failed: {e}"}, as_json=as_json, exit_code=1)
        return

    from agentmemory import marketplace as mp
    formatter = {
        "accept": mp.format_accept_memo,
        "reject": mp.format_reject_memo,
        "withdraw": mp.format_withdraw_memo,
    }[action]
    memo = formatter(args.offer_id)
    tx = _post_memo_or_die(memo, args.cluster, as_json, op=action)

    api_caller = {
        "accept": api.accept_offer,
        "reject": api.reject_offer,
        "withdraw": api.withdraw_offer,
    }[action]
    try:
        result = api_caller(
            api_base, args.offer_id,
            tx_signature=tx, cluster=args.cluster, session_token=token,
        )
    except api.MarketplaceApiError as e:
        _emit({"ok": False, "error": str(e), **e.payload, "tx_signature": tx},
              as_json=as_json, exit_code=1)
        return

    _emit({**result, "tx_signature": tx, "action": action}, as_json=as_json)


def cmd_accept(args: Any) -> None:
    _cmd_offer_action(args, "accept")


def cmd_reject(args: Any) -> None:
    _cmd_offer_action(args, "reject")


def cmd_withdraw(args: Any) -> None:
    _cmd_offer_action(args, "withdraw")


def cmd_listen(args: Any) -> None:
    """Seller daemon: watch for buy memos + release bundle keys."""
    from agentmemory.marketplace_listen import listen_loop
    try:
        listen_loop(
            cluster=args.cluster,
            poll_interval_seconds=float(args.poll_interval),
            max_iterations=args.max_iterations if args.max_iterations > 0 else None,
            verbose=not bool(args.quiet),
        )
    except KeyboardInterrupt:
        print("\n[listen] stopped by user", flush=True)
    except Exception as e:
        print(f"[listen] fatal: {e}", flush=True)
        import sys as _sys
        _sys.exit(1)


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
    p_status.add_argument("--auto-decrypt", dest="auto_decrypt", action="store_true",
                          help="When the seller has released, fetch the SealedBox envelope, "
                               "decrypt to recover the bundle key, fetch + AES-decrypt the bundle, "
                               "return the bundle inline. Implies --wait if --wait isn't set.")
    p_status.add_argument("--ingest", action="store_true",
                          help="Combine with --auto-decrypt: insert the decrypted memories into "
                               "your brain.db under scope=imported:<listing_id> (quarantined). "
                               "Promotion to your primary scope is a separate explicit step.")
    p_status.add_argument("--output", default=None,
                          help="Combine with --auto-decrypt: write the decrypted bundle JSON to this path.")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    # ----- list (seller publishes a signed bundle) -----
    p_list = api_op.add_parser(
        "list",
        help=(
            "Publish a signed memory bundle as a marketplace listing. "
            "Reads bundle from --bundle (output of `brainctl export --sign`), "
            "builds + signs the listing manifest, uploads to Arweave, posts "
            "the list memo on Solana, and registers with brainctl.org."
        ),
    )
    p_list.add_argument("--bundle", required=True,
                        help="Path to a signed bundle JSON (from `brainctl export --sign -o ...`)")
    p_list.add_argument("--price-usd", dest="price_usd", required=True, type=float,
                        help="Listing price in USD (capped at $10,000)")
    p_list.add_argument("--duration-hours", dest="duration_hours", type=float, default=24,
                        help="Listing TTL in hours (default 24, max 720)")
    p_list.add_argument("--visibility", default="auction",
                        choices=["auction", "private"],
                        help="auction = offers public; private = offers visible only to seller + offerer")
    p_list.add_argument("--description", default=None,
                        help="Optional 1-line pitch surfaced in browse")
    p_list.add_argument("--encrypted-bundle-uri", dest="encrypted_bundle_uri",
                        required=True,
                        help="ar://... — the encrypted bundle ciphertext URI (typically the output of `brainctl export --sign --mint`)")
    p_list.add_argument("--metadata-uri", dest="metadata_uri",
                        required=True,
                        help="ar://... — the metadata URI the JIT-minted cNFT will reference at settlement")
    p_list.add_argument("--treasury-pubkey", dest="treasury_pubkey", default=None,
                        help="Override the protocol treasury (advanced)")
    p_list.add_argument("--cluster", default="mainnet-beta",
                        choices=["mainnet-beta", "devnet"])
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    # ----- listen (seller daemon) -----
    p_listen = api_op.add_parser(
        "listen",
        help=(
            "Seller-side daemon: watch the seller's wallet for buy "
            "memos, JIT-mint a cNFT to the buyer, SealedBox-encrypt "
            "the bundle key, upload the envelope to Arweave, post "
            "the release memo. Foreground process; run under tmux / "
            "screen / systemd."
        ),
    )
    p_listen.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_listen.add_argument("--poll-interval", dest="poll_interval", type=float, default=10.0,
                          help="Seconds between Solana scans (default 10)")
    p_listen.add_argument("--max-iterations", dest="max_iterations", type=int, default=0,
                          help="Stop after N iterations (0 = run forever; useful for tests)")
    p_listen.add_argument("--quiet", action="store_true",
                          help="Suppress per-event log lines")
    p_listen.set_defaults(func=cmd_listen)

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

    # ----- offers (list offers on a listing) -----
    p_offers = api_op.add_parser(
        "offers",
        help=(
            "List open offers on a listing. Auction-mode offers are public; "
            "private-mode offers are visible only to the seller + the offerer."
        ),
    )
    p_offers.add_argument("listing_id")
    p_offers.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_offers.add_argument("--json", action="store_true")
    p_offers.set_defaults(func=cmd_offers)

    # ----- offer (buyer creates offer) -----
    p_offer = api_op.add_parser(
        "offer",
        help=(
            "Submit a buy-side offer on a listing. Builds + signs the offer "
            "manifest, uploads to Arweave, posts the offer memo, registers "
            "with the marketplace API. TTL capped at 24h."
        ),
    )
    p_offer.add_argument("listing_id")
    p_offer.add_argument("--price-usd", dest="price_usd", required=True, type=float,
                         help="Offered price in USD")
    p_offer.add_argument("--expires-hours", dest="expires_hours", type=float, default=24,
                         help="Offer TTL in hours (default 24, max 24)")
    p_offer.add_argument("--visibility", default="private",
                         choices=["auction", "private"],
                         help="auction = offer is public on the listing; private = only seller sees")
    p_offer.add_argument("--message", default=None,
                         help="Optional message to seller (max 280 chars)")
    p_offer.add_argument("--cluster", default="mainnet-beta",
                         choices=["mainnet-beta", "devnet"])
    p_offer.add_argument("--json", action="store_true")
    p_offer.set_defaults(func=cmd_offer)

    # ----- counter (counter an existing offer) -----
    p_counter = api_op.add_parser(
        "counter",
        help=(
            "Counter an existing offer with a new price. Either side of a "
            "negotiation can counter — the chain audit trail captures the "
            "full thread."
        ),
    )
    p_counter.add_argument("offer_id")
    p_counter.add_argument("--price-usd", dest="price_usd", required=True, type=float,
                           help="Counter price in USD")
    p_counter.add_argument("--expires-hours", dest="expires_hours", type=float, default=24,
                           help="Counter TTL in hours (default 24, max 24)")
    p_counter.add_argument("--message", default=None,
                           help="Optional message (max 280 chars)")
    p_counter.add_argument("--cluster", default="mainnet-beta",
                           choices=["mainnet-beta", "devnet"])
    p_counter.add_argument("--json", action="store_true")
    p_counter.set_defaults(func=cmd_counter)

    # ----- accept / reject / withdraw -----
    p_accept = api_op.add_parser(
        "accept", help="Seller-side: accept an offer (unlocks the settlement path)"
    )
    p_accept.add_argument("offer_id")
    p_accept.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_accept.add_argument("--json", action="store_true")
    p_accept.set_defaults(func=cmd_accept)

    p_reject = api_op.add_parser(
        "reject", help="Seller-side: reject an offer"
    )
    p_reject.add_argument("offer_id")
    p_reject.add_argument("--cluster", default="mainnet-beta",
                          choices=["mainnet-beta", "devnet"])
    p_reject.add_argument("--json", action="store_true")
    p_reject.set_defaults(func=cmd_reject)

    p_withdraw = api_op.add_parser(
        "withdraw", help="Buyer-side: retract an offer you posted"
    )
    p_withdraw.add_argument("offer_id")
    p_withdraw.add_argument("--cluster", default="mainnet-beta",
                            choices=["mainnet-beta", "devnet"])
    p_withdraw.add_argument("--json", action="store_true")
    p_withdraw.set_defaults(func=cmd_withdraw)


__all__ = ["register_parser"]
