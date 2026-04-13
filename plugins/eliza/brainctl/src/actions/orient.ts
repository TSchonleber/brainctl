import type { BrainctlService } from "../service.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type HandlerCallback = any;

export function createOrientAction(
  getService: (runtime: AnyRuntime) => BrainctlService,
) {
  return {
    name: "BRAINCTL_ORIENT",
    similes: ["SESSION_START", "GET_CONTEXT", "CATCH_ME_UP", "ORIENT"],
    description:
      "Pull the full session-start snapshot from brainctl: pending handoff from the last session, recent events, active triggers, and top-of-mind memories. Use at the start of a new session or when the user asks you to catch them up.",

    async validate(_runtime: AnyRuntime, _message: AnyMemory): Promise<boolean> {
      return true;
    },

    async handler(
      runtime: AnyRuntime,
      _message: AnyMemory,
      _state: AnyState,
      options: { project?: string } = {},
      callback?: HandlerCallback,
    ): Promise<boolean> {
      const svc = getService(runtime);
      try {
        const snap = await svc.orient(options.project);
        const parts: string[] = [];
        if (snap?.handoff) {
          parts.push(
            `Handoff: ${snap.handoff.goal ?? "(no goal)"} — next step: ${
              snap.handoff.next_step ?? "(none)"
            }`,
          );
        }
        if (snap?.recent_events && snap.recent_events.length > 0) {
          parts.push(
            `Recent events:\n${snap.recent_events
              .slice(0, 5)
              .map((e) => `  - ${e.summary}`)
              .join("\n")}`,
          );
        }
        if (snap?.triggers && snap.triggers.length > 0) {
          parts.push(
            `Active triggers: ${snap.triggers.map((t) => t.name).join(", ")}`,
          );
        }
        const text =
          parts.length > 0
            ? parts.join("\n\n")
            : "No session context yet — this looks like a fresh start.";
        callback?.({ text, action: "BRAINCTL_ORIENT" });
        return true;
      } catch (err) {
        callback?.({
          text: `Orient failed: ${(err as Error).message}`,
          action: "BRAINCTL_ORIENT",
        });
        return false;
      }
    },

    examples: [
      [
        {
          user: "{{user1}}",
          content: { text: "Catch me up on where we left off." },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Pulling the handoff packet from the last session...",
            action: "BRAINCTL_ORIENT",
          },
        },
      ],
    ],
  };
}
