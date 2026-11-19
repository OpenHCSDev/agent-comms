import { realpath, stat } from 'node:fs/promises';
import { DEFAULT_INHERITED_ENV_VARS } from '@modelcontextprotocol/sdk/client/stdio.js';
import { WorkingDirectory } from './working-directory.mjs';

/** Turn an eligible source snapshot into inert SDK stdio parameters. No process starts here. */
export async function prepareStdioParameters(entry, ctx, hostEnv = process.env) {
  const directory = WorkingDirectory.of(entry.declaration.transport.cwd);
  directory.validateScope(entry.scope);
  if (entry.status !== 'approved' || !directory.trusted(ctx.isProjectTrusted())) {
    throw new Error('MCP launch is not authorized');
  }
  if (entry.scope === 'project' && Object.keys(entry.declaration.transport.env).length) {
    throw new Error('Project MCP literal environment is not supported; use envFrom');
  }
  const projectRoot = await realpath(ctx.cwd);
  if (entry.projectRoot !== projectRoot || !(await stat(projectRoot)).isDirectory()) {
    throw new Error('MCP project context changed before launch');
  }
  const transport = entry.declaration.transport;
  if (transport.type !== 'stdio') {
    throw new Error('Unsupported MCP transport');
  }
  const cwd = await directory.resolve(entry);
  // The SDK itself merges getDefaultEnvironment() at spawn. Mirror that exact
  // documented safe allowlist here so explicit values are never accompanied by
  // the rest of the caller's ambient environment.
  const env = {};
  for (const name of DEFAULT_INHERITED_ENV_VARS) {
    const value = hostEnv[name];
    if (typeof value === 'string' && !value.startsWith('()')) env[name] = value;
  }
  Object.assign(env, transport.env);
  for (const [childName, hostName] of Object.entries(transport.envFrom)) {
    const value = hostEnv[hostName];
    if (typeof value !== 'string') throw new Error(`Missing MCP host environment variable ${hostName}`);
    env[childName] = value;
  }
  return { command: transport.command, args: [...transport.args], cwd,
    env, stderr: 'pipe', maxBufferSize: 2_097_152 };
}
