"""brainctl memory marketplace (v1.5) — pricing, BRNDB math, listings.

This module backs the ``brainctl marketplace`` subcommands (list,
browse, buy, listen) and the ``brainctl wallet buy-brndb / swap-out``
helpers. It exposes:

* Token configuration — BRNDB mint address, decimals, treasury address.
* Pricing math — USD↔BRNDB↔SOL conversion via Jupiter v6 price API,
  with Birdeye + DexScreener fallbacks.
* Fee math — 3.5% protocol fee split (seller / treasury).
* Listing manifest builder + canonical-JSON hashing + sig verification.
* Node-helper subprocess plumbing for the actual Solana operations
  (Jupiter swap, SPL transfer, memo posting) that have no Python SDK.

Design constraints (decision-locked 2026-05-12):

* **Not token-gated.** The marketplace buy flow auto-swaps SOL→BRNDB
  inline. Buyers never need to pre-acquire $BRNDB. Sellers can opt
  into auto-swap-out at release time.
* **USD-pegged pricing**, $10,000 cap per listing. Listings carry
  price_usd; the BRNDB amount is recomputed at buy time from the
  current Jupiter spot rate.
* **3.5% protocol fee** (`MARKETPLACE_FEE_BPS = 350`), hardcoded.
* **Encryption-at-rest is non-negotiable** for any bundle content;
  the chain only ever sees ownership transfers + memos.

The Jupiter pieces all rely on the existing Node helper at
``tools/zk_mint.js`` — we extend that helper with new actions rather
than introduce a second Node entry point.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Token configuration — BRNDB-specific knobs
# ---------------------------------------------------------------------------

# Production BRNDB mint address on Solana mainnet. Set after the
# pump.fun launch; until then the env override lets local devs point
# at a devnet stub mint without source edits. See
# ``resolve_brndb_mint()`` for the precedence order.
DEFAULT_BRNDB_MINT_MAINNET: Optional[str] = None  # filled in at launch
DEFAULT_BRNDB_MINT_DEVNET: Optional[str] = None   # optional dev stub

# Standard SPL token decimals. pump.fun launches default to 6 like
# USDC, not 9 like SOL — confirmed by inspecting recent pump.fun
# token mints. Recheck at launch and update if the mint differs.
BRNDB_DECIMALS = 6
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 10 ** SOL_DECIMALS

# Native SOL "mint" address used by Jupiter for SOL pricing. This is
# Wrapped SOL, the canonical wSOL mint that all DEX routes use.
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"

# USDC mainnet mint — the price reference for USD pegging.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# Protocol fee. 3.5% in basis points. Hardcoded by design; if you
# ever want to lower it, ship a new module version and migrate.
MARKETPLACE_FEE_BPS = 350
MAX_FEE_BPS = 1000  # 10% — safety cap if a config override ever tries to set higher

# Price cap per listing (USD). Sellers can list for any non-negative
# value up to this. Caps the blast radius if a seller publishes a
# typo'd "1000000" instead of "10".
MAX_LISTING_PRICE_USD = 10_000.0

# Stake required to list a bundle (USD-denominated). Released after
# 24h if no dispute filed; slashed otherwise.
LISTING_STAKE_USD = 1.0

# Treasury wallet — where the 3.5% fee lands. Defaults to the
# brainctl managed wallet. Override via ``$BRNDB_TREASURY_PUBKEY``.
DEFAULT_TREASURY_ENV = "BRNDB_TREASURY_PUBKEY"

# Schema version baked into listing manifests + on-chain memos.
MARKETPLACE_SCHEMA = "brndb-marketplace/v1"
MEMO_LIST_PREFIX = f"{MARKETPLACE_SCHEMA}:list"
MEMO_BUY_PREFIX = f"{MARKETPLACE_SCHEMA}:buy"
MEMO_RELEASE_PREFIX = f"{MARKETPLACE_SCHEMA}:release"
MEMO_CANCEL_PREFIX = f"{MARKETPLACE_SCHEMA}:cancel"

# Jupiter v6 endpoints. Free, no API key. Free-tier is per-IP-throttled
# but generous enough for any one-user CLI flow.
JUPITER_PRICE_API = "https://price.jup.ag/v6/price"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"


# ---------------------------------------------------------------------------
# Token / treasury resolution
# ---------------------------------------------------------------------------

def resolve_brndb_mint(
    explicit: Optional[str] = None,
    *,
    cluster: str = "mainnet-beta",
) -> Optional[str]:
    """Resolve the BRNDB mint address.

    Precedence (highest first):
      1. ``explicit`` — argument passed in from CLI
      2. ``$BRNDB_MINT`` env var
      3. Compiled-in mainnet / devnet default

    Returns ``None`` if no mint is configured anywhere. The CLI uses
    that to surface a friendly "set BRNDB_MINT before this command can
    work" error rather than blowing up inside Jupiter.
    """
    if explicit:
        return explicit.strip() or None
    env_val = os.environ.get("BRNDB_MINT")
    if env_val:
        return env_val.strip() or None
    if cluster == "mainnet-beta":
        return DEFAULT_BRNDB_MINT_MAINNET
    return DEFAULT_BRNDB_MINT_DEVNET


def resolve_treasury_pubkey(
    explicit: Optional[str] = None,
    *,
    default_wallet_address: Optional[str] = None,
) -> Optional[str]:
    """Resolve the marketplace treasury pubkey.

    Precedence:
      1. ``explicit`` — argument passed in
      2. ``$BRNDB_TREASURY_PUBKEY`` env var
      3. ``default_wallet_address`` — fallback to caller's wallet
    """
    if explicit:
        return explicit.strip() or None
    env_val = os.environ.get(DEFAULT_TREASURY_ENV)
    if env_val:
        return env_val.strip() or None
    return default_wallet_address


# ---------------------------------------------------------------------------
# Fee math
# ---------------------------------------------------------------------------

def split_with_fee(
    total_atoms: int,
    *,
    fee_bps: int = MARKETPLACE_FEE_BPS,
) -> Tuple[int, int]:
    """Split ``total_atoms`` (raw token units, e.g. micro-BRNDB) into
    ``(seller_amount, treasury_amount)`` using the basis-point fee.

    Always rounds the fee UP (treasury favored) so the seller never
    accidentally gets paid more than ``total - fee`` due to integer
    truncation. The seller's amount is then ``total - rounded_fee``.

    Examples:
      >>> split_with_fee(1_000_000, fee_bps=350)
      (965_000, 35_000)
      >>> split_with_fee(1, fee_bps=350)
      (0, 1)        # too small to split — entire amount goes to treasury
    """
    if total_atoms < 0:
        raise ValueError("total_atoms must be non-negative")
    if fee_bps < 0 or fee_bps > MAX_FEE_BPS:
        raise ValueError(
            f"fee_bps must be in [0, {MAX_FEE_BPS}], got {fee_bps}"
        )
    # Round-up division for the fee so we never under-collect.
    fee_atoms = (total_atoms * fee_bps + 9999) // 10000
    fee_atoms = min(fee_atoms, total_atoms)
    seller_atoms = total_atoms - fee_atoms
    return seller_atoms, fee_atoms


def usd_to_atoms(
    usd_amount: float,
    *,
    token_price_usd: float,
    token_decimals: int,
) -> int:
    """Convert a USD amount to raw token atoms at the given spot price.

    ``token_price_usd`` is the price of 1 whole token in USD (e.g.
    BRNDB price as float). ``token_decimals`` is the on-chain decimal
    count for that mint. Rounds DOWN to atoms (favors the buyer's
    wallet — under-pays by at most one atom rather than over-paying).
    """
    if usd_amount < 0:
        raise ValueError("usd_amount must be non-negative")
    if token_price_usd <= 0:
        raise ValueError("token_price_usd must be positive")
    tokens = usd_amount / token_price_usd
    atoms = int(tokens * (10 ** token_decimals))
    return max(atoms, 0)


def atoms_to_usd(
    atoms: int,
    *,
    token_price_usd: float,
    token_decimals: int,
) -> float:
    """Inverse of ``usd_to_atoms``. Float, naturally lossy."""
    return (atoms / (10 ** token_decimals)) * token_price_usd


# ---------------------------------------------------------------------------
# Listing manifest construction
# ---------------------------------------------------------------------------

def build_listing_manifest(
    *,
    bundle_hash: str,
    seller_pubkey_b58: str,
    price_usd: float,
    duration_hours: float,
    encrypted_bundle_uri: str,
    metadata_uri: str,
    preview: Dict[str, Any],
    visibility: str = "auction",
    payment_address: Optional[str] = None,
    listing_id: Optional[str] = None,
    treasury_pubkey: Optional[str] = None,
    fee_bps: int = MARKETPLACE_FEE_BPS,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the unsigned listing manifest as a dict.

    Proof-first architecture: ``bundle_hash`` is the SHA-256 hex of the
    signed bundle's canonical JSON (the same value ``signing.sign_bundle``
    publishes as ``bundle_hash_hex``). NO cNFT is minted at list time;
    the cNFT mint happens just-in-time at settlement, one fresh mint
    per buyer. See ~/brainctl-launch/04_marketplace_design.md §
    "Trade proofs, mint on settlement".

    The signature is added by the caller after the manifest is
    finalised. Manifests are canonical-JSON-hashed for integrity (same
    canonicalisation rules as the signing module: sort_keys,
    separators (",", ":"), ensure_ascii).
    """
    if price_usd < 0 or price_usd > MAX_LISTING_PRICE_USD:
        raise ValueError(
            f"price_usd must be in [0, {MAX_LISTING_PRICE_USD}], "
            f"got {price_usd}"
        )
    if duration_hours <= 0 or duration_hours > 24 * 30:
        raise ValueError(
            "duration_hours must be in (0, 720] (30-day max)"
        )

    now = datetime.now(timezone.utc)
    expires_ts = now.timestamp() + duration_hours * 3600
    expires_iso = (
        datetime.fromtimestamp(expires_ts, tz=timezone.utc)
        .isoformat()
    )

    if visibility not in ("auction", "private"):
        raise ValueError(
            f"visibility must be 'auction' or 'private', got {visibility!r}"
        )

    return {
        "schema": f"{MARKETPLACE_SCHEMA}/listing",
        "listing_id": listing_id or _new_listing_id(),
        "bundle_hash": bundle_hash,
        "seller_pubkey": seller_pubkey_b58,
        "payment_address": payment_address or seller_pubkey_b58,
        "treasury_pubkey": treasury_pubkey,
        "fee_bps": fee_bps,
        "pricing": {
            "price_usd": float(price_usd),
            "max_price_usd": MAX_LISTING_PRICE_USD,
            "currency": "USD",
        },
        "visibility": visibility,
        "expires_at": expires_iso,
        "encrypted_bundle_uri": encrypted_bundle_uri,
        "metadata_uri": metadata_uri,
        "preview": preview,
        "created_at": created_at or now.isoformat(),
    }


