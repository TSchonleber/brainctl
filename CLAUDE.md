# Claude Code — brainctl / agentmemory

## What This Is
Unified agent memory system. SQLite-backed (brain.db) with FTS5, vector embeddings (sqlite-vec + Ollama nomic-embed-text), knowledge graph, affect tracking, belief collapse mechanics, and AGM conflict resolution.

Published as `brainctl` on PyPI (v2.2.1+, current 2.4.10).

## Key Paths
- **DB:** `db/brain.db` (WAL mode, foreign keys ON, 59 user-facing tables, 49 numbered migrations + one unnumbered V2-4 quantum-schema file). The numbered sequence has an intentional gap at 050 — the V2-4 quantum schema (`db/migrations/quantum_schema_migration_sqlite.sql`) occupies that slot without a number because it was applied ad-hoc during the V2-4 rollout and pre-dates the idempotent runner fix in 2.4.8. The runner only picks up files matching `^\d+_.+\.sql$` so the quantum file is a no-op for `brainctl migrate` on fresh installs — apply manually if you need the quantum columns on a new DB. (Audit I28 — 2026-04-19.)
- **CLI:** `bin/brainctl` — main CLI entry
- **MCP server:** canonical entry is `agentmemory.mcp_server:run` (201 tools across `mcp_server.py` + 29 `mcp_tools_*.py` modules). Installed as the `brainctl-mcp` console script via pip. The legacy standalone `bin/brainctl-mcp` only registers a subset and is being phased out.
- **Bench:** `bin/brainctl-bench` — retrieval eval harness (P@k / MRR / nDCG@k regression gate, fixtures under `tests/bench/`)
- **Source:** `src/agentmemory/` — Python package
- **Config:** `config/` — quiet hours, consolidation schedules
- **Agents:** `agents/` — per-agent config (pipeline, engram, etc.)

## Build & Test
```bash
pip install -e .                                      # dev install
brainctl stats                                        # verify DB
brainctl search "test"                                # test search
python3 -m agentmemory.mcp_server --list-tools        # full 199-tool MCP surface
python3 -m tests.bench.run                            # retrieval quality benchmark
python3 -m tests.bench.run --check                    # fail on >2% regression vs baseline
```

## Architecture
- Tables: memories, events, entities, decisions, context, knowledge_edges, affect_log, access_log, agent_state, agent_beliefs
- FTS5 indexes on memories, events, entities
- Vector embeddings via sqlite-vec extension
- Hybrid retrieval: FTS5 + vector via Reciprocal Rank Fusion, routed through a regex intent classifier (`bin/intent_classifier.py`) that normalises 10 intent labels onto 6 rerank profiles inside `cmd_search`
- Retrieval regression-gated by `tests/bench/` (P@1 / P@5 / Recall@5 / MRR / nDCG@5; >2% drop fails CI)
- W(m) worthiness gate on memory writes (surprise scoring + semantic dedup)
- PII recency gate (Proactive Interference Index) on supersedes
- Bayesian alpha/beta tracking on memory recall
- Entities carry a rewriteable `compiled_truth` synthesis, a 3-level `enrichment_tier`, and a first-class `aliases` JSON list (migrations 033–035)
- Knowledge-gap scanner (`brainctl gaps scan`) also detects orphan memories, broken knowledge_edges, and unreferenced entities (migration 036)

## Conventions
- Agent IDs: use descriptive names like `my-agent`, `research-bot`, `code-reviewer`
- Memory categories: convention, decision, environment, identity, integration, lesson, preference, project, user
- Event types: artifact, decision, error, handoff, memory_promoted, memory_retired, observation, result, session_start, session_end, stale_context, task_update, warning
- Entity types: agent, concept, document, event, location, organization, other, person, project, service, tool

## Don't Touch
- Migration files in `db/migrations/` — append-only
- The W(m) gate logic without understanding surprise scoring
- Quiet hours scripts — they're cron-scheduled

## Signed exports (2.3.0+)

`brainctl export --sign` and `brainctl verify` produce / check
portable, signed JSON bundles of memories using the user's Solana
keypair. Local-first by design — memories never leave the machine;
only the SHA-256 hash is ever pinned on-chain (opt-in via
`--pin-onchain`, ~$0.001 per pin). Implementation lives in
`src/agentmemory/signing.py` (offline + on-chain plumbing) and
`src/agentmemory/commands/sign.py` (CLI handlers, parser
registration). Optional dep:

    pip install 'brainctl[signing]'   # pulls solders>=0.21

