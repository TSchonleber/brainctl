import type { BrainctlService } from "../service.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type HandlerCallback = any;

export function createLogAction(
  getService: (runtime: AnyRuntime) => BrainctlService,
) {
  return {
    name: "BRAINCTL_LOG",
    similes: ["LOG_EVENT", "RECORD_EVENT", "JOURNAL"],
    description:
      "Log an event to brainctl's event stream. Use for observations, results, warnings, errors, or handoffs — anything that happened and should be recoverable later via `orient()` or search.",

    async validate(_runtime: AnyRuntime, _message: AnyMemory): Promise<boolean> {
      return true;
    },

    async handler(
      runtime: AnyRuntime,
      message: AnyMemory,
      _state: AnyState,
      options: {
        summary?: string;
        event_type?: string;
        project?: string;
        importance?: number;
      } = {},
      callback?: HandlerCallback,
    ): Promise<boolean> {
      const svc = getService(runtime);
      const summary =
        options.summary ??
        (message?.content?.text as string | undefined) ??
        "";
      if (!summary) return false;
      try {
        const result = await svc.logEvent(summary, {
          event_type: options.event_type,
          project: options.project,
          importance: options.importance,
        });
        callback?.({
          text: `Event logged (id=${result?.id ?? "?"}): ${summary}`,
          action: "BRAINCTL_LOG",
        });
        return true;
      } catch (err) {
        callback?.({
          text: `Log failed: ${(err as Error).message}`,
          action: "BRAINCTL_LOG",
        });
        return false;
      }
    },

    examples: [
      [
        {
          user: "{{user1}}",
          content: { text: "Deployed v2.0 to production just now." },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Logging the v2.0 deploy as a result event.",
            action: "BRAINCTL_LOG",
          },
        },
      ],
    ],
  };
}
