import { CONFIG_DIR_NAME, getAgentDir } from '@earendil-works/pi-coding-agent';
import { decideProjectServer } from './src/commands.mjs';
import { loadEffectiveDeclarations } from './src/sources.mjs';

/** Opt-in, inert Pi control surface. No MCP transport or model tool is registered here. */
export default function (pi) {
  const options = (ctx) => ({ ctx, agentDir: getAgentDir(), configDirName: CONFIG_DIR_NAME });
  pi.registerCommand('mcp-status', {
    description: 'Show inert MCP declaration eligibility (does not start servers)',
    handler: async (_args, ctx) => {
      const entries = await loadEffectiveDeclarations(options(ctx));
      const status = entries.length
        ? entries.map(({ scope, declaration, status: state }) =>
          `${scope}/${declaration.id}: ${state}`).join('\n')
        : 'No MCP declarations';
      ctx.ui.notify(`MCP (no servers connected):\n${status}`, 'info');
    },
  });
  for (const [name, decision] of [['mcp-approve', 'approve'], ['mcp-deny', 'deny']]) {
    pi.registerCommand(name, {
      description: `${decision} an exact project MCP declaration in the local TUI`,
      handler: async (args, ctx) => {
        const confirmed = await decideProjectServer(ctx, { ...options(ctx),
          id: args.trim(), decision });
        ctx.ui.notify(confirmed ? `MCP declaration ${decision === 'deny' ? 'denied' : 'approved'}`
          : 'MCP decision cancelled', 'info');
      },
    });
  }
}