CLI surface:

    brainctl export --sign --keystore <path> [--filter-agent X]
        [--category Y] [--scope Z] [--created-after T]
        [--created-before T] [--ids 1,2,3] [--pin-onchain]
        [--rpc-url <url>] [-o bundle.json] [--json]

    brainctl verify <bundle.json> [--check-onchain]
        [--rpc-url <url>] [--json]

Exit codes: `0` ok, `1` tamper / missing keystore / IO, `2` unsigned
export attempted or `--check-onchain` found no receipt.

No token gating — anyone with brainctl + a Solana keypair can sign
their own memories (preference memory #1691). Threat model + bundle
format spec + a 30-line "verify without brainctl" recipe live in
`docs/SIGNED_EXPORTS.md`.

## Managed wallet (2.3.2+)

For users who don't already have a Solana wallet (most chat-bot
operators and agent builders), `brainctl wallet new` creates a
non-custodial keypair at `~/.brainctl/wallet.json` (chmod 0600). The
key is generated locally and **never** transmitted, copied, or backed
up by brainctl — the user owns the file. `brainctl export --sign`
auto-discovers the managed wallet when `--keystore` isn't passed
(precedence: `--keystore` > `~/.brainctl/wallet.json` >
`$BRAINCTL_SIGNING_KEY_PATH` > error). Implementation in
`src/agentmemory/commands/wallet.py`. Subcommands: `new` (with
`--force`/`--yes`), `address`, `balance`, `show`, `export <path>`,
`import <path>`, `rm`, `onboard`. Override default location via
`$BRAINCTL_WALLET_PATH` or `--path`.

For AI agents: when a user wants to sign a bundle but doesn't have a
wallet, surface this flow. The MCP tools `wallet_show` and
`wallet_create` let you check / create on the user's behalf — but
`wallet_create` will REFUSE to overwrite an existing keystore unless
explicitly passed `force=true` (destructive — pass only after the
user explicitly consents). Or use `brainctl export --sign
--auto-setup-wallet` which creates a wallet inline and signs in one
shot. `--pin-onchain` with 0 SOL skips cleanly with `exit 0` and
`pin_skipped_reason="zero_balance"` — the offline signature is still
valid; pinning is opportunistic.

### Exporting the wallet to Phantom / Backpack / Solflare / Glow (2.6.3+)

`brainctl wallet export` produces the **Solana CLI 64-int JSON
keystore format**, which standard wallet UIs do NOT accept. For
wallet-UI import use the `export-key` subcommand instead:

```
brainctl wallet export-key             # prints base58 secret to stdout
brainctl wallet export-key -o key.b58  # writes to file, mode 0600
```

The base58 string is exactly what Phantom (`Settings → Add / Connect
Wallet → Import Private Key`), Backpack (`+ → Import Wallet → Private
Key`), Solflare (`Settings → Wallets → + → Import Private Key`), and
Glow accept under their "import private key" flows.

**Important security note for agents**: brainctl wallets are not
BIP39-mnemonic-derived — they're random ed25519 keypairs. There is no
recovery phrase. The base58 secret IS the only backup. Surface this
to the user before running `export-key` so they don't expect a 12/24-
word mnemonic. The CLI prints prominent stderr SAFETY warnings; an
agent should mirror them in chat output and refuse to read the secret
back from CLI output (it should never be logged or echoed).

## Mint — Light Protocol compressed tokens (v1, branch `feat/cnft-mint`, optional `[mint]` extra)

`brainctl export --sign --mint` ships the next layer on top of signed
exports: every bundle becomes a Light Protocol **compressed token**
owned by the user's brainctl wallet, with the bundle's encrypted
content on Arweave and metadata pointing back at it. The chain mediates
ownership; it never sees plaintext. This is the v1 primitive that the
agent memory-marketplace narrative is built on.

Architecture: Python orchestrator
(`src/agentmemory/minting.py`) + Node.js helper
(`tools/zk_mint.js`) shelled out via subprocess. Light Protocol's SDK is
TypeScript-only as of v0.23, so brainctl Python does the encryption +
CLI plumbing and shells to Node for the actual mint via
`@lightprotocol/stateless.js` + `@lightprotocol/compressed-token`,
plus `@irys/sdk` for Arweave.

