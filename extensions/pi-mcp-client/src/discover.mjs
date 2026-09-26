// Discovery only. The caller must have already approved and connected the SDK client.
// No config parsing, credentials, server launch, model exposure, or tool calls here.
const DEFAULT_LIMITS = Object.freeze({ pages: 16, entries: 256, requestMs: 15_000 });

async function listAll(list, key, limits) {
  const entries = [];
  const seenCursors = new Set();
  let cursor;
  for (let page = 0; page < limits.pages; page++) {
    const result = await list(cursor === undefined ? {} : { cursor }, { timeout: limits.requestMs });
    if (!Array.isArray(result[key])) throw new Error(`Invalid MCP ${key} page`);
    entries.push(...result[key]);
    if (entries.length > limits.entries) throw new Error(`MCP ${key} discovery exceeded entry limit`);
    const next = result.nextCursor;
    if (next === undefined) return entries;
    if (typeof next !== 'string' || !next || seenCursors.has(next)) {
      throw new Error(`Invalid or repeated MCP ${key} cursor`);
    }
    seenCursors.add(next);
    cursor = next;
  }
  throw new Error(`MCP ${key} discovery exceeded page limit`);
}

/** An all-or-error, attributed snapshot; a partial catalog is never reported as complete. */
export async function discover(client, limits = DEFAULT_LIMITS) {
  const caps = client.getServerCapabilities();
  const version = client.getServerVersion();
  if (!caps || !version) throw new Error('MCP client is not initialized');
  const tools = caps.tools
    ? await listAll(client.listTools.bind(client), 'tools', limits)
    : [];
  const resources = caps.resources
    ? await listAll(client.listResources.bind(client), 'resources', limits)
    : [];
  const prompts = caps.prompts
    ? await listAll(client.listPrompts.bind(client), 'prompts', limits)
    : [];
  return { server: version, capabilities: caps, instructions: client.getInstructions(),
    tools, resources, prompts };
}
