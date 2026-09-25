#!/usr/bin/env node
import { existsSync } from 'node:fs';
import { realpath } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline/promises';
import { CONFIG_DIR_NAME, getAgentDir, ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { writeNativeServer } from '../src/config-write.mjs';
import { decideCallGrant, decideProjectServer } from '../src/commands.mjs';
import { declarationDigest, parseNativeConfig } from '../src/config.mjs';
import { loadEffectiveDeclarations } from '../src/sources.mjs';
import { inventorySnapshot } from '../src/inventory.mjs';

function options(args) {
  const parsed = { arg: [], env: {}, envFrom: {} };
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === '--json' || flag === '--dry-run' || flag === '--replace') {
      if (parsed[flag.slice(2)]) throw new Error(`Duplicate ${flag}`);
      parsed[flag.slice(2)] = true;
      continue;
    }
    if (!['--project', '--scope', '--id', '--digest', '--command', '--arg', '--env', '--env-from'].includes(flag)) {
      throw new Error(`Unknown MCP option ${flag}`);
    }
    const value = args[++index];
    if (value === undefined) throw new Error(`Missing value for ${flag}`);
    if (flag === '--arg') { parsed.arg.push(value); continue; }
    if (flag === '--env' || flag === '--env-from') {
      const separator = value.indexOf('=');
      if (separator < 1) throw new Error(`Expected NAME=VALUE for ${flag}`);
      const [key, item] = [value.slice(0, separator), value.slice(separator + 1)];
      if (!/^[A-Z_][A-Z0-9_]*$/.test(key) ||
          (flag === '--env-from' && !/^[A-Z_][A-Z0-9_]*$/.test(item))) {
        throw new Error('Invalid MCP environment mapping name');
      }
      const destination = flag === '--env' ? parsed.env : parsed.envFrom;
      if (Object.hasOwn(destination, key)) throw new Error(`Duplicate MCP variable ${key}`);
      destination[key] = item;
      continue;
    }
    const key = flag.slice(2);
    if (parsed[key] !== undefined) throw new Error(`Duplicate ${flag}`);
    parsed[key] = value;
  }
  return parsed;
}

async function status(opts, agentDir) {
  const projectRoot = await realpath(opts.project ?? process.cwd());
  const projectTrusted = new ProjectTrustStore(agentDir).get(projectRoot) === true;
  const ctx = { cwd: projectRoot, isProjectTrusted: () => projectTrusted };
  const servers = (await loadEffectiveDeclarations({ ctx, agentDir, configDirName: CONFIG_DIR_NAME }))
    .map(({ declaration, scope, status: state, digest }) => ({ id: declaration.id,
      scope, status: state, digest }));
  return { version: 1, projectRoot, projectTrustedSaved: projectTrusted,
    projectConfigSkipped: !projectTrusted && existsSync(join(projectRoot, CONFIG_DIR_NAME, 'mcp.json')),
    servers };
}

async function decisionAction(action, decision, opts, agentDir) {
  if (Object.keys(opts).some((key) => !['arg', 'env', 'envFrom', 'project', 'id', 'digest'].includes(key)) ||
      !opts.id || !/^[a-f0-9]{64}$/.test(opts.digest ?? '')) {
    throw new Error('MCP decision requires --id and an exact --digest');
  }
  // CLI is an out-of-band LOCAL controller, not a headless approval API.
  // A same-user process can emulate a TTY; this is consent UX, not an OS boundary.
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    throw new Error('MCP decisions require an interactive local TTY');
  }
  const projectRoot = await realpath(opts.project ?? process.cwd());
  const projectTrusted = new ProjectTrustStore(agentDir).get(projectRoot) === true;
  const ctx = { cwd: projectRoot, isProjectTrusted: () => projectTrusted, mode: 'tui',
    ui: { async confirm(title, display) {
      const challenge = `${decision}:${opts.id}:${opts.digest}`;
      process.stdout.write(`${title}\n${display}\n`);
      const prompt = createInterface({ input: process.stdin, output: process.stdout });
      try {
        return (await prompt.question(`Type ${challenge} to apply: `)) === challenge;
      } finally { prompt.close(); }
    } },
  };
  const args = { agentDir, configDirName: CONFIG_DIR_NAME, id: opts.id,
    decision, expectedDigest: opts.digest };
  const applied = action === 'trust' ? await decideProjectServer(ctx, args)
    : await decideCallGrant(ctx, args);
  return { version: 2, action, decision, id: opts.id, digest: opts.digest,
    projectRoot, applied, applies: 'next_pi_turn' };
}

