// Provider-free PR #48 contract fixtures; no native Pi copy or RPC invocation.
import assert from 'node:assert/strict';
import { CompactionPolicy } from './native-compaction-policy.mjs';
import { TriggerRule, RetentionPolicy } from './adaptive-compaction-contracts.mjs';

const baseline = { cursor: 'seq-314', sessionRevision: 's-21', ownerEpoch: 'owner-3',
    turnId: 'turn-5', goalRevision: 'goal-9' };
const trigger = new TriggerRule();
const observed = { source: baseline, boundary: 'completed-subtask',
    boundaryEvidenceRef: 'transcript:314', newHistoryBytes: 22000, hardBackstopDue: false };
const savedInput = structuredClone(observed);
const candidate = trigger.evaluate(observed, baseline);
assert.equal(candidate.kind, 'candidate');
assert.equal(candidate.authority, 'none');
assert.equal(candidate.evidenceRef, 'transcript:314');
assert.notEqual(candidate.source, baseline);
assert.ok(Object.isFrozen(candidate.source) && Object.isFrozen(candidate));
assert.equal(trigger.stillCurrent(candidate, baseline), true);
for (const changed of [
    { cursor: 'seq-315' }, { sessionRevision: 's-22' }, { ownerEpoch: 'owner-4' },
    { turnId: 'turn-6' }, { goalRevision: 'goal-10' },
]) {
    assert.equal(trigger.evaluate(observed, { ...baseline, ...changed }).reason, 'stale-source');
    assert.equal(trigger.stillCurrent(candidate, { ...baseline, ...changed }), false);
}
assert.equal(trigger.evaluate({ ...observed, boundary: 'unfinished', boundaryEvidenceRef: null }, baseline).reason, 'unfinished-work');
assert.equal(trigger.evaluate({ ...observed, boundary: 'none', boundaryEvidenceRef: null }, baseline).reason, 'unfinished-work');
assert.equal(trigger.evaluate({ ...observed, newHistoryBytes: 16383 }, baseline).reason, 'cadence');
assert.equal(trigger.evaluate({ ...observed, hardBackstopDue: true }, baseline).kind, 'independent-hard-path');
assert.equal(trigger.evaluate({ ...observed, hardBackstopDue: true }, { ...baseline, ownerEpoch: 'stale' }).kind, 'independent-hard-path');
assert.throws(() => trigger.evaluate({ ...observed, hiddenGrant: 'secret' }, baseline), /Invalid trigger snapshot/);
assert.throws(() => trigger.evaluate({ ...observed, boundaryEvidenceRef: '\ud800' }, baseline), /Invalid boundary evidence/);
assert.throws(() => new TriggerRule({ minNewHistoryBytes: 1 }), /Invalid trigger cadence/);
assert.deepEqual(observed, savedInput, 'evaluation must not mutate owner state');

