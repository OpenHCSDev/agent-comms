import { CONFIG_DIR_NAME, getAgentDir } from '@earendil-works/pi-coding-agent';
import { decideProjectServer } from './src/commands.mjs';
import { loadEffectiveDeclarations } from './src/sources.mjs';
import { McpRuntime } from './src/runtime.mjs';
import { registerReadyTools } from './src/tools.mjs';

/** Package-owned Pi session; approved stdio connections start by default. */
export default function (pi) {
  let runtime;
  const options = (ctx) => ({ ctx, agentDir: getAgentDir(), configDirName: CONFIG_DIR_NAME });
  pi.on('session_start', async (_event, ctx) => {
    runtime = new McpRuntime(options(ctx));
    await runtime.start();
    registerReadyTools(pi, runtime);
  });
  pi.on('session_shutdown', async () => {
    await runtime?.stop();
    runtime = undefined;
  });
  pi.registerCommand('mcp-status', {
    description: 'Show inert MCP declaration eligibility (does not start servers)',
    handler: async (_args, ctx) => {
      const entries = runtime?.snapshot() ?? (await loadEffectiveDeclarations(options(ctx)))
        .map(({ scope, declaration, status }) => ({ scope, id: declaration.id, status }));
      const status = entries.length
        ? entries.map(({ scope, id, status: state, server, tools, resources, prompts }) =>
          `${scope}/${id}: ${state}${server ? ` (${server}; ${tools} tools, ${resources} resources, ${prompts} prompts)` : ''}`)
          .join('\n')
        : 'No MCP declarations';
      ctx.ui.notify(`MCP:\n${status}`, 'info');
    },
  });
  for (const [name, decision] of [['mcp-approve', 'approve'], ['mcp-deny', 'deny']]) {
    pi.registerCommand(name, {
      description: `${decision} an exact project MCP declaration in the local TUI`,
      handler: async (args, ctx) => {
        const confirmed = await decideProjectServer(ctx, { ...options(ctx),
          id: args.trim(), decision });
        ctx.ui.notify(confirmed ? `MCP declaration ${decision === 'deny' ? 'denied' : 'approved'}; restart session to apply`
          : 'MCP decision cancelled', 'info');
      },
    });
  }
}
