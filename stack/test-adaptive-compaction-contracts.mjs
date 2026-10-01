// Provider-free PR #48 contract fixtures; no native Pi copy or RPC invocation.
import assert from 'node:assert/strict';
import { TriggerRule } from './adaptive-compaction-contracts.mjs';

const baseline = { cursor: 'seq-314', sessionRevision: 's-21', ownerEpoch: 'owner-3',
    turnId: 'turn-5', goalId: 'goal-9', goalRevision: 'revision-9', correctionRevision: 'correction-1' };
const provenance = { goalId: baseline.goalId, goalRevision: baseline.goalRevision,
    correctionRevision: baseline.correctionRevision };
const trigger = new TriggerRule();
const observed = { source: baseline, boundary: 'completed-subtask',
    boundaryEvidenceRef: { store: 'native-transcript', entryId: 'entry-314', source: baseline },
    newHistoryBytes: 22000, hardBackstopDue: false };
const observedFor = source => ({ ...observed, source,
    boundaryEvidenceRef: { ...observed.boundaryEvidenceRef, source } });
const savedInput = structuredClone(observed);
const candidate = trigger.evaluate(observed, baseline);
assert.equal(candidate.kind, 'candidate');
assert.equal(candidate.authority, 'none');
assert.deepEqual(candidate.evidenceRef, observed.boundaryEvidenceRef);
assert.notEqual(candidate.evidenceRef, observed.boundaryEvidenceRef);
assert.ok(Object.isFrozen(candidate.source) && Object.isFrozen(candidate));
assert.ok(Object.isFrozen(candidate.evidenceRef) && Object.isFrozen(candidate.evidenceRef.source));
assert.equal(trigger.stillCurrent(candidate, baseline), true);
assert.equal(trigger.stillCurrent({ ...candidate, evidenceRef: null }, baseline), false);
assert.equal(trigger.stillCurrent({ ...candidate, evidenceRef: {
    ...candidate.evidenceRef, source: { ...baseline, turnId: 'old-turn' },
} }, baseline), false);
for (const changed of [
    { cursor: 'seq-315' }, { sessionRevision: 's-22' }, { ownerEpoch: 'owner-4' },
    { turnId: 'turn-6' }, { goalId: 'goal-10' }, { goalRevision: 'revision-10' },
    { correctionRevision: 'correction-2' },
]) {
    assert.equal(trigger.evaluate(observed, { ...baseline, ...changed }).reason, 'stale-source');
    assert.equal(trigger.stillCurrent(candidate, { ...baseline, ...changed }), false);
}
assert.equal(trigger.evaluate({ ...observed, boundary: 'unfinished', boundaryEvidenceRef: null }, baseline).reason, 'unfinished-work');
assert.equal(trigger.evaluate({ ...observed, boundary: 'none', boundaryEvidenceRef: null }, baseline).reason, 'unfinished-work');
assert.equal(trigger.evaluate({ ...observed, newHistoryBytes: 16383 }, baseline).reason, 'cadence');
assert.equal(trigger.evaluate({ ...observed, hardBackstopDue: true }, baseline).kind, 'independent-hard-path');
assert.equal(trigger.evaluate({ ...observed, hardBackstopDue: true }, { ...baseline, ownerEpoch: 'stale' }).kind, 'independent-hard-path');
assert.equal(trigger.evaluate({ ...observed, boundaryEvidenceRef: null }, baseline).reason, 'missing-boundary-evidence');
assert.equal(trigger.evaluate({ ...observed, boundaryEvidenceRef: {
    ...observed.boundaryEvidenceRef, source: { ...baseline, correctionRevision: 'correction-0' },
} }, baseline).reason, 'stale-boundary-evidence');
assert.throws(() => trigger.evaluate({ ...observed, boundaryEvidenceRef: {
    ...observed.boundaryEvidenceRef, grant: 'secret',
} }, baseline), /Invalid boundary evidence/);
assert.throws(() => trigger.evaluate({ ...observed, boundaryEvidenceRef: {
    ...observed.boundaryEvidenceRef, store: 'registry',
} }, baseline), /Invalid boundary evidence/);
assert.throws(() => trigger.evaluate({ ...observed, boundaryEvidenceRef: '\ud800' }, baseline), /Invalid boundary evidence/);
assert.equal(trigger.evaluate({ ...observed, boundaryEvidenceRef: '\ud800', hardBackstopDue: true },
    { ...baseline, ownerEpoch: 'stale' }).kind, 'independent-hard-path',
'independent hard owner must not depend on validity of adaptive evidence');
assert.equal(trigger.evaluate({ ...observed, hiddenGrant: 'secret', hardBackstopDue: true },
    baseline).kind, 'independent-hard-path');
assert.throws(() => trigger.evaluate({ ...observed, hiddenGrant: 'secret' }, baseline), /Invalid trigger snapshot/);
assert.throws(() => trigger.evaluate(observed, { ...baseline, goalId: null }), /Invalid goal source fence/);
const mutableObservation = structuredClone(observed);
const preservedCandidate = trigger.evaluate(mutableObservation, baseline);
mutableObservation.boundaryEvidenceRef.source.correctionRevision = 'correction-0';
assert.equal(preservedCandidate.evidenceRef.source.correctionRevision, baseline.correctionRevision);
assert.throws(() => new TriggerRule({ minNewHistoryBytes: 1 }), /Invalid trigger cadence/);
assert.deepEqual(observed, savedInput, 'evaluation must not mutate owner state');

console.log('Existing trigger contract passed; dormant retention removed.');
