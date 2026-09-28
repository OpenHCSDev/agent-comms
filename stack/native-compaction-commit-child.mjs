// Internal, non-forking single-shot helper. No provider/session runtime imports.
// The Python owner holds and passes the registry flock until this process exits.
// This program is NOT a public RPC accepting caller-stamped owner receipts.
import { fstatSync, readFileSync, readSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [packageDir, descriptor, encodedLength] = process.argv.slice(2);
try {
    const fd = Number(descriptor);
    if (!Number.isSafeInteger(fd) || fd < 3) throw new Error('Inherited authority FD required');
    const held = fstatSync(fd, { bigint: true });
    // The existing owner serializes its admitted request once. This exact
    // length frames the pipe; only the inherited descriptor grants authority.
    const length = Number(encodedLength);
    if (!Number.isSafeInteger(length) || length <= 0)
        throw new Error('Exact encoded commit length required');
    const input = Buffer.allocUnsafe(length);
    let offset = 0;
    while (offset < length) {
        const received = readSync(0, input, offset, length - offset, null);
        if (!received) throw new Error('Incomplete native commit request');
        offset += received;
    }
    if (readSync(0, Buffer.allocUnsafe(1), 0, 1, null))
        throw new Error('Unexpected bytes after native commit request');
    const request = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(input));
    const keys = ['action', 'authority', 'witness', 'commit', 'summary', 'tokensBefore', 'details', 'usage'];
    if (Object.keys(request).some(key => !keys.includes(key)))
        throw new Error('Unexpected request fields; JSON receipts are not authority');
    const commit = request.commit;
    if (!commit || typeof commit !== 'object' || Array.isArray(commit) ||
        Object.keys(commit).sort().join(',') !== 'commitId,metadataDigest,payloadDigest' ||
        !/^[0-9a-f]{32}$/.test(commit.commitId) ||
        !/^[0-9a-f]{64}$/.test(commit.payloadDigest) ||
        !/^[0-9a-f]{64}$/.test(commit.metadataDigest))
        throw new Error('Bound native compaction commit identity required');
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
        const operations = request.details;
        if (operations !== undefined) {
            const paths = values => Array.isArray(values) &&
                values.every(path => typeof path === 'string' && path.length > 0 &&
                    Buffer.byteLength(path, 'utf8') <= 4096 && !path.includes('\\0'));
            if (!operations || typeof operations !== 'object' || Array.isArray(operations) ||
                Object.keys(operations).sort().join(',') !== 'modifiedFiles,readFiles' ||
                !paths(operations.readFiles) || !paths(operations.modifiedFiles))
                throw new Error('Invalid native file operations');
        }
        const usage = request.usage;
        if (usage !== undefined) {
            const counter = value => Number.isSafeInteger(value) && value >= 0;
            const nonnegative = value => typeof value === 'number' && Number.isFinite(value) &&
                value >= 0 && value <= Number.MAX_SAFE_INTEGER;
            const counters = ['input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens'];
            const costs = ['input', 'output', 'cacheRead', 'cacheWrite', 'total'];
            if (!usage || typeof usage !== 'object' || Array.isArray(usage) ||
                Object.keys(usage).some(key => ![...counters, 'cost', 'reasoning', 'cacheWrite1h'].includes(key)) ||
                counters.some(key => !counter(usage[key])) ||
                ['reasoning', 'cacheWrite1h'].some(key => usage[key] !== undefined && !counter(usage[key])) ||
                !usage.cost || typeof usage.cost !== 'object' || Array.isArray(usage.cost) ||
                Object.keys(usage.cost).sort().join(',') !== costs.sort().join(',') ||
                costs.some(key => !nonnegative(usage.cost[key])))
                throw new Error('Invalid native compaction usage');
        }
        manager.appendCompactionIfCurrent(request.witness, request.summary, request.tokensBefore,
            { ...(operations ?? {}), agentCommsCommit: request.commit }, usage);
    } else if (request.action !== 'reconcile') throw new Error('Invalid native commit action');
    console.log(JSON.stringify(manager.reconcileCompactionCommit(request.commit, request.witness)));
    // Never close the authority FD early. Kernel closes it at process exit.
} catch (error) {
    // Even an exception that appears pre-write cannot authorize replay. The
    // owner journals UNKNOWN and requires a separate explicit reconciliation.
    console.log(JSON.stringify({ status: 'unknown', reason: String(error).slice(0, 1024) }));
    process.exitCode = 1;
}
