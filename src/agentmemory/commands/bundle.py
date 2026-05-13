"""`brainctl bundle ...` — operations on memory bundles you've minted.

Subcommands:
  decrypt   — local AES decryption of a bundle you minted yourself.
              Reads the per-bundle key from ~/.brainctl/keys/<mint>.key
              and the ciphertext from Arweave. Useful for verifying a
              minted bundle without going through the marketplace flow.

Future (v2.6.1+):
  send-key    SealedBox-encrypt the per-bundle AES key to a recipient
              wallet, upload the envelope to Arweave, post a release-
              style memo on chain. Makes a gifted cNFT useful.
  receive-key Inverse: poll chain for a release memo addressed to your
              wallet, decrypt, optionally ingest into brain.db.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _emit(payload: Dict[str, Any], *, as_json: bool, exit_code: int = 0) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        if payload.get("ok"):
            mem_count = payload.get("memories_count")
            if mem_count is not None:
                print(
                    f"decrypted {mem_count} memories from mint "
                    f"{payload.get('mint')}"
                )
            out = payload.get("output_path")
            if out:
                print(f"  wrote: {out}")
            else:
                # Stream the bundle to stdout if no -o given.
                pass
        else:
            print(f"decrypt failed: {payload.get('error', 'unknown error')}")
    sys.exit(exit_code)


def cmd_bundle_decrypt(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))
    mint = args.mint
    uri = args.ciphertext_uri

    # 1. Read the per-bundle AES key from disk.
    from agentmemory import minting
    keys_dir = (
        Path(args.keys_dir).expanduser() if getattr(args, "keys_dir", None) else None
    )
    try:
        key = minting.read_bundle_key(mint, keys_dir=keys_dir)
    except FileNotFoundError as e:
        _emit({"ok": False, "error": "key_not_found", "detail": str(e),
               "mint": mint},
              as_json=as_json, exit_code=2)
        return
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": "key_read_failed", "detail": str(e),
               "mint": mint},
              as_json=as_json, exit_code=1)
        return

    # 2. Fetch the ciphertext from Arweave.
    from agentmemory import marketplace_buy as _buy
    try:
        if uri.startswith("ar://"):
            arweave_id = uri[len("ar://"):].split("?")[0]
        else:
            arweave_id = uri
        blob = _buy.fetch_arweave_bytes(arweave_id)
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": "arweave_fetch_failed", "detail": str(e),
               "mint": mint, "ciphertext_uri": uri},
              as_json=as_json, exit_code=1)
        return

    # 3. Unpack + AES decrypt.
    try:
        parts = minting.unpack_encrypted_blob(blob)
        plaintext = minting.decrypt_bundle(
            parts["nonce"], parts["ciphertext"], key
        )
        bundle = json.loads(plaintext)
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": "decrypt_failed", "detail": str(e),
               "mint": mint},
              as_json=as_json, exit_code=1)
        return

    # 4. Optionally write to disk or stream.
    out_path: Optional[str] = getattr(args, "output", None)
    if out_path:
        Path(out_path).expanduser().write_text(
            json.dumps(bundle, indent=2, default=str), encoding="utf-8"
        )

    payload = {
        "ok": True,
        "mint": mint,
        "ciphertext_uri": uri,
        "memories_count": len(bundle.get("memories", [])) if isinstance(bundle, dict) else 0,
        "output_path": out_path,
    }
    if not out_path and not as_json:
        # No -o, no --json → dump the bundle to stdout for piping.
        print(json.dumps(bundle, indent=2, default=str))
        sys.exit(0)
    _emit(payload, as_json=as_json)


def register_parser(sub: Any) -> None:
    p = sub.add_parser(
        "bundle",
        help="Operations on memory bundles you've minted (decrypt, ...)",
        description=(
            "Local-side operations on minted memory bundles. "
            "Decrypt your own bundles offline using the per-bundle AES "
            "key persisted at ~/.brainctl/keys/<mint>.key."
        ),
    )
    bsub = p.add_subparsers(dest="bundle_command", required=True)

    p_dec = bsub.add_parser(
        "decrypt",
        help="Decrypt a bundle you minted (local-side).",
        description=(
            "Fetches the ciphertext from Arweave, reads the per-bundle "
            "AES key from disk, AES-256-GCM decrypts, and returns the "
            "plaintext bundle JSON."
        ),
    )
    p_dec.add_argument("mint",
                       help="Compressed-token mint address from the original "
                            "`brainctl export --sign --mint`")
    p_dec.add_argument("--ciphertext-uri", dest="ciphertext_uri",
                       required=True,
                       help="ar://... URI of the encrypted bundle "
                            "(from the mint command's "
                            "arweave_ciphertext_uri output)")
    p_dec.add_argument("-o", "--output", default=None,
                       help="Write the decrypted bundle JSON to this path. "
                            "Without -o, the JSON is streamed to stdout.")
    p_dec.add_argument("--keys-dir", dest="keys_dir", default=None,
                       help="Override the key directory "
                            "(default: ~/.brainctl/keys)")
    p_dec.add_argument("--json", action="store_true",
                       help="Emit a JSON summary instead of the bundle JSON.")
    p_dec.set_defaults(func=cmd_bundle_decrypt)


__all__ = ["register_parser", "cmd_bundle_decrypt"]
