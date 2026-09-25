import { realpath } from 'node:fs/promises';
import { loadEffectiveDeclarations } from './sources.mjs';
import { recordProjectDecision } from './ledger-write.mjs';

const serverId = /^[a-z][a-z0-9_-]{0,31}$/;

/** Render every executable argument, but never literal environment values. */
function approvalDisplay(entry, action, projectRoot) {
  const transport = entry.declaration.transport;
  const info = {
    action, projectRoot, serverId: entry.declaration.id, digest: entry.digest,
    command: transport.command, args: transport.args, cwd: transport.cwd,
    envNames: Object.keys(transport.env).sort(), envFromNames: Object.entries(transport.envFrom).sort(),
  };
  // Escape ANSI, non-ASCII directionality controls, and other control characters.
  const text = JSON.stringify(info, null, 2).replace(/[^\x20-\x7e\n]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
  if (text.length > 4_096) throw new Error('MCP declaration too large for approval display');
  return text;
}

/** Only a real local TUI may prompt: RPC/headless clients have no verified dialog responder yet. */
export async function decideProjectServer(ctx, { agentDir, configDirName, id, decision }) {
  if (ctx.mode !== 'tui') throw new Error('MCP approval requires a local interactive TUI');
  if (!ctx.isProjectTrusted()) throw new Error('Pi project trust is required for MCP approval');
  if (!serverId.test(id) || !['approve', 'deny'].includes(decision)) {
    throw new Error('Invalid MCP approval request');
  }
  const options = { ctx, agentDir, configDirName };
  const eligible = (await loadEffectiveDeclarations(options)).find((entry) =>
    entry.scope === 'project' && entry.declaration.id === id);
  if (!eligible || !eligible.declaration.enabled) throw new Error('Enabled project MCP declaration not found');
  const projectRoot = await realpath(ctx.cwd);
  const prompt = approvalDisplay(eligible, decision, projectRoot);
  if (!await ctx.ui.confirm(`${decision === 'approve' ? 'Approve' : 'Deny'} MCP project server?`, prompt)) {
    return false;
  }
  // A project declaration can change while the user reads the prompt; refuse to
  // commit an approval for bytes that no longer match what the human saw.
  const current = (await loadEffectiveDeclarations(options)).find((entry) =>
    entry.scope === 'project' && entry.declaration.id === id);
  if (!current || current.digest !== eligible.digest || !current.declaration.enabled) {
    throw new Error('MCP declaration changed during approval; try again');
  }
  await recordProjectDecision({ agentDir, projectRoot,
    declaration: current.declaration, decision });
  return true;
}
