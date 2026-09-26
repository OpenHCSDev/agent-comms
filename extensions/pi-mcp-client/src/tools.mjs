import { createHash } from 'node:crypto';
import { Type } from 'typebox';
import { Compile } from 'typebox/compile';
import { confirmedClient } from './operation.mjs';
import { toPiResult } from './tool-result.mjs';

function toolName(serverId, remoteName) {
  const prefix = `mcp_${serverId}_`;
  const suffix = createHash('sha256').update(remoteName).digest('hex').slice(0, 10);
  const slug = remoteName.toLowerCase().replace(/[^a-z0-9_]/g, '_')
    .slice(0, 64 - prefix.length - suffix.length - 1);
  return `${prefix}${slug}_${suffix}`;
}

/** Register the complete bounded discovered catalog, not a silently truncated subset. */
export function registerReadyTools(pi, runtime) {
  const definitions = [];
  for (const entry of runtime.snapshot()) {
    if (entry.status !== 'ready') continue;
    const catalog = runtime.ready(entry.id)?.catalog;
    if (!catalog) continue;
    for (const remote of catalog.tools) {
      if (typeof remote.name !== 'string' || !remote.name || remote.name.length > 128 ||
          !remote.inputSchema || typeof remote.inputSchema !== 'object' ||
          remote.inputSchema.type !== 'object') {
        throw new Error('Invalid discovered MCP tool schema');
      }
      const parameters = Type.Unsafe(remote.inputSchema);
      Compile(parameters); // Fail the whole catalog before registering any partial tool subset.
      definitions.push({ serverId: entry.id, remote, parameters,
        name: toolName(entry.id, remote.name) });
    }
  }
  if (definitions.length > 64 || new Set(definitions.map((item) => item.name)).size !== definitions.length) {
    throw new Error('MCP tool catalog exceeds exposure limit or contains duplicate identities');
  }
  const existing = new Set(pi.getAllTools().map((tool) => tool.name));
  if (definitions.some(({ name }) => existing.has(name))) throw new Error('MCP tool name collision');
  for (const { serverId, remote, name, parameters } of definitions) {
    const label = `MCP ${serverId}/${remote.name}`.replace(/[^\x20-\x7e]/g, '?');
    pi.registerTool({
      name, label,
      description: `${label}: ${String(remote.description ?? '').replace(/[\r\n\t]/g, ' ').slice(0, 512)}`,
      parameters,
      async execute(_toolCallId, params, signal, onUpdate, ctx) {
        const client = await confirmedClient(runtime, serverId, remote.name, params, ctx, signal);
        // One SDK request. Cancellation/timeout never triggers an automatic replay.
        const result = await client.callTool({ name: remote.name, arguments: params }, undefined, {
          signal, timeout: 60_000, resetTimeoutOnProgress: true, maxTotalTimeout: 900_000,
          onprogress(update) {
            if (!signal.aborted && Number.isFinite(update.progress)) {
              const progress = Math.max(0, update.progress);
              const total = Number.isFinite(update.total) ? `/${Math.max(0, update.total)}` : '';
              onUpdate?.({ content: [{ type: 'text', text: `MCP ${serverId}/${remote.name}: ${progress}${total}` }],
                details: { serverId, toolName: remote.name } });
            }
          },
        });
        const mapped = toPiResult(result);
        mapped.details.serverId = serverId;
        mapped.details.toolName = remote.name;
        return mapped;
      },
    });
  }
  return definitions.map(({ name, serverId, remote }) => ({ name, serverId, remoteName: remote.name }));
}
