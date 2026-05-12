"""Unit tests for ``agentmemory.minting`` (v1 Light Token mint).

These tests cover the pieces that don't need a Node runtime or live
network calls: encryption, key persistence, metadata building, and the
pipeline glue with the Node helper + Arweave upload both mocked out.

Live mint integration testing happens in
``tests/test_minting_live.py`` (gated on ``BRAINCTL_LIVE_MINT_TEST=1``)
which is run manually before each release.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentmemory import minting


# ---------------------------------------------------------------------------
# Encryption roundtrip
# ---------------------------------------------------------------------------

class TestEncryption:
    def test_roundtrip_recovers_plaintext(self):
        plaintext = b'{"hello":"world","memory":"important fact"}'
        enc = minting.encrypt_bundle(plaintext)
        recovered = minting.decrypt_bundle(
            enc["nonce"], enc["ciphertext"], enc["key"]
        )
        assert recovered == plaintext

    def test_key_is_32_bytes(self):
        enc = minting.encrypt_bundle(b"x")
        assert len(enc["key"]) == 32

    def test_nonce_is_12_bytes(self):
        enc = minting.encrypt_bundle(b"x")
        assert len(enc["nonce"]) == 12

    def test_keys_differ_across_calls(self):
        a = minting.encrypt_bundle(b"same plaintext")
        b = minting.encrypt_bundle(b"same plaintext")
        assert a["key"] != b["key"]
        assert a["nonce"] != b["nonce"]
        assert a["ciphertext"] != b["ciphertext"]

    def test_wrong_key_fails_decrypt(self):
        enc = minting.encrypt_bundle(b"secret")
        bad_key = bytes(32)  # all zeros
        # cryptography raises InvalidTag, an exception subclass we don't
        # need to import here — we just confirm decryption refuses.
        with pytest.raises(Exception):
            minting.decrypt_bundle(enc["nonce"], enc["ciphertext"], bad_key)

    def test_tampered_ciphertext_fails_decrypt(self):
        enc = minting.encrypt_bundle(b"secret")
        tampered = bytearray(enc["ciphertext"])
        tampered[0] ^= 0x01
        with pytest.raises(Exception):
            minting.decrypt_bundle(
                enc["nonce"], bytes(tampered), enc["key"]
            )


# ---------------------------------------------------------------------------
# Pack / unpack wire format
# ---------------------------------------------------------------------------

class TestPackUnpack:
    def test_pack_unpack_roundtrip(self):
        nonce = bytes(range(12))
        ct = b"\x42" * 64
        blob = minting.pack_encrypted_blob(nonce, ct)
        recovered = minting.unpack_encrypted_blob(blob)
        assert recovered["nonce"] == nonce
        assert recovered["ciphertext"] == ct

    def test_pack_rejects_wrong_nonce_length(self):
        with pytest.raises(ValueError):
            minting.pack_encrypted_blob(b"too short", b"ct")

    def test_unpack_rejects_short_blob(self):
        with pytest.raises(ValueError):
            minting.unpack_encrypted_blob(b"x" * 10)


# ---------------------------------------------------------------------------
# Key file persistence
# ---------------------------------------------------------------------------

class TestKeyPersistence:
    def test_write_and_read(self, tmp_path: Path):
        key = b"\x11" * 32
        mint_addr = "MintXyz1234567890"
        path = minting.write_bundle_key(mint_addr, key, keys_dir=tmp_path)
        assert path.exists()
        assert path.name == f"{mint_addr}.key"
        recovered = minting.read_bundle_key(mint_addr, keys_dir=tmp_path)
        assert recovered == key

    def test_write_creates_0600_file(self, tmp_path: Path):
        if os.name == "nt":
            pytest.skip("POSIX-only permission semantics")
        key = b"\x22" * 32
        path = minting.write_bundle_key("M", key, keys_dir=tmp_path)
        st = path.stat()
        # Owner read+write only.
        assert stat.S_IMODE(st.st_mode) == 0o600

    def test_write_rejects_existing_key(self, tmp_path: Path):
        key = b"\x33" * 32
        minting.write_bundle_key("dup", key, keys_dir=tmp_path)
        with pytest.raises(FileExistsError):
            minting.write_bundle_key("dup", key, keys_dir=tmp_path)

    def test_write_rejects_wrong_length(self, tmp_path: Path):
        with pytest.raises(ValueError):
            minting.write_bundle_key("x", b"too short", keys_dir=tmp_path)

    def test_read_missing_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            minting.read_bundle_key("nope", keys_dir=tmp_path)


# ---------------------------------------------------------------------------
# Metadata builder
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_metadata_shape(self):
        m = minting.build_token_metadata(
            bundle_hash_hex="abcd" * 16,
            signer_pubkey_b58="SignerPubkey",
            memories_count=12,
            arweave_ciphertext_uri="ar://abc",
            signed_at="2026-05-12T19:00:00+00:00",
            cluster="devnet",
        )
        assert m["name"].startswith("BRNDB #")
        assert m["symbol"] == "BRNDB"
        attrs = {a["trait_type"]: a["value"] for a in m["attributes"]}
        assert attrs["bundle_hash"] == "abcd" * 16
        assert attrs["signer"] == "SignerPubkey"
        assert attrs["memories"] == 12
        assert attrs["cluster"] == "devnet"
        assert attrs["encryption"] == "AES-256-GCM"
        assert attrs["schema"] == "brnctl/mint/v1"


# ---------------------------------------------------------------------------
# Pipeline with mocked Node helper + Arweave upload
# ---------------------------------------------------------------------------

class TestMintPipeline:
    def _signed_bundle(self):
        # Minimal fixture matching the signing.sign_bundle output shape.
        return {
            "version": 1,
            "bundle": {
                "version": 1,
                "generated_at": "2026-05-12T19:00:00+00:00",
                "filter_used": {},
                "memories": [
                    {"id": 1, "content": "fact one", "category": "decision"},
                    {"id": 2, "content": "fact two", "category": "lesson"},
                ],
            },
            "bundle_hash_hex": "11" * 32,
            "signature_b58": "Sig1",
            "signer_pubkey_b58": "Owner1",
            "signed_at": "2026-05-12T19:00:00+00:00",
        }

    def test_mock_pipeline_happy_path(self, tmp_path: Path):
        captured = {}

        def fake_upload(**kw):
            captured["upload"] = kw
            return {
                "ok": True,
                "ciphertext_uri": "ar://CT-OK",
                "metadata_uri": "ar://META-OK",
                "error": None,
            }

        def fake_runner(req):
            captured["mint_request"] = req
            return {
                "ok": True,
                "mint": "MintAddr123",
                "tx_signature": "TxSig123",
                "supply_tx_signature": "SupplyTx123",
                "cluster": req["cluster"],
            }

        result = minting.mint_bundle(
            self._signed_bundle(),
            owner_pubkey_b58="Owner1",
            cluster="devnet",
            arweave_uploader="mock",
            keys_dir=tmp_path,
            upload_fn=fake_upload,
            helper_runner=fake_runner,
        )

        assert result["ok"] is True
        assert result["mint"] == "MintAddr123"
        assert result["tx_signature"] == "TxSig123"
        assert result["arweave_ciphertext_uri"] == "ar://CT-OK"
        assert result["arweave_metadata_uri"] == "ar://META-OK"
        assert result["cluster"] == "devnet"
        assert Path(result["key_path"]).exists()

        # Key was persisted with correct length.
        recovered = minting.read_bundle_key("MintAddr123", keys_dir=tmp_path)
        assert len(recovered) == 32

        # The mint request carried the metadata URI from the upload step.
        assert captured["mint_request"]["metadata_uri"] == "ar://META-OK"
        assert captured["mint_request"]["owner_pubkey"] == "Owner1"

    def test_rejects_unknown_cluster(self, tmp_path: Path):
        result = minting.mint_bundle(
            self._signed_bundle(),
            owner_pubkey_b58="Owner1",
            cluster="testnet",  # unsupported
            keys_dir=tmp_path,
            upload_fn=lambda **_kw: {"ok": True, "ciphertext_uri": "x",
                                     "metadata_uri": "y", "error": None},
            helper_runner=lambda _req: {"ok": True, "mint": "M", "tx_signature": "T"},
        )
        assert result["ok"] is False
        assert "unsupported cluster" in result["error"]

    def test_oversized_bundle_refuses_upload(self, tmp_path: Path):
        # Build a signed bundle whose canonical JSON exceeds the cap.
        big_content = "x" * (minting.MAX_CIPHERTEXT_BYTES + 1024)
        signed = self._signed_bundle()
        signed["bundle"]["memories"] = [
            {"id": 1, "content": big_content, "category": "decision"}
        ]
        result = minting.mint_bundle(
            signed,
            owner_pubkey_b58="Owner1",
            cluster="devnet",
            arweave_uploader="mock",
            keys_dir=tmp_path,
            upload_fn=lambda **_kw: {"ok": True, "ciphertext_uri": "x",
                                     "metadata_uri": "y", "error": None},
            helper_runner=lambda _req: {"ok": True, "mint": "M", "tx_signature": "T"},
        )
        assert result["ok"] is False
        assert "exceeds" in result["error"]

    def test_upload_failure_surfaces(self, tmp_path: Path):
        result = minting.mint_bundle(
            self._signed_bundle(),
            owner_pubkey_b58="Owner1",
            cluster="devnet",
            arweave_uploader="mock",
            keys_dir=tmp_path,
            upload_fn=lambda **_kw: {"ok": False, "error": "irys 503"},
            helper_runner=lambda _req: {"ok": True, "mint": "M", "tx_signature": "T"},
        )
        assert result["ok"] is False
        assert "irys 503" in result["error"]

    def test_helper_failure_preserves_arweave_uris(self, tmp_path: Path):
        # If the mint helper fails after Arweave upload succeeds, we
        # should still surface the URIs so the user can debug / retry
        # the mint without re-uploading.
        result = minting.mint_bundle(
            self._signed_bundle(),
            owner_pubkey_b58="Owner1",
            cluster="devnet",
            arweave_uploader="mock",
            keys_dir=tmp_path,
            upload_fn=lambda **_kw: {"ok": True, "ciphertext_uri": "ar://C",
                                     "metadata_uri": "ar://M", "error": None},
            helper_runner=lambda _req: {"ok": False, "error": "rpc 429"},
        )
        assert result["ok"] is False
        assert result["arweave_ciphertext_uri"] == "ar://C"
        assert result["arweave_metadata_uri"] == "ar://M"
        assert "rpc 429" in result["error"]
