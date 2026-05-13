"""Light Protocol compressed-token minting for brainctl bundles (v1).

Public surface for ``brainctl export --mint`` — mints one Light Protocol
compressed token per signed memory bundle, owned by the user's brainctl
wallet, with metadata pointing at an Arweave URI that holds the
client-side-encrypted bundle content.

Design constraints
------------------

* **Encryption is non-negotiable.** No bundle content reaches a public
  storage layer (Arweave, IPFS, on-chain) without first being encrypted
  client-side with a fresh per-bundle AES-256-GCM key. The chain
  mediates ownership; it never sees plaintext.

* **Keys live next to the bundle, never in the cloud.** The symmetric
  key for bundle X is written to ``~/.brainctl/keys/<mint>.key`` (mode
  0600). Marketplace key-wrapping (sale-time threshold encryption via
  Lit Protocol) is v1.5 work — out of scope for v1 mint.

* **Cross-language.** Light Protocol ships TS + Rust SDKs but no
  Python SDK. The minting itself happens in a Node.js helper
  (``tools/zk_mint.js``) that brainctl Python shells out to. The
  subprocess boundary keeps the Python install path solders/light-free
  and lets us stay on the existing `[mint]` Python extras.

* **Devnet first.** Default ``--cluster devnet`` so a demo run can't
  cost real SOL. Mainnet pinning is behind ``--cluster mainnet-beta``
  and prints a fat warning.

Pipeline
--------

1. Take a signed bundle (output of ``signing.sign_bundle``) and the
   raw bundle JSON.
2. Generate a fresh 32-byte symmetric key (``secrets.token_bytes(32)``).
3. AES-256-GCM encrypt the canonical bundle JSON. Output:
   ``{nonce: 12B, ciphertext: N, tag: 16B}`` concatenated.
4. Upload ciphertext blob to Arweave via Irys (free tier for ≤100 KB).
5. Build NFT-style metadata JSON pointing to the Arweave ciphertext
   URI, with the bundle hash + signer pubkey carried in attributes.
6. Upload metadata JSON to Arweave.
7. Shell to ``tools/zk_mint.js``, which uses the brainctl managed
   wallet (``~/.brainctl/wallet.json``) to mint one Light Protocol
   compressed token to the owner pubkey with the metadata URI.
8. Persist the symmetric key to ``~/.brainctl/keys/<mint>.key`` (0600).
9. Return ``{mint, tx_signature, arweave_metadata_uri,
   arweave_ciphertext_uri, key_path}``.

The Node helper's wire protocol is dead simple: brainctl Python
serialises a request dict to a JSON file, passes the path on argv,
and parses the helper's stdout as JSON.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

# Default Solana cluster for v1 demos. Mainnet requires an explicit flag.
DEFAULT_CLUSTER = "devnet"
SUPPORTED_CLUSTERS = ("devnet", "mainnet-beta")

# Where per-bundle symmetric keys live on disk. One file per mint.
DEFAULT_KEYS_DIR = "~/.brainctl/keys"

# Path to the Node helper, resolved relative to the brainctl package root
# at runtime. Falls back to ``BRAINCTL_ZK_MINT_HELPER`` env override for
# tests and dev-from-source.
_HELPER_RELATIVE = Path("tools") / "zk_mint.js"

# Where a user-or-agent-managed Helius API key lives on disk when the
# ``HELIUS_API_KEY`` env var isn't set. One key per line in dotenv
# shape: ``HELIUS_API_KEY=<value>``. Created by the user or by an agent
# walking the user through the mint flow.
DEFAULT_HELIUS_ENV_FILE = "~/.brainctl/helius.env"

# Token symbol baked into mint metadata. Matches the launch ticker so
# downstream wallets / explorers surface the affiliation.
DEFAULT_TOKEN_SYMBOL = "BRNDB"

# Brand prefix for the token name. Each bundle's mint name becomes
# ``BRNDB #<first 8 chars of bundle_hash>`` so a wallet view is scannable.
DEFAULT_NAME_PREFIX = "BRNDB"

# Hard cap on ciphertext upload size to Arweave free tier (Irys gives
# us ~100 KB free; we cap at 80 KB to leave headroom). Above this, the
# mint pipeline refuses upload and returns a structured error rather
# than spending real money.
MAX_CIPHERTEXT_BYTES = 80 * 1024


# ---------------------------------------------------------------------------
# Helius API key resolution
# ---------------------------------------------------------------------------

def resolve_helius_api_key(
    explicit: Optional[str] = None,
    *,
    env_file: Optional[str] = None,
) -> Optional[str]:
    """Find a Helius API key from CLI arg, env var, or on-disk dotenv file.

    Precedence (highest first):
      1. ``explicit`` — argument passed in from CLI ``--helius-api-key``
      2. ``$HELIUS_API_KEY`` env var
      3. ``~/.brainctl/helius.env`` (or ``env_file`` override) — single
         ``HELIUS_API_KEY=<value>`` line in dotenv shape.

    Returns the key string or ``None``. Empty / placeholder values
    (length < 8) are treated as not-set so the brainctl-launch
    Vercel ``""`` quirk doesn't accidentally satisfy a missing-key
    check.
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_val = os.environ.get("HELIUS_API_KEY")
    if env_val:
        candidates.append(env_val)
    target = Path(env_file or DEFAULT_HELIUS_ENV_FILE).expanduser()
    if target.exists():
        try:
            for line in target.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "HELIUS_API_KEY":
                    # Strip whitespace + surrounding quotes that dotenv
                    # files sometimes pick up from Vercel pulls.
                    v = v.strip().strip('"').strip("'")
                    if v:
                        candidates.append(v)
                    break
        except OSError:
            pass

    for cand in candidates:
        cand = cand.strip().strip('"').strip("'")
        if len(cand) >= 8:
            return cand
    return None


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def _require_cryptography():
    """Import the cryptography package on demand with a clear error."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        return AESGCM
    except ImportError:
        sys.stderr.write(
            "brainctl mint requires the 'cryptography' package.\n"
            "Install with:  pip install 'brainctl[mint]'\n"
        )
        raise SystemExit(1)


def encrypt_bundle(canonical_bundle_bytes: bytes) -> Dict[str, bytes]:
    """AES-256-GCM encrypt ``canonical_bundle_bytes``.

    Returns a dict with ``nonce`` (12 bytes), ``ciphertext`` (N bytes,
    GCM tag appended by the library), and ``key`` (32 bytes). The
    caller is responsible for persisting ``key`` somewhere safe — this
    function never writes it to disk.

    The output ``ciphertext`` from ``AESGCM.encrypt`` already includes
    the 16-byte GCM tag at the end; the wire format we store on
    Arweave is the concatenation ``nonce || ciphertext_with_tag``.
    """
    AESGCM = _require_cryptography()
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, canonical_bundle_bytes, associated_data=None)
    return {"nonce": nonce, "ciphertext": ct, "key": key}


def decrypt_bundle(nonce: bytes, ciphertext: bytes, key: bytes) -> bytes:
    """Reverse of ``encrypt_bundle``. Returns the canonical bundle bytes.

    Wrong key / tampered ciphertext → ``cryptography.exceptions.InvalidTag``.
    """
    AESGCM = _require_cryptography()
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def pack_encrypted_blob(nonce: bytes, ciphertext: bytes) -> bytes:
    """Serialise ``nonce || ciphertext`` for Arweave upload.

    The decryption side just slices ``[:12]`` off the front to recover
    the nonce. Format is intentionally trivial so a non-Python verifier
    can decrypt with no brainctl-specific knowledge.
    """
    if len(nonce) != 12:
        raise ValueError(f"nonce must be 12 bytes, got {len(nonce)}")
    return nonce + ciphertext


def unpack_encrypted_blob(blob: bytes) -> Dict[str, bytes]:
    """Reverse of ``pack_encrypted_blob``. Returns ``{nonce, ciphertext}``."""
    if len(blob) < 12 + 16:  # nonce + minimum GCM tag
        raise ValueError("encrypted blob too short to contain nonce + tag")
    return {"nonce": blob[:12], "ciphertext": blob[12:]}


# ---------------------------------------------------------------------------
# Key persistence
# ---------------------------------------------------------------------------

def resolve_keys_dir(override: Optional[str] = None) -> Path:
    """Resolve the per-bundle keys directory (default ``~/.brainctl/keys``)."""
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("BRAINCTL_KEYS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path(DEFAULT_KEYS_DIR).expanduser().resolve()


def write_bundle_key(
    mint_address: str,
    key: bytes,
    *,
    keys_dir: Optional[Path] = None,
) -> Path:
    """Write a per-bundle symmetric key atomically with 0600 permissions.

    Path: ``<keys_dir>/<mint_address>.key``. Hex-encoded 32 bytes. The
    parent directory is created 0700 if it doesn't exist.
    """
    if len(key) != 32:
        raise ValueError(f"key must be 32 bytes, got {len(key)}")
    target_dir = keys_dir or resolve_keys_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target_dir, 0o700)
    except (OSError, NotImplementedError):
        # chmod is a noop on Windows; the dir exists, we move on.
        pass

    target_path = target_dir / f"{mint_address}.key"
    if target_path.exists():
        raise FileExistsError(
            f"refusing to overwrite existing key at {target_path}"
        )

    # Atomic write with restrictive perms — same pattern wallet.py uses.
    fd = os.open(
        str(target_path),
        os.O_CREAT | os.O_WRONLY | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
    except Exception:
        try:
            target_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return target_path


def read_bundle_key(
    mint_address: str,
    *,
    keys_dir: Optional[Path] = None,
) -> bytes:
    """Read a 32-byte key for ``mint_address`` from the keys directory."""
    target_dir = keys_dir or resolve_keys_dir()
    target_path = target_dir / f"{mint_address}.key"
    if not target_path.exists():
        raise FileNotFoundError(f"no key on disk for mint {mint_address}")
    hex_str = target_path.read_text(encoding="utf-8").strip()
    raw = bytes.fromhex(hex_str)
    if len(raw) != 32:
        raise ValueError(
            f"corrupt key at {target_path}: expected 32 bytes, got {len(raw)}"
        )
    return raw


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def build_token_metadata(
    *,
    bundle_hash_hex: str,
    signer_pubkey_b58: str,
    memories_count: int,
    arweave_ciphertext_uri: str,
    signed_at: str,
    cluster: str,
) -> Dict[str, Any]:
    """Build the NFT-style JSON metadata blob for a memory token.

    Layout follows the Metaplex Token Standard so wallets and explorers
    that already parse NFT metadata can render brainctl mints out of the
    box. The custom brainctl fields live under ``properties`` /
    ``attributes`` to stay forward-compatible.
    """
    short_hash = bundle_hash_hex[:8]
    return {
        "name": f"{DEFAULT_NAME_PREFIX} #{short_hash}",
        "symbol": DEFAULT_TOKEN_SYMBOL,
        "description": (
            "A signed brainctl memory bundle. Encrypted content lives on "
            "Arweave; the cryptographic hash, signer pubkey, and "
            "ownership are anchored on Solana via Light Protocol."
        ),
        "image": "https://brnctl.fun/og/brndb-default.png",
        "external_url": "https://brnctl.fun",
        "attributes": [
            {"trait_type": "bundle_hash",  "value": bundle_hash_hex},
            {"trait_type": "signer",       "value": signer_pubkey_b58},
            {"trait_type": "memories",     "value": memories_count},
            {"trait_type": "signed_at",    "value": signed_at},
            {"trait_type": "cluster",      "value": cluster},
            {"trait_type": "ciphertext_uri", "value": arweave_ciphertext_uri},
            {"trait_type": "encryption",   "value": "AES-256-GCM"},
            {"trait_type": "schema",       "value": "brnctl/mint/v1"},
        ],
        "properties": {
            "category": "memory_bundle",
            "files": [
                {
                    "uri": arweave_ciphertext_uri,
                    "type": "application/x-brnctl-encrypted-bundle",
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Node.js helper shell-out
# ---------------------------------------------------------------------------

def _resolve_helper_path() -> Path:
    """Locate the ``zk_mint.js`` Node helper script."""
    env = os.environ.get("BRAINCTL_ZK_MINT_HELPER")
    if env:
        return Path(env).expanduser().resolve()

    # Walk up from this module looking for tools/zk_mint.js. Works for
    # both `pip install -e .` (helper sits next to src/) and packaged
    # installs (helper sits next to the package).
    here = Path(__file__).resolve()
    for candidate in (
        here.parent.parent.parent / _HELPER_RELATIVE,  # repo root in dev
        here.parent.parent / _HELPER_RELATIVE,         # adjacent install
        here.parent / _HELPER_RELATIVE,                # in-package install
    ):
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "could not find tools/zk_mint.js — set BRAINCTL_ZK_MINT_HELPER "
        "or install brainctl from a checkout that includes the helper."
    )


def _ensure_node_runtime() -> str:
    """Locate the Node binary; raise with a friendly install hint."""
    node = shutil.which("node")
    if not node:
        raise FileNotFoundError(
            "Node.js is required for `brainctl export --mint` (the Light "
            "Protocol SDK is TypeScript-only). Install Node ≥20 from "
            "https://nodejs.org/ or via your package manager."
        )
    return node


def _run_helper(request: Dict[str, Any], *, timeout: float = 120.0) -> Dict[str, Any]:
    """Shell to the Node helper with ``request`` as JSON; parse stdout JSON."""
    node = _ensure_node_runtime()
    helper = _resolve_helper_path()

    with tempfile.NamedTemporaryFile(
        prefix="brnctl-mint-",
        suffix=".json",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as f:
        json.dump(request, f)
        request_path = f.name

    try:
        proc = subprocess.run(
            [node, str(helper), "--request", request_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    finally:
        try:
            os.unlink(request_path)
        except FileNotFoundError:
            pass

    if proc.returncode != 0:
        return {
            "ok": False,
            "mint": None,
            "tx_signature": None,
            "error": (
                f"zk_mint helper exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            ),
        }

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "mint": None,
            "tx_signature": None,
            "error": f"helper returned non-JSON stdout: {e}",
        }


# ---------------------------------------------------------------------------
# Top-level mint pipeline
# ---------------------------------------------------------------------------

def mint_bundle(
    signed_bundle: Dict[str, Any],
    *,
    owner_pubkey_b58: str,
    cluster: str = DEFAULT_CLUSTER,
    helius_api_key: Optional[str] = None,
    keystore_path: Optional[str] = None,
    arweave_uploader: str = "irys",
    keys_dir: Optional[Path] = None,
    upload_fn: Optional[Any] = None,
    helper_runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Mint a Light Protocol compressed token for a signed memory bundle.

    Parameters
    ----------
    signed_bundle:
        The output of ``agentmemory.signing.sign_bundle``. Must contain
        ``bundle_hash_hex``, ``signer_pubkey_b58``, ``signed_at``, and
        the inner ``bundle`` dict (with ``memories``).
    owner_pubkey_b58:
        Base58 pubkey of the wallet that should receive the minted
        token. Usually equal to ``signed_bundle['signer_pubkey_b58']``
        but allowed to differ for "mint and send to another account"
        flows.
    cluster:
        ``devnet`` (default) or ``mainnet-beta``. Mainnet emits a
        warning on stderr before proceeding.
    helius_api_key:
        Optional Helius API key for Photon RPC. Falls back to
        ``HELIUS_API_KEY`` env if not provided. Required for
        mainnet-beta in this v1; devnet works against the public
        Light Protocol devnet RPC.
    keystore_path:
        Path to the Solana keypair file the helper should use to sign
        the mint transaction. Defaults to ``~/.brainctl/wallet.json``.
    arweave_uploader:
        Which Arweave path to use. ``"irys"`` (default) shells to the
        Node helper's Irys-backed uploader. ``"mock"`` skips upload
        entirely and uses placeholder URIs (for tests + dry runs).
    keys_dir:
        Where to persist the AES key. Default ``~/.brainctl/keys``.
    upload_fn, helper_runner:
        Injection points for tests. Both default to the real impls.

    Returns
    -------
    dict
        ``{ok, mint, tx_signature, arweave_ciphertext_uri,
        arweave_metadata_uri, key_path, cluster, error}``.
    """
    # --- 1. Validate input shape ------------------------------------------
    bundle = signed_bundle.get("bundle")
    bundle_hash_hex = signed_bundle.get("bundle_hash_hex")
    signer = signed_bundle.get("signer_pubkey_b58")
    signed_at = signed_bundle.get("signed_at")
    if not (isinstance(bundle, dict) and bundle_hash_hex and signer and signed_at):
        return {
            "ok": False, "mint": None, "tx_signature": None,
            "error": "signed_bundle missing required fields",
        }
    if cluster not in SUPPORTED_CLUSTERS:
        return {
            "ok": False, "mint": None, "tx_signature": None,
            "error": (
                f"unsupported cluster {cluster!r}; expected one of "
                f"{SUPPORTED_CLUSTERS}"
            ),
        }
    if cluster == "mainnet-beta":
        sys.stderr.write(
            "WARNING: minting on mainnet-beta. Each mint costs real SOL "
            "(~0.000017 SOL per token via Light Protocol). Press Ctrl-C "
            "in the next 3 seconds to abort if this was unintended.\n"
        )

    # --- 2. Encrypt the canonical bundle JSON -----------------------------
    from agentmemory import signing
    canonical = signing.canonical_json(bundle)
    enc = encrypt_bundle(canonical)
    blob = pack_encrypted_blob(enc["nonce"], enc["ciphertext"])
    if len(blob) > MAX_CIPHERTEXT_BYTES:
        return {
            "ok": False, "mint": None, "tx_signature": None,
            "error": (
                f"encrypted bundle is {len(blob)} bytes — exceeds the "
                f"{MAX_CIPHERTEXT_BYTES}-byte cap for free-tier Arweave "
                "upload. Filter the bundle (--ids / --created-after / "
                "--category) and try again."
            ),
        }

    # --- 3. Upload ciphertext + metadata to Arweave -----------------------
    if upload_fn is None:
        upload_fn = _default_arweave_upload
    upload = upload_fn(
        ciphertext_blob=blob,
        bundle_hash_hex=bundle_hash_hex,
        signer_pubkey_b58=signer,
        memories_count=len(bundle.get("memories", [])),
        signed_at=signed_at,
        cluster=cluster,
        uploader=arweave_uploader,
    )
    if not upload.get("ok"):
        return {
            "ok": False, "mint": None, "tx_signature": None,
            "error": f"arweave upload failed: {upload.get('error')}",
        }

    arweave_ciphertext_uri = upload["ciphertext_uri"]
    arweave_metadata_uri = upload["metadata_uri"]

    # --- 4. Mint via Node helper ------------------------------------------
    resolved_key = resolve_helius_api_key(helius_api_key)
    if cluster == "mainnet-beta" and not resolved_key:
        return {
            "ok": False, "mint": None, "tx_signature": None,
            "arweave_ciphertext_uri": arweave_ciphertext_uri,
            "arweave_metadata_uri": arweave_metadata_uri,
            "error": (
                "mainnet-beta mint requires a Helius API key. Set "
                "HELIUS_API_KEY in your shell, pass --helius-api-key, "
                "or drop the key into ~/.brainctl/helius.env as "
                "HELIUS_API_KEY=<value> (chmod 0600)."
            ),
        }
    request = {
        "action": "mint",
        "cluster": cluster,
        "owner_pubkey": owner_pubkey_b58,
        "keystore_path": keystore_path,
        "helius_api_key": resolved_key,
        "metadata_uri": arweave_metadata_uri,
        "name": f"{DEFAULT_NAME_PREFIX} #{bundle_hash_hex[:8]}",
        "symbol": DEFAULT_TOKEN_SYMBOL,
    }
    runner = helper_runner or _run_helper
    helper_result = runner(request)
    if not helper_result.get("ok"):
        return {
            "ok": False,
            "mint": None,
            "tx_signature": None,
            "arweave_ciphertext_uri": arweave_ciphertext_uri,
            "arweave_metadata_uri": arweave_metadata_uri,
            "error": helper_result.get("error") or "zk_mint helper failed",
        }

    mint = helper_result["mint"]
    tx_signature = helper_result.get("tx_signature")

    # --- 5. Persist symmetric key under <keys_dir>/<mint>.key -------------
    try:
        key_path = write_bundle_key(mint, enc["key"], keys_dir=keys_dir)
    except FileExistsError as e:
        # Don't blow up the mint — the chain part succeeded. Surface
        # the conflict in the response and let the user move the key.
        return {
            "ok": True,
            "mint": mint,
            "tx_signature": tx_signature,
            "arweave_ciphertext_uri": arweave_ciphertext_uri,
            "arweave_metadata_uri": arweave_metadata_uri,
            "key_path": None,
            "cluster": cluster,
            "warning": (
                f"key write skipped: {e}. The encryption key was "
                "generated but not persisted; re-running this mint will "
                "produce a new key. Capture the key manually from a "
                "future verify run."
            ),
            "error": None,
        }

    return {
        "ok": True,
        "mint": mint,
        "tx_signature": tx_signature,
        "arweave_ciphertext_uri": arweave_ciphertext_uri,
        "arweave_metadata_uri": arweave_metadata_uri,
        "key_path": str(key_path),
        "cluster": cluster,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Default Arweave upload — shells to the Node helper for `arweave_upload`
# ---------------------------------------------------------------------------

def _default_arweave_upload(
    *,
    ciphertext_blob: bytes,
    bundle_hash_hex: str,
    signer_pubkey_b58: str,
    memories_count: int,
    signed_at: str,
    cluster: str,
    uploader: str,
) -> Dict[str, Any]:
    """Upload ciphertext + metadata to Arweave via the Node helper.

    Returns ``{ok, ciphertext_uri, metadata_uri, error}``.
    """
    if uploader == "mock":
        # Used by tests + dry-runs. Produces deterministic-ish URIs.
        return {
            "ok": True,
            "ciphertext_uri": f"ar://mock/ciphertext/{bundle_hash_hex}",
            "metadata_uri": f"ar://mock/metadata/{bundle_hash_hex}",
            "error": None,
        }

    metadata = build_token_metadata(
        bundle_hash_hex=bundle_hash_hex,
        signer_pubkey_b58=signer_pubkey_b58,
        memories_count=memories_count,
        # Placeholder ciphertext_uri replaced inside the helper once the
        # blob upload completes — the helper rebuilds metadata with the
        # actual URI before its own upload to keep the integrity intact.
        arweave_ciphertext_uri="ar://pending",
        signed_at=signed_at,
        cluster=cluster,
    )

    # Stash ciphertext as a base64 string inside the request — the Node
    # helper decodes and streams it to Irys. Subprocess argv is too
    # small for binary payloads.
    import base64
    request = {
        "action": "arweave_upload",
        "uploader": uploader,
        "cluster": cluster,
        "ciphertext_b64": base64.b64encode(ciphertext_blob).decode("ascii"),
        "metadata_template": metadata,
    }
    return _run_helper(request)


__all__ = [
    "DEFAULT_CLUSTER",
    "SUPPORTED_CLUSTERS",
    "DEFAULT_KEYS_DIR",
    "DEFAULT_HELIUS_ENV_FILE",
    "DEFAULT_TOKEN_SYMBOL",
    "DEFAULT_NAME_PREFIX",
    "MAX_CIPHERTEXT_BYTES",
    "resolve_helius_api_key",
    "encrypt_bundle",
    "decrypt_bundle",
    "pack_encrypted_blob",
    "unpack_encrypted_blob",
    "write_bundle_key",
    "read_bundle_key",
    "resolve_keys_dir",
    "build_token_metadata",
    "mint_bundle",
]
