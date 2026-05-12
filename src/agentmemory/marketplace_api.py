"""brainctl marketplace REST client — the Python SDK that drives the
brainctl.org/api/marketplace endpoints.

This module is the bridge between local brainctl operations (signing,
keystore management, bundle building) and the chain-canonical
marketplace API. Every agent that wants to participate in the
marketplace can use this client to:

  1. Authenticate via wallet signature (challenge → sign → verify).
  2. List signed memory proofs for sale.
  3. Browse + filter open listings.
  4. Make / counter / accept / reject offers.
  5. Settle accepted offers (build + sign + submit settlement tx).
  6. Poll for the seller's release memo to receive the bundle key.

The client is HTTP-only — it does NOT submit Solana transactions
itself. Tx submission is done via the existing brainctl Node helper
or the user's preferred RPC; the SDK just returns the partially-signed
tx for the caller to handle.

Session tokens are persisted to ``~/.brainctl/marketplace-session.json``
(0600) so repeated CLI invocations don't re-auth every time.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# Default API base URL. Override via env for staging / local testing.
DEFAULT_API_BASE = "https://brainctl.org/api/marketplace"

# Persisted session location. Per-cluster so devnet + mainnet don't
# collide.
SESSION_DIR = "~/.brainctl"
SESSION_FILENAME = "marketplace-session.json"

# HTTP request timeout. Marketplace endpoints do RPC + Arweave fetches
# server-side so they can take up to ~20s on the cold path.
DEFAULT_HTTP_TIMEOUT_SEC = 30


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def _session_path() -> Path:
    return Path(SESSION_DIR).expanduser() / SESSION_FILENAME


def _read_session() -> Dict[str, Any]:
    p = _session_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_session(data: Dict[str, Any]) -> None:
    """Persist session data atomically with 0600 perms."""
    p = _session_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except (OSError, NotImplementedError):
        pass
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    tmp.replace(p)


def stored_session_for(api_base: str, pubkey: str) -> Optional[Dict[str, Any]]:
    """Return the persisted session for (api_base, pubkey) if not expired."""
    data = _read_session()
    key = f"{api_base}::{pubkey}"
    entry = data.get(key)
    if not entry:
        return None
    expires_at_iso = entry.get("expires_at")
    if not expires_at_iso:
        return None
    # Treat as expired with 30s slop so we don't issue requests that
    # land after expiry.
    try:
        from datetime import datetime, timezone
        expires = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if (expires - now).total_seconds() < 30:
            return None
    except Exception:
        return None
    return entry


def store_session(api_base: str, pubkey: str, payload: Dict[str, Any]) -> None:
    """Save a session token to disk for future calls."""
    data = _read_session()
    key = f"{api_base}::{pubkey}"
    data[key] = payload
    _write_session(data)


def clear_session(api_base: str, pubkey: Optional[str] = None) -> None:
    """Drop persisted session(s). Useful for ``brainctl marketplace api logout``."""
    data = _read_session()
    if pubkey is None:
        # Drop all sessions for this api_base.
        data = {k: v for k, v in data.items() if not k.startswith(f"{api_base}::")}
    else:
        data.pop(f"{api_base}::{pubkey}", None)
    _write_session(data)


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------

class MarketplaceApiError(Exception):
    """Raised on any non-2xx response from the marketplace API.

    The body is captured in ``self.payload`` and the HTTP status in
    ``self.status`` so callers can branch on error codes (e.g., 401
    means re-auth, 409 means terminal state, etc.).
    """

    def __init__(self, status: int, payload: Dict[str, Any]) -> None:
        self.status = status
        self.payload = payload
        msg = f"HTTP {status}"
        if isinstance(payload, dict):
            err = payload.get("error")
            detail = payload.get("detail")
            if err:
                msg += f" — {err}"
            if detail:
                msg += f" ({detail})"
        super().__init__(msg)


def _http_call(
    method: str,
    url: str,
    *,
    body: Optional[Dict[str, Any]] = None,
    session_token: Optional[str] = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Send an HTTP request + parse JSON response. Raises on non-2xx."""
    headers = {"accept": "application/json"}
    data_bytes: Optional[bytes] = None
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    if session_token:
        headers["authorization"] = f"Bearer {session_token}"

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw.decode("utf-8", errors="replace")}
            return parsed
    except urllib.error.HTTPError as e:
        # Capture body of error responses so the caller sees error codes.
        try:
            err_payload = json.loads(e.read())
        except Exception:
            err_payload = {"error": "http_error", "detail": str(e)}
        raise MarketplaceApiError(e.code, err_payload) from None
    except urllib.error.URLError as e:
        raise MarketplaceApiError(0, {"error": "network_error", "detail": str(e)}) from None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def request_challenge(api_base: str, pubkey: str) -> Dict[str, Any]:
    """POST /auth/challenge — returns { nonce, message_to_sign, expires_at }."""
    return _http_call("POST", f"{api_base}/auth/challenge", body={"pubkey": pubkey})


