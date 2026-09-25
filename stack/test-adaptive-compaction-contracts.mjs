// Provider-free PR #48 contract fixtures; no native Pi copy or RPC invocation.
import assert from 'node:assert/strict';
import { CompactionPolicy } from './native-compaction-policy.mjs';
import { TriggerRule, RetentionPolicy } from './adaptive-compaction-contracts.mjs';

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

const nativePolicy = new CompactionPolicy({ strategy: 'serial' });
const retention = new RetentionPolicy({ recentGroups: 4, compactionPolicy: nativePolicy });
const budget = { model: { contextWindow: 128000 }, reserveTokens: 16384 };
assert.equal(retention.budgetFor(budget.model, budget.reserveTokens), nativePolicy.inputBytes(budget.model, budget.reserveTokens));
assert.throws(() => new RetentionPolicy({ recentGroups: 0 }), /recent window/);
assert.throws(() => new RetentionPolicy({ compactionPolicy: {} }), /declared compaction policy/);
const facts = [
    { id: 'goal', kind: 'goal-reference', text: 'continue task',
        evidenceRef: { store: 'registry', goalId: 'goal-9', revision: 'revision-9' },
        sourceRevision: 's-21', ...provenance },
    { id: 'old-decision', kind: 'task', text: 'use obsolete path', evidenceRef: 'transcript:101', sourceRevision: 's-21', ...provenance },
    { id: 'correction', kind: 'correction', text: 'use src/new.py instead', evidenceRef: 'message:310', sourceRevision: 's-21', ...provenance },
    { id: 'unresolved', kind: 'failure', text: 'test timeout remains unresolved', evidenceRef: 'test:313', sourceRevision: 's-21', ...provenance },
    { id: 'identifier', kind: 'identifier', text: 'sha256:aabbcc', evidenceRef: 'file:312', sourceRevision: 's-21', ...provenance },
];
const rows = [
    { id: 'r1', kind: 'user', text: 'older context', ...provenance },
    { id: 'r2', kind: 'tool_call', toolCallId: 'edit/1', text: 'edit src/new.py', ...provenance },
    { id: 'r3', kind: 'tool_result', toolCallId: 'edit/1', text: 'success', ...provenance },
    { id: 'r4', kind: 'assistant', text: 'revised implementation', ...provenance },
    { id: 'r5', kind: 'user', text: 'correct the test path', ...provenance },
];
const focus = { text: 'KEEP_CORRECTIONS_AND_TOOL_PAIRS', ...provenance };
let summaryText = 'PRIOR_SUMMARY_EXACT_PATH_src/old.py';
for (let round = 1; round <= 3; round++) {
    const snapshot = { source: baseline, facts, tombstones: ['old-decision'], rows,
        previousSummary: { text: summaryText, ...provenance }, customFocus: focus };
    const copied = structuredClone(snapshot);
    const result = retention.select(snapshot, candidate, budget);
    assert.equal(result.kind, 'selection');
    assert.equal(result.authority, 'none');
    assert.equal(result.facts.length, 4);
    assert.equal(result.facts.find(item => item.id === 'old-decision'), undefined);
    assert.ok(result.facts.some(item => item.id === 'correction' && item.evidenceRef === 'message:310'));
    assert.ok(result.facts.some(item => item.id === 'unresolved'));
    assert.ok(result.recent.some(row => row.kind === 'tool_call'));
    assert.ok(result.recent.some(row => row.kind === 'tool_result'));
    assert.equal(result.previousSummary, summaryText);
    assert.equal(result.customFocus, focus.text);
    assert.ok(Object.isFrozen(result) && Object.isFrozen(result.recent));
    assert.deepEqual(snapshot, copied);
    summaryText = `ROUND_${round}_SUMMARY ${result.previousSummary}`;
}
// A repeated split turn can change session revision/cursor but not goal or
// correction. No new history rows: stamped prior summary and focus survive.
const splitSource = { ...baseline, cursor: 'seq-320', sessionRevision: 's-22', turnId: 'turn-6' };
const splitCandidate = trigger.evaluate(observedFor(splitSource), splitSource);
const emptyHistory = retention.select({ source: splitSource, facts: [], tombstones: [], rows: [],
    previousSummary: { text: summaryText, ...provenance }, customFocus: focus }, splitCandidate, budget);
