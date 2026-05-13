"""Signed memory exports for brainctl.

Produces portable, cryptographically-signed JSON bundles of brain.db
memories. Verification is offline by default; on-chain pinning to the
Solana memo program is opt-in via ``pin_onchain``.

Design constraints (memory #1691):
  * No token gating — anyone with brainctl + a Solana keypair can sign.
  * Local-first — memories never leave the user's machine; only the
    bundle's SHA-256 hash is ever pinned on-chain.
  * Privacy-preserving — a verifier with just the on-chain receipt
    cannot reconstruct any memory content.

The bundle wire format is intentionally tiny so any verifier (Python,
JavaScript, Rust, Go) can reproduce the hash and check the signature
without depending on brainctl.

Bundle format (version 1)::

    {
      "version": 1,
      "bundle": {
        "version": 1,
        "generated_at": "2026-04-16T12:00:00+00:00",
        "filter_used": {...},
        "memories": [{...row dict...}, ...]
      },
      "bundle_hash_hex": "<sha256 of canonical bundle>",
      "signature_b58": "<base58 of ed25519 signature over bundle_hash bytes>",
      "signer_pubkey_b58": "<base58 of ed25519 public key>",
      "signed_at": "2026-04-16T12:00:00+00:00"
    }

Canonical JSON for hashing is exactly::

    json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

Document those four kwargs anywhere a non-Python verifier reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Bundle wire-format version. Bump when fields change shape.
BUNDLE_VERSION = 1

# SPL Memo program v2 (canonical, mainnet + devnet + testnet).
# Source: https://spl.solana.com/memo
# (Verify against any block explorer or solders.system_program docs.)
MEMO_PROGRAM_ID_B58 = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# Default Solana RPC endpoint for on-chain pin/verify. Always overridable.
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

# Memo body prefix — namespaces the on-chain receipt so verifiers can
# filter brainctl pins out of unrelated memos by the same wallet.
MEMO_PREFIX = "brainctl/v1"


# ---------------------------------------------------------------------------
# Bundle construction
# ---------------------------------------------------------------------------

# Columns we copy verbatim from the memories table into the bundle.
# Picked for stability across schema migrations: every column listed here
# has been present since 2.0.x and survives the W(m) gate refactor.
_BUNDLE_COLS: Sequence[str] = (
    "id", "agent_id", "category", "scope", "content", "confidence",
    "tags", "created_at", "updated_at", "source_event_id",
    "supersedes_id", "trust_score", "memory_type",
)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Serialise a sqlite3.Row to a JSON-safe dict."""
    out: Dict[str, Any] = {}
    for col in _BUNDLE_COLS:
        try:
            v = row[col]
        except (IndexError, KeyError):
            v = None
        out[col] = v
    return out