const nativePolicy = new CompactionPolicy({ strategy: 'serial' });
const retention = new RetentionPolicy({ recentGroups: 4, compactionPolicy: nativePolicy });
const budget = { model: { contextWindow: 128000 }, reserveTokens: 16384 };
assert.equal(retention.budgetFor(budget.model, budget.reserveTokens), nativePolicy.inputBytes(budget.model, budget.reserveTokens));
assert.throws(() => new RetentionPolicy({ recentGroups: 0 }), /recent window/);
assert.throws(() => new RetentionPolicy({ compactionPolicy: {} }), /declared compaction policy/);
const facts = [
    { id: 'goal', kind: 'goal-reference', text: 'continue task', evidenceRef: 'registry:goal-9', sourceRevision: 's-21' },
    { id: 'old-decision', kind: 'task', text: 'use obsolete path', evidenceRef: 'transcript:101', sourceRevision: 's-21' },
    { id: 'correction', kind: 'correction', text: 'use src/new.py instead', evidenceRef: 'message:310', sourceRevision: 's-21' },
    { id: 'unresolved', kind: 'failure', text: 'test timeout remains unresolved', evidenceRef: 'test:313', sourceRevision: 's-21' },
    { id: 'identifier', kind: 'identifier', text: 'sha256:aabbcc', evidenceRef: 'file:312', sourceRevision: 's-21' },
];
const rows = [
    { id: 'r1', kind: 'user', text: 'older context' },
    { id: 'r2', kind: 'tool_call', toolCallId: 'edit/1', text: 'edit src/new.py' },
    { id: 'r3', kind: 'tool_result', toolCallId: 'edit/1', text: 'success' },
    { id: 'r4', kind: 'assistant', text: 'revised implementation' },
    { id: 'r5', kind: 'user', text: 'correct the test path' },
];
let previousSummary = 'PRIOR_SUMMARY_EXACT_PATH_src/old.py';
for (let round = 1; round <= 3; round++) {
    const snapshot = { source: baseline, facts, tombstones: ['old-decision'], rows,
        previousSummary, customFocus: 'KEEP_CORRECTIONS_AND_TOOL_PAIRS' };
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
    assert.equal(result.previousSummary, previousSummary);
    assert.equal(result.customFocus, 'KEEP_CORRECTIONS_AND_TOOL_PAIRS');
    assert.ok(Object.isFrozen(result) && Object.isFrozen(result.recent));
    assert.deepEqual(snapshot, copied);
    previousSummary = `ROUND_${round}_SUMMARY ${result.previousSummary}`;
}
// A repeated split turn has a saved summary but no newly added history rows.
const emptyHistory = retention.select({ source: baseline, facts: [], tombstones: [], rows: [],
    previousSummary, customFocus: 'KEEP_CORRECTIONS_AND_TOOL_PAIRS' }, candidate, budget);
assert.equal(emptyHistory.kind, 'selection');
assert.equal(emptyHistory.previousSummary, previousSummary);
assert.deepEqual(emptyHistory.recent, []);
const staleFact = { ...facts[0], sourceRevision: 's-20' };
const request = { source: baseline, facts: [staleFact], tombstones: [], rows: [],
    previousSummary: '', customFocus: '' };
assert.equal(retention.select(request, candidate, budget).reason, 'stale-fact');
assert.equal(retention.select({ ...request, facts }, { ...candidate, source: { ...baseline, goalRevision: 'goal-10' } }, budget).reason, 'stale-source');
assert.throws(() => retention.select({ ...request, facts: [{ ...facts[0], grant: 'private' }] }, candidate, budget), /Invalid retained fact/);
assert.throws(() => retention.select({ ...request, facts: [{ ...facts[0], kind: 'launch-grant' }] }, candidate, budget), /Unsupported fact kind/);
assert.throws(() => retention.select({ ...request, facts: [{ ...facts[0], text: '\ud800' }] }, candidate, budget), /Invalid fact text/);
assert.equal(retention.select({ ...request, facts: [], rows: rows.slice(0, 2) }, candidate, budget).reason, 'incomplete-tool-pair');
assert.equal(retention.select({ ...request, facts: [], rows: [rows[2]] }, candidate, budget).reason, 'incomplete-tool-pair');
assert.equal(retention.select({ ...request, facts: [], rows: [rows[1], { ...rows[2], toolCallId: 'edit/2' }] }, candidate, budget).reason, 'incomplete-tool-pair');
assert.throws(() => retention.select({ ...request, facts: [], rows: Array(257).fill(rows[0]) }, candidate, budget), /Invalid bounded retention snapshot/);
const tightBudget = { model: { contextWindow: 12000 }, reserveTokens: 4000 };
const huge = { ...request, facts: [{ ...facts[0], text: 'exact '.repeat(1400) }],
    previousSummary: 'SUMMARY'.repeat(1400) };
assert.equal(retention.select(huge, candidate, tightBudget).reason, 'budget-exceeded');
assert.deepEqual(huge.facts[0].text, 'exact '.repeat(1400));
assert.throws(() => retention.select(request, candidate, { ...budget, reserveTokens: '16384' }), /Invalid retention model reserve/);
assert.throws(() => retention.budgetFor({ contextWindow: '128000' }, 16384), /Invalid retention model reserve/);
console.log('adaptive compaction contracts PASS: pure fences, independent hard path, exact references, atomic tool pairs, no partial budget');