def verify_challenge(
    api_base: str,
    pubkey: str,
    nonce: str,
    signature_b58: str,
) -> Dict[str, Any]:
    """POST /auth/verify — returns { session_token, pubkey, expires_at }."""
    return _http_call(
        "POST",
        f"{api_base}/auth/verify",
        body={"pubkey": pubkey, "nonce": nonce, "signature_b58": signature_b58},
    )


def ensure_session(
    api_base: str,
    pubkey: str,
    *,
    keystore_path: Optional[str] = None,
) -> str:
    """Return a valid session token, re-authing if necessary.

    Flow:
      1. Check persisted session for (api_base, pubkey).
      2. If absent or near-expiry, do challenge → sign → verify.
      3. Persist the new session.
      4. Return the token.

    Signing uses solders via the ``agentmemory.signing`` module, which
    is already a hard dep of the [signing] / [mint] / [marketplace]
    extras. The keystore defaults to the brainctl managed wallet at
    ``~/.brainctl/wallet.json``.
    """
    existing = stored_session_for(api_base, pubkey)
    if existing and existing.get("session_token"):
        return existing["session_token"]

    # No usable session — do the full flow.
    from agentmemory import signing
    from agentmemory.commands import wallet as _wallet

    # Resolve keystore.
    if keystore_path is None:
        managed = _wallet.resolve_wallet_path(None)
        if not managed.exists():
            raise RuntimeError(
                "no wallet found at ~/.brainctl/wallet.json — run "
                "`brainctl wallet new --yes` first"
            )
        keystore_path = str(managed)

    keypair = signing.load_keystore(keystore_path)
    keystore_pubkey = str(keypair.pubkey())
    if keystore_pubkey != pubkey:
        raise RuntimeError(
            f"keystore pubkey {keystore_pubkey[:8]}… doesn't match "
            f"requested {pubkey[:8]}…"
        )

    challenge = request_challenge(api_base, pubkey)
    message = challenge["message_to_sign"].encode("utf-8")
    sig = keypair.sign_message(message)
    sig_b58 = str(sig)
    verified = verify_challenge(api_base, pubkey, challenge["nonce"], sig_b58)
    store_session(api_base, pubkey, verified)
    return verified["session_token"]


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def browse_listings(
    api_base: str,
    *,
    cluster: str = "mainnet-beta",
    max_price_usd: Optional[float] = None,
    category: Optional[str] = None,
    visibility: Optional[str] = None,
    seller_pubkey: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """GET /listings — public browse, no auth."""
    params: Dict[str, str] = {"cluster": cluster, "limit": str(limit)}
    if max_price_usd is not None:
        params["max_price_usd"] = str(max_price_usd)
    if category:
        params["category"] = category
    if visibility:
        params["visibility"] = visibility
    if seller_pubkey:
        params["seller_pubkey"] = seller_pubkey
    qs = "&".join(f"{k}={_url_encode(v)}" for k, v in params.items())
    return _http_call("GET", f"{api_base}/listings?{qs}")


def get_listing(
    api_base: str,
    listing_id: str,
    *,
    cluster: str = "mainnet-beta",
    session_token: Optional[str] = None,
) -> Dict[str, Any]:
    """GET /listings/[id] — detail. Pass session_token to see private offer info."""
    return _http_call(
        "GET",
        f"{api_base}/listings/{_url_encode(listing_id)}?cluster={cluster}",
        session_token=session_token,
    )


def create_listing(
    api_base: str,
    *,
    manifest: Dict[str, Any],
    listing_arweave_id: str,
    list_tx_signature: str,
    cluster: str,
    session_token: str,
) -> Dict[str, Any]:
    """POST /listings — publish a signed manifest after on-chain memo."""
    return _http_call(
        "POST",
        f"{api_base}/listings",
        body={
            "manifest": manifest,
            "listing_arweave_id": listing_arweave_id,
            "list_tx_signature": list_tx_signature,
            "cluster": cluster,
        },
        session_token=session_token,
    )


def cancel_listing(
    api_base: str,
    listing_id: str,
    *,
    cancel_tx_signature: str,
    cluster: str,
    session_token: str,
) -> Dict[str, Any]:
    return _http_call(
        "POST",
        f"{api_base}/listings/{_url_encode(listing_id)}/cancel",
        body={"cancel_tx_signature": cancel_tx_signature, "cluster": cluster},
        session_token=session_token,
    )


# ---------------------------------------------------------------------------
# Offers + negotiation
# ---------------------------------------------------------------------------

def list_offers(
    api_base: str,
    listing_id: str,
    *,
    cluster: str = "mainnet-beta",
    session_token: Optional[str] = None,
) -> Dict[str, Any]:
    return _http_call(
        "GET",
        f"{api_base}/listings/{_url_encode(listing_id)}/offers?cluster={cluster}",
        session_token=session_token,
    )


def create_offer(
    api_base: str,
    listing_id: str,
    *,
    manifest: Dict[str, Any],
    offer_arweave_id: str,
    offer_tx_signature: str,
    cluster: str,
    session_token: str,
) -> Dict[str, Any]:
    return _http_call(
        "POST",
        f"{api_base}/listings/{_url_encode(listing_id)}/offers",
        body={
            "manifest": manifest,
            "offer_arweave_id": offer_arweave_id,
            "offer_tx_signature": offer_tx_signature,
            "cluster": cluster,
        },
        session_token=session_token,
    )


def get_offer(
    api_base: str,
    offer_id: str,
    *,
    cluster: str = "mainnet-beta",
    session_token: Optional[str] = None,
) -> Dict[str, Any]:
    return _http_call(
        "GET",
        f"{api_base}/offers/{_url_encode(offer_id)}?cluster={cluster}",
        session_token=session_token,
    )


def _offer_action(
    api_base: str,
    offer_id: str,
    action: str,
    *,
    tx_signature: str,
    cluster: str,
    session_token: str,
) -> Dict[str, Any]:
    return _http_call(
        "POST",
        f"{api_base}/offers/{_url_encode(offer_id)}/{action}",
        body={"tx_signature": tx_signature, "cluster": cluster},
        session_token=session_token,
    )


def accept_offer(api_base: str, offer_id: str, **kw: Any) -> Dict[str, Any]:
    return _offer_action(api_base, offer_id, "accept", **kw)


def reject_offer(api_base: str, offer_id: str, **kw: Any) -> Dict[str, Any]:
    return _offer_action(api_base, offer_id, "reject", **kw)


def withdraw_offer(api_base: str, offer_id: str, **kw: Any) -> Dict[str, Any]:
    return _offer_action(api_base, offer_id, "withdraw", **kw)


def counter_offer(
    api_base: str,
    offer_id: str,
    *,
    manifest: Dict[str, Any],
    counter_arweave_id: str,
    counter_tx_signature: str,
    cluster: str,
    session_token: str,
) -> Dict[str, Any]:
    return _http_call(
        "POST",
        f"{api_base}/offers/{_url_encode(offer_id)}/counter",
        body={
            "manifest": manifest,
            "counter_arweave_id": counter_arweave_id,
            "counter_tx_signature": counter_tx_signature,
            "cluster": cluster,
        },
        session_token=session_token,
    )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def build_settle_tx(
    api_base: str,
    listing_id: str,
    *,
    buyer_x25519_pubkey: str,
    cluster: str,
    session_token: str,
    offer_id: Optional[str] = None,
    currency: Optional[str] = None,  # "SOL" | "BRNDB" | None (server picks)
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "buyer_x25519_pubkey": buyer_x25519_pubkey,
        "cluster": cluster,
    }
    if offer_id:
        body["offer_id"] = offer_id
    if currency:
        body["currency"] = currency
    return _http_call(
        "POST",
        f"{api_base}/listings/{_url_encode(listing_id)}/settle",
        body=body,
        session_token=session_token,
    )


def settlement_status(
    api_base: str,
    listing_id: str,
    *,
    buyer_pubkey: str,
    cluster: str = "mainnet-beta",
) -> Dict[str, Any]:
    return _http_call(
        "GET",
        f"{api_base}/settlements/{_url_encode(listing_id)}"
        f"?buyer={_url_encode(buyer_pubkey)}&cluster={cluster}",
    )


def sign_and_submit_settle_tx(
    *,
    tx_base64: str,
    keystore_path: str,
    cluster: str,
) -> Dict[str, Any]:
    """Sign the base64-encoded settlement tx with the user's wallet
    and submit it to a Solana RPC.

    Returns ``{ok, tx_signature, slot?}`` on success.

    Uses solders for signing + a public Solana RPC (devnet) or the
    Helius RPC (mainnet) for submission. This is intentionally
    minimal: it does NOT confirm the tx beyond the initial submission.
    Callers should poll ``settlement_status`` afterwards.
    """
    import base64

    # Late imports to keep this module solders-free at import time.
    from agentmemory import signing
    from solders.transaction import Transaction  # type: ignore

    keypair = signing.load_keystore(keystore_path)

    tx_bytes = base64.b64decode(tx_base64)
    tx = Transaction.from_bytes(tx_bytes)

    # The settlement tx's fee payer is the buyer; the buyer is the
    # only required signer for all instructions in the tx (SPL/SystemProgram
    # transfers from the buyer's accounts + the buyer-signed memo). We
    # sign with the buyer's keypair.
    blockhash = tx.message.recent_blockhash
    tx.sign([keypair], blockhash)
    signed_bytes = bytes(tx)
    signed_b64 = base64.b64encode(signed_bytes).decode("ascii")

    # Submit via sendTransaction RPC.
    if cluster == "mainnet-beta":
        rpc_url = "https://api.mainnet-beta.solana.com"
    else:
        rpc_url = "https://api.devnet.solana.com"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            signed_b64,
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ],
    }
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT_SEC) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"rpc_http_error_{e.code}", "detail": str(e)}
    except urllib.error.URLError as e:
        return {"ok": False, "error": "rpc_network_error", "detail": str(e)}

    if "error" in body:
        return {
            "ok": False,
            "error": "rpc_error",
            "detail": body["error"],
        }
    sig = body.get("result")
    if not isinstance(sig, str):
        return {"ok": False, "error": "no_signature_in_response", "detail": str(body)}
    return {"ok": True, "tx_signature": sig}


