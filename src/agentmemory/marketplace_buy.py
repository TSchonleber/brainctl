"""Buyer-side post-release helpers — fetch the SealedBox envelope,
decrypt the bundle key, decrypt the bundle ciphertext, optionally
ingest into brain.db.

This is the last mile of the marketplace flow. After
``brainctl marketplace api settle --submit`` lands a payment + the
seller's daemon posts a release memo, ``brainctl marketplace api
status`` returns the envelope's Arweave id and the minted cNFT
address. From there:

  1. Fetch the envelope JSON from Arweave.
  2. base64-decode the ciphertext.
  3. SealedBox-decrypt with the buyer's X25519 priv key
     (derived from their Solana ed25519 secret seed).
  4. The plaintext IS the bundle's AES-256-GCM key.
  5. Fetch the encrypted bundle ciphertext from the listing's
     ``encrypted_bundle_uri``.
  6. AES-decrypt it with the recovered key.
  7. The plaintext is the canonical-JSON of the original signed
     bundle.
  8. Optionally ingest the memories into brain.db under
     ``scope=imported:<listing_id>`` (quarantined).
"""
from __future__ import annotations

import base64
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ARWEAVE_GATEWAY = "https://arweave.net"
FETCH_TIMEOUT_SEC = 30


def fetch_arweave_json(arweave_id: str) -> Dict[str, Any]:
    """Pull a JSON manifest from the public Arweave gateway."""
    url = f"{ARWEAVE_GATEWAY}/{arweave_id}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        return json.loads(resp.read())


def fetch_arweave_bytes(arweave_id: str) -> bytes:
    """Pull raw bytes from Arweave."""
    url = f"{ARWEAVE_GATEWAY}/{arweave_id}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Decryption pipeline
# ---------------------------------------------------------------------------

def derive_x25519_priv_from_wallet(keystore_path: str) -> bytes:
    """Load the user's Solana ed25519 seed and convert it to X25519
    private bytes for SealedBox decryption.
    """
    from agentmemory import marketplace as mp

    # The brainctl wallet file stores a 64-byte concatenation of
    # secret_seed(32) || pubkey(32). PyNaCl's ed25519→X25519 helper
    # wants just the 32-byte seed.
    p = Path(keystore_path).expanduser()
    raw = json.loads(p.read_text())
    if not isinstance(raw, list) or len(raw) != 64:
        raise ValueError("invalid Solana keystore")
    seed = bytes(raw[:32])
    return mp.ed25519_to_x25519_seed(seed)


def fetch_and_decrypt_envelope(
    *,
    envelope_arweave_id: str,
    keystore_path: str,
) -> bytes:
    """Pull the SealedBox envelope from Arweave + decrypt to recover
    the bundle's AES key.
    """
    from agentmemory import marketplace as mp

    envelope_manifest = fetch_arweave_json(envelope_arweave_id)
    ciphertext_b64 = envelope_manifest.get("envelope_b64")
    if not ciphertext_b64:
        raise ValueError(
            f"envelope manifest missing envelope_b64 (got keys: "
            f"{list(envelope_manifest.keys())})"
        )
    ciphertext = base64.b64decode(ciphertext_b64)

    x25519_priv = derive_x25519_priv_from_wallet(keystore_path)
    bundle_key = mp.sealedbox_decrypt(ciphertext, x25519_priv)
    if len(bundle_key) != 32:
        raise ValueError(
            f"decrypted bundle key has wrong length {len(bundle_key)} "
            "(expected 32 bytes for AES-256-GCM)"
        )
    return bundle_key


def fetch_and_decrypt_bundle(
    *,
    encrypted_bundle_uri: str,
    bundle_key: bytes,
) -> Dict[str, Any]:
    """Pull the encrypted bundle ciphertext from Arweave + AES-decrypt.
    Returns the parsed bundle JSON.
    """
    from agentmemory import minting

    # The URI is ``ar://<id>``; strip the scheme.
    if encrypted_bundle_uri.startswith("ar://"):
        arweave_id = encrypted_bundle_uri[len("ar://") :].split("?")[0]
    else:
        arweave_id = encrypted_bundle_uri
    blob = fetch_arweave_bytes(arweave_id)
    parts = minting.unpack_encrypted_blob(blob)
    plaintext = minting.decrypt_bundle(
        parts["nonce"], parts["ciphertext"], bundle_key
    )
    return json.loads(plaintext)


def post_release_pipeline(
    *,
    envelope_arweave_id: str,
    encrypted_bundle_uri: str,
    keystore_path: str,
) -> Dict[str, Any]:
    """Run the full envelope → bundle key → bundle decrypt chain.

    Returns ``{ bundle, bundle_key_hex }``. Caller decides whether to
    ingest the bundle into their brain.db.
    """
    bundle_key = fetch_and_decrypt_envelope(
        envelope_arweave_id=envelope_arweave_id,
        keystore_path=keystore_path,
    )
    bundle = fetch_and_decrypt_bundle(
        encrypted_bundle_uri=encrypted_bundle_uri,
        bundle_key=bundle_key,
    )
    return {
        "bundle": bundle,
        "bundle_key_hex": bundle_key.hex(),
    }


# ---------------------------------------------------------------------------
# Quarantine ingestion
# ---------------------------------------------------------------------------

def ingest_into_quarantine(
    bundle: Dict[str, Any],
    *,
    listing_id: str,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert a purchased bundle's memories into brain.db under a
    quarantine scope.

    Scope = ``imported:<listing_id>``. The buyer's agent can search
    these memories but they don't blend into the agent's primary
    scope until explicitly promoted via
    ``brainctl marketplace promote --to-scope <target>`` (v1.5.1).
    """
    from agentmemory import _impl as _br

    scope = f"imported:{listing_id}"
    memories = bundle.get("memories", [])
    ingested = 0
    skipped = 0
    errors: list[str] = []

    for mem in memories:
        try:
            # Honour the original category if it's one of the canonical
            # nine; default to "lesson" otherwise.
            cat = mem.get("category") or "lesson"
            content = mem.get("content")
            if not content:
                skipped += 1
                continue
            _br.cmd_memory_add(
                _SimpleArgs(
                    content=content,
                    category=cat,
                    scope=scope,
                    agent_id=agent_id or "marketplace-buyer",
                    tags=mem.get("tags"),
                    confidence=mem.get("confidence", 1.0),
                    force=True,  # bypass W(m) gate — buyer explicitly opted in
                )
            )
            ingested += 1
        except Exception as e:
            errors.append(str(e))

    return {
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors[:5],
        "scope": scope,
    }


class _SimpleArgs:
    """Tiny argparse-Namespace stand-in for calling the existing
    ``cmd_memory_add`` without restructuring it.
    """

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


__all__ = [
    "fetch_arweave_json",
    "fetch_arweave_bytes",
    "derive_x25519_priv_from_wallet",
    "fetch_and_decrypt_envelope",
    "fetch_and_decrypt_bundle",
    "post_release_pipeline",
    "ingest_into_quarantine",
]
