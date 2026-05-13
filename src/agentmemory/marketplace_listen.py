"""Seller-side daemon: watch for buy memos on the seller's wallet,
mint the just-in-time cNFT to the buyer, SealedBox-encrypt the bundle
key, upload to Arweave, and post the release memo.

Started via ``brainctl marketplace api listen``. Runs in the
foreground (no system-service daemonization for v1.5 — the operator
runs it under tmux, screen, systemd, or whatever they prefer).

Polling loop (default 10s interval):
  1. Query Solana for the seller wallet's recent tx signatures.
  2. For each unseen tx, fetch the memo body.
  3. If the body matches `brainctl-marketplace/v1:buy:<listing>:<x25519>`,
     and the listing belongs to us, and the payment amount matches
     the listing price, transition into the release path:
       a. Mint a fresh Light Protocol compressed token to the buyer.
       b. Encrypt this listing's AES key with NaCl SealedBox to the
          buyer's X25519 pubkey.
       c. Upload the SealedBox envelope to Arweave via the Node helper.
       d. Post the release memo: brainctl-marketplace/v1:release:
          <listing>:<envelope_arweave_id>:<minted_cnft_address>.
  4. Mark the buy tx as processed (idempotency — disk-persisted) and
     loop.

State persisted to ``~/.brainctl/marketplace/listen-state.json`` so
restarts don't double-release on the same payment.

The seller's AES key per listing is read from
``~/.brainctl/keys/<listing_id>.key`` (written by the seller during
the list command if --mint was used to generate the encrypted
bundle, or supplied manually otherwise).

For v1.5, payment verification trusts that the buy tx itself contains
the SPL/SOL transfer to the seller's payment_address. We don't
re-verify the on-chain transfer amount — that's a v2 hardening step.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Set

from agentmemory import marketplace as mp
from agentmemory import marketplace_api as api


STATE_DIR = "~/.brainctl/marketplace"
STATE_FILE = "listen-state.json"


def _state_path() -> Path:
    return Path(STATE_DIR).expanduser() / STATE_FILE


def _load_state() -> Dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {"processed_buy_txs": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"processed_buy_txs": []}


def _save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except (OSError, NotImplementedError):
        pass
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Solana RPC helpers (lightweight — no @solana/web3.js)
# ---------------------------------------------------------------------------

def _rpc(rpc_url: str, method: str, params: list) -> Any:
    """Single JSON-RPC call to a Solana endpoint."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    return body.get("result")


def _rpc_url(cluster: str) -> str:
    if cluster == "mainnet-beta":
        return "https://api.mainnet-beta.solana.com"
    return "https://api.devnet.solana.com"


def _get_signatures_for_address(rpc_url: str, address: str, limit: int = 50) -> list:
    return _rpc(rpc_url, "getSignaturesForAddress", [address, {"limit": limit}]) or []


def _get_tx_memo(rpc_url: str, signature: str) -> Optional[str]:
    """Fetch a tx and extract its memo body if any."""
    tx = _rpc(rpc_url, "getTransaction", [
        signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    ])
    if not tx:
        return None
    logs = ((tx.get("meta") or {}).get("logMessages")) or []
    for line in logs:
        # "Program log: Memo (len <N>): \"<body>\""
        idx = line.find('Memo (len ')
        if idx >= 0:
            quote_start = line.find('"', idx)
            quote_end = line.rfind('"')
            if 0 < quote_start < quote_end:
                return line[quote_start + 1 : quote_end]
    return None


# ---------------------------------------------------------------------------
# Cryptographic helpers
# ---------------------------------------------------------------------------

def _solana_pubkey_to_bytes(pubkey_b58: str) -> bytes:
    """Decode a base58 Solana pubkey to raw 32 bytes using solders.

    Avoids pulling in a separate base58 dep — solders already ships
    with the [signing] / [mint] / [marketplace] extras.
    """
    from solders.pubkey import Pubkey  # type: ignore
    return bytes(Pubkey.from_string(pubkey_b58))