### Agent flow for users without a setup

When a user wants to mint a memory bundle, walk them through this
exactly. The flow is designed so you can drive it with `Bash` calls
without ever seeing the user's key.

1. **Confirm Node 20+.** `node --version`. If missing, ask the user
   to install from https://nodejs.org/ (or `brew install node`).
2. **One-time helper deps.** `cd $(python3 -c "import agentmemory,os;print(os.path.dirname(os.path.dirname(os.path.dirname(agentmemory.__file__))))")/tools && npm install`. ~80 MB. The directory varies by install style — for `pip install -e .` it's the repo root; for a wheel install it's next to the package.
3. **Wallet.** `brainctl wallet new --yes` if `~/.brainctl/wallet.json` doesn't already exist. Surface the printed `SAFETY:` warning verbatim.
4. **Devnet SOL.** `solana airdrop 1 $(brainctl wallet address) --url devnet`. Required for tx fees; rent is sponsored by Light Protocol.
5. **Helius API key.** If `$HELIUS_API_KEY` isn't set and `~/.brainctl/helius.env` doesn't have a valid key (≥8 chars), tell the user: "Sign up free at https://helius.dev/ and paste the key into `~/.brainctl/helius.env` as a single line: `HELIUS_API_KEY=<value>`. I'll chmod it 0600." Then `chmod 600 ~/.brainctl/helius.env` after they confirm. **Never** ask them to paste the key into the chat — your transcript may be logged.
6. **Mint.** `brainctl export --sign --mint --cluster devnet --json`. Capture the JSON output. The `mint_address`, `arweave_metadata_uri`, and `bundle_key_path` are the artifacts the user keeps.

For mainnet-beta, swap `--cluster mainnet-beta` and ensure the wallet
has a small amount of real SOL (~0.001 SOL covers many mints). The
mint pipeline prints a 3-second stderr warning before the mainnet
mint so a misclick can be aborted.

### Key resolution precedence (`minting.resolve_helius_api_key`)

