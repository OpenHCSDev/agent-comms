import { createHash } from 'node:crypto';
import { z } from 'zod';
import { parseUniqueJson } from './strict-json.mjs';

const name = z.string().regex(/^[a-z][a-z0-9_-]{0,31}$/);
const variable = z.string().regex(/^[A-Z_][A-Z0-9_]*$/);
const variables = z.record(variable, z.string().max(8_192)).default({});
const transport = z.strictObject({
  type: z.literal('stdio'),
  command: z.string().min(1).max(4_096),
  args: z.array(z.string().max(8_192)).max(32),
  cwd: z.literal('project'),
  env: variables,
  envFrom: z.record(variable, variable).default({}),
}).superRefine((value, ctx) => {
  for (const key of Object.keys(value.env)) {
    if (Object.hasOwn(value.envFrom, key)) {
      ctx.addIssue({ code: 'custom', message: 'Environment mapping conflict', path: ['envFrom', key] });
    }
  }
});
const server = z.strictObject({
  id: name,
  transport,
  enabled: z.boolean(),
  instructionsPolicy: z.literal('status-only'),
});
const document = z.strictObject({
  version: z.literal(1),
  servers: z.array(server).max(16),
}).superRefine((value, ctx) => {
  const seen = new Set();
  for (const [index, item] of value.servers.entries()) {
    if (seen.has(item.id)) {
      ctx.addIssue({ code: 'custom', message: 'Duplicate server ID', path: ['servers', index, 'id'] });
    }
    seen.add(item.id);
  }
});

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}

/** Parse inert native declarations. A separate Pi/project and approval gate must precede any launch. */
export function parseNativeConfig(text) {
  if (typeof text !== 'string' || Buffer.byteLength(text, 'utf8') > 120_000) {
    throw new Error('Invalid MCP config: size limit');
  }
  let raw;
  try {
    raw = parseUniqueJson(text);
  } catch (error) {
    // Never include raw config or parser excerpts: declarations can contain secrets.
    throw new Error(error.message === 'Duplicate JSON key'
      ? 'Invalid MCP config: duplicate key' : 'Invalid MCP config: JSON syntax');
  }
  const result = document.safeParse(raw);
  if (!result.success) {
    throw new Error('Invalid MCP config: declaration schema');
  }
  return result.data;
}

/** Digest the full validated declaration; the approval ledger stores only this hash, not its fields. */
export function declarationDigest(declaration) {
  const result = server.safeParse(declaration);
  if (!result.success) throw new Error('Invalid MCP declaration');
  return createHash('sha256')
    .update('pi-mcp-declaration-v1\n' + JSON.stringify(canonical(result.data)))
    .digest('hex');
}
