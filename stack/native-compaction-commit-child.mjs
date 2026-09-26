// Internal, non-forking single-shot helper. No provider/session runtime imports.
// The Python owner holds and passes the registry flock until this process exits.
// This program is NOT a public RPC accepting caller-stamped owner receipts.
import { fstatSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [packageDir, descriptor] = process.argv.slice(2);
try {
    const fd = Number(descriptor);
    if (!Number.isSafeInteger(fd) || fd < 3) throw new Error('Inherited authority FD required');
    const held = fstatSync(fd, { bigint: true });
    const input = readFileSync(0);
    if (input.length > 524288) throw new Error('Native commit request too large');
    const request = JSON.parse(input.toString('utf8'));
    const keys = ['action', 'authority', 'witness', 'commit', 'summary', 'tokensBefore'];
    if (Object.keys(request).some(key => !keys.includes(key)))
        throw new Error('Unexpected request fields; JSON receipts are not authority');
    const authority = request.authority;
    if (authority?.parentPid !== process.ppid ||
        String(held.dev) !== authority.device || String(held.ino) !== authority.inode ||
        !held.isFile()) throw new Error('Inherited authority lineage unavailable');
    // Do not accept legacy sessions that opening could migrate/rewrite before
    // the guarded CAS. Deployment must prohibit other/unpatched writers.
    const file = request.witness?.sessionFile;
    if (!file || statSync(file).size > 256 * 1024 * 1024)
        throw new Error('Bounded native session required');
    const raw = readFileSync(file, 'utf8');
    if (!raw.endsWith('\n')) throw new Error('Incomplete session');
    const rows = raw.trimEnd().split('\n').map(line => JSON.parse(line));
    if (rows[0]?.type !== 'session' || rows[0]?.version !== 3)
        throw new Error('Native session migration prohibited in commit helper');
    const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/core/session-manager.js')));
    const manager = SessionManager.open(file);
    if (request.action === 'commit') {
        manager.appendCompactionIfCurrent(request.witness, request.summary, request.tokensBefore,
            { agentCommsCommit: request.commit });
    } else if (request.action !== 'reconcile') throw new Error('Invalid native commit action');
    console.log(JSON.stringify(manager.reconcileCompactionCommit(request.commit, request.witness)));
    // Never close the authority FD early. Kernel closes it at process exit.
} catch (error) {
    // Even an exception that appears pre-write cannot authorize replay. The
    // owner journals UNKNOWN and requires a separate explicit reconciliation.
    console.log(JSON.stringify({ status: 'unknown', reason: String(error).slice(0, 1024) }));
    process.exitCode = 1;
}
