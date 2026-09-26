const ID = /^[a-z][a-z0-9_-]{0,31}$/;
const INPUT_ID = /^[a-f0-9]{32}$/;
const STATES = new Set([
  'ready', 'error', 'disabled', 'trust_required', 'unsupported_env',
  'denied', 'stale_restart_required', 'connecting', 'approved',
]);

/** Ephemeral, redacted status for one Pi-native user input. Never a grant or approval. */
export async function liveStatusReceipt(runtime, ctx, inputId) {
  if (!INPUT_ID.test(inputId) || !runtime.isRunning()) return undefined;
  const entries = runtime.snapshot();
  if (entries.length > 32 || entries.some(({ id, scope, status }) =>
    !ID.test(id) || !['user', 'project'].includes(scope) || !STATES.has(status))) {
    return undefined;
  }
  const servers = [];
  for (const entry of entries) {
    const initiallyReady = entry.status === 'ready' && await runtime.authorized(entry.id, ctx);
    const automatic = initiallyReady && await runtime.preauthorized(entry.id, ctx);
    // The second authority check can itself retire a revoked connection.
    const ready = initiallyReady && !!runtime.ready(entry.id);
    const count = (value) => Number.isSafeInteger(value) && value >= 0
      ? Math.min(value, 10_000) : 0;
    servers.push({
      id: entry.id,
      scope: entry.scope,
      state: ready ? 'ready' : entry.status === 'ready' ? 'stale_restart_required' : entry.status,
      calls: ready ? automatic ? 'automatic' : 'confirm' : 'unavailable',
      tools: ready ? count(entry.tools) : 0,
      resources: ready ? count(entry.resources) : 0,
      prompts: ready ? count(entry.prompts) : 0,
    });
  }
  if (!runtime.isRunning()) return undefined;
  return { version: 1, source: 'pi-mcp-client', inputId, state: 'running',
    lifetime: 'turn', servers };
}