1. `--helius-api-key <key>` CLI arg
2. `$HELIUS_API_KEY` env var
3. `~/.brainctl/helius.env` file (or `$BRAINCTL_HELIUS_ENV_FILE` override) — dotenv shape, one `HELIUS_API_KEY=<value>` line
4. Returns `None` (treated as not-set if any of the above is shorter than 8 chars, so the Vercel `""` quirk doesn't accidentally satisfy the check)

### Marketplace context: personal mint vs. JIT mint

There are two scenarios where a Light Protocol compressed-token mint
happens, and they're explicitly different:

1. **Personal mint** — `brainctl export --sign --mint`. The user mints
   a cNFT to their own wallet. Useful for personal collections,
   gifting, or proving early ownership. The current 2.5.1 behaviour.
2. **Marketplace JIT mint** — happens during a `brainctl marketplace`
   settlement when a buyer's payment lands. The *seller's* daemon
   mints a fresh cNFT to the *buyer's* wallet as part of the release
   memo path. The seller can sell the same bundle to many buyers; each
   gets their own freshly-minted cNFT.

Don't conflate these. The marketplace listing flow does NOT require
`--mint` first — sellers list signed bundles (proofs), and the mint
happens at settlement. `--mint` is purely a personal-collection
convenience and is unrelated to listing.

### Design invariants (these are load-bearing, do NOT regress)

- **Memory content is always AES-256-GCM encrypted client-side before any pointer touches a public storage layer.** Each bundle gets a fresh 32-byte symmetric key, written to `~/.brainctl/keys/<mint>.key` at mode 0600. Marketplace key-wrapping (sale-time threshold encryption via Lit Protocol) is v1.5 — not this build.
- **Devnet by default.** Mainnet-beta requires explicit `--cluster mainnet-beta` and a Helius key.
- **80 KB ciphertext cap.** Stays inside Irys free tier; refuses upload above that with an actionable error suggesting `--ids` / `--category` / `--created-after` filtering.
- **The chain mediates ownership; never sees plaintext.** Arweave stores ciphertext. The chain stores the compressed-token mint, ownership transfers, and metadata URI.

### Output payload fields

The JSON output of `brainctl export --sign --mint --json` adds these on
top of the standard signed-export fields:

- `minted`: bool — whether the mint succeeded.
- `mint_address`: base58 compressed-token mint address.
- `mint_tx_signature`: Solana tx signature for the mint creation.
- `mint_cluster`: `"devnet"` or `"mainnet-beta"`.
- `arweave_ciphertext_uri`: `ar://<id>` for the encrypted bundle blob.
- `arweave_metadata_uri`: `ar://<id>` for the token metadata JSON.
- `bundle_key_path`: filesystem path to the per-bundle AES key.

A failed mint after a successful Arweave upload still surfaces the
`arweave_*` URIs so the user can retry the mint without re-uploading.

### Decrypting a minted bundle locally

After minting, the user can decrypt their own bundle without going
through the marketplace flow:

```bash
brainctl bundle decrypt <mint> --ciphertext-uri ar://<id> [-o out.json]
```

Reads `~/.brainctl/keys/<mint>.key`, fetches ciphertext from Arweave,
AES-256-GCM decrypts. Streams the bundle JSON to stdout if `-o` isn't
passed.

For transferring decryption capability to a recipient (gifted cNFT,
off-marketplace sale), the `send-key` / `receive-key` commands are
roadmap (v2.6.1) — currently the marketplace settle flow is the only
turnkey way to hand a bundle key to another wallet.

## Importers — onboarding from other providers (v2.6.0)

`brainctl import <provider> <source>` brings a third-party memory
export into brain.db. Imported memories land in a quarantine scope
``imported:<provider>`` by default so the agent's primary scope stays
clean until the user explicitly promotes specific records.

Shipped today:
  - ``brainctl import mem0 <export.json>`` — parses every mem0 export
    shape I've seen (SDK ``{"results": [...]}``, ``{"memories": [...]}``,
    legacy top-level list). Each mem0 ``memory`` becomes a brainctl
    memory under category ``user`` (overridable via ``--category``).
    Provider extras (score, app_id, run_id, etc.) round-trip via the
    memory's source_metadata.
  - ``brainctl import json <records.json>`` — generic JSON ingest.
    Accepts ``.json`` (list or ``{"memories":[...]}``) and ``.jsonl``.
    Schema: ``{"content": str, "category": str, "tags": [str],
    "confidence": float, "source_id": str, "created_at": iso,
    "agent_id": str, "metadata": {...}}`` — only ``content`` is
    required.

Common flags:
  - ``--scope <scope>`` — override the destination scope
  - ``--category <cat>`` — override category for every record
  - ``--no-quarantine`` — write to global scope (skip the quarantine)
  - ``--dry-run`` — parse + summarize without touching brain.db

Adding a new provider:
  1. Drop a new module in ``src/agentmemory/importers/`` that
     subclasses ``BaseImporter`` (see ``mem0_importer.py``).
  2. Call ``register_importer("<provider>", YourImporter)`` at module
     load.
  3. Add an autoload import in ``importers/base.py::_autoload``.
  4. Write round-trip tests in ``tests/test_importers.py``.

## Marketplace — agent memory trading (v1.5, branch `feat/cnft-mint`, optional `[marketplace]` extra)

`brainctl marketplace api ...` drives the chain-canonical agent memory
marketplace at brainctl.org/marketplace. The marketplace API is
backend-less — chain-canonical state lives entirely in Solana memos +
Arweave manifests. Anyone can run their own indexer.

### Quick reference (agent-callable)

```bash
# Authenticate (challenge-response signed by your wallet)
brainctl marketplace api login

# Discover + inspect
brainctl marketplace api browse --max-price-usd 50 --category facts
brainctl marketplace api show <listing_id>
brainctl marketplace api offers <listing_id>             # visible offers

# Negotiation (every move is a signed memo + Arweave manifest)
brainctl marketplace api offer   <listing_id> --price-usd 12 --message "agent-bot here"
brainctl marketplace api counter <offer_id>   --price-usd 14
brainctl marketplace api accept  <offer_id>              # seller side
brainctl marketplace api reject  <offer_id>              # seller side
brainctl marketplace api withdraw <offer_id>             # offerer side

# Settle (atomic on-chain payment + memo)
brainctl marketplace api settle <listing_id> \
    --offer-id <offer_id> --buyer-x25519-pubkey <X> --submit
brainctl marketplace api status <listing_id> --wait \
    --auto-decrypt --ingest

# Seller side
brainctl marketplace api list --bundle bundle.json --price-usd 25 \
    --encrypted-bundle-uri ar://... --metadata-uri ar://...
brainctl marketplace api listen     # daemon: JIT-mint + release bundle key
```