def build_bundle(
    db: sqlite3.Connection,
    *,
    agent_id: Optional[str] = None,
    category: Optional[str] = None,
    scope: Optional[str] = None,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    ids: Optional[Iterable[int]] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an unsigned memory bundle by filtering ``db`` rows.

    All filters are AND-combined. ``ids`` short-circuits the other
    filters when provided (selects exactly that ID set, including
    retired rows). All other filters skip retired memories.

    ``generated_at`` is injectable so callers (and tests) can produce
    a deterministic bundle. Defaults to current UTC timestamp.
    """
    where_parts: List[str] = []
    params: List[Any] = []

    explicit_ids = list(ids) if ids is not None else None
    if explicit_ids is not None:
        if not explicit_ids:
            # Empty id list → empty bundle (don't run a useless query).
            rows: List[sqlite3.Row] = []
        else:
            placeholders = ",".join("?" for _ in explicit_ids)
            sql = (
                f"SELECT {', '.join(_BUNDLE_COLS)} FROM memories "
                f"WHERE id IN ({placeholders}) ORDER BY id"
            )
            rows = list(db.execute(sql, explicit_ids).fetchall())
    else:
        where_parts.append("retired_at IS NULL")
        if agent_id:
            where_parts.append("agent_id = ?")
            params.append(agent_id)
        if category:
            where_parts.append("category = ?")
            params.append(category)
        if scope:
            where_parts.append("scope = ?")
            params.append(scope)
        if created_after:
            where_parts.append("created_at >= ?")
            params.append(created_after)
        if created_before:
            where_parts.append("created_at <= ?")
            params.append(created_before)
        sql = (
            f"SELECT {', '.join(_BUNDLE_COLS)} FROM memories "
            f"WHERE {' AND '.join(where_parts)} ORDER BY id"
        )
        rows = list(db.execute(sql, params).fetchall())

    memories = [_row_to_dict(r) for r in rows]

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()

    filter_used: Dict[str, Any] = {
        "agent_id": agent_id,
        "category": category,
        "scope": scope,
        "created_after": created_after,
        "created_before": created_before,
        "ids": explicit_ids,
    }

    return {
        "version": BUNDLE_VERSION,
        "generated_at": generated_at,
        "filter_used": filter_used,
        "memories": memories,
    }


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> bytes:
    """Serialise ``obj`` to canonical JSON bytes.

    External verifiers MUST use these exact four kwargs:
      * ``sort_keys=True``
      * ``separators=(",", ":")``
      * ``ensure_ascii=True``
      * (no ``indent``)

    ``ensure_ascii=True`` is the subtle one — it guarantees byte-
    identical output across Python patch versions, platforms, and
    locale settings.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def bundle_hash(bundle: Dict[str, Any]) -> bytes:
    """SHA-256 of the canonical JSON serialisation of ``bundle``."""
    return hashlib.sha256(canonical_json(bundle)).digest()


# ---------------------------------------------------------------------------
# Lazy solders import + keystore loader
# ---------------------------------------------------------------------------

def _require_solders():
    """Import solders on demand. Raises a SystemExit with install hint."""
    try:
        import solders  # noqa: F401  (just the package presence check)
        return solders
    except ImportError:
        sys.stderr.write(
            "brainctl signing requires the 'solders' package.\n"
            "Install with:  pip install 'brainctl[signing]'\n"
        )
        raise SystemExit(1)


def load_keystore(path: str | os.PathLike[str]):
    """Load a Solana CLI keystore (JSON array of 64 ints) into a Keypair.

    Solana CLI format is a JSON list whose 64 entries are the
    concatenation ``secret_scalar(32) || pubkey(32)``. ``solders``
    accepts that buffer directly via ``Keypair.from_bytes``.
    """
    _require_solders()
    from solders.keypair import Keypair  # type: ignore

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"keystore not found: {p}")
    raw = json.loads(p.read_text())
    if not isinstance(raw, list) or len(raw) != 64:
        raise ValueError(
            "invalid Solana keystore: expected JSON array of 64 ints "
            f"(got {type(raw).__name__} of len "
            f"{len(raw) if hasattr(raw, '__len__') else 'n/a'})"
        )
    if not all(isinstance(x, int) and 0 <= x <= 255 for x in raw):
        raise ValueError("invalid Solana keystore: entries must be ints 0-255")
    return Keypair.from_bytes(bytes(raw))


def resolve_keystore_path(cli_arg: Optional[str]) -> str:
    """Resolve a keystore path from CLI arg or env, with a clear error."""
    if cli_arg:
        return cli_arg
    env_path = os.environ.get("BRAINCTL_SIGNING_KEY_PATH")
    if env_path:
        return env_path
    raise FileNotFoundError(
        "no keystore specified — pass --keystore <path> "
        "or set BRAINCTL_SIGNING_KEY_PATH"
    )


# ---------------------------------------------------------------------------
# Signing + verification (offline)
# ---------------------------------------------------------------------------

