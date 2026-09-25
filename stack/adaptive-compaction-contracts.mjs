/** Dormant, provider-free PR #48 contracts. Not imported by the Pi adapter or ACP.
 * CompactionPolicy remains the only owner of native strategy/budget defaults.
 * These derived views never authorize a provider send, goal, claim, or commit.
 */
import { CompactionPolicy } from './native-compaction-policy.mjs';

const sourceKeys = ['cursor', 'sessionRevision', 'ownerEpoch', 'turnId', 'goalId', 'goalRevision', 'correctionRevision'];
const provenanceKeys = ['goalId', 'goalRevision', 'correctionRevision'];
const snapshotKeys = ['source', 'boundary', 'boundaryEvidenceRef', 'newHistoryBytes', 'hardBackstopDue'];
const factKeys = ['id', 'kind', 'text', 'evidenceRef', 'sourceRevision', ...provenanceKeys];
const rowKeys = ['id', 'kind', 'text', 'toolCallId', ...provenanceKeys];
const narrativeKeys = ['text', ...provenanceKeys];
const factKinds = new Set(['task', 'correction', 'failure', 'identifier', 'goal-reference']);
const rowKinds = new Set(['user', 'assistant', 'tool_call', 'tool_result']);
const enc = new TextEncoder();

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
function sameProvenance(value, captured) {
    return provenanceKeys.every(key => Object.hasOwn(value, key) && value[key] === captured[key]);
}
function text(value, name) {
    if (typeof value !== 'string' || !value.isWellFormed() || enc.encode(value).length > 131072)
        throw new Error(`Invalid ${name}`);
    return value;
}
function bytes(value) {
    return enc.encode(JSON.stringify(value)).length;
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

/** Conservative derived view from the canonical stores. Facts are explicit
 * owner-provided references, not inferred authority or permission to mutate
 * registry, goal/attempt, input disposition, or claim state. Caller-supplied
 * provenance labels are NOT authenticated owner proof; an adapter cannot
 * activate this view until it projects and rechecks canonical owner revisions
 * at the native send and durable commit boundaries. The budget is computed
 * using the existing native CompactionPolicy for the selected model.
 */
export class RetentionPolicy {
    constructor({ recentGroups = 3, compactionPolicy = new CompactionPolicy() } = {}) {
        if (!Number.isSafeInteger(recentGroups) || recentGroups < 1 || recentGroups > 32)
            throw new Error('Invalid recent window');
        if (!(compactionPolicy instanceof CompactionPolicy))
            throw new Error('Invalid declared compaction policy');
        this.recentGroups = recentGroups;
        this.compactionPolicy = compactionPolicy;
        Object.freeze(this);
    }
    budgetFor(model, reserveTokens) {
        if (!Number.isSafeInteger(model?.contextWindow) || model.contextWindow <= 0 ||
            !Number.isSafeInteger(reserveTokens) || reserveTokens < 0)
            throw new Error('Invalid retention model reserve');
        // The native declaration owns the model/reserve calculation; no
        // second local context-window formula or provider route is invented.
        return this.compactionPolicy.inputBytes(model, reserveTokens);
    }
    select(snapshot, candidate, budget) {
        record(snapshot, ['source', 'facts', 'tombstones', 'rows', 'previousSummary', 'customFocus'], 'retention snapshot');
        record(budget, ['model', 'reserveTokens'], 'retention budget');
        const budgetBytes = this.budgetFor(budget.model, budget.reserveTokens);
        if (!Number.isSafeInteger(budgetBytes) || budgetBytes < 4096 || budgetBytes > 1 << 20)
            throw new Error('Invalid retention budget');
        const captured = source(snapshot.source);
        if (!candidate || candidate.kind !== 'candidate' ||
            !sameSource(source(candidate.source), captured)) return skip('stale-source');
        const evidenceRef = boundaryEvidence(candidate.evidenceRef, captured);
        if (evidenceRef.kind === 'skip') return evidenceRef;
        if (!Array.isArray(snapshot.facts) || snapshot.facts.length > 128 ||
            !Array.isArray(snapshot.tombstones) || snapshot.tombstones.length > 128 ||
            !Array.isArray(snapshot.rows) || snapshot.rows.length > 256)
            throw new Error('Invalid bounded retention snapshot');
        const tombstones = new Set(snapshot.tombstones.map(id => label(id, 'tombstone')));
        const facts = [];
        const ids = new Set();
        for (const item of snapshot.facts) {
            record(item, factKeys, 'retained fact');
            const id = label(item.id, 'fact ID');
            if (ids.has(id)) throw new Error('Duplicate fact ID');
            ids.add(id);
            if (!factKinds.has(item.kind)) throw new Error('Unsupported fact kind');
            if (item.kind === 'goal-reference' && captured.goalId === null)
                return skip('goal-unavailable');
            if (item.sourceRevision !== captured.sessionRevision ||
                !sameProvenance(item, captured)) return skip('stale-fact');
            let evidenceRef;
            if (item.kind === 'goal-reference') {
                // Compare fields, not free-form "registry:..." text; this is
                // still a derived reference, never a verified goal receipt.
                record(item.evidenceRef, ['store', 'goalId', 'revision'], 'goal evidence');
                if (item.evidenceRef.store !== 'registry' ||
                    item.evidenceRef.goalId !== captured.goalId ||
                    item.evidenceRef.revision !== captured.goalRevision)
                    return skip('stale-goal-reference');
                evidenceRef = Object.freeze({ store: 'registry', goalId: captured.goalId,
                    revision: captured.goalRevision });
            } else evidenceRef = label(item.evidenceRef, 'fact evidence');
            const fact = Object.freeze({
                id, kind: item.kind, text: text(item.text, 'fact text'), evidenceRef,
                sourceRevision: captured.sessionRevision,
                goalId: captured.goalId, goalRevision: captured.goalRevision,
                correctionRevision: captured.correctionRevision,
            });
            if (!tombstones.has(id)) facts.push(fact);
        }
        const rows = [];
        const rowIds = new Set();
        for (const row of snapshot.rows) {
            record(row, rowKeys, 'recent row');
            const id = label(row.id, 'row ID');
            if (rowIds.has(id)) throw new Error('Duplicate row ID');
            rowIds.add(id);
            if (!rowKinds.has(row.kind)) throw new Error('Unsupported row kind');
            if (!sameProvenance(row, captured)) return skip('stale-row');
            const pair = row.kind === 'tool_call' || row.kind === 'tool_result';
            if (pair) label(row.toolCallId, 'tool pair ID');
            else if (row.toolCallId !== undefined) throw new Error('Unexpected tool pair ID');
            rows.push(Object.freeze({ id, kind: row.kind, text: text(row.text, 'row text'),
                goalId: captured.goalId, goalRevision: captured.goalRevision,
                correctionRevision: captured.correctionRevision,
                ...(pair ? { toolCallId: row.toolCallId } : {}) }));
        }
        // Treat adjacent tool-call/result as an indivisible recent group. An
        // unmatched result or pending call is a refusal, never a dropped half.
        const groups = [];
        for (let index = 0; index < rows.length; index++) {
            const row = rows[index];
            if (row.kind === 'tool_result') return skip('incomplete-tool-pair');
            if (row.kind === 'tool_call') {
                const next = rows[++index];
                if (!next || next.kind !== 'tool_result' || next.toolCallId !== row.toolCallId)
                    return skip('incomplete-tool-pair');
                groups.push(Object.freeze([row, next]));
            } else groups.push(Object.freeze([row]));
        }
        const recent = Object.freeze(groups.slice(-this.recentGroups).flat());
        // Null denotes absence. A narrative carrying old goal/correction text
        // with no owner-supplied provenance cannot be safely reintroduced.
        for (const [name, value] of [
            ['previousSummary', snapshot.previousSummary],
            ['customFocus', snapshot.customFocus],
        ]) {
            if (value === null) continue;
            if (!value || typeof value !== 'object' || Array.isArray(value))
                return skip(`unproven-${name}`);
            record(value, narrativeKeys, name);
            if (!sameProvenance(value, captured)) return skip(`stale-${name}`);
        }
        const result = Object.freeze({
            kind: 'selection', authority: 'none', source: captured,
            facts: Object.freeze(facts), recent,
            previousSummary: snapshot.previousSummary === null ? '' :
                text(snapshot.previousSummary.text, 'previous summary'),
            customFocus: snapshot.customFocus === null ? '' :
                text(snapshot.customFocus.text, 'custom focus'),
        });
        // No partial reduction: caller must use the existing bounded strategy
        // or decline this candidate, not truncate a tool pair or owner fact.
        return bytes(result) <= budgetBytes ? result : skip('budget-exceeded');
    }
}