### Architectural invariants

- **Wallet = identity.** Every API call carries a session token bound
  to the wallet that signed the auth challenge. The on-chain memo of
  any negotiation step is verified to be signed by the same pubkey the
  manifest claims as `buyer_pubkey` / `from_pubkey` / `seller_pubkey`.
- **Trade proofs, mint on settlement.** Sellers list signed bundles
  (the `bundle_hash` lives on chain via the list memo). The cNFT is
  forged just-in-time by the seller's `listen` daemon on payment
  detection — one fresh mint per buyer.
- **TTL on offers is 24h max.** Auction-mode listings can run up to
  30 days; offers always expire within a day.
- **2.5% protocol fee at settlement.** Atomic with the seller transfer at settle
  time. No off-chain bookkeeping.
- **$10,000 USD price cap.** Listings + offers + counters all enforce.
- **Pre-launch: SOL settlement.** Post-launch the community token
  takes over via a single env-var flip (`BRNDB_MINT`).

### Memo prefix

All marketplace memos use the schema prefix
`brainctl-marketplace/v1:<action>:<args>`. The Python formatters live
in `agentmemory.marketplace` (`format_list_memo`,
`format_offer_memo`, `format_counter_memo`, etc.) and the TypeScript
side mirrors them byte-for-byte in
`brainctl-launch/lib/marketplace/memos.ts`.

### Agent flow for first-time users

1. `pip install 'brainctl[marketplace]'`
2. `brainctl wallet new --yes` if no wallet yet.
3. Fund the wallet with a small amount of SOL.
4. `brainctl marketplace api login` — opens a wallet-signature session.
5. Pick a flow: `list` to sell, `browse` → `offer` to buy.

The auth session is persisted at `~/.brainctl/marketplace-session.json`
(chmod 0600). `logout` clears it.

## Code-aware ingestion (2.4.5+, optional `[code]` extra)

`brainctl ingest code <path>` walks a source tree and writes file /
function / class entities plus `contains` and `imports` relations into
the existing entity graph. CPU-only via tree-sitter — no LLM, no GPU,
no network at ingest time. SHA256-cached in migration 051 so re-runs
on unchanged trees are metadata-only and finish in well under a
second for a ~100-file package.

Ships three grammars on purpose (python, typescript, go) to keep the
wheel footprint around 4 MB. Adding a language means:

 1. Add grammar to `[code]` extra in `pyproject.toml`.
 2. Add suffix(es) to `EXT_TO_LANG` in `src/agentmemory/code_ingest.py`.
 3. Write `extract_<lang>(path, src, relpath)` and register in `EXTRACTORS`.

CLI surface:

    pip install 'brainctl[code]'
    brainctl ingest code <path> [--scope project:<name>]
        [--languages python,typescript,go] [--no-cache]
        [--max-files N] [--verbose] [--json]

Entity naming is prefixed so searches stay unambiguous: `file:<relpath>`,
`fn:<relpath>:<qualname>`, `class:<relpath>:<qualname>`,
`module:<import_spec>`. The fine-grained kind lives in
`properties.kind` alongside `language`, `path`, `line`, `signature`,
`parent`. Provenance is encoded on `knowledge_edges.weight` — 1.0 for
direct-source (`contains`, local resolvable imports), 0.7 for
unresolved external imports. Re-ingest does **not** touch
`last_reinforced_at` / `co_activation_count`: those are synaptic
reinforcement signals owned by hippocampus, and re-parsing a file is
an idempotent state-sync, not an activation event.

Inspired by `github.com/safishamsi/graphify` (the `{nodes, edges}`
extractor protocol + SHA256 skip-when-unchanged pattern).

Known follow-ups (not blocking the extra):

 * No `mcp__brainctl__ingest_code` tool yet — agents that want to
   trigger ingest must shell out via the CLI. MCP wrapper is a
   separate PR.
 * `init_schema.sql` won't include `code_ingest_cache` until it's
   regenerated in a release commit. Until then, fresh installs that
   want code-ingest need `brainctl migrate` applied after
   `brainctl init`.
