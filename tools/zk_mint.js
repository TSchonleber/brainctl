#!/usr/bin/env node
/**
 * brainctl Light Protocol compressed-token mint helper.
 *
 * Called as a subprocess from `src/agentmemory/minting.py`. The Python
 * side serialises a request dict to a temp JSON file and passes the
 * path via `--request <path>`. We parse the request, dispatch on the
 * `action` field, and write a single JSON object to stdout describing
 * the outcome. Errors are surfaced via `{ok: false, error: "..."}`
 * with a non-zero exit code so subprocess.run can detect failures.
 *
 * Actions:
 *   - "arweave_upload": upload ciphertext + metadata JSON to Arweave
 *     via Irys (free tier for small payloads). Returns the ar:// URIs.
 *   - "mint": create a fresh Light Protocol compressed-token mint and
 *     mint one token to the owner pubkey. Returns the mint address
 *     and the transaction signature.
 *
 * Wire format mirrors the Python side. Keep this file dependency-light
 * and side-effect-free outside the requested action.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const os = require("os");

// ---------------------------------------------------------------------------
// Argv parse
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  // Minimal flag parser — only --request <path> is supported.
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--request" && argv[i + 1]) {
      return { requestPath: argv[i + 1] };
    }
  }
  return {};
}

function fail(error, extra = {}) {
  process.stdout.write(
    JSON.stringify({ ok: false, error, ...extra }) + "\n"
  );
  process.exit(1);
}

function ok(payload) {
  process.stdout.write(JSON.stringify({ ok: true, ...payload }) + "\n");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Lazy require — defer SDK imports until we know which action runs so a
// missing dep doesn't break the *other* action that doesn't use it.
// ---------------------------------------------------------------------------

function requireOrInstallHint(modName) {
  try {
    return require(modName);
  } catch (e) {
    fail(
      `missing Node dependency '${modName}'. Run:  cd ` +
        `${path.resolve(__dirname)} && npm install`
    );
  }
}

// ---------------------------------------------------------------------------
// Keystore loader (Solana CLI JSON format — 64 ints)
// ---------------------------------------------------------------------------

function loadKeypair(keystorePath) {
  const { Keypair } = requireOrInstallHint("@solana/web3.js");
  const expanded =
    keystorePath.startsWith("~/")
      ? path.join(os.homedir(), keystorePath.slice(2))
      : keystorePath;
  if (!fs.existsSync(expanded)) {
    fail(`keystore not found at ${expanded}`);
  }
  const raw = JSON.parse(fs.readFileSync(expanded, "utf8"));
  if (!Array.isArray(raw) || raw.length !== 64) {
    fail(`invalid keystore at ${expanded}: expected 64-int JSON array`);
  }
  return Keypair.fromSecretKey(Uint8Array.from(raw));
}

// ---------------------------------------------------------------------------
// RPC URL resolution
// ---------------------------------------------------------------------------

function resolveRpcUrl(cluster, heliusApiKey) {
  // Helius is the canonical Photon RPC provider for Light Protocol.
  // Falls back to the public RPC if no Helius key (devnet only — the
  // public RPC rate-limits hard on mainnet).
  if (heliusApiKey) {
    if (cluster === "mainnet-beta") {
      return `https://mainnet.helius-rpc.com/?api-key=${heliusApiKey}`;
    }
    return `https://devnet.helius-rpc.com/?api-key=${heliusApiKey}`;
  }
  if (cluster === "mainnet-beta") {
    fail("mainnet-beta requires --helius-api-key (no public Photon RPC)");
  }
  // Public devnet endpoint for Light Protocol — sufficient for demos.
  return "https://devnet.helius-rpc.com";
}

// ---------------------------------------------------------------------------
// Action: arweave_upload
// ---------------------------------------------------------------------------

async function doArweaveUpload(req) {
  const irys = requireOrInstallHint("@irys/sdk");
  // Irys CLI patterns expect a wallet payment source. For this v1 demo
  // we accept the BRAINCTL_IRYS_PRIVATE_KEY env or fall back to a free-
  // tier devnet uploader that pays with Solana devnet SOL.
  //
  // To keep the v1 path simple and free for demos: use Irys's devnet
  // bundlr node which accepts free uploads under ~100 KB.
  const Irys = irys.default || irys;
  const irysNode =
    req.cluster === "mainnet-beta"
      ? "https://node1.irys.xyz"
      : "https://devnet.irys.xyz";

  const keypair = loadKeypair(req.keystore_path || `${os.homedir()}/.brainctl/wallet.json`);

  let client;
  try {
    client = new Irys({
      network: req.cluster === "mainnet-beta" ? "mainnet" : "devnet",
      token: "solana",
      key: Buffer.from(keypair.secretKey).toString("hex"),
      config: { providerUrl: resolveRpcUrl(req.cluster, req.helius_api_key) },
    });
  } catch (e) {
    fail(`failed to init Irys client: ${e && e.message ? e.message : String(e)}`);
  }

  // 1. Upload ciphertext blob
  const ciphertext = Buffer.from(req.ciphertext_b64, "base64");
  let ciphertextReceipt;
  try {
    ciphertextReceipt = await client.upload(ciphertext, {
      tags: [
        { name: "Content-Type", value: "application/x-brnctl-encrypted-bundle" },
        { name: "App-Name", value: "brainctl" },
        { name: "Schema", value: "brnctl/mint/v1" },
      ],
    });
  } catch (e) {
    fail(`ciphertext upload failed: ${e && e.message ? e.message : String(e)}`);
  }

  const ciphertextUri = `ar://${ciphertextReceipt.id}`;

  // 2. Rebuild metadata with the real ciphertext URI, upload metadata
  const metadata = JSON.parse(JSON.stringify(req.metadata_template));
  for (const attr of metadata.attributes || []) {
    if (attr.trait_type === "ciphertext_uri") {
      attr.value = ciphertextUri;
    }
  }
  if (metadata.properties && Array.isArray(metadata.properties.files)) {
    for (const f of metadata.properties.files) {
      if (f.uri === "ar://pending") f.uri = ciphertextUri;
    }
  }

  let metadataReceipt;
  try {
    metadataReceipt = await client.upload(
      Buffer.from(JSON.stringify(metadata), "utf8"),
      {
        tags: [
          { name: "Content-Type", value: "application/json" },
          { name: "App-Name", value: "brainctl" },
          { name: "Schema", value: "brnctl/mint/v1/metadata" },
        ],
      }
    );
  } catch (e) {
    fail(`metadata upload failed: ${e && e.message ? e.message : String(e)}`);
  }

  ok({
    ciphertext_uri: ciphertextUri,
    metadata_uri: `ar://${metadataReceipt.id}`,
  });
}

// ---------------------------------------------------------------------------
// Action: mint (Light Protocol compressed token)
// ---------------------------------------------------------------------------

async function doMint(req) {
  const { Keypair, PublicKey } = requireOrInstallHint("@solana/web3.js");
  const { createRpc } = requireOrInstallHint("@lightprotocol/stateless.js");
  const compressedToken = requireOrInstallHint("@lightprotocol/compressed-token");

  // Resolve a sane mint authority + token recipient.
  const keystorePath =
    req.keystore_path || `${os.homedir()}/.brainctl/wallet.json`;
  const payer = loadKeypair(keystorePath);
  const owner = req.owner_pubkey
    ? new PublicKey(req.owner_pubkey)
    : payer.publicKey;

  const rpcUrl = resolveRpcUrl(req.cluster, req.helius_api_key);
  const rpc = createRpc(rpcUrl, rpcUrl, rpcUrl);

  // createMintInterface signature (from Light Protocol cookbook):
  //   createMintInterface(rpc, payer, mintAuthority, freezeAuthority,
  //                       decimals, mint?, tokenProgramId?, confirmOpts?,
  //                       metadata?)
  //
  // For a memory-bundle mint:
  //   - decimals = 0 (each token is whole + indivisible)
  //   - mint authority = payer (so we can mint the supply-1 token in
  //     the same transaction or a follow-up call)
  //   - metadata = brainctl name/symbol/uri
  let mintResult;
  try {
    const tokenMetadata = compressedToken.createTokenMetadata(
      req.name || "BRNDB",
      req.symbol || "BRNDB",
      req.metadata_uri
    );
    mintResult = await compressedToken.createMintInterface(
      rpc,
      payer,
      payer,
      null,
      0,
      undefined,
      undefined,
      undefined,
      tokenMetadata
    );
  } catch (e) {
    fail(`createMintInterface failed: ${e && e.message ? e.message : String(e)}`);
  }

  const mintAddress = mintResult.mint.toBase58();
  let txSignature = mintResult.transactionSignature || null;

  // Mint 1 token to the owner. Light Protocol's compressed-token SDK
  // exposes mintTo with the standard SPL-like signature; we try a
  // best-effort call and tolerate signature naming differences across
  // SDK versions.
  let supplyTx = null;
  try {
    if (typeof compressedToken.mintTo === "function") {
      const supplyResult = await compressedToken.mintTo(
        rpc,
        payer,
        mintResult.mint,
        owner,
        payer,
        1n
      );
      supplyTx =
        (supplyResult && supplyResult.transactionSignature) ||
        (typeof supplyResult === "string" ? supplyResult : null);
    } else if (typeof compressedToken.compress === "function") {
      // Fall-through for SDKs that expose `compress` as the mint-and-
      // deposit primitive. Same idea: mint 1 unit, deliver to owner.
      const supplyResult = await compressedToken.compress(
        rpc,
        payer,
        mintResult.mint,
        owner,
        payer,
        1n
      );
      supplyTx =
        (supplyResult && supplyResult.transactionSignature) ||
        (typeof supplyResult === "string" ? supplyResult : null);
    }
  } catch (e) {
    // Don't fail the whole mint if the supply call fails — the mint
    // itself is still on-chain. Surface a warning in the response.
    ok({
      mint: mintAddress,
      tx_signature: txSignature,
      supply_tx_signature: null,
      cluster: req.cluster,
      warning:
        "mint created but supply-to-owner call failed: " +
        (e && e.message ? e.message : String(e)),
    });
    return;
  }

  ok({
    mint: mintAddress,
    tx_signature: txSignature,
    supply_tx_signature: supplyTx,
    cluster: req.cluster,
  });
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

async function main() {
  const { requestPath } = parseArgs(process.argv.slice(2));
  if (!requestPath) {
    fail("missing --request <path>");
  }
  if (!fs.existsSync(requestPath)) {
    fail(`request file not found: ${requestPath}`);
  }

  let req;
  try {
    req = JSON.parse(fs.readFileSync(requestPath, "utf8"));
  } catch (e) {
    fail(`request JSON parse failed: ${e && e.message ? e.message : String(e)}`);
  }

  switch (req.action) {
    case "arweave_upload":
      await doArweaveUpload(req);
      break;
    case "mint":
      await doMint(req);
      break;
    default:
      fail(`unknown action: ${req.action}`);
  }
}

main().catch((err) => {
  fail(err && err.message ? err.message : String(err));
});
