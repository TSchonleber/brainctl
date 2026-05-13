"""Tests for agentmemory.protocol_fees."""
from __future__ import annotations

import pytest

from agentmemory import protocol_fees as pf


class TestResolveTreasury:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_TREASURY, raising=False)
        assert pf.resolve_treasury_pubkey() == pf.DEFAULT_TREASURY_PUBKEY_B58

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_TREASURY, "EnvTreasuryPubkey")
        assert pf.resolve_treasury_pubkey() == "EnvTreasuryPubkey"

    def test_explicit_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_TREASURY, "EnvTreasuryPubkey")
        assert pf.resolve_treasury_pubkey("ExplicitOverride") == "ExplicitOverride"

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_TREASURY, "  PaddedAddr  ")
        assert pf.resolve_treasury_pubkey() == "PaddedAddr"


class TestFeeAmounts:
    def test_op_fee_default(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_OP_FEE, raising=False)
        assert pf.resolve_op_fee_lamports() == pf.OP_FEE_LAMPORTS_DEFAULT
        assert pf.OP_FEE_LAMPORTS_DEFAULT == 500_000  # $0.10 at SOL@$200

    def test_mint_fee_default(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_MINT_FEE, raising=False)
        assert pf.resolve_mint_fee_lamports() == pf.MINT_FEE_LAMPORTS_DEFAULT
        assert pf.MINT_FEE_LAMPORTS_DEFAULT == 2_500_000  # $0.50 at SOL@$200

    def test_op_fee_env_override(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_OP_FEE, "1000000")
        assert pf.resolve_op_fee_lamports() == 1_000_000

    def test_mint_fee_env_override(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_MINT_FEE, "5000000")
        assert pf.resolve_mint_fee_lamports() == 5_000_000

    def test_op_fee_env_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_OP_FEE, "not-a-number")
        assert pf.resolve_op_fee_lamports() == pf.OP_FEE_LAMPORTS_DEFAULT

    def test_op_fee_env_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_OP_FEE, "-1")
        assert pf.resolve_op_fee_lamports() == pf.OP_FEE_LAMPORTS_DEFAULT


class TestFeeLamportsForOp:
    def test_mint_uses_mint_fee(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_MINT_FEE, raising=False)
        monkeypatch.delenv(pf.ENV_OP_FEE, raising=False)
        assert pf.fee_lamports_for_op("mint") == pf.MINT_FEE_LAMPORTS_DEFAULT

    def test_other_ops_use_op_fee(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_MINT_FEE, raising=False)
        monkeypatch.delenv(pf.ENV_OP_FEE, raising=False)
        for op in ("pin", "list", "offer", "counter", "accept", "reject",
                   "withdraw", "cancel"):
            assert pf.fee_lamports_for_op(op) == pf.OP_FEE_LAMPORTS_DEFAULT


class TestChargeFee:
    def test_mainnet_charges(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_DISABLE, raising=False)
        assert pf.charge_fee("mainnet-beta") is True

    def test_devnet_free(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_DISABLE, raising=False)
        assert pf.charge_fee("devnet") is False

    def test_marketplace_jit_skipped(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_DISABLE, raising=False)
        assert pf.charge_fee("mainnet-beta", marketplace_jit=True) is False

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_DISABLE, "1")
        assert pf.charge_fee("mainnet-beta") is False

    @pytest.mark.parametrize("v", ["1", "true", "yes"])
    def test_kill_switch_accepts(self, monkeypatch, v):
        monkeypatch.setenv(pf.ENV_DISABLE, v)
        assert pf.fees_disabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", ""])
    def test_kill_switch_rejects(self, monkeypatch, v):
        monkeypatch.setenv(pf.ENV_DISABLE, v)
        assert pf.fees_disabled() is False


class TestDisclosure:
    def test_devnet_returns_empty(self):
        assert pf.format_fee_disclosure(cluster="devnet", op="mint") == ""

    def test_marketplace_jit_returns_empty(self):
        assert (
            pf.format_fee_disclosure(
                cluster="mainnet-beta", op="mint", marketplace_jit=True
            )
            == ""
        )

    def test_kill_switch_returns_empty(self, monkeypatch):
        monkeypatch.setenv(pf.ENV_DISABLE, "1")
        assert pf.format_fee_disclosure(cluster="mainnet-beta", op="mint") == ""

    def test_mint_includes_sol_amount_and_usd(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_DISABLE, raising=False)
        monkeypatch.delenv(pf.ENV_REF_SOL_USD, raising=False)
        s = pf.format_fee_disclosure(cluster="mainnet-beta", op="mint")
        assert "mint" in s
        assert "SOL" in s
        # Default is 2.5M lamports = 0.0025 SOL; at $200/SOL that's $0.50
        assert "0.002500" in s
        assert "0.50" in s

    def test_op_includes_short_treasury(self, monkeypatch):
        monkeypatch.delenv(pf.ENV_DISABLE, raising=False)
        s = pf.format_fee_disclosure(cluster="mainnet-beta", op="list")
        # 0.0005 SOL → $0.10 at $200
        assert "0.000500" in s
        assert "0.10" in s
        # treasury hint should be shortened
        addr = pf.DEFAULT_TREASURY_PUBKEY_B58
        assert addr[:4] in s
        assert addr[-4:] in s