def sign_bundle(bundle: Dict[str, Any], keypair, *, signed_at: Optional[str] = None) -> Dict[str, Any]:
    """Wrap a bundle with an Ed25519 signature over its canonical hash.

    The signature is computed over ``bundle_hash(bundle)`` (the raw 32
    SHA-256 bytes), not over the hex string, so external verifiers can
    re-sign with ``ed25519_sign(secret, sha256(canonical_bundle))``.
    """
    _require_solders()
    from solders.keypair import Keypair  # type: ignore  # noqa: F401

    h = bundle_hash(bundle)
    sig = keypair.sign_message(h)
    if signed_at is None:
        signed_at = datetime.now(timezone.utc).isoformat()

    return {
        "version": BUNDLE_VERSION,
        "bundle": bundle,
        "bundle_hash_hex": h.hex(),
        "signature_b58": str(sig),       # solders Signature stringifies to base58
        "signer_pubkey_b58": str(keypair.pubkey()),
        "signed_at": signed_at,
    }


def _verify_v1(signed_bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Verify a version-1 signed bundle. Returns the standard verify dict."""
    _require_solders()
    from solders.pubkey import Pubkey       # type: ignore
    from solders.signature import Signature  # type: ignore

    bundle = signed_bundle.get("bundle")
    sig_b58 = signed_bundle.get("signature_b58")
    pub_b58 = signed_bundle.get("signer_pubkey_b58")
    claimed_hash_hex = signed_bundle.get("bundle_hash_hex")
    signed_at = signed_bundle.get("signed_at")

    if not isinstance(bundle, dict) or not sig_b58 or not pub_b58:
        return {
            "ok": False,
            "signer_pubkey": pub_b58,
            "signed_at": signed_at,
            "bundle_hash": claimed_hash_hex,
            "memories_count": 0,
            "error": "malformed signed bundle: missing bundle/signature/pubkey",
        }

    # Recompute hash and compare to the claimed one (catches off-by-one
    # tampering of the hash field separately from signature failures).
    actual_hash = bundle_hash(bundle)
    actual_hash_hex = actual_hash.hex()
    memories_count = len(bundle.get("memories", []))

    if claimed_hash_hex and claimed_hash_hex != actual_hash_hex:
        return {
            "ok": False,
            "signer_pubkey": pub_b58,
            "signed_at": signed_at,
            "bundle_hash": actual_hash_hex,
            "memories_count": memories_count,
            "error": (
                "bundle tampered: hash mismatch "
                f"(claimed={claimed_hash_hex[:16]}..., "
                f"actual={actual_hash_hex[:16]}...)"
            ),
        }

    try:
        signature = Signature.from_string(sig_b58)
        pubkey = Pubkey.from_string(pub_b58)
    except Exception as e:
        return {
            "ok": False,
            "signer_pubkey": pub_b58,
            "signed_at": signed_at,
            "bundle_hash": actual_hash_hex,
            "memories_count": memories_count,
            "error": f"malformed signature or pubkey: {e}",
        }

    # solders Signature.verify returns bool, never raises on bad sig.
    # In solders >=0.21 the signature is verify(pubkey, message_bytes).
    ok = bool(signature.verify(pubkey, actual_hash))
    if not ok:
        return {
            "ok": False,
            "signer_pubkey": pub_b58,
            "signed_at": signed_at,
            "bundle_hash": actual_hash_hex,
            "memories_count": memories_count,
            "error": "signature verification failed",
        }

    return {
        "ok": True,
        "signer_pubkey": pub_b58,
        "signed_at": signed_at,
        "bundle_hash": actual_hash_hex,
        "memories_count": memories_count,
        "error": None,
    }


def verify_bundle(signed_bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Verify a signed bundle. Dispatches on the outer ``version`` field.

    Forward-compat: a brainctl that supports version 2 still runs the
    v1 path for v1 bundles. Unknown versions return a structured error
    rather than raising.
    """
    version = signed_bundle.get("version")
    if version == 1:
        return _verify_v1(signed_bundle)
    return {
        "ok": False,
        "signer_pubkey": signed_bundle.get("signer_pubkey_b58"),
        "signed_at": signed_bundle.get("signed_at"),
        "bundle_hash": signed_bundle.get("bundle_hash_hex"),
        "memories_count": 0,
        "error": f"unsupported bundle version: {version}",
    }


# ---------------------------------------------------------------------------
# Solana RPC plumbing
# ---------------------------------------------------------------------------

def _rpc_call(rpc_url: str, method: str, params: list, *, timeout: float = 30.0) -> Dict[str, Any]:
    """Minimal JSON-RPC 2.0 client. Patch this for tests."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    parsed = json.loads(body)
    if "error" in parsed:
        raise RuntimeError(f"RPC error: {parsed['error']}")
    return parsed.get("result", {})


def _build_memo_tx(
    payer_keypair,
    memo_bytes: bytes,
    recent_blockhash_b58: str,
    *,
    fee_lamports: int = 0,
    fee_treasury_b58: str = "",
):
    """Construct a single-instruction memo transaction signed by ``payer_keypair``.

    If ``fee_lamports > 0`` and ``fee_treasury_b58`` is set, prepends a
    ``SystemProgram.Transfer`` instruction so the protocol fee is paid
    atomically with the memo. The whole bundle is signed once and lands
    in a single tx.

    The memo program accepts arbitrary UTF-8 bytes as its instruction
    data. Signers listed in the instruction's accounts metadata become
    required signers on the transaction.
    """
    _require_solders()
    from solders.hash import Hash                              # type: ignore
    from solders.instruction import AccountMeta, Instruction   # type: ignore
    from solders.message import Message                        # type: ignore
    from solders.pubkey import Pubkey                          # type: ignore
    from solders.system_program import TransferParams, transfer  # type: ignore
    from solders.transaction import Transaction                # type: ignore

    program_id = Pubkey.from_string(MEMO_PROGRAM_ID_B58)
    payer_pub = payer_keypair.pubkey()

    instructions = []
    if fee_lamports > 0 and fee_treasury_b58:
        instructions.append(transfer(TransferParams(
            from_pubkey=payer_pub,
            to_pubkey=Pubkey.from_string(fee_treasury_b58),
            lamports=fee_lamports,
        )))

    # Memo v2 records signers via account metadata so the on-chain
    # receipt is provably linked to the wallet that paid for it.
    accounts = [AccountMeta(pubkey=payer_pub, is_signer=True, is_writable=True)]
    instructions.append(
        Instruction(program_id=program_id, accounts=accounts, data=memo_bytes)
    )

    blockhash = Hash.from_string(recent_blockhash_b58)
    msg = Message.new_with_blockhash(instructions, payer_pub, blockhash)
    return Transaction([payer_keypair], msg, blockhash)


def pin_onchain(
    signed_bundle: Dict[str, Any],
    keypair,
    rpc_url: str = DEFAULT_RPC_URL,
    *,
    cluster: str = "mainnet-beta",
) -> Dict[str, Any]:
    """Pin a bundle's hash on-chain via the SPL memo program.

    Memo body: ``brainctl/v1:<bundle_hash_hex>:<signer_pubkey_b58>``.
    The memo + the signer pubkey is enough for any third party to
    later verify "this wallet attested to this hash at this slot".
    Memory contents themselves never touch the network.

    A flat protocol fee (set in ``agentmemory.protocol_fees``) is
    bundled into the same transaction when ``cluster="mainnet-beta"``
    and the fee kill-switch is not set. Devnet is free.
    """
    _require_solders()
    from solders.signature import Signature  # type: ignore  # noqa: F401
    from agentmemory import protocol_fees as _pfees

    bundle_hash_hex = signed_bundle.get("bundle_hash_hex")
    signer_b58 = signed_bundle.get("signer_pubkey_b58")
    if not bundle_hash_hex or not signer_b58:
        return {
            "ok": False, "signature": None, "slot": None,
            "error": "signed bundle missing bundle_hash_hex or signer_pubkey_b58",
        }

    memo_str = f"{MEMO_PREFIX}:{bundle_hash_hex}:{signer_b58}"
    memo_bytes = memo_str.encode("utf-8")

    # Protocol fee: bundle in the same tx as the memo so it's atomic.
    if _pfees.charge_fee(cluster):
        fee_lamports = _pfees.fee_lamports_for_op("pin")
        fee_treasury = _pfees.resolve_treasury_pubkey()
    else:
        fee_lamports = 0
        fee_treasury = ""

    try:
        # 1. recent blockhash
        bh = _rpc_call(rpc_url, "getLatestBlockhash", [{"commitment": "finalized"}])
        recent_bh = bh["value"]["blockhash"]

        # 2. build + sign tx (with fee transfer prepended atomically)
        tx = _build_memo_tx(
            keypair,
            memo_bytes,
            recent_bh,
            fee_lamports=fee_lamports,
            fee_treasury_b58=fee_treasury,
        )
        raw = bytes(tx)
        import base64 as _b64
        tx_b64 = _b64.b64encode(raw).decode("ascii")

        # 3. submit
        sig = _rpc_call(rpc_url, "sendTransaction", [
            tx_b64,
            {"encoding": "base64", "preflightCommitment": "finalized"},
        ])
        # sendTransaction returns the signature string at the top level.

        # 4. lookup slot (optional, soft-fail)
        slot = None
        try:
            stat = _rpc_call(rpc_url, "getSignatureStatuses", [[sig]])
            entry = (stat.get("value") or [None])[0]
            if entry:
                slot = entry.get("slot")
        except Exception:
            pass

        return {
            "ok": True,
            "signature": sig,
            "slot": slot,
            "error": None,
            "fee_lamports": fee_lamports,
            "fee_treasury": fee_treasury or None,
        }
    except Exception as e:
        return {"ok": False, "signature": None, "slot": None, "error": str(e)}


def verify_onchain(
    bundle_hash_hex: str,
    expected_signer_b58: str,
    rpc_url: str = DEFAULT_RPC_URL,
    *,
    limit: int = 100,
) -> Dict[str, Any]:
    """Search a wallet's recent memo transactions for a matching pin.

    Returns ``found=True`` if any of the wallet's recent transactions
    contains a memo-program log line with the expected
    ``brainctl/v1:<hash>:<signer>`` payload.
    """
    needle = f"{MEMO_PREFIX}:{bundle_hash_hex}:{expected_signer_b58}"
    try:
        sigs = _rpc_call(rpc_url, "getSignaturesForAddress", [
            expected_signer_b58,
            {"limit": int(limit)},
        ])
        if not isinstance(sigs, list):
            return {"found": False, "tx_signature": None, "block_time": None,
                    "error": "unexpected getSignaturesForAddress shape"}

        for entry in sigs:
            sig = entry.get("signature")
            if not sig:
                continue
            tx = _rpc_call(rpc_url, "getTransaction", [
                sig,
                {"encoding": "json", "maxSupportedTransactionVersion": 0},
            ])
            # The memo body shows up as a log line under meta.logMessages
            # (format: "Program log: Memo (len <n>): \"<body>\"").
            meta = (tx or {}).get("meta") or {}
            for log in meta.get("logMessages", []) or []:
                if needle in log:
                    return {
                        "found": True,
                        "tx_signature": sig,
                        "block_time": (tx or {}).get("blockTime"),
                        "error": None,
                    }
        return {"found": False, "tx_signature": None, "block_time": None, "error": None}
    except Exception as e:
        return {"found": False, "tx_signature": None, "block_time": None, "error": str(e)}


__all__ = [
    "BUNDLE_VERSION",
    "MEMO_PROGRAM_ID_B58",
    "MEMO_PREFIX",
    "DEFAULT_RPC_URL",
    "build_bundle",
    "canonical_json",
    "bundle_hash",
    "load_keystore",
    "resolve_keystore_path",
    "sign_bundle",
    "verify_bundle",
    "pin_onchain",
    "verify_onchain",
]
