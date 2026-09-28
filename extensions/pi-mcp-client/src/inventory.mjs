import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { callGrantDecision } from './authority.mjs';
import { declarationDigest } from './config.mjs';
import { loadDeclarationSnapshot } from './sources.mjs';

/** Inert, redacted package-owned inventory for CLI and thin UI projections. */
export async function inventorySnapshot(options) {
  const snapshot = await loadDeclarationSnapshot(options);
  const { projectRoot, projectTrusted, user, project, ledger, effective } = snapshot;
  const winning = new Map(effective.map((entry) => [entry.declaration.id, entry]));
  const rows = (scope, declarations) => declarations.map((declaration) => {
    const selected = winning.get(declaration.id);
    const current = selected?.scope === scope ? selected : undefined;
    const transport = declaration.transport;
    return {
      id: declaration.id, scope, digest: declarationDigest(declaration),
      effective: !!current, enabled: declaration.enabled,
      status: current?.status ?? 'shadowed',
      callPolicy: current?.status === 'approved' ? callGrantDecision(ledger, current) : 'unavailable',
      // Executable arguments may themselves be credentials. The local-only
      // approval display owns the complete command; inventory never exports it.
      transport: { type: transport.type, argumentCount: transport.args.length,
        cwd: transport.cwd, envNames: Object.keys(transport.env).sort(),
        envFrom: Object.entries(transport.envFrom).sort() },
    };
  });
  return {
    version: 2, projectRoot, projectTrustedSaved: projectTrusted,
    projectConfigSkipped: !projectTrusted && existsSync(join(projectRoot, options.configDirName, 'mcp.json')),
    lifetime: 'active_pi_turn', live: { state: 'not_running' },
    declarations: { user: rows('user', user.servers),
      project: rows('project', project?.servers ?? []) },
  };
}
