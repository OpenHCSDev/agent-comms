import { lstat, mkdir, realpath } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { updateJsonFile } from './atomic-file.mjs';
import { declarationDigest, parseNativeConfig } from './config.mjs';

const EMPTY_CONFIG = '{"version":1,"servers":[]}';

/** Package authority for native config replacement. It never connects or approves a server. */
export async function writeNativeServer({ agentDir, projectRoot, configDirName, scope,
  declaration, replace = false }) {
  if (!isAbsolute(agentDir) || !['user', 'project'].includes(scope) ||
      !/^[.a-zA-Z0-9_-]+$/.test(configDirName)) {
    throw new Error('Invalid MCP config destination');
  }
  const canonicalRoot = await realpath(projectRoot);
  const server = parseNativeConfig(JSON.stringify({ version: 1, servers: [declaration] })).servers[0];
  const directory = scope === 'user' ? agentDir : join(canonicalRoot, configDirName);
  await mkdir(directory, { recursive: true, mode: scope === 'user' ? 0o700 : 0o755 });
  if ((await lstat(directory)).isSymbolicLink()) throw new Error('MCP config directory symlink refused');
  const path = join(directory, 'mcp.json');
  await updateJsonFile(path, { initial: EMPTY_CONFIG, parse: parseNativeConfig, mode: 0o600,
    update(current) {
      const index = current.servers.findIndex((item) => item.id === server.id);
      if (index !== -1 && !replace) throw new Error('MCP server ID already exists; use explicit replace');
      if (index === -1 && replace) throw new Error('MCP server ID not found for replace');
      const servers = [...current.servers];
      if (index === -1) servers.push(server);
      else servers[index] = server;
      return { version: 1, servers };
    },
  });
  return { scope, projectRoot: canonicalRoot, id: server.id,
    digest: declarationDigest(server), configPath: path };
}
