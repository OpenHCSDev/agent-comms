function displayCall(serverId, label, params) {
  const body = JSON.stringify({ serverId, operation: label, arguments: params }, null, 2);
  if (!body || body.length > 8_192) throw new Error('MCP operation too large for human review');
  return body.replace(/[^\x20-\x7e\n]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

/** Confirm one operation; re-read current trust/config after confirmation and before SDK I/O. */
export async function confirmedClient(runtime, serverId, label, args, ctx, signal) {
  const before = runtime.ready(serverId);
  const scope = runtime.snapshot().find((item) => item.id === serverId)?.scope;
  if (!before || (scope === 'project' && !ctx.isProjectTrusted())) {
    throw new Error('MCP operation requires an approved server');
  }
  if (signal.aborted) throw new Error('MCP operation cancelled');
  if (!await runtime.authorized(serverId, ctx)) throw new Error('MCP operation no longer authorized');
  if (await runtime.preauthorized(serverId, ctx)) {
    const ready = runtime.ready(serverId);
    if (!ready) throw new Error('MCP operation no longer connected');
    return ready.client;
  }
  if (ctx.mode !== 'tui') throw new Error('MCP operation requires a local human controller');
  if (!await ctx.ui.confirm(`Run MCP ${serverId}/${label}?`, displayCall(serverId, label, args))) {
    throw new Error('MCP operation denied by user');
  }
  if (signal.aborted || !await runtime.authorized(serverId, ctx)) {
    throw new Error('MCP operation no longer authorized');
  }
  const ready = runtime.ready(serverId);
  if (!ready) throw new Error('MCP operation no longer connected');
  return ready.client;
}
