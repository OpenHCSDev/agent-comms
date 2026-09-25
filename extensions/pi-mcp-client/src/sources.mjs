import { constants } from 'node:fs';
import { open, realpath, lstat } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { effectiveDeclarations, parseTrustLedger } from './authority.mjs';
import { parseNativeConfig } from './config.mjs';

const EMPTY_CONFIG = '{"version":1,"servers":[]}';
const EMPTY_LEDGER = '{"version":1,"decisions":[]}';

/** A durable pre-rename marker means a grant/revocation commit is uncertain. */
export async function readTrustLedger(agentDir) {
  const unsafe = join(agentDir, 'mcp-trust.json.unsafe');
  const check = async () => {
    try {
      await lstat(unsafe);
      throw new Error('MCP decision ledger durability is uncertain');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  };
  await check();
  const ledger = parseTrustLedger(await readOptional(join(agentDir, 'mcp-trust.json')) ?? EMPTY_LEDGER);
  await check();
  // A copied Windows ledger has no locally proven durable revocation policy.
  // User-scope local TUI calls remain possible, but no persisted approvals
  // or headless grants are ever accepted there.
  if (process.platform === 'win32') return { version: 1, decisions: [], callGrants: [] };
  return ledger;
}

export async function readOptional(path) {
  let file;
  try {
    // O_NOFOLLOW prevents treating a project-controlled symlink as a user config.
    // lstat covers platforms where O_NOFOLLOW is not implemented.
    if ((await lstat(path)).isSymbolicLink()) throw new Error('MCP config symlink refused');
    file = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  } catch (error) {
    if (error.code === 'ENOENT') return undefined;
    throw error;
  }
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > 120_000) throw new Error('Invalid MCP config file');
    // A project file can grow after fstat; never let readFile allocate without
    // a bound on the already-open descriptor.
    const buffer = Buffer.alloc(120_001);
    let length = 0;
    while (length < buffer.length) {
      const { bytesRead } = await file.read(buffer, length, buffer.length - length, null);
      if (bytesRead === 0) break;
      length += bytesRead;
    }
    if (length > 120_000) throw new Error('Invalid MCP config file');
    return buffer.toString('utf8', 0, length);
  } finally {
    await file.close();
  }
}

/** Read Pi-owned user/approval files; read project config ONLY after Pi trust. No launch here. */
export async function loadDeclarationSnapshot({ ctx, agentDir, configDirName }) {
  if (!isAbsolute(agentDir)) throw new Error('Absolute Pi agent directory required');
  const projectRoot = await realpath(ctx.cwd);
  const user = parseNativeConfig(await readOptional(join(agentDir, 'mcp.json')) ?? EMPTY_CONFIG);
  const ledger = await readTrustLedger(agentDir);
  // Pi's built-in resource scan does not include .pi/mcp.json, and auto-trusts
  // cwd without recognized resources. Require a saved Pi trust decision before
  // reading MCP project config OR running user servers from project cwd.
  const savedTrust = new ProjectTrustStore(agentDir).get(projectRoot) === true;
  const projectTrusted = ctx.isProjectTrusted() && savedTrust;
  const project = projectTrusted
    ? parseNativeConfig(await readOptional(join(projectRoot, configDirName, 'mcp.json')) ?? EMPTY_CONFIG)
    : undefined;
  return { projectRoot, projectTrusted, user, project, ledger,
    effective: effectiveDeclarations({ user, project, projectTrusted, projectRoot, ledger }) };
}

export async function loadEffectiveDeclarations(options) {
  return (await loadDeclarationSnapshot(options)).effective;
}
