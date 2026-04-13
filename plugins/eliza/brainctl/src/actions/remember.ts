import type { BrainctlService } from "../service.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type HandlerCallback = any;

export function createRememberAction(
  getService: (runtime: AnyRuntime) => BrainctlService,
) {
  return {
    name: "BRAINCTL_REMEMBER",
    similes: ["REMEMBER", "STORE_MEMORY", "SAVE_FACT", "LEARN"],
    description:
      "Store a durable fact in the brainctl long-term memory store. Use when the user shares a preference, a fact about themselves or their project, a decision, or any piece of information worth remembering across sessions.",

    async validate(_runtime: AnyRuntime, _message: AnyMemory): Promise<boolean> {
      return true;
    },

    async handler(
      runtime: AnyRuntime,
      message: AnyMemory,
      _state: AnyState,
      options: { content?: string; category?: string; tags?: string[] } = {},
      callback?: HandlerCallback,
    ): Promise<boolean> {
      const svc = getService(runtime);
      const content =
        options.content ??
        (message?.content?.text as string | undefined) ??
        "";
      if (!content || content.trim().length === 0) return false;

      try {
        const result = await svc.remember(content, {
          category: options.category ?? "conversation",
          tags: options.tags,
        });
        callback?.({
          text: `Remembered (id=${result?.id ?? "?"}): ${content}`,
          action: "BRAINCTL_REMEMBER",
        });
        return true;
      } catch (err) {
        callback?.({
          text: `Failed to remember: ${(err as Error).message}`,
          action: "BRAINCTL_REMEMBER",
        });
        return false;
      }
    },

    examples: [
      [
        {
          user: "{{user1}}",
          content: { text: "I prefer dark mode in all my tools." },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Got it — I'll remember you prefer dark mode.",
            action: "BRAINCTL_REMEMBER",
          },
        },
      ],
      [
        {
          user: "{{user1}}",
          content: { text: "Our API rate limits at 100 requests per 15 seconds." },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Noted — storing the rate limit as an integration fact.",
            action: "BRAINCTL_REMEMBER",
          },
        },
      ],
    ],
  };
}
