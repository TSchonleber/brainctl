# brainctl Node.js helpers

This directory holds the small Node.js helpers that brainctl Python
shells out to for things with no Python SDK (today: Light Protocol
compressed-token minting and Irys/Arweave uploads).

## Setup (one-time)

```bash
cd tools
npm install
```

This pulls:
- `@solana/web3.js` — Solana keypair + transaction utilities
- `@lightprotocol/stateless.js` — Light Protocol RPC layer
- `@lightprotocol/compressed-token` — the compressed-token program SDK
- `@irys/sdk` — Arweave upload via Irys (free tier for small payloads)

Total install footprint is ~80 MB on disk. The helper is only invoked
when you run `brainctl export --mint`; if you don't use the mint
feature, you can skip the install entirely.

## How it's called

`src/agentmemory/minting.py` writes a JSON request file to a temp
path and invokes:

```bash
node tools/zk_mint.js --request /tmp/brnctl-mint-xxxx.json
```

The helper reads the request, performs the requested action (`mint`
or `arweave_upload`), and writes a single JSON object to stdout. Exit
code is 0 on success, non-zero on any failure with `{ok: false,
error: "..."}` on stdout.

## Manual smoke test

You can hand-run a mint without going through brainctl Python:

```bash
cat > /tmp/smoke.json <<EOF
{
  "action": "mint",
  "cluster": "devnet",
  "owner_pubkey": "<your wallet pubkey>",
  "keystore_path": "~/.brainctl/wallet.json",
  "helius_api_key": null,
  "metadata_uri": "ar://test-uri",
  "name": "BRNDB smoke",
  "symbol": "BRNDB"
}
EOF
node tools/zk_mint.js --request /tmp/smoke.json
```

Output on success looks like:

```json
{
  "ok": true,
  "mint": "...",
  "tx_signature": "...",
  "supply_tx_signature": "...",
  "cluster": "devnet"
}
```

## Devnet prerequisite: SOL

Even though Light Protocol sponsors rent, the helper signs Solana
transactions that need a fee. Airdrop yourself a single devnet SOL
once and you're set for hundreds of mints:

```bash
solana airdrop 1 $(brainctl wallet address) --url devnet
```

For mainnet-beta usage, pass `--helius-api-key` and ensure the wallet
has a small amount of real SOL (~0.001 covers many mints).

## Why not pure Python?

Light Protocol's SDK is TypeScript + Rust only as of v0.23. A clean
Python wrapper is on their roadmap but not shipped. The subprocess
boundary is the cheapest correct adapter — switching to a Rust
binary or eventual native Python is a transparent change behind the
brainctl CLI.
