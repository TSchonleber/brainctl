/**
 * @brainctl/eliza-plugin — persistent memory for Eliza agents.
 *
 * Wraps the brainctl MCP server as an Eliza plugin. Exposes
 * `remember`, `search`, `orient`, `wrap_up`, `decide`, and `log`
 * as Eliza actions, plus a memory provider that auto-injects
 * recalled context + session handoff packets before each LLM call.
 *
 * ## Usage
 *
 * ```ts
 * import { createBrainctlPlugin } from "@brainctl/eliza-plugin";
 *
 * const agent = new AgentRuntime({
 *   // ...
 *   plugins: [
 *     createBrainctlPlugin({
 *       agentId: "milady-trader",
 *       project: "market-maker",
 *       memoryMode: "hybrid",
 *       recallMethod: "search",
 *     }),
 *   ],
 * });
 * ```
 *
 * ## Prerequisites
 *
 * Install brainctl and its MCP server:
 *
 * ```bash
 * pip install 'brainctl[mcp]'
 * ```
 *
 * The plugin spawns `brainctl-mcp` as a subprocess. Make sure it's on
 * your PATH, or pass `mcpPath` in the config.
 */

import { BrainctlService } from "./service.js";
import { createBrainctlProvider } from "./provider.js";
import { createRememberAction } from "./actions/remember.js";
import { createSearchAction } from "./actions/search.js";
import { createOrientAction } from "./actions/orient.js";
import { createWrapUpAction } from "./actions/wrapUp.js";
import { createDecideAction } from "./actions/decide.js";
import { createLogAction } from "./actions/log.js";
import type { BrainctlConfig } from "./types.js";

export { BrainctlService } from "./service.js";
export { createBrainctlProvider } from "./provider.js";
export type { BrainctlConfig, OrientSnapshot, RecalledMemory } from "./types.js";

/**
 * Build the Eliza plugin object. Pass the result to your
 * `AgentRuntime({ plugins: [...] })` config.
 *
 * The plugin manages a single `BrainctlService` instance that spawns
 * and owns the `brainctl-mcp` subprocess for the lifetime of the agent.
 */
export function createBrainctlPlugin(config: BrainctlConfig = {}) {
  const service = new BrainctlService(config);

  // Eliza retrieves services via runtime. We attach the same instance
  // regardless of runtime so actions can share it.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const getService = (_runtime: any): BrainctlService => service;

  return {
    name: "brainctl",
    description:
      "Persistent memory for Eliza agents — SQLite-backed long-term store with FTS5 + vector recall, knowledge graph, affect tracking, and session handoffs. Powered by the brainctl MCP server.",

    // Eliza's runtime will await this during startup.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    async init(runtime: any): Promise<void> {
      await service.initialize(runtime);
    },

    // Some Eliza versions expect `services: Service[]`.
    services: [service],

    actions: [
      createRememberAction(getService),
      createSearchAction(getService),
      createOrientAction(getService),
      createWrapUpAction(getService),
      createDecideAction(getService),
      createLogAction(getService),
    ],

    providers: [createBrainctlProvider(getService)],

    evaluators: [],
  };
}

export default createBrainctlPlugin;
