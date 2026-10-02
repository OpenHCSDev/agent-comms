/** On-demand hosted web search for Pi, including agent-comms managed turns. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { formatSearchResult, searchWeb } from "./client.mjs";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description: "Search the live web using OpenAI or OpenRouter and return a concise answer with clickable source URLs. Use for current facts, documentation, and research. OpenRouter supports native OpenAI search with model openai/gpt-4.1-mini and engine native, or Exa with any model. The openai provider requires a direct OpenAI API credential.",
    parameters: Type.Object({
      query: Type.String({ description: "The specific web search question", minLength: 1 }),
      provider: Type.Optional(Type.Union([Type.Literal("openai"), Type.Literal("openrouter")])),
      model: Type.Optional(Type.String({ description: "Search model override; otherwise uses the current provider's model or gpt-4.1-mini" })),
      engine: Type.Optional(Type.Union([Type.Literal("auto"), Type.Literal("native"), Type.Literal("exa")])),
      max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 10, default: 5 })),
    }),
    async execute(_toolCallId, params, signal, onUpdate, ctx) {
      const provider = params.provider ?? (ctx.model?.provider === "openai" ? "openai" : "openrouter");
      const auth = (await ctx.modelRegistry.getProviderAuth(provider))?.auth;
      if (!auth) throw new Error(`No ${provider} authentication configured. Use /login ${provider}.`);
      const model = params.model ?? (provider === "openai"
        ? process.env.PI_WEB_SEARCH_OPENAI_MODEL || "gpt-4.1-mini"
        : process.env.PI_WEB_SEARCH_OPENROUTER_MODEL ||
          (ctx.model?.provider === "openrouter" ? ctx.model.id : "openai/gpt-4.1-mini"));
      onUpdate?.({ content: [{ type: "text", text: `Searching via ${provider}: ${params.query}` }], details: {} });
      const result = await searchWeb({
        provider, model, query: params.query, engine: params.engine,
        maxResults: params.max_results, signal,
        apiKey: auth.apiKey, baseUrl: auth.baseUrl, headers: auth.headers,
      });
      return { content: [{ type: "text", text: formatSearchResult(result) }], details: result };
    },
  });
}
