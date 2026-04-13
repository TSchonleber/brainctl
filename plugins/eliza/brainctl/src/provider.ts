/**
 * brainctlMemoryProvider — injects recalled memories + the orient snapshot
 * into the LLM prompt before each message.
 *
 * Runs on every `get()` call from Eliza's context pipeline. On the first
 * call of a session it also includes the full orient snapshot (pending
 * handoff, active triggers, recent events). Subsequent calls just recall
 * memories relevant to the user's latest message.
 */

import type { BrainctlService } from "./service.js";
import type { OrientSnapshot, RecalledMemory } from "./types.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;

// Session-local flag — on the first call we include the orient snapshot
// as a one-time injection. Keyed by agent runtime id.
const orientedSessions = new WeakSet<object>();

function formatMemories(memories: RecalledMemory[] | undefined): string {
  if (!memories || memories.length === 0) return "";
  const lines = memories.map((m, i) => {
    const cat = m.category ? ` [${m.category}]` : "";
    return `  ${i + 1}.${cat} ${m.content}`;
  });
  return `Relevant memories from long-term store:\n${lines.join("\n")}`;
}

function formatOrient(snap: OrientSnapshot | undefined): string {
  if (!snap) return "";
  const parts: string[] = [];

  if (snap.handoff) {
    const h = snap.handoff;
    const openLoops =
      h.open_loops && h.open_loops.length > 0
        ? `\n  Open loops:\n    - ${h.open_loops.join("\n    - ")}`
        : "";
    parts.push(
      `Session handoff from previous agent:\n  Goal: ${h.goal ?? "—"}\n  State: ${
        h.current_state ?? "—"
      }${openLoops}\n  Next step: ${h.next_step ?? "—"}`,
    );
  }

  if (snap.recent_events && snap.recent_events.length > 0) {
    const ev = snap.recent_events
      .slice(0, 5)
      .map((e) => `  - ${e.summary}`)
      .join("\n");
    parts.push(`Recent events:\n${ev}`);
  }

  if (snap.triggers && snap.triggers.length > 0) {
    const tr = snap.triggers.map((t) => `  - ${t.name}: ${t.action}`).join("\n");
    parts.push(`Active triggers:\n${tr}`);
  }

  return parts.join("\n\n");
}

export function createBrainctlProvider(getService: (runtime: AnyRuntime) => BrainctlService) {
  return {
    name: "brainctl_memory",
    description:
      "Long-term memory recall and session handoff from the brainctl store.",

    async get(runtime: AnyRuntime, message: AnyMemory, _state?: AnyState): Promise<string> {
      const svc = getService(runtime);
      if (!svc) return "";
      if (svc.config.memoryMode === "tools") {
        // Tools-only mode: don't auto-inject anything.
        return "";
      }

      const sections: string[] = [];

      // One-time orient snapshot per session.
      const runtimeKey: object =
        typeof runtime === "object" && runtime !== null ? runtime : {};
      if (svc.config.sessionBookends && !orientedSessions.has(runtimeKey)) {
        orientedSessions.add(runtimeKey);
        try {
          const snap = await svc.orient();
          const formatted = formatOrient(snap);
          if (formatted) sections.push(formatted);
        } catch (err) {
          // brainctl might not be installed or configured; degrade gracefully.
          console.warn("[brainctl] orient failed:", (err as Error).message);
        }
      }

      // Recall memories relevant to the current message.
      const query =
        (message?.content?.text as string | undefined) ??
        (typeof message?.content === "string" ? message.content : "") ??
        "";
      if (query.trim().length > 0) {
        try {
          const recall =
            svc.config.recallMethod === "vsearch"
              ? await svc.vsearch(query)
              : svc.config.recallMethod === "think"
                ? await svc.think(query)
                : await svc.search(query);
          const formatted = formatMemories(recall?.results);
          if (formatted) sections.push(formatted);
        } catch (err) {
          console.warn("[brainctl] recall failed:", (err as Error).message);
        }
      }

      return sections.join("\n\n");
    },
  };
}