def poll_settlement_until_released(
    api_base: str,
    listing_id: str,
    *,
    buyer_pubkey: str,
    cluster: str = "mainnet-beta",
    timeout_seconds: int = 120,
    poll_interval_seconds: float = 3.0,
) -> Dict[str, Any]:
    """Poll the settlement status until released (or timeout)."""
    deadline = time.time() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = settlement_status(
            api_base, listing_id, buyer_pubkey=buyer_pubkey, cluster=cluster
        )
        if last.get("status") == "released":
            return last
        time.sleep(poll_interval_seconds)
    return last


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _url_encode(value: Any) -> str:
    import urllib.parse
    return urllib.parse.quote(str(value), safe="")


def api_base_from_env(default: str = DEFAULT_API_BASE) -> str:
    """Resolve the API base URL. Override via $BRNCTL_MARKETPLACE_API."""
    return os.environ.get("BRNCTL_MARKETPLACE_API", default).rstrip("/")


__all__ = [
    "DEFAULT_API_BASE",
    "MarketplaceApiError",
    "api_base_from_env",
    "request_challenge",
    "verify_challenge",
    "ensure_session",
    "stored_session_for",
    "store_session",
    "clear_session",
    "browse_listings",
    "get_listing",
    "create_listing",
    "cancel_listing",
    "list_offers",
    "create_offer",
    "get_offer",
    "accept_offer",
    "reject_offer",
    "withdraw_offer",
    "counter_offer",
    "build_settle_tx",
    "settlement_status",
    "poll_settlement_until_released",
]
