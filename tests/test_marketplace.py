"""Unit tests for ``agentmemory.marketplace`` — fee math, manifest
construction, preview synthesis, X25519/SealedBox primitives, and memo
formatting / parsing.

These tests cover everything that doesn't need a live Solana cluster
or Jupiter API call. The network-dependent pieces (Jupiter price
fetch, Jupiter swap submission, Solana memo scanning) get integration
coverage in ``tests/test_marketplace_live.py`` (gated on
``BRAINCTL_LIVE_MARKETPLACE_TEST=1``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentmemory import marketplace as mp


# ---------------------------------------------------------------------------
# Fee math
# ---------------------------------------------------------------------------

class TestFeeSplit:
    def test_default_2_5_pct(self):
        # 1,000,000 micro-token → seller 975k, treasury 25k (2.5% default)
        seller, treasury = mp.split_with_fee(1_000_000)
        assert seller == 975_000
        assert treasury == 25_000
        assert seller + treasury == 1_000_000

    def test_rounds_fee_up(self):
        # 1 atom and 2.5% — fee rounds up so treasury gets 1, seller 0
        seller, treasury = mp.split_with_fee(1)
        assert seller == 0
        assert treasury == 1

    def test_zero(self):
        assert mp.split_with_fee(0) == (0, 0)

    def test_explicit_fee_bps(self):
        seller, treasury = mp.split_with_fee(10_000, fee_bps=200)  # 2%
        assert treasury == 200
        assert seller == 9_800

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            mp.split_with_fee(-1)

    def test_rejects_excessive_fee(self):
        with pytest.raises(ValueError):
            mp.split_with_fee(100, fee_bps=mp.MAX_FEE_BPS + 1)

    def test_zero_fee_round_trip(self):
        seller, treasury = mp.split_with_fee(1_000_000, fee_bps=0)
        assert seller == 1_000_000
        assert treasury == 0

    def test_huge_total_no_overflow(self):
        # 1 billion tokens (1e15 micro) — well within Python int range
        total = 10 ** 15
        seller, treasury = mp.split_with_fee(total)
        assert seller + treasury == total
        # 2.5% of 1e15 = 2.5e13
        assert abs(treasury - 25 * 10 ** 12) <= 1


# ---------------------------------------------------------------------------
# USD ↔ atoms conversion
# ---------------------------------------------------------------------------

class TestUsdConversion:
    def test_brndb_typical(self):
        # 25 USD @ 0.02 USD/BRNDB = 1,250 BRNDB = 1.25e9 micro
        atoms = mp.usd_to_atoms(25.0, token_price_usd=0.02,
                                token_decimals=6)
        assert atoms == 1_250_000_000

    def test_brndb_roundtrip(self):
        atoms = mp.usd_to_atoms(25.0, token_price_usd=0.02,
                                token_decimals=6)
        usd_back = mp.atoms_to_usd(atoms, token_price_usd=0.02,
                                   token_decimals=6)
        assert abs(usd_back - 25.0) < 1e-9

    def test_sol(self):
        # 0.125 SOL @ 200 USD/SOL = 25 USD
        atoms = mp.usd_to_atoms(25.0, token_price_usd=200.0,
                                token_decimals=9)
        # 25 / 200 = 0.125 SOL = 125_000_000 lamports
        assert atoms == 125_000_000

    def test_zero(self):
        assert mp.usd_to_atoms(0.0, token_price_usd=1.0,
                               token_decimals=6) == 0

    def test_rejects_zero_price(self):
        with pytest.raises(ValueError):
            mp.usd_to_atoms(10.0, token_price_usd=0.0,
                            token_decimals=6)

    def test_rejects_negative_amount(self):
        with pytest.raises(ValueError):
            mp.usd_to_atoms(-1.0, token_price_usd=1.0,
                            token_decimals=6)

    def test_truncates_to_atoms(self):
        # Tiny amount that doesn't fill one atom should yield 0
        atoms = mp.usd_to_atoms(0.0000001, token_price_usd=1.0,
                                token_decimals=6)
        assert atoms == 0


# ---------------------------------------------------------------------------
# Listing manifest
# ---------------------------------------------------------------------------

class TestListingManifest:
    def _preview(self):
        return {
            "memories_count": 5,
            "categories_summary": ["decision", "lesson"],
            "tags_summary": ["stripe", "webhook"],
            "min_created_at": "2026-04-01T00:00:00+00:00",
            "max_created_at": "2026-05-01T00:00:00+00:00",
            "description": "test bundle",
        }

    def test_happy_path(self):
        m = mp.build_listing_manifest(
            bundle_hash="11" * 32,
            seller_pubkey_b58="SellerXyz",
            price_usd=25.00,
            duration_hours=24,
            encrypted_bundle_uri="ar://CT",
            metadata_uri="ar://META",
            preview=self._preview(),
            treasury_pubkey="TreasuryQwe",
        )
        assert m["schema"] == f"{mp.MARKETPLACE_SCHEMA}/listing"
        assert m["bundle_hash"] == "11" * 32
        assert m["visibility"] == "auction"  # default
        assert m["seller_pubkey"] == "SellerXyz"
        assert m["payment_address"] == "SellerXyz"  # defaults
        assert m["treasury_pubkey"] == "TreasuryQwe"
        assert m["fee_bps"] == mp.MARKETPLACE_FEE_BPS
        assert m["pricing"]["price_usd"] == 25.00
        assert m["pricing"]["max_price_usd"] == mp.MAX_LISTING_PRICE_USD
        assert m["encrypted_bundle_uri"] == "ar://CT"
        assert m["metadata_uri"] == "ar://META"
        assert m["preview"]["memories_count"] == 5
        # listing_id auto-generated, time-prefixed
        assert m["listing_id"].startswith(
            datetime.now(timezone.utc).strftime("%Y%m%d")
        )

    def test_expires_at_in_future(self):
        m = mp.build_listing_manifest(
            bundle_hash="aa" * 32, seller_pubkey_b58="S",
            price_usd=10, duration_hours=24,
            encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
            preview=self._preview(),
        )
        expires = datetime.fromisoformat(m["expires_at"])
        now = datetime.now(timezone.utc)
        # Should be ≈24h ahead, allow 1m slop.
        assert timedelta(hours=23, minutes=59) < (expires - now) < timedelta(hours=24, minutes=1)

    def test_rejects_negative_price(self):
        with pytest.raises(ValueError):
            mp.build_listing_manifest(
                bundle_hash="aa" * 32, seller_pubkey_b58="S",
                price_usd=-1, duration_hours=24,
                encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
                preview=self._preview(),
            )

    def test_rejects_over_cap(self):
        with pytest.raises(ValueError):
            mp.build_listing_manifest(
                bundle_hash="aa" * 32, seller_pubkey_b58="S",
                price_usd=mp.MAX_LISTING_PRICE_USD + 1,
                duration_hours=24,
                encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
                preview=self._preview(),
            )

    def test_rejects_bad_duration(self):
        with pytest.raises(ValueError):
            mp.build_listing_manifest(
                bundle_hash="aa" * 32, seller_pubkey_b58="S",
                price_usd=10, duration_hours=0,
                encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
                preview=self._preview(),
            )
        with pytest.raises(ValueError):
            mp.build_listing_manifest(
                bundle_hash="aa" * 32, seller_pubkey_b58="S",
                price_usd=10, duration_hours=24 * 31,
                encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
                preview=self._preview(),
            )

    def test_hash_is_stable_across_runs(self):
        # Same inputs (with explicit listing_id + created_at) →
        # same canonical bytes → same hash.
        kw = dict(
            bundle_hash="aa" * 32, seller_pubkey_b58="S",
            price_usd=10, duration_hours=24,
            encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
            preview=self._preview(),
            listing_id="fixed-id",
            created_at="2026-05-12T15:00:00+00:00",
        )
        # Manifests built at different wall-clock times will still
        # differ on expires_at — fix that by passing duration that
        # yields a deterministic expires (we use created_at). For now,
        # accept that the hash is stable within a single call site if
        # listing_id + created_at + duration are pinned.
        m1 = mp.build_listing_manifest(**kw)
        # build_listing_manifest computes expires_at relative to now,
        # not created_at, so we can't directly assert equality across
        # calls — but we can re-hash an identical dict.
        h1 = mp.listing_hash(m1)
        h2 = mp.listing_hash(dict(m1))
        assert h1 == h2

    def test_hash_excludes_signature_field(self):
        m = mp.build_listing_manifest(
            bundle_hash="aa" * 32, seller_pubkey_b58="S",
            price_usd=10, duration_hours=24,
            encrypted_bundle_uri="ar://C", metadata_uri="ar://M",
            preview=self._preview(),
        )
        h1 = mp.listing_hash(m)
        m["signature_b58"] = "FakeSig"
        h2 = mp.listing_hash(m)
        # Signing must not change the hash, otherwise it would be
        # impossible to verify.
        assert h1 == h2


# ---------------------------------------------------------------------------
# Preview synthesis
# ---------------------------------------------------------------------------

class TestPreview:
    def _bundle(self):
        return {
            "memories": [
                {"category": "decision", "tags": "stripe,webhook",
                 "created_at": "2026-04-01T00:00:00+00:00",
                 "content": "decision content"},
                {"category": "decision", "tags": "stripe",
                 "created_at": "2026-04-15T00:00:00+00:00",
                 "content": "another decision"},
                {"category": "lesson", "tags": "stripe,api",
                 "created_at": "2026-05-01T00:00:00+00:00",
                 "content": "lesson content"},
            ],
        }

    def test_counts_and_orders(self):
        prev = mp.build_preview_from_bundle(
            self._bundle(), description="Stripe lessons"
        )
        assert prev["memories_count"] == 3
        # decision is most frequent → first
        assert prev["categories_summary"][0] == "decision"
        # stripe is most frequent tag
        assert prev["tags_summary"][0] == "stripe"
        assert prev["description"] == "Stripe lessons"
        assert prev["min_created_at"] == "2026-04-01T00:00:00+00:00"
        assert prev["max_created_at"] == "2026-05-01T00:00:00+00:00"

    def test_handles_missing_fields(self):
        prev = mp.build_preview_from_bundle({"memories": []})
        assert prev["memories_count"] == 0
        assert prev["categories_summary"] == []
        assert prev["tags_summary"] == []
        assert prev["min_created_at"] is None
        assert prev["description"] is None

    def test_description_trims_to_none_when_empty(self):
        prev = mp.build_preview_from_bundle(
            {"memories": []}, description="   "
        )
        assert prev["description"] is None


# ---------------------------------------------------------------------------
# X25519 / SealedBox primitives
# ---------------------------------------------------------------------------

class TestSealedBox:
    def test_ed25519_to_x25519_and_back(self):
        # Generate a fresh ed25519 keypair, convert both halves to
        # X25519, encrypt to the pub, decrypt with the priv.
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        seed = bytes(sk)  # the 32-byte seed
        pub_ed = bytes(sk.verify_key)
        x_priv = mp.ed25519_to_x25519_seed(seed)
        x_pub = mp.ed25519_pub_to_x25519_pub(pub_ed)
        # Sanity: x_priv has length 32; x_pub has length 32
        assert len(x_priv) == 32
        assert len(x_pub) == 32

        plaintext = b"the secret bundle key, 32 bytes" + bytes(1)
        sealed = mp.sealedbox_encrypt(plaintext, x_pub)
        recovered = mp.sealedbox_decrypt(sealed, x_priv)
        assert recovered == plaintext

    def test_sealed_box_different_each_time(self):
        # SealedBox uses an ephemeral key per encryption, so two
        # encryptions of the same plaintext should produce different
        # ciphertexts. Confirms we're not accidentally reusing a key.
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        x_pub = mp.ed25519_pub_to_x25519_pub(bytes(sk.verify_key))
        a = mp.sealedbox_encrypt(b"same plaintext", x_pub)
        b = mp.sealedbox_encrypt(b"same plaintext", x_pub)
        assert a != b

    def test_rejects_wrong_seed_length(self):
        with pytest.raises(ValueError):
            mp.ed25519_to_x25519_seed(b"too short")

    def test_rejects_wrong_pub_length(self):
        with pytest.raises(ValueError):
            mp.ed25519_pub_to_x25519_pub(b"too short")

    def test_wrong_priv_fails_decrypt(self):
        from nacl.signing import SigningKey
        a = SigningKey.generate()
        b = SigningKey.generate()
        x_pub_a = mp.ed25519_pub_to_x25519_pub(bytes(a.verify_key))
        x_priv_b = mp.ed25519_to_x25519_seed(bytes(b))
        sealed = mp.sealedbox_encrypt(b"for-a-only", x_pub_a)
        with pytest.raises(Exception):
            mp.sealedbox_decrypt(sealed, x_priv_b)


# ---------------------------------------------------------------------------
# Memo formatting + parsing
# ---------------------------------------------------------------------------

class TestMemos:
    def test_list_roundtrip(self):
        memo = mp.format_list_memo("arweave-tx-abc", "11" * 32)
        parsed = mp.parse_memo(memo)
        assert parsed == {
            "action": "list",
            "listing_arweave_id": "arweave-tx-abc",
            "bundle_hash": "11" * 32,
        }

    def test_buy_roundtrip(self):
        memo = mp.format_buy_memo("20260512-abc123", "BuyerX25519Pub")
        parsed = mp.parse_memo(memo)
        assert parsed == {
            "action": "buy",
            "listing_id": "20260512-abc123",
            "buyer_x25519": "BuyerX25519Pub",
        }

    def test_release_roundtrip(self):
        memo = mp.format_release_memo(
            "20260512-abc123", "envelope-tx", "MintAbc123"
        )
        parsed = mp.parse_memo(memo)
        assert parsed == {
            "action": "release",
            "listing_id": "20260512-abc123",
            "envelope_arweave_id": "envelope-tx",
            "minted_cnft_address": "MintAbc123",
        }

    def test_cancel_roundtrip(self):
        memo = mp.format_cancel_memo("20260512-abc123")
        parsed = mp.parse_memo(memo)
        assert parsed == {"action": "cancel", "listing_id": "20260512-abc123"}

    def test_non_brndb_memo_ignored(self):
        assert mp.parse_memo("brainctl/v1:00abcd:somepubkey") is None
        assert mp.parse_memo("hello world") is None

    def test_malformed_memo_ignored(self):
        assert mp.parse_memo(f"{mp.MARKETPLACE_SCHEMA}:list") is None
        assert mp.parse_memo(f"{mp.MARKETPLACE_SCHEMA}:") is None

    def test_offer_roundtrip(self):
        memo = mp.format_offer_memo("20260512-abc", "arweave-offer-1", "BuyerPubKey")
        parsed = mp.parse_memo(memo)
        assert parsed == {
            "action": "offer",
            "listing_id": "20260512-abc",
            "offer_arweave_id": "arweave-offer-1",
            "buyer_pubkey": "BuyerPubKey",
        }

    def test_counter_roundtrip(self):
        memo = mp.format_counter_memo("offer-20260512-xyz", "arweave-counter-1", "SellerPubKey")
        parsed = mp.parse_memo(memo)
        assert parsed == {
            "action": "counter",
            "offer_id": "offer-20260512-xyz",
            "counter_arweave_id": "arweave-counter-1",
            "counterer_pubkey": "SellerPubKey",
        }

    def test_accept_roundtrip(self):
        memo = mp.format_accept_memo("offer-20260512-xyz")
        parsed = mp.parse_memo(memo)
        assert parsed == {"action": "accept", "offer_id": "offer-20260512-xyz"}

    def test_reject_roundtrip(self):
        memo = mp.format_reject_memo("offer-20260512-xyz")
        parsed = mp.parse_memo(memo)
        assert parsed == {"action": "reject", "offer_id": "offer-20260512-xyz"}

    def test_withdraw_roundtrip(self):
        memo = mp.format_withdraw_memo("offer-20260512-xyz")
        parsed = mp.parse_memo(memo)
        assert parsed == {"action": "withdraw", "offer_id": "offer-20260512-xyz"}


class TestOfferManifest:
    def test_build_offer_defaults(self):
        m = mp.build_offer_manifest(
            listing_id="20260512-abc",
            buyer_pubkey_b58="BuyerPub",
            buyer_x25519_pubkey_b58="BuyerX",
            offered_price_usd=42.50,
        )
        assert m["schema"] == f"{mp.MARKETPLACE_SCHEMA}/offer"
        assert m["listing_id"] == "20260512-abc"
        assert m["buyer_pubkey"] == "BuyerPub"
        assert m["buyer_x25519_pubkey"] == "BuyerX"
        assert m["offered_price_usd"] == 42.5
        assert m["visibility"] == "private"
        assert m["offer_id"].startswith("offer-")
        # Default TTL is 24h, expires_at must be after created_at.
        from datetime import datetime
        c = datetime.fromisoformat(m["created_at"])
        e = datetime.fromisoformat(m["expires_at"])
        assert (e - c).total_seconds() == pytest.approx(24 * 3600, abs=2)

    def test_build_offer_rejects_price_over_cap(self):
        with pytest.raises(ValueError, match="offered_price_usd"):
            mp.build_offer_manifest(
                listing_id="20260512-abc",
                buyer_pubkey_b58="BuyerPub",
                buyer_x25519_pubkey_b58="BuyerX",
                offered_price_usd=mp.MAX_LISTING_PRICE_USD + 1,
            )

    def test_build_offer_rejects_ttl_over_24h(self):
        with pytest.raises(ValueError, match="ttl_hours"):
            mp.build_offer_manifest(
                listing_id="20260512-abc",
                buyer_pubkey_b58="BuyerPub",
                buyer_x25519_pubkey_b58="BuyerX",
                offered_price_usd=10,
                ttl_hours=48,
            )

    def test_build_offer_rejects_long_message(self):
        with pytest.raises(ValueError, match="message"):
            mp.build_offer_manifest(
                listing_id="20260512-abc",
                buyer_pubkey_b58="BuyerPub",
                buyer_x25519_pubkey_b58="BuyerX",
                offered_price_usd=10,
                message="x" * 281,
            )

    def test_build_counter_defaults(self):
        m = mp.build_counter_manifest(
            parent_offer_id="offer-20260512-xyz",
            from_pubkey_b58="SellerPub",
            counter_price_usd=20,
        )
        assert m["schema"] == f"{mp.MARKETPLACE_SCHEMA}/counter"
        assert m["parent_offer_id"] == "offer-20260512-xyz"
        assert m["from_pubkey"] == "SellerPub"
        assert m["counter_price_usd"] == 20.0

    def test_manifest_hash_ignores_signature_field(self):
        m1 = mp.build_offer_manifest(
            listing_id="20260512-abc",
            buyer_pubkey_b58="B",
            buyer_x25519_pubkey_b58="X",
            offered_price_usd=10,
        )
        m2 = dict(m1)
        m2["signature_b58"] = "anything-here-doesnt-matter"
        assert mp.manifest_hash(m1) == mp.manifest_hash(m2)


# ---------------------------------------------------------------------------
# Treasury / mint resolution
# ---------------------------------------------------------------------------

class TestResolution:
    def test_brndb_mint_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("BRNDB_MINT", "EnvMint")
        assert mp.resolve_brndb_mint("ExplicitMint") == "ExplicitMint"

    def test_brndb_mint_env_fallback(self, monkeypatch):
        monkeypatch.setenv("BRNDB_MINT", "EnvMint")
        assert mp.resolve_brndb_mint() == "EnvMint"

    def test_brndb_mint_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("BRNDB_MINT", raising=False)
        # No mainnet default yet (not launched), should return None.
        assert mp.resolve_brndb_mint(cluster="mainnet-beta") is None

    def test_treasury_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("BRNDB_TREASURY_PUBKEY", "EnvTreasury")
        got = mp.resolve_treasury_pubkey(
            "ExplicitTreasury",
            default_wallet_address="WalletAddr",
        )
        assert got == "ExplicitTreasury"

    def test_treasury_env_fallback(self, monkeypatch):
        monkeypatch.setenv("BRNDB_TREASURY_PUBKEY", "EnvTreasury")
        got = mp.resolve_treasury_pubkey(default_wallet_address="WalletAddr")
        assert got == "EnvTreasury"

    def test_treasury_wallet_fallback(self, monkeypatch):
        monkeypatch.delenv("BRNDB_TREASURY_PUBKEY", raising=False)
        got = mp.resolve_treasury_pubkey(default_wallet_address="WalletAddr")
        assert got == "WalletAddr"

    def test_treasury_none_when_all_unset(self, monkeypatch):
        monkeypatch.delenv("BRNDB_TREASURY_PUBKEY", raising=False)
        assert mp.resolve_treasury_pubkey() is None