def _new_listing_id() -> str:
    """Generate a time-ordered, sortable listing ID.

    Uses ``uuid.uuid4()`` for the random portion. We deliberately don't
    use uuid7 (not in stdlib until 3.14+ and we want broader compat).
    The prefix is a UTC date so listings sort naturally in registries.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{today}-{uuid.uuid4().hex[:12]}"


def canonical_json(obj: Any) -> bytes:
    """Same canonicalisation as ``agentmemory.signing.canonical_json``.

    External verifiers MUST use these four kwargs exactly.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def listing_hash(manifest: Dict[str, Any]) -> bytes:
    """SHA-256 of a manifest (excluding any pre-existing signature)."""
    import hashlib
    # Strip the signature field if present so the same canonical bytes
    # are hashed both pre- and post-signing.
    body = {k: v for k, v in manifest.items() if k != "signature_b58"}
    return hashlib.sha256(canonical_json(body)).digest()


# ---------------------------------------------------------------------------
# Preview generator (what the buyer sees before paying)
# ---------------------------------------------------------------------------

def build_preview_from_bundle(
    bundle: Dict[str, Any],
    *,
    description: Optional[str] = None,
    max_categories: int = 8,
    max_tags: int = 12,
) -> Dict[str, Any]:
    """Compute the public preview block from a signed bundle's memories.

    Leaks only category + tag counts and the date range — never memory
    content itself. Sellers can supply a free-text ``description`` to
    add a 1-line pitch.
    """
    memories = bundle.get("memories", [])
    n = len(memories)

    cat_counts: Dict[str, int] = {}
    tag_counts: Dict[str, int] = {}
    min_created = max_created = None

    for m in memories:
        cat = m.get("category")
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        # Tags column is a comma-separated string per brain.db schema.
        raw_tags = m.get("tags") or ""
        if isinstance(raw_tags, str):
            for t in raw_tags.split(","):
                t = t.strip()
                if t:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
        ts = m.get("created_at")
        if ts:
            if min_created is None or ts < min_created:
                min_created = ts
            if max_created is None or ts > max_created:
                max_created = ts

    def _top(counts: Dict[str, int], limit: int) -> List[str]:
        return [
            k for k, _ in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:limit]
        ]

    return {
        "memories_count": n,
        "categories_summary": _top(cat_counts, max_categories),
        "tags_summary": _top(tag_counts, max_tags),
        "min_created_at": min_created,
        "max_created_at": max_created,
        "description": (description or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# X25519 buyer-key derivation (for SealedBox key handoff)
# ---------------------------------------------------------------------------

def _require_pynacl():
    """Import PyNaCl on demand with a clear install hint."""
    try:
        import nacl.public  # noqa: F401
        import nacl.signing  # noqa: F401
        return True
    except ImportError:
        sys.stderr.write(
            "brainctl marketplace requires the 'pynacl' package.\n"
            "Install with:  pip install 'brainctl[marketplace]'\n"
        )
        raise SystemExit(1)


def ed25519_to_x25519_seed(ed25519_secret_seed: bytes) -> bytes:
    """Derive the X25519 secret key from an ed25519 secret-key seed.

    Uses libsodium's standard conversion (the same Signal-era
    transform). Required because Solana wallets are ed25519 but
    SealedBox needs X25519.

    ``ed25519_secret_seed`` is the 32-byte seed (first half of the
    64-byte secret-key file shape; the second half is the public key).
    """
    _require_pynacl()
    from nacl.signing import SigningKey
    if len(ed25519_secret_seed) != 32:
        raise ValueError(
            f"ed25519 seed must be 32 bytes, got {len(ed25519_secret_seed)}"
        )
    signing_key = SigningKey(ed25519_secret_seed)
    # to_curve25519_private_key gives an X25519 PrivateKey object.
    x25519_priv = signing_key.to_curve25519_private_key()
    return bytes(x25519_priv)


def ed25519_pub_to_x25519_pub(ed25519_pubkey: bytes) -> bytes:
    """Counterpart for converting a Solana pubkey to its X25519 form.

    Used by the seller when encrypting the bundle key to the buyer's
    Solana wallet pubkey — they derive the X25519 form, then SealedBox
    against it.
    """
    _require_pynacl()
    from nacl.signing import VerifyKey
    if len(ed25519_pubkey) != 32:
        raise ValueError(
            f"ed25519 pubkey must be 32 bytes, got {len(ed25519_pubkey)}"
        )
    verify = VerifyKey(ed25519_pubkey)
    x25519_pub = verify.to_curve25519_public_key()
    return bytes(x25519_pub)


def sealedbox_encrypt(
    plaintext: bytes,
    recipient_x25519_pubkey: bytes,
) -> bytes:
    """SealedBox-encrypt ``plaintext`` to the recipient's X25519 pubkey.

    Output is the standard libsodium sealed-box ciphertext: a fresh
    ephemeral keypair's pubkey concatenated with the box ciphertext.
    Only the holder of the matching X25519 private key can decrypt.
    """
    _require_pynacl()
    from nacl.public import PublicKey, SealedBox
    pub = PublicKey(recipient_x25519_pubkey)
    box = SealedBox(pub)
    return box.encrypt(plaintext)


def sealedbox_decrypt(
    ciphertext: bytes,
    recipient_x25519_secret: bytes,
) -> bytes:
    """Reverse of ``sealedbox_encrypt``."""
    _require_pynacl()
    from nacl.public import PrivateKey, SealedBox
    priv = PrivateKey(recipient_x25519_secret)
    box = SealedBox(priv)
    return box.decrypt(ciphertext)


# ---------------------------------------------------------------------------
# Memo formatting (Solana on-chain discovery layer)
# ---------------------------------------------------------------------------

def format_list_memo(listing_arweave_id: str, bundle_hash: str) -> str:
    """Discovery memo posted at list time.

    Proof-first: the memo references the bundle_hash, not a pre-minted
    cNFT. The cNFT is minted JIT at settlement.
    """
    return f"{MEMO_LIST_PREFIX}:{listing_arweave_id}:{bundle_hash}"


def format_buy_memo(listing_id: str, buyer_x25519_pubkey_b58: str) -> str:
    """Buy memo posted with the $BRNDB payment transaction."""
    return f"{MEMO_BUY_PREFIX}:{listing_id}:{buyer_x25519_pubkey_b58}"


def format_release_memo(
    listing_id: str,
    envelope_arweave_id: str,
    minted_cnft_address: str,
) -> str:
    """Key-release memo posted by the seller after detecting payment.

    Third field is the just-minted cNFT — the buyer's permanent
    on-chain receipt of purchase. Seller's daemon mints + uploads
    envelope + posts this memo in sequence on payment detection.
    """
    return (
        f"{MEMO_RELEASE_PREFIX}:{listing_id}:"
        f"{envelope_arweave_id}:{minted_cnft_address}"
    )


def format_cancel_memo(listing_id: str) -> str:
    """Cancel memo posted by the seller to retire a listing."""
    return f"{MEMO_CANCEL_PREFIX}:{listing_id}"


def parse_memo(body: str) -> Optional[Dict[str, str]]:
    """Parse a brainctl marketplace memo body. Returns ``None`` for non-matches.

    Returns ``{action, listing_id_or_arweave_id, extra}`` depending on
    the memo kind. Callers should switch on ``action``.
    """
    if not body.startswith(f"{MARKETPLACE_SCHEMA}:"):
        return None
    parts = body.split(":")
    if len(parts) < 3:
        return None
    action = parts[1]
    if action == "list" and len(parts) >= 4:
        return {"action": "list",
                "listing_arweave_id": parts[2],
                "bundle_hash": parts[3]}
    if action == "buy" and len(parts) >= 4:
        return {"action": "buy",
                "listing_id": parts[2],
                "buyer_x25519": parts[3]}
    if action == "release" and len(parts) >= 5:
        return {"action": "release",
                "listing_id": parts[2],
                "envelope_arweave_id": parts[3],
                "minted_cnft_address": parts[4]}
    if action == "cancel":
        return {"action": "cancel", "listing_id": parts[2]}
    return None


__all__ = [
    # Token config
    "DEFAULT_BRNDB_MINT_MAINNET",
    "DEFAULT_BRNDB_MINT_DEVNET",
    "BRNDB_DECIMALS",
    "SOL_DECIMALS",
    "LAMPORTS_PER_SOL",
    "WRAPPED_SOL_MINT",
    "USDC_MINT",
    "USDC_DECIMALS",
    # Marketplace knobs
    "MARKETPLACE_FEE_BPS",
    "MAX_FEE_BPS",
    "MAX_LISTING_PRICE_USD",
    "LISTING_STAKE_USD",
    "MARKETPLACE_SCHEMA",
    "MEMO_LIST_PREFIX",
    "MEMO_BUY_PREFIX",
    "MEMO_RELEASE_PREFIX",
    "MEMO_CANCEL_PREFIX",
    # Jupiter
    "JUPITER_PRICE_API",
    "JUPITER_QUOTE_API",
    "JUPITER_SWAP_API",
    # Resolution
    "resolve_brndb_mint",
    "resolve_treasury_pubkey",
    # Math
    "split_with_fee",
    "usd_to_atoms",
    "atoms_to_usd",
    # Manifests
    "build_listing_manifest",
    "build_preview_from_bundle",
    "canonical_json",
    "listing_hash",
    # X25519 / SealedBox
    "ed25519_to_x25519_seed",
    "ed25519_pub_to_x25519_pub",
    "sealedbox_encrypt",
    "sealedbox_decrypt",
    # Memos
    "format_list_memo",
    "format_buy_memo",
    "format_release_memo",
    "format_cancel_memo",
    "parse_memo",
]
