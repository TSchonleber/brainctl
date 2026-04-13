import type { BrainctlService } from "../service.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type HandlerCallback = any;

export function createWrapUpAction(
  getService: (runtime: AnyRuntime) => BrainctlService,
) {
  return {
    name: "BRAINCTL_WRAP_UP",
    similes: ["WRAP_UP", "END_SESSION", "HANDOFF", "SAVE_SESSION"],
    description:
      "Persist a session handoff packet via brainctl. Summarizes what was accomplished this session so the next agent or the same agent in a later session can pick up exactly where things left off. Use at the end of a session or when the user says goodbye.",

    async validate(_runtime: AnyRuntime, _message: AnyMemory): Promise<boolean> {
      return true;
    },

    async handler(
      runtime: AnyRuntime,
      message: AnyMemory,
      _state: AnyState,
      options: { summary?: string; project?: string } = {},
      callback?: HandlerCallback,
    ): Promise<boolean> {
      const svc = getService(runtime);
      const summary =
        options.summary ??
        (message?.content?.text as string | undefined) ??
        "Session ended.";
      try {
        await svc.wrapUp(summary, options.project);
        callback?.({
          text: `Session wrapped. Handoff packet saved: ${summary}`,
          action: "BRAINCTL_WRAP_UP",
        });
        return true;
      } catch (err) {
        callback?.({
          text: `Wrap-up failed: ${(err as Error).message}`,
          action: "BRAINCTL_WRAP_UP",
        });
        return false;
      }
    },

    examples: [
      [
        {
          user: "{{user1}}",
          content: { text: "That's it for tonight — we finished the auth module." },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Saving the handoff so we can pick up here next session.",
            action: "BRAINCTL_WRAP_UP",
          },
        },
      ],
    ],
  };
}