assert.equal(emptyHistory.kind, 'selection');
assert.equal(emptyHistory.previousSummary, summaryText);
assert.equal(emptyHistory.customFocus, focus.text);
assert.deepEqual(emptyHistory.recent, []);
const request = { source: baseline, facts, tombstones: [], rows,
    previousSummary: { text: summaryText, ...provenance }, customFocus: focus };
assert.equal(retention.select(request, { ...candidate, evidenceRef: null }, budget).reason, 'missing-boundary-evidence');
assert.equal(retention.select(request, { ...candidate, evidenceRef: {
    ...candidate.evidenceRef, source: { ...baseline, cursor: 'old-cursor' },
} }, budget).reason, 'stale-boundary-evidence');
assert.equal(retention.select({ ...request, facts: [{ ...facts[0], sourceRevision: 's-20' }] }, candidate, budget).reason, 'stale-fact');
assert.equal(retention.select(request, { ...candidate, source: { ...baseline, goalId: 'goal-10', goalRevision: 'revision-10' } }, budget).reason, 'stale-source');
assert.throws(() => retention.select({ ...request, facts: [{ ...facts[0], grant: 'private' }] }, candidate, budget), /Invalid retained fact/);
assert.throws(() => retention.select({ ...request, facts: [{ ...facts[0], kind: 'launch-grant' }] }, candidate, budget), /Unsupported fact kind/);
assert.throws(() => retention.select({ ...request, facts: [{ ...facts[0], text: '\ud800' }] }, candidate, budget), /Invalid fact text/);
assert.equal(retention.select({ ...request, facts: [], rows: rows.slice(0, 2) }, candidate, budget).reason, 'incomplete-tool-pair');
assert.equal(retention.select({ ...request, facts: [], rows: [rows[2]] }, candidate, budget).reason, 'incomplete-tool-pair');
assert.equal(retention.select({ ...request, facts: [], rows: [rows[1], { ...rows[2], toolCallId: 'edit/2' }] }, candidate, budget).reason, 'incomplete-tool-pair');
assert.throws(() => retention.select({ ...request, facts: [], rows: Array(257).fill(rows[0]) }, candidate, budget), /Invalid bounded retention snapshot/);

// Repro from independent review: same session revision, but a new canonical
// goal. Neither the old registry reference nor old narrative may be selected.
const replacedGoal = { ...baseline, goalId: 'goal-10', goalRevision: 'revision-10' };
const replacedCandidate = trigger.evaluate(observedFor(replacedGoal), replacedGoal);
const newGoalFact = { ...facts[0], goalId: 'goal-10', goalRevision: 'revision-10',
    evidenceRef: { store: 'registry', goalId: 'goal-10', revision: 'revision-10' } };
assert.equal(retention.select({ ...request, source: replacedGoal }, replacedCandidate, budget).reason, 'stale-fact');
assert.equal(retention.select({ ...request, source: replacedGoal, facts: [newGoalFact], rows: [] }, replacedCandidate, budget).reason, 'stale-previousSummary');
assert.equal(retention.select({ ...request, source: replacedGoal, facts: [{ ...newGoalFact,
    evidenceRef: { store: 'registry', goalId: 'goal-9', revision: 'revision-9' } }],
    rows: [], previousSummary: null, customFocus: null }, replacedCandidate, budget).reason, 'stale-goal-reference');
assert.throws(() => retention.select({ ...request, source: replacedGoal, facts: [{ ...newGoalFact,
    evidenceRef: 'registry:goal-9' }], rows: [], previousSummary: null, customFocus: null },
    replacedCandidate, budget), /Invalid goal evidence/);
