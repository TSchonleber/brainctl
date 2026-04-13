/**
 * Configuration for the brainctl Eliza plugin.
 *
 * All fields are optional. Sensible defaults apply. Environment variables
 * take precedence over defaults but are overridden by runtime config passed
 * to the service.
 */
export interface BrainctlConfig {
  /**
   * Path to the `brainctl-mcp` executable. Defaults to `brainctl-mcp` on
   * PATH. Override via `BRAINCTL_MCP_PATH` env var.
   */
  mcpPath?: string;

  /**
   * Path to the SQLite brain database file. Defaults to
   * `~/agentmemory/db/brain.db` (brainctl's default). Override via
   * `BRAIN_DB` env var.
   */
  dbPath?: string;

  /**
   * Agent identifier recorded on every write. Used for multi-agent
   * scoping. Defaults to `eliza` or the runtime's character name.
   */
  agentId?: string;

  /**
   * Optional project scope for events, decisions, and handoffs.
   */
  project?: string;

  /**
   * How the plugin surfaces memory to the LLM.
   * - `context`: auto-inject recalled memories into the prompt via the provider only
   * - `tools`: expose actions to the LLM, no auto-injection
   * - `hybrid`: both (recommended)
   */
  memoryMode?: "context" | "tools" | "hybrid";

  /**
   * Recall method used by the context provider.
   * - `search`: FTS5 full-text search (default, fastest, zero deps)
   * - `vsearch`: vector similarity search (requires sqlite-vec + Ollama)
   * - `think`: spreading-activation associative recall
   */
  recallMethod?: "search" | "vsearch" | "think";

  /**
   * Max memories returned per auto-recall. Default 8.
   */
  recallLimit?: number;

  /**
   * Call `brain.orient()` on first turn and `brain.wrap_up()` at session
   * end. Default true.
   */
  sessionBookends?: boolean;

  /**
   * Extra environment variables to pass through to the brainctl-mcp
   * subprocess.
   */
  env?: Record<string, string>;
}

export interface RecalledMemory {
  id?: string | number;
  content: string;
  category?: string;
  tags?: string[];
  score?: number;
  created_at?: string;
}

export interface OrientSnapshot {
  handoff?: {
    goal?: string;
    current_state?: string;
    open_loops?: string[];
    next_step?: string;
  } | null;
  recent_events?: Array<{
    summary: string;
    event_type?: string;
    created_at?: string;
  }>;
  triggers?: Array<{ name: string; action: string }>;
  memories?: RecalledMemory[];
  stats?: Record<string, unknown>;
}
