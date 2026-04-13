import type { BrainctlService } from "../service.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRuntime = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMemory = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyState = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type HandlerCallback = any;

export function createSearchAction(
  getService: (runtime: AnyRuntime) => BrainctlService,
) {
  return {
    name: "BRAINCTL_SEARCH",
    similes: ["SEARCH_MEMORY", "RECALL", "LOOK_UP", "FIND_FACT"],
    description:
      "Search the brainctl long-term memory store for facts relevant to a query. Use when the user asks what you know about a topic, person, project, or past decision.",

    async validate(_runtime: AnyRuntime, _message: AnyMemory): Promise<boolean> {
      return true;
    },

    async handler(
      runtime: AnyRuntime,
      message: AnyMemory,
      _state: AnyState,
      options: { query?: string; limit?: number; method?: "search" | "vsearch" | "think" } = {},
      callback?: HandlerCallback,
    ): Promise<boolean> {
      const svc = getService(runtime);
      const query =
        options.query ??
        (message?.content?.text as string | undefined) ??
        "";
      if (!query || query.trim().length === 0) return false;

      const method = options.method ?? svc.config.recallMethod;
      const limit = options.limit ?? svc.config.recallLimit;

      try {
        const result =
          method === "vsearch"
            ? await svc.vsearch(query, limit)
            : method === "think"
              ? await svc.think(query, 2, limit)
              : await svc.search(query, limit);
        const hits = result?.results ?? [];
        const text =
          hits.length === 0
            ? `No memories matched "${query}".`
            : `Found ${hits.length} memor${hits.length === 1 ? "y" : "ies"}:\n` +
              hits
                .map((h, i) => `${i + 1}. ${h.category ? `[${h.category}] ` : ""}${h.content}`)
                .join("\n");
        callback?.({ text, action: "BRAINCTL_SEARCH" });
        return true;
      } catch (err) {
        callback?.({
          text: `Search failed: ${(err as Error).message}`,
          action: "BRAINCTL_SEARCH",
        });
        return false;
      }
    },

    examples: [
      [
        {
          user: "{{user1}}",
          content: { text: "What do you remember about the api-v2 project?" },
        },
        {
          user: "{{agent}}",
          content: {
            text: "Searching long-term memory for api-v2...",
            action: "BRAINCTL_SEARCH",
          },
        },
      ],
    ],
  };
}
