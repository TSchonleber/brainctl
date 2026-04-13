/**
 * BrainctlService — manages a long-lived MCP client connection to the
 * brainctl-mcp stdio server subprocess.
 *
 * The service is a singleton attached to the Eliza runtime. Actions and
 * providers retrieve it via `runtime.getService(...)` and call typed
 * wrappers over `callTool(...)`.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import type {
  BrainctlConfig,
  OrientSnapshot,
  RecalledMemory,
} from "./types.js";

// Eliza's Service base is imported lazily so the package can build
// without @elizaos/core present at dev time. Consumers install
// @elizaos/core as a peer dep.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;

const DEFAULT_MCP_PATH = "brainctl-mcp";

export class BrainctlService {
  static readonly serviceType = "brainctl" as const;

  private client: Client | null = null;
  private transport: StdioClientTransport | null = null;
  private initialized = false;
  readonly config: Required<
    Pick<
      BrainctlConfig,
      "mcpPath" | "agentId" | "memoryMode" | "recallMethod" | "recallLimit" | "sessionBookends"
    >
  > & BrainctlConfig;

  constructor(config: BrainctlConfig = {}) {
    this.config = {
      mcpPath: config.mcpPath ?? process.env.BRAINCTL_MCP_PATH ?? DEFAULT_MCP_PATH,
      dbPath: config.dbPath ?? process.env.BRAIN_DB,
      agentId: config.agentId ?? process.env.BRAINCTL_AGENT_ID ?? "eliza",
      project: config.project,
      memoryMode:
        config.memoryMode ??
        (process.env.BRAINCTL_MEMORY_MODE as BrainctlConfig["memoryMode"]) ??
        "hybrid",
      recallMethod:
        config.recallMethod ??
        (process.env.BRAINCTL_RECALL_METHOD as BrainctlConfig["recallMethod"]) ??
        "search",
      recallLimit:
        config.recallLimit ??
        (process.env.BRAINCTL_RECALL_LIMIT
          ? parseInt(process.env.BRAINCTL_RECALL_LIMIT, 10)
          : 8),
      sessionBookends: config.sessionBookends ?? true,
      env: config.env,
    };
  }

  /**
   * Start the brainctl-mcp subprocess and connect the MCP client.
   * Idempotent — safe to call multiple times.
   */
  async initialize(_runtime?: AnyRuntime): Promise<void> {
    if (this.initialized) return;

    const env: Record<string, string> = {
      ...(process.env as Record<string, string>),
      ...(this.config.env ?? {}),
    };
    if (this.config.dbPath) env.BRAIN_DB = this.config.dbPath;
    if (this.config.agentId) env.BRAINCTL_AGENT_ID = this.config.agentId;

    this.transport = new StdioClientTransport({
      command: this.config.mcpPath,
      args: [],
      env,
    });

    this.client = new Client(
      { name: "eliza-brainctl-plugin", version: "0.1.0" },
      { capabilities: {} },
    );

    await this.client.connect(this.transport);
    this.initialized = true;
  }

  async stop(): Promise<void> {
    if (!this.initialized) return;
    try {
      await this.client?.close();
    } catch {
      /* noop */
    }
    this.client = null;
    this.transport = null;
    this.initialized = false;
  }

  /**
   * Call a brainctl MCP tool by name with typed arguments. Callers in
   * `actions/` and `provider.ts` use thin wrappers over this.
   */
  async callTool<T = unknown>(
    name: string,
    args: Record<string, unknown> = {},
  ): Promise<T> {
    if (!this.initialized || !this.client) {
      await this.initialize();
    }
    const result = await this.client!.callTool({ name, arguments: args });
    // MCP returns content as an array of parts; we expect a single JSON
    // text part from brainctl-mcp tools.
    const content = (result.content ?? []) as Array<{ type: string; text?: string }>;
    for (const part of content) {
      if (part.type === "text" && typeof part.text === "string") {
        try {
          return JSON.parse(part.text) as T;
        } catch {
          return part.text as unknown as T;
        }
      }
    }
    return undefined as unknown as T;
  }

  // ---------- High-level convenience wrappers ----------

  remember(
    content: string,
    opts: { category?: string; tags?: string[]; confidence?: number } = {},
  ) {
    return this.callTool<{ id: number }>("memory_add", {
      content,
      category: opts.category,
      tags: opts.tags,
      confidence: opts.confidence,
    });
  }

  search(query: string, limit = this.config.recallLimit) {
    return this.callTool<{ results: RecalledMemory[] }>("memory_search", {
      query,
      limit,
    });
  }

  vsearch(query: string, limit = this.config.recallLimit) {
    return this.callTool<{ results: RecalledMemory[] }>("vsearch", {
      query,
      limit,
    });
  }

  think(query: string, hops = 2, topK = this.config.recallLimit) {
    return this.callTool<{ results: RecalledMemory[] }>("think", {
      query,
      hops,
      top_k: topK,
    });
  }

  /**
   * Compose an orient snapshot client-side. brainctl-mcp does not (yet)
   * expose a single `orient` tool — we call the underlying primitives
   * and assemble a session-start packet the provider can inject.
   *
   * TODO: once brainctl core adds a native `agent_orient` MCP tool,
   * collapse this into a single `callTool("agent_orient", ...)`.
   */
  async orient(project?: string): Promise<OrientSnapshot> {
    const scope = project ?? this.config.project;
    const snapshot: OrientSnapshot = {};

    // Latest pending handoff (if any).
    try {
      const h = await this.callTool<{
        handoff?: {
          goal?: string;
          current_state?: string;
          open_loops?: string;
          next_step?: string;
        };
      }>("handoff_latest", {
        status: "pending",
        project: scope,
      });
      if (h?.handoff) {
        const open =
          typeof h.handoff.open_loops === "string"
            ? h.handoff.open_loops
                .split("\n")
                .map((s) => s.trim())
                .filter(Boolean)
            : [];
        snapshot.handoff = {
          goal: h.handoff.goal,
          current_state: h.handoff.current_state,
          open_loops: open,
          next_step: h.handoff.next_step,
        };
      }
    } catch {
      /* no handoff yet — that's fine for a fresh session */
    }

    // Recent events — uses the generic `search` primitive on events.
    try {
      const events = await this.callTool<{
        results: Array<{ summary: string; event_type?: string; created_at?: string }>;
      }>("event_search", { query: "", limit: 5, project: scope });
      if (events?.results) snapshot.recent_events = events.results;
    } catch {
      /* noop */
    }

    return snapshot;
  }

  /**
   * Persist a session handoff packet. brainctl-mcp's `handoff_add` tool
   * requires goal / current_state / open_loops / next_step. When the
   * caller only has a summary string we synthesize reasonable defaults.
   *
   * TODO: once brainctl core adds a native `agent_wrap_up` MCP tool,
   * collapse this into a single `callTool("agent_wrap_up", ...)`.
   */
  wrapUp(
    summary: string,
    project?: string,
    opts: {
      goal?: string;
      current_state?: string;
      open_loops?: string;
      next_step?: string;
    } = {},
  ) {
    return this.callTool<{ id: number }>("handoff_add", {
      title: summary.slice(0, 80),
      goal: opts.goal ?? summary,
      current_state: opts.current_state ?? summary,
      open_loops: opts.open_loops ?? "",
      next_step: opts.next_step ?? "Resume work in next session.",
      project: project ?? this.config.project,
      scope: "global",
      status: "pending",
    });
  }

  logEvent(
    summary: string,
    opts: {
      event_type?: string;
      project?: string;
      importance?: number;
    } = {},
  ) {
    return this.callTool<{ id: number }>("event_add", {
      summary,
      event_type: opts.event_type ?? "observation",
      project: opts.project ?? this.config.project,
      importance: opts.importance,
    });
  }

  decide(title: string, rationale: string, project?: string) {
    return this.callTool<{ id: number }>("decision_add", {
      title,
      rationale,
      project: project ?? this.config.project,
    });
  }

  /**
   * Create or extend a knowledge-graph entity. Calls `entity_create`
   * (idempotent on name+type) then appends observations via
   * `entity_observe` if any were provided.
   */
  async entity(
    name: string,
    entity_type: string,
    observations: string[] = [],
  ): Promise<{ id: number }> {
    const created = await this.callTool<{ id: number }>("entity_create", {
      name,
      entity_type,
    });
    if (observations.length > 0 && created?.id != null) {
      await this.callTool("entity_observe", {
        entity_id: created.id,
        observations,
      });
    }
    return created;
  }
}