async function add(opts, agentDir) {
  if (!opts.id || !opts.command || !['user', 'project'].includes(opts.scope)) {
    throw new Error('add requires --scope user|project --id ID --command EXECUTABLE');
  }
  const projectRoot = await realpath(opts.project ?? process.cwd());
  const document = parseNativeConfig(JSON.stringify({ version: 1, servers: [{
    id: opts.id, enabled: true, instructionsPolicy: 'status-only', transport: {
      type: 'stdio', command: opts.command, args: opts.arg, cwd: 'project',
      env: opts.env, envFrom: opts.envFrom,
    },
  }] }));
  const declaration = document.servers[0];
  if (opts.scope === 'project' && Object.keys(declaration.transport.env).length) {
    throw new Error('Project MCP literal environment is not supported; use envFrom');
  }
  const digest = declarationDigest(declaration);
  const receipt = { version: 1, scope: opts.scope, projectRoot, id: declaration.id,
    digest, command: declaration.transport.command, args: declaration.transport.args,
    envNames: Object.keys(declaration.transport.env),
    envFrom: declaration.transport.envFrom, replace: !!opts.replace };
  if (opts['dry-run']) return { ...receipt, applied: false };
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    throw new Error('MCP config changes require an interactive local TTY; use --dry-run for JSON preview');
  }
  const challenge = `${declaration.id}:${digest.slice(0, 12)}`;
  process.stdout.write(JSON.stringify(receipt, null, 2) + '\n');
  const prompt = createInterface({ input: process.stdin, output: process.stdout });
  let answer;
  try { answer = await prompt.question(`Type ${challenge} to apply: `); }
  finally { prompt.close(); }
  if (answer !== challenge) throw new Error('MCP config change cancelled');
  const written = await writeNativeServer({ agentDir, projectRoot,
    configDirName: CONFIG_DIR_NAME, scope: opts.scope, declaration, replace: !!opts.replace });
  return { version: 1, scope: written.scope, projectRoot: written.projectRoot,
    id: written.id, digest: written.digest, applied: true };
}

try {
  const [action, ...args] = process.argv.slice(2);
  const opts = action === 'trust' || action === 'calls' ? {} : options(args);
  const agentDir = getAgentDir();
  if (action === 'status') {
    if (Object.keys(opts).some((key) => !['arg', 'env', 'envFrom', 'project', 'json'].includes(key))) {
      throw new Error('status accepts only --project and --json');
    }
    process.stdout.write(JSON.stringify(await status(opts, agentDir)) + '\n');
  } else if (action === 'inventory') {
    if (Object.keys(opts).some((key) => !['arg', 'env', 'envFrom', 'project', 'json'].includes(key))) {
      throw new Error('inventory accepts only --project and --json');
    }
    const projectRoot = await realpath(opts.project ?? process.cwd());
    const projectTrusted = new ProjectTrustStore(agentDir).get(projectRoot) === true;
    process.stdout.write(JSON.stringify(await inventorySnapshot({
      ctx: { cwd: projectRoot, isProjectTrusted: () => projectTrusted },
      agentDir, configDirName: CONFIG_DIR_NAME,
    })) + '\n');
  } else if (action === 'trust' || action === 'calls') {
    const [decision, ...optionsArgs] = args;
    if (!((action === 'trust' && ['approve', 'deny'].includes(decision)) ||
          (action === 'calls' && ['allow', 'ask'].includes(decision)))) {
      throw new Error('Invalid MCP decision action');
    }
    process.stdout.write(JSON.stringify(await decisionAction(action, decision,
      options(optionsArgs), agentDir)) + '\n');
  } else if (action === 'add') {
    process.stdout.write(JSON.stringify(await add(opts, agentDir)) + '\n');
  } else {
    throw new Error('Usage: pi-mcp status|inventory [--project PATH] [--json] | trust approve|deny --id ID --digest SHA256 [--project PATH] | calls allow|ask --id ID --digest SHA256 [--project PATH] | add --scope user|project --id ID --command EXECUTABLE [--arg ARG ...] [--env NAME=VALUE] [--env-from NAME=SOURCE] [--dry-run] [--replace]');
  }
} catch (error) {
  console.error(`pi-mcp: ${error.message}`);
  process.exitCode = 1;
}
