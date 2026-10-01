/** Dormant, provider-free PR #48 contracts. Not imported by the Pi adapter or ACP.
 * CompactionPolicy remains the only owner of native strategy/budget defaults.
 * These derived views never authorize a provider send, goal, claim, or commit.
 */

const sourceKeys = ['cursor', 'sessionRevision', 'ownerEpoch', 'turnId', 'goalId', 'goalRevision', 'correctionRevision'];
const provenanceKeys = ['goalId', 'goalRevision', 'correctionRevision'];
const snapshotKeys = ['source', 'boundary', 'boundaryEvidenceRef', 'newHistoryBytes', 'hardBackstopDue'];

function record(value, keys, name) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        Object.keys(value).some(key => !keys.includes(key)))
        throw new Error(`Invalid ${name}`);
}
function label(value, name) {
    if (typeof value !== 'string' || !value || value.length > 128 || !value.isWellFormed())
        throw new Error(`Invalid ${name}`);
    return value;
}
function source(value) {
    record(value, sourceKeys, 'source fence');
    const result = {};
    for (const key of sourceKeys) {
        result[key] = provenanceKeys.includes(key) && value[key] === null
            ? null : label(value[key], key);
    }
    if ((result.goalId === null) !== (result.goalRevision === null))
        throw new Error('Invalid goal source fence');
    return Object.freeze(result);
}
function sameSource(a, b) {
    return sourceKeys.every(key => a[key] === b[key]);
}
function skip(reason) {
    return Object.freeze({ kind: 'skip', reason });
}
function boundaryEvidence(value, captured) {
    if (value == null) return skip('missing-boundary-evidence');
    record(value, ['store', 'entryId', 'source'], 'boundary evidence');
    if (value.store !== 'native-transcript') throw new Error('Invalid boundary evidence');
    const evidenceRef = Object.freeze({
        store: 'native-transcript', entryId: label(value.entryId, 'boundary evidence'),
        source: source(value.source),
    });
    return sameSource(evidenceRef.source, captured) ? evidenceRef : skip('stale-boundary-evidence');
}

/** Owner-observed candidate only. `source` must be the current owner fence:
 * cursor, saved-session revision, owner epoch, turn ID, goal ID/revision, and
 * latest owner-observed correction/deletion revision.
 * A model rubric may supply a structured native-transcript entry reference,
 * but neither it nor this decision grants admission. Its complete source fence
 * must match the observation; correction revision advances for deletes/renames.
 * These caller-supplied stamps are NOT proof from canonical owners: a future
 * adapter must obtain them from owners and recheck at send and commit.
 */
export class TriggerRule {
    constructor({ minNewHistoryBytes = 16384 } = {}) {
        if (!Number.isSafeInteger(minNewHistoryBytes) || minNewHistoryBytes < 4096 ||
            minNewHistoryBytes > 1 << 20) throw new Error('Invalid trigger cadence');
        this.minNewHistoryBytes = minNewHistoryBytes;
        Object.freeze(this);
    }
    evaluate(snapshot, currentSource) {
        // The native hard-limit owner must run regardless of malformed or stale
        // adaptive input. This marker does not attest that the limit is due or
        // grant a send: the native owner independently checks the real context.
        if (snapshot?.hardBackstopDue === true)
            return Object.freeze({ kind: 'independent-hard-path', reason: 'hard-context-limit' });
        record(snapshot, snapshotKeys, 'trigger snapshot');
        const captured = source(snapshot.source);
        const current = source(currentSource);
        if (typeof snapshot.hardBackstopDue !== 'boolean' ||
            !Number.isSafeInteger(snapshot.newHistoryBytes) || snapshot.newHistoryBytes < 0 ||
            snapshot.newHistoryBytes > 1 << 30 ||
            !['completed-subtask', 'unfinished', 'none'].includes(snapshot.boundary))
            throw new Error('Invalid trigger snapshot');
        if (snapshot.boundary !== 'completed-subtask' && snapshot.boundaryEvidenceRef !== null)
            throw new Error('Invalid boundary evidence');
        if (!sameSource(captured, current)) return skip('stale-source');
        if (snapshot.boundary !== 'completed-subtask') return skip('unfinished-work');
        const evidenceRef = boundaryEvidence(snapshot.boundaryEvidenceRef, captured);
        if (evidenceRef.kind === 'skip') return evidenceRef;
        if (snapshot.newHistoryBytes < this.minNewHistoryBytes) return skip('cadence');
        return Object.freeze({
            kind: 'candidate', authority: 'none', source: captured, evidenceRef,
        });
    }
    stillCurrent(candidate, currentSource) {
        if (!candidate || candidate.kind !== 'candidate') return false;
        const captured = source(candidate.source);
        if (!sameSource(captured, source(currentSource))) return false;
        return boundaryEvidence(candidate.evidenceRef, captured).kind !== 'skip';
    }
}
