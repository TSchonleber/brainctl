# brainctl licensing

brainctl is dual-licensed:

| Component | License | Files covered |
|-----------|---------|---------------|
| **Core brainctl** | [MIT](LICENSE) | Everything *except* the marketplace components listed below. |
| **brainctl marketplace** | [Apache 2.0](LICENSE-MARKETPLACE-APACHE-2.0) | The chain-canonical agent memory marketplace primitives. |

## Files under Apache 2.0

Every file in this list carries an SPDX identifier (`SPDX-License-Identifier: Apache-2.0`) at the top:

- `src/agentmemory/marketplace.py` — pricing math, manifest builders, memo formatters, SealedBox helpers
- `src/agentmemory/marketplace_api.py` — Python REST client + session management
- `src/agentmemory/marketplace_buy.py` — buyer-side decrypt + quarantine ingest
- `src/agentmemory/marketplace_listen.py` — seller daemon (JIT mint + release)
- `src/agentmemory/commands/marketplace_cli.py` — argparse handlers for `brainctl marketplace api ...`

Tests under `tests/test_marketplace.py` and `tests/test_marketplace_api.py` exercise this code and inherit Apache 2.0 by association.

## Files explicitly NOT under the Apache 2.0 split

These are MIT (under the top-level `LICENSE`) even though they're adjacent to the marketplace:

- `src/agentmemory/protocol_fees.py` — used by mint, pin-onchain, **and** marketplace; lives in the core surface
- `src/agentmemory/minting.py` and `src/agentmemory/signing.py` — predate the marketplace; reusable for any chain-pinning workflow
- `tools/zk_mint.js` — mixed-use Node helper for both standalone mint and marketplace memo posting

## Why the split

The marketplace is the part of brainctl most likely to be forked, integrated, or extended by other agent platforms — putting it under Apache 2.0 gives downstream operators:

- An explicit patent grant (MIT has none)
- Standardized NOTICE / contribution mechanics
- Compatibility with the licenses of the agent ecosystems we expect to integrate (mem0 / Letta / cognee all use Apache 2.0)

The rest of brainctl stays MIT to keep the lowest-friction integration story for libraries, plugins, and CLI users.

## Attribution

Apache 2.0 requires preserving copyright notices and the LICENSE-MARKETPLACE-APACHE-2.0 file in any redistribution of the marketplace components. The SPDX identifier on each file is the canonical pointer; the full text is at the repo root.

Copyright (c) 2026 Terrence Schonleber.