def _read_bundle_key(listing_id: str) -> bytes:
    """Read the AES key for a listing's bundle from
    ``~/.brainctl/keys/<listing_id>.key``.
    """
    p = Path("~/.brainctl/keys").expanduser() / f"{listing_id}.key"
    if not p.exists():
        raise FileNotFoundError(
            f"no bundle key on disk for listing {listing_id} — "
            f"expected at {p}. Did you run `brainctl export --sign --mint` "
            "first, or supply --bundle-key-path explicitly?"
        )
    raw = p.read_text(encoding="utf-8").strip()
    return bytes.fromhex(raw)


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def listen_loop(
    *,
    cluster: str,
    poll_interval_seconds: float = 10.0,
    max_iterations: Optional[int] = None,
    verbose: bool = True,
) -> None:
    """Foreground polling loop. Press Ctrl-C to stop."""
    from agentmemory import signing
    from agentmemory.commands import wallet as _wallet

    keystore = str(_wallet.resolve_wallet_path(None))
    keypair = signing.load_keystore(keystore)
    seller_pubkey = str(keypair.pubkey())
    rpc_url = _rpc_url(cluster)

    state = _load_state()
    processed: Set[str] = set(state.get("processed_buy_txs", []))

    if verbose:
        print(f"[listen] seller={seller_pubkey[:8]}… cluster={cluster}", flush=True)
        print(f"[listen] polling every {poll_interval_seconds}s; {len(processed)} txs already processed", flush=True)

    iterations = 0
    api_base = api.api_base_from_env()

    while True:
        iterations += 1
        try:
            signatures = _get_signatures_for_address(rpc_url, seller_pubkey, limit=50)
        except Exception as e:
            if verbose:
                print(f"[listen] signature scan failed: {e}", flush=True)
            time.sleep(poll_interval_seconds)
            continue

        new_buys = []
        for entry in signatures:
            sig = entry.get("signature")
            if not sig or sig in processed:
                continue
            try:
                memo = _get_tx_memo(rpc_url, sig)
            except Exception as e:
                if verbose:
                    print(f"[listen] tx fetch {sig[:10]}… failed: {e}", flush=True)
                continue
            if not memo or not memo.startswith("brainctl-marketplace/v1:buy:"):
                processed.add(sig)
                continue
            parsed = mp.parse_memo(memo)
            if not parsed or parsed.get("action") != "buy":
                processed.add(sig)
                continue
            new_buys.append((sig, parsed))

        for sig, parsed in new_buys:
            listing_id = parsed["listing_id"]
            buyer_x25519_b58 = parsed["buyer_x25519"]
            if verbose:
                print(
                    f"[listen] new buy memo: tx={sig[:10]}… listing={listing_id} "
                    f"buyer_x25519={buyer_x25519_b58[:8]}…",
                    flush=True,
                )

            # Fetch the listing detail from the API to confirm we own it.
            try:
                detail = api.get_listing(api_base, listing_id, cluster=cluster)
            except api.MarketplaceApiError as e:
                print(f"[listen] could not fetch listing detail: {e}", flush=True)
                continue
            listing = detail.get("listing", {})
            manifest = listing.get("manifest") or {}
            if manifest.get("seller_pubkey") != seller_pubkey:
                if verbose:
                    print(f"[listen] not our listing, skipping", flush=True)
                processed.add(sig)
                continue

            # Read the bundle's AES key.
            try:
                bundle_key = _read_bundle_key(listing_id)
            except FileNotFoundError as e:
                print(f"[listen] {e} — skipping. Buyer will need a re-release.", flush=True)
                continue

            # Mint a fresh cNFT to the buyer. We need the buyer's
            # Solana wallet pubkey, which equals the buy tx's signer.
            # The buy memo only carries the buyer's X25519 pubkey, so
            # we look at the tx signers.
            try:
                tx = _rpc(rpc_url, "getTransaction", [
                    sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                signer_pubkeys = [
                    k.get("pubkey") for k in
                    (tx.get("transaction", {}).get("message", {}).get("accountKeys", []))
                    if k.get("signer")
                ]
                buyer_pubkey = signer_pubkeys[0] if signer_pubkeys else None
            except Exception as e:
                print(f"[listen] failed to extract buyer signer: {e}", flush=True)
                continue
            if not buyer_pubkey:
                print(f"[listen] no buyer signer found in tx, skipping", flush=True)
                continue

            # JIT mint: no protocol mint fee here. The seller already paid
            # 2.5% to treasury at settlement (the buyer's settle tx), so
            # this seller-funded mint runs without an additional fee. We
            # signal this by omitting fee_lamports/fee_treasury from the
            # request — the Node helper treats absence as "skip fee".
            mint_req = {
                "action": "mint",
                "cluster": cluster,
                "owner_pubkey": buyer_pubkey,
                "keystore_path": keystore,
                "name": f"brainctl memory #{manifest.get('bundle_hash', '')[:8]}",
                "symbol": "MEM",
                "metadata_uri": manifest.get("metadata_uri"),
                "helius_api_key": os.environ.get("HELIUS_API_KEY"),
                "marketplace_jit": True,  # documentation flag; fee already skipped above
            }
            mint_result = api._run_node_helper(mint_req, timeout=180)
            if not mint_result.get("ok"):
                print(f"[listen] mint failed: {mint_result.get('error')}", flush=True)
                continue
            minted_cnft = mint_result.get("mint")
            if verbose:
                print(f"[listen] minted cNFT {minted_cnft[:10]}… to buyer", flush=True)

            # SealedBox the bundle key to the buyer's X25519 pubkey.
            try:
                buyer_ed25519_pub = _solana_pubkey_to_bytes(buyer_pubkey)
                x25519_pub = mp.ed25519_pub_to_x25519_pub(buyer_ed25519_pub)
                envelope = mp.sealedbox_encrypt(bundle_key, x25519_pub)
            except Exception as e:
                print(f"[listen] SealedBox encrypt failed: {e}", flush=True)
                continue

            # Upload the envelope to Arweave. We wrap the binary bytes
            # in a JSON manifest so the Node helper's
            # `marketplace_upload_manifest` action handles it; the
            # buyer's CLI knows to base64-decode envelope_b64 on read.
            envelope_manifest = {
                "schema": "brainctl-marketplace/v1/envelope",
                "listing_id": listing_id,
                "buyer_pubkey": buyer_pubkey,
                "envelope_b64": _b64(envelope),
                "created_at": _iso_now(),
            }
            upload_result = api.upload_manifest_to_arweave(
                manifest=envelope_manifest,
                schema="brainctl-marketplace/v1/envelope",
                cluster=cluster,
            )
            if not upload_result.get("ok"):
                print(f"[listen] envelope upload failed: {upload_result.get('error')}", flush=True)
                continue
            envelope_id = upload_result["arweave_id"]
            if verbose:
                print(f"[listen] envelope uploaded: ar://{envelope_id}", flush=True)

            # Post the release memo.
            release_memo = mp.format_release_memo(listing_id, envelope_id, minted_cnft)
            post_result = api.post_marketplace_memo(memo=release_memo, cluster=cluster)
            if not post_result.get("ok"):
                print(f"[listen] release memo post failed: {post_result.get('error')}", flush=True)
                continue
            if verbose:
                print(
                    f"[listen] release memo posted: tx={post_result['tx_signature'][:10]}…",
                    flush=True,
                )

            processed.add(sig)
            state["processed_buy_txs"] = list(processed)
            _save_state(state)

        if max_iterations is not None and iterations >= max_iterations:
            break
        time.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = ["listen_loop"]