assert.equal(retention.select({ ...request, source: replacedGoal, facts: [newGoalFact], rows: [], previousSummary: 'Old goal X remains active' }, replacedCandidate, budget).reason, 'unproven-previousSummary');
assert.equal(retention.select({ ...request, source: replacedGoal, facts: [newGoalFact], rows: [], previousSummary: null }, replacedCandidate, budget).reason, 'stale-customFocus');
assert.equal(retention.select({ ...request, source: replacedGoal, facts: [newGoalFact], previousSummary: null,
    customFocus: null }, replacedCandidate, budget).reason, 'stale-row');

// A later correction under the SAME goal invalidates the old summary/focus
// without looking for words such as "old" or "goal" inside the narrative.
const corrected = { ...baseline, correctionRevision: 'correction-2' };
const correctionCandidate = trigger.evaluate(observedFor(corrected), corrected);
const revisedFacts = facts.map(fact => ({ ...fact, correctionRevision: 'correction-2' }));
assert.equal(retention.select({ ...request, source: corrected }, correctionCandidate, budget).reason, 'stale-fact');
assert.equal(retention.select({ ...request, source: corrected, facts: revisedFacts, rows: [] }, correctionCandidate, budget).reason, 'stale-previousSummary');
assert.equal(retention.select({ ...request, source: corrected, facts: revisedFacts, rows: [], previousSummary: null }, correctionCandidate, budget).reason, 'stale-customFocus');
assert.equal(retention.select({ ...request, source: corrected, facts: revisedFacts, rows: [],
    previousSummary: null, customFocus: focus.text }, correctionCandidate, budget).reason, 'unproven-customFocus');

// Deleting a fact advances the owner correction/deletion revision: an old
// summary cannot re-introduce that fact. A current tombstone then filters it.
const deleted = { ...baseline, correctionRevision: 'correction-3' };
const deleteCandidate = trigger.evaluate(observedFor(deleted), deleted);
const projected = facts.map(fact => ({ ...fact, correctionRevision: 'correction-3' }));
assert.equal(retention.select({ ...request, source: deleted, facts: projected, rows: [] }, deleteCandidate, budget).reason, 'stale-previousSummary');
const deletion = retention.select({ ...request, source: deleted, facts: projected,
    tombstones: ['old-decision'], rows: [], previousSummary: null, customFocus: null }, deleteCandidate, budget);
assert.equal(deletion.kind, 'selection');
assert.ok(!deletion.facts.some(fact => fact.id === 'old-decision'));
assert.equal(deletion.previousSummary, '');
const withoutGoal = { ...baseline, goalId: null, goalRevision: null };
const withoutGoalCandidate = trigger.evaluate(observedFor(withoutGoal), withoutGoal);
assert.equal(retention.select({ ...request, source: withoutGoal,
    facts: [{ ...facts[0], goalId: null, goalRevision: null }], rows: [], previousSummary: null,
    customFocus: null }, withoutGoalCandidate, budget).reason, 'goal-unavailable');
const staleRow = { ...request, facts: [], rows: [{ ...rows[0], correctionRevision: 'correction-0' }],
    previousSummary: null, customFocus: null };
assert.equal(retention.select(staleRow, candidate, budget).reason, 'stale-row');
assert.equal(retention.select({ ...request, facts: [], rows: [], previousSummary: {
    text: summaryText, goalId: provenance.goalId, goalRevision: provenance.goalRevision,
} }, candidate, budget).reason, 'stale-previousSummary');

const tightBudget = { model: { contextWindow: 12000 }, reserveTokens: 4000 };
const huge = { ...request, facts: [{ ...facts[0], text: 'exact '.repeat(1400) }],
    previousSummary: { text: 'SUMMARY'.repeat(1400), ...provenance } };
assert.equal(retention.select(huge, candidate, tightBudget).reason, 'budget-exceeded');
assert.deepEqual(huge.facts[0].text, 'exact '.repeat(1400));
assert.throws(() => retention.select(request, candidate, { ...budget, reserveTokens: '16384' }), /Invalid retention model reserve/);
assert.throws(() => retention.budgetFor({ contextWindow: '128000' }, 16384), /Invalid retention model reserve/);
console.log('adaptive compaction contracts PASS: provenance fences, independent hard path, exact references, atomic tool pairs, no partial budget');
