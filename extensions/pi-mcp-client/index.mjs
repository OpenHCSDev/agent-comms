import { CONFIG_DIR_NAME, getAgentDir } from '@earendil-works/pi-coding-agent';
import { decideCallGrant, decideProjectServer } from './src/commands.mjs';
import { loadEffectiveDeclarations } from './src/sources.mjs';
import { McpRuntime } from './src/runtime.mjs';
import { liveStatusReceipt } from './src/live-status.mjs';
import { registerReadyTools } from './src/tools.mjs';
import { registerResourceTools } from './src/resources.mjs';

/** Package-owned Pi session; approved stdio connections start by default. */
export default function (pi) {
  let runtime;
  const options = (ctx) => ({ ctx, agentDir: getAgentDir(), configDirName: CONFIG_DIR_NAME });
  pi.on('session_start', async (_event, ctx) => {
    runtime = new McpRuntime(options(ctx));
    try {
      await runtime.start();
      registerReadyTools(pi, runtime);
      registerResourceTools(pi, runtime);
    } catch (error) {
      await runtime.stop();
      runtime = undefined;
      throw error;
    }
  });
  pi.on('session_shutdown', async () => {
    await runtime?.stop();
    runtime = undefined;
  });
  pi.on('message_start', (event, ctx) => {
    // Native Pi stamps the exact user input on its own event. Stock Pi (no
    // native input proof), non-RPC clients, and extension-created messages do
    // not produce a live receipt. The status has no trust/decision authority.
    const inputId = event.message?.inputId;
    if (ctx.mode !== 'rpc' || event.message?.role !== 'user' ||
        !/^[a-f0-9]{32}$/.test(inputId)) return;
    const owner = runtime;
    if (!owner) return;
    // Pi sends its authoritative user message after extension event handlers.
    // Defer this fire-and-forget status until that native start can reach RPC.
    setImmediate(() => {
      if (runtime !== owner) return;
      void liveStatusReceipt(owner, ctx, inputId).then((receipt) => {
        if (receipt && runtime === owner) {
          ctx.ui.setStatus('pi-mcp/live-v1', JSON.stringify(receipt));
        }
      }).catch(() => {}); // Missing/stale status remains unknown, never positive.
    });
  });
  pi.registerCommand('mcp-status', {
    description: 'Show MCP connection, discovery and call-approval status',
    handler: async (_args, ctx) => {
      const active = runtime;
      const entries = active?.snapshot() ?? (await loadEffectiveDeclarations(options(ctx)))
        .map(({ scope, declaration, status }) => ({ scope, id: declaration.id, status }));
      const lines = await Promise.all(entries.map(async ({ scope, id, status, server, tools, resources, prompts }) => {
        const valid = !!active && status === 'ready' && await active.authorized(id, ctx);
        const automatic = valid && await active.preauthorized(id, ctx);
        const live = valid && !!active.ready(id);
        const state = status === 'ready' && !live ? 'stale_restart_required' : status;
        const calls = live ? automatic ? 'automatic' : 'confirm' : 'unavailable';
        return `${scope}/${id}: ${state}; calls=${calls}` +
          (server ? ` (${server}; ${tools} tools, ${resources} resources, ${prompts} prompts)` : '');
      }));
      ctx.ui.notify(`MCP:\n${lines.join('\n') || 'No MCP declarations'}`, 'info');
    },
  });
  for (const [name, decision] of [['mcp-allow-calls', 'allow'], ['mcp-confirm-calls', 'ask']]) {
    pi.registerCommand(name, {
      description: `${decision === 'allow' ? 'Preauthorize headless' : 'Require confirmation for'} calls on one exact MCP declaration`,
      handler: async (args, ctx) => {
        let changed;
        try { changed = await decideCallGrant(ctx, { ...options(ctx), id: args.trim(), decision }); }
        finally { await runtime?.refresh(ctx); }
        ctx.ui.notify(changed ? `MCP call policy set to ${decision}` : 'MCP call policy unchanged', 'info');
      },
    });
  }
  for (const [name, decision] of [['mcp-approve', 'approve'], ['mcp-deny', 'deny']]) {
    pi.registerCommand(name, {
      description: `${decision} an exact project MCP declaration in the local TUI`,
      handler: async (args, ctx) => {
        let confirmed;
        try {
          confirmed = await decideProjectServer(ctx, { ...options(ctx), id: args.trim(), decision });
        } finally { await runtime?.refresh(ctx); }
        ctx.ui.notify(confirmed ? decision === 'deny'
          ? 'MCP declaration denied; active connection closed if present'
          : 'MCP declaration approved; restart session to connect'
          : 'MCP decision cancelled', 'info');
      },
    });
  }
}
