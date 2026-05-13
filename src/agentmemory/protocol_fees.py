"""Protocol-fee primitives for brainctl.

Two fee classes:

  1. Flat USD-pegged op fees on every wallet-signed CLI op that touches
     the chain. Funded by a small ``SystemProgram.Transfer`` instruction
     bundled into the same transaction the op already builds (atomic
     with the op). USD-pegged means: hardcoded lamports calibrated to
     a target USD value at a reference SOL price, adjustable via env
     vars when SOL moves significantly. Default reference: SOL ≈ $200.

       - sign --pin-onchain          $0.10  (500_000 lamports)
       - marketplace list             $0.10  (500_000 lamports)
       - marketplace offer            $0.10
       - marketplace counter          $0.10
       - marketplace accept           $0.10
       - marketplace reject           $0.10
       - marketplace withdraw         $0.10
       - marketplace cancel           $0.10
       - mint                         $0.50  (2_500_000 lamports)

  2. Percentage fee on marketplace settlement value, handled in the
     existing marketplace settle path (``MARKETPLACE_FEE_BPS`` in
     ``agentmemory.marketplace``). Set at 200 bps = 2% of the trade
     value, transferred atomically with the seller payment.

Exemptions on flat op fees:

  - cluster == "devnet": free dev/test, never charge fees.
  - marketplace JIT mint at settlement (seller's `listen` daemon
    minting a cNFT for the buyer): seller already paid the 2% at
    settle, so no additional mint fee.
  - buyer's settle transaction itself: the 2% settlement fee already
    covers protocol revenue for that path; no extra op fee.
  - ``BRAINCTL_PROTOCOL_FEE_DISABLE=1``: kill-switch for tests.

Treasury pubkey is hardcoded as ``DEFAULT_TREASURY_PUBKEY_B58`` and
overridable per-call (``explicit`` arg) or via
``BRAINCTL_TREASURY_PUBKEY`` environment variable.
"""
from __future__ import annotations

import os
from typing import Optional

# ---------------------------------------------------------------------------
# Treasury wallet
# ---------------------------------------------------------------------------

# Public protocol treasury wallet. Separate from the dev wallet (which is
# held privately as an anti-sniping hold ahead of token launch). Override
# per-environment via $BRAINCTL_TREASURY_PUBKEY.
DEFAULT_TREASURY_PUBKEY_B58 = "AYyx94RdL4LpBozqZahQ37Q3ziKEoiGZnypp8h9WwW4D"

# ---------------------------------------------------------------------------
# Flat op fees (lamports calibrated to a reference SOL price)
# ---------------------------------------------------------------------------

LAMPORTS_PER_SOL = 1_000_000_000

# Reference SOL price the defaults below were calibrated against. Adjust
# the lamport values (or set env overrides) when SOL moves enough that
# the implied USD value drifts outside the intended band.
REFERENCE_SOL_USD_DEFAULT = 200.0

# $0.10 at SOL ≈ $200 → 0.0005 SOL → 500_000 lamports. Applied to
# --pin-onchain and all marketplace ops EXCEPT settle (which has its
# own % fee) and JIT mint at settle (seller already paid).
OP_FEE_LAMPORTS_DEFAULT = 500_000

# $0.50 at SOL ≈ $200 → 0.0025 SOL → 2_500_000 lamports. Total fee for
# a mint op (this is the entire mint fee, NOT an extra on top of the op
# fee — minting does not double-charge).
MINT_FEE_LAMPORTS_DEFAULT = 2_500_000

# ---------------------------------------------------------------------------
# Env override names
# ---------------------------------------------------------------------------

ENV_TREASURY = "BRAINCTL_TREASURY_PUBKEY"
ENV_OP_FEE = "BRAINCTL_OP_FEE_LAMPORTS"
ENV_MINT_FEE = "BRAINCTL_MINT_FEE_LAMPORTS"
ENV_REF_SOL_USD = "BRAINCTL_REF_SOL_USD"
ENV_DISABLE = "BRAINCTL_PROTOCOL_FEE_DISABLE"


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def fees_disabled() -> bool:
    """`BRAINCTL_PROTOCOL_FEE_DISABLE=1` → skip all flat fees."""
    return os.environ.get(ENV_DISABLE, "").strip() in {"1", "true", "yes"}


