import type { BrainctlService } from "../service.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type HandlerCallback = any;

export function createDecideAction(
  getService: (runtime: AnyRuntime) => BrainctlService,
) {
  return {
    name: "BRAINCTL_DECIDE",
    similes: ["RECORD_DECISION", "LOG_DECISION", "DECIDE"],
    description:
      "Record a decision with its rationale in brainctl. Use when an architectural choice, strategy call, or tradeoff is made — the rationale is what matters, so future agents understand the 'why' and not just the 'what'.",

    async validate(_runtime: AnyRuntime, _message: AnyMemory): Promise<boolean> {
      return true;
    },

    async handler(
      runtime: AnyRuntime,
      _message: AnyMemory,
      _state: AnyState,
      options: { title?: string; rationale?: string; project?: string } = {},
      callback?: HandlerCallback,
    ): Promise<boolean> {
      const svc = getService(runtime);
      if (!options.title || !options.rationale) return false;
      try {
        const result = await svc.decide(options.title, options.rationale, options.project);
        callback?.({
          text: `Decision recorded (id=${result?.id ?? "?"}): ${options.title}`,
          action: "BRAINCTL_DECIDE",
        });
        return true;
      } catch (err) {
        callback?.({
          text: `Decide failed: ${(err as Error).message}`,
          action: "BRAINCTL_DECIDE",
        });
        return false;
      }
    },

    examples: [
      [
        {
          user: "{{user1}}",
          content: { text: "Let's go with JWT for auth, 24-hour expiry." },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Recording: JWT auth, 24h expiry — chosen for security/UX balance.",
            action: "BRAINCTL_DECIDE",
          },
        },
      ],
    ],
  };
}
