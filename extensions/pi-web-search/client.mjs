/** Provider-hosted web search with verifiable URL citations. */

const BASE_URLS = {
  openai: "https://api.openai.com/v1",
  openrouter: "https://openrouter.ai/api/v1",
};

function source(annotation) {
  const item = annotation.url_citation ?? annotation;
  if (typeof item.url !== "string") return null;
  try {
    const url = new URL(item.url);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return { title: String(item.title || url.hostname), url: item.url };
  } catch {
    return null;
  }
}

export function parseSearchResponse(provider, data, maxResults = 5) {
  let text = "";
  const citations = [];
  if (provider === "openai") {
    const pieces = [];
    for (const item of data.output ?? []) {
      if (item.type === "web_search_call") {
        citations.push(...(item.action?.sources ?? []));
      }
      if (item.type !== "message") continue;
      for (const part of item.content ?? []) {
        if (part.type === "output_text") pieces.push(part.text ?? "");
        citations.push(...(part.annotations ?? []));
      }
    }
    text = pieces.join("\n") || data.output_text || "";
  } else {
    const message = data.choices?.[0]?.message ?? {};
    text = typeof message.content === "string"
      ? message.content
      : (message.content ?? []).map(part => part.text ?? "").join("\n");
    citations.push(...(message.annotations ?? []));
  }
  const unique = new Map();
  for (const citation of citations) {
    const value = source(citation);
    if (value && !unique.has(value.url)) unique.set(value.url, value);
  }
  const sources = [...unique.values()].slice(0, maxResults);
  if (!sources.length) {
    throw new Error(`${provider} returned no verifiable web sources; search was not confirmed.`);
  }
  return { text, sources };
}

export async function searchWeb(options, fetchImpl = fetch) {
  const { provider, query, apiKey, signal, model } = options;
  const maxResults = options.maxResults ?? 5;
  const engine = options.engine ?? "auto";
  if (!(provider in BASE_URLS)) throw new Error("Search provider must be openai or openrouter.");
  if (!query?.trim()) throw new Error("A non-empty search query is required.");
  if (!Number.isInteger(maxResults) || maxResults < 1 || maxResults > 10) {
    throw new Error("max_results must be between 1 and 10.");
  }
  if (!["auto", "native", "exa"].includes(engine)) throw new Error("Unknown search engine.");
  if (provider === "openai" && engine === "exa") {
    throw new Error("Exa search is available through OpenRouter; OpenAI uses native search.");
  }
  const headers = new Headers({ "Content-Type": "application/json" });
  if (apiKey) headers.set("Authorization", `Bearer ${apiKey}`);
  for (const [name, value] of Object.entries(options.headers ?? {})) {
    if (value === null) headers.delete(name);
    else headers.set(name, value);
  }
  if (!headers.has("Authorization")) {
    throw new Error(`No ${provider} credential configured. Use Pi's /login ${provider}.`);
  }
  const prompt = `Search the web for: ${query.trim()}\nReturn a concise answer with citations to the sources you actually consulted.`;
  const body = provider === "openai" ? {
    model,
    input: prompt,
    tools: [{ type: "web_search", search_context_size: "low" }],
    tool_choice: { type: "web_search" },
    include: ["web_search_call.action.sources"],
    max_output_tokens: 2048,
    store: false,
  } : {
    model: model.replace(/:online(?=:|$)/g, ""),
    messages: [{ role: "user", content: prompt }],
    tools: [{ type: "openrouter:web_search", parameters: {
      engine, max_results: maxResults, max_total_results: maxResults, max_uses: 2,
    } }],
    max_tool_calls: 2,
    max_tokens: 2048,
    stream: false,
  };
  const base = (options.baseUrl || BASE_URLS[provider]).replace(/\/+$/, "");
  const endpoint = provider === "openai" ? "responses" : "chat/completions";
  const timeout = AbortSignal.timeout(options.timeoutMs ?? 120_000);
  const response = await fetchImpl(`${base}/${endpoint}`, {
    method: "POST", headers, body: JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`${provider} search returned HTTP ${response.status} with an invalid JSON response.`);
  }
  if (!response.ok || data.error) {
    let detail = String(data.error?.message ?? data.message ?? "Request failed");
    if (apiKey) detail = detail.replaceAll(apiKey, "[redacted]");
    throw new Error(`${provider} search HTTP ${response.status}: ${detail.slice(0, 500)}`);
  }
  return { provider, model: body.model, query: query.trim(), ...parseSearchResponse(provider, data, maxResults) };
}

export function formatSearchResult(result) {
  const links = result.sources.map(({ title, url }, index) => {
    const label = title.replace(/[\\[\]]/g, "\\$&");
    const link = url.replace(/\(/g, "%28").replace(/\)/g, "%29");
    return `${index + 1}. [${label}](${link})`;
  });
  return `Web search · ${result.provider} · ${result.model}\n\nSources:\n${links.join("\n")}\n\n${result.text}`;
}