def resolve_treasury_pubkey(explicit: Optional[str] = None) -> str:
    """Treasury pubkey. Order: explicit arg → env → hardcoded default."""
    if explicit:
        return explicit.strip()
    env = os.environ.get(ENV_TREASURY, "").strip()
    if env:
        return env
    return DEFAULT_TREASURY_PUBKEY_B58


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        if v < 0:
            return default
        return v
    except ValueError:
        return default


def _parse_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        if v <= 0:
            return default
        return v
    except ValueError:
        return default


def resolve_op_fee_lamports() -> int:
    """Flat per-op fee in lamports (sign/list/offer/etc). Default $0.10."""
    return _parse_int_env(ENV_OP_FEE, OP_FEE_LAMPORTS_DEFAULT)


def resolve_mint_fee_lamports() -> int:
    """Total mint fee in lamports. Default $0.50."""
    return _parse_int_env(ENV_MINT_FEE, MINT_FEE_LAMPORTS_DEFAULT)


def resolve_ref_sol_usd() -> float:
    """Reference SOL/USD used to derive disclosure strings."""
    return _parse_float_env(ENV_REF_SOL_USD, REFERENCE_SOL_USD_DEFAULT)


# ---------------------------------------------------------------------------
# Cluster + op gating
# ---------------------------------------------------------------------------

def charge_fee(cluster: str, *, marketplace_jit: bool = False) -> bool:
    """Should the caller append fee instructions for this op?

    False when the kill-switch is set, when the cluster is devnet
    (free dev/test), or when the call is a marketplace JIT mint (the
    seller's daemon at settlement, which has already paid the 2%).
    """
    if fees_disabled():
        return False
    if cluster != "mainnet-beta":
        return False
    if marketplace_jit:
        return False
    return True


# ---------------------------------------------------------------------------
# Disclosure helpers (for the CLI to print before signing)
# ---------------------------------------------------------------------------

def fee_lamports_for_op(op: str) -> int:
    """Return the flat-fee lamports for a given op name. Mint is the
    only outlier; everything else uses the standard op fee.
    """
    if op == "mint":
        return resolve_mint_fee_lamports()
    return resolve_op_fee_lamports()


def format_fee_disclosure(
    *,
    cluster: str,
    op: str,
    marketplace_jit: bool = False,
    treasury: Optional[str] = None,
) -> str:
    """Human-readable one-line summary of the protocol fee for this op.

    Returns an empty string when no fee will be charged (devnet, JIT,
    kill-switch). Caller is expected to print this to the user before
    they sign anything.
    """
    if not charge_fee(cluster, marketplace_jit=marketplace_jit):
        return ""
    lamports = fee_lamports_for_op(op)
    sol = lamports / LAMPORTS_PER_SOL
    usd_estimate = sol * resolve_ref_sol_usd()
    treasury_full = treasury or resolve_treasury_pubkey()
    treasury_short = (
        f"{treasury_full[:4]}…{treasury_full[-4:]}"
        if len(treasury_full) > 12
        else treasury_full
    )
    return (
        f"brainctl protocol fee on {op}: "
        f"{sol:.6f} SOL (~${usd_estimate:.2f}) → {treasury_short}"
    )


__all__ = [
    "DEFAULT_TREASURY_PUBKEY_B58",
    "OP_FEE_LAMPORTS_DEFAULT",
    "MINT_FEE_LAMPORTS_DEFAULT",
    "REFERENCE_SOL_USD_DEFAULT",
    "LAMPORTS_PER_SOL",
    "ENV_TREASURY",
    "ENV_OP_FEE",
    "ENV_MINT_FEE",
    "ENV_REF_SOL_USD",
    "ENV_DISABLE",
    "fees_disabled",
    "resolve_treasury_pubkey",
    "resolve_op_fee_lamports",
    "resolve_mint_fee_lamports",
    "resolve_ref_sol_usd",
    "charge_fee",
    "fee_lamports_for_op",
    "format_fee_disclosure",
]
