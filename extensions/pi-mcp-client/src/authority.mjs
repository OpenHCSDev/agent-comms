import { isAbsolute } from 'node:path';
import { z } from 'zod';
import { declarationDigest } from './config.mjs';
import { parseUniqueJson } from './strict-json.mjs';

const approval = z.strictObject({
  projectRoot: z.string().min(1),
  scope: z.literal('project'),
  serverId: z.string().regex(/^[a-z][a-z0-9_-]{0,31}$/),
  digest: z.string().regex(/^[a-f0-9]{64}$/),
  decision: z.enum(['approve', 'deny']),
});
const callGrant = z.strictObject({
  projectRoot: z.string().min(1),
  scope: z.enum(['user', 'project']),
  serverId: z.string().regex(/^[a-z][a-z0-9_-]{0,31}$/),
  digest: z.string().regex(/^[a-f0-9]{64}$/),
  decision: z.enum(['allow', 'ask']),
});
const ledgerSchema = z.strictObject({
  version: z.literal(1),
  decisions: z.array(approval).max(256),
  callGrants: z.array(callGrant).max(256).default([]),
}).superRefine((value, ctx) => {
  const seen = new Set();
  for (const [index, row] of value.decisions.entries()) {
    const key = JSON.stringify([row.projectRoot, row.scope, row.serverId, row.digest]);
    if (seen.has(key)) {
      ctx.addIssue({ code: 'custom', message: 'Duplicate decision', path: ['decisions', index] });
    }
    seen.add(key);
  }
  seen.clear();
  for (const [index, row] of value.callGrants.entries()) {
    const key = JSON.stringify([row.projectRoot, row.scope, row.serverId, row.digest]);
    if (seen.has(key)) {
      ctx.addIssue({ code: 'custom', message: 'Duplicate call grant', path: ['callGrants', index] });
    }
    seen.add(key);
  }
});

/** The approval ledger belongs outside every project. No caller may infer approval from config. */
export function parseTrustLedger(text) {
  if (typeof text !== 'string' || Buffer.byteLength(text, 'utf8') > 120_000) {
    throw new Error('Invalid MCP trust ledger: size limit');
  }
  let raw;
  try {
    raw = parseUniqueJson(text);
  } catch (error) {
    throw new Error(error.message === 'Duplicate JSON key'
      ? 'Invalid MCP trust ledger: duplicate key' : 'Invalid MCP trust ledger: JSON syntax');
  }
  const result = ledgerSchema.safeParse(raw);
  if (!result.success) throw new Error('Invalid MCP trust ledger: schema');
  return result.data;
}

/** Derive executable eligibility. This does not open transports or resolve credentials. */
export function callGrantDecision(ledger, entry) {
  return ledger.callGrants.find((row) => row.projectRoot === entry.projectRoot &&
    row.scope === entry.scope && row.serverId === entry.declaration.id &&
    row.digest === entry.digest)?.decision ?? 'ask';
}

export function effectiveDeclarations({ user, project, projectTrusted, projectRoot, ledger }) {
  if (!isAbsolute(projectRoot)) throw new Error('Canonical absolute project root required');
  const combined = new Map(user.servers.map((server) => [server.id,
    { scope: 'user', declaration: server }]));
  if (projectTrusted) {
    for (const server of project?.servers ?? []) {
      combined.set(server.id, { scope: 'project', declaration: server });
    }
  } else if (project !== undefined) {
    throw new Error('Untrusted project declarations must not be read');
  }
  return [...combined.values()].map(({ scope, declaration }) => {
    const digest = declarationDigest(declaration);
    const decision = scope === 'project' && ledger.decisions.find((row) =>
      row.projectRoot === projectRoot && row.scope === 'project' &&
      row.serverId === declaration.id && row.digest === digest
    )?.decision;
    // Every stdio child uses the current project as cwd, including user-scope
    // declarations. Relative executables/args can execute project-controlled
    // code, so Pi project trust is required even for owner-trusted user config.
    const status = !declaration.enabled ? 'disabled'
      : !projectTrusted ? 'trust_required'
      : scope === 'project' && Object.keys(declaration.transport.env).length ? 'unsupported_env'
      : scope === 'user' || decision === 'approve' ? 'approved'
      : decision === 'deny' ? 'denied' : 'trust_required';
    return { projectRoot, scope, declaration, digest, status };
  });
}
