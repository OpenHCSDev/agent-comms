"""Final controls for unavailable evidence, exact SDK bytes and scoped loss.

Authored records exercise measurement plumbing only, never model recall.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_comms.comms import Comms
from agent_comms.compaction_identity import SummaryOperationIdentity
from agent_comms.compaction_records import NativeForkCreation, SelectedSummaryAttempt
from agent_comms.compaction_states import ReservedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.native_compaction_request import NativeSummaryPayload
from agent_comms.native_entries import MessageEntry, NativeEntry
from agent_comms.native_input_record import NativeInputCommit
from agent_comms.native_pi import NativeContextRecord, NativePiUnavailable
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_turn_context import NativeContextData
from agent_comms.native_tools import ReadTool, WriteTool
from agent_comms.pi_summary_payloads import SummaryCost, SummaryUsage
from agent_comms.pi_payloads import AssistantMessage, PiCost, PiUsage, ToolCallContent, ToolResultMessage, UserMessage
from agent_comms.request_progress import RequestProgress
from agent_comms.private_path import FileRevision
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.text_digest import TextDigest
from agent_comms.pi_payloads import ReportedModel
from agent_comms.turn_lease import TurnLeaseFence
from agent_comms.retained_task_facts import GoalTaskFact, HumanConstraintTaskFact, RetainedTaskFacts
from agent_comms.task_sources import CorrectionTaskChange, Decision, UserTaskDrop
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.threads import Thread
from agent_comms.turn_context import (
    ContextManifest, FileProvenance, JournalProvenance, NativeProvenance, RecordedContextTurn,
    SegmentManifest, SystemLayerSegment, TranscriptSegment, InjectionMessageSegment, ToolCatalogSegment,
)
from compaction_retention_fixture import Condition, Measurement, PairedRecallDesign, Question, RecallRound, RecallScenario, RecordedAnswers, RecordedNativeProbes, ScoredScenario, coding_scenario
from retained_native_fixture import RecordedConditionInstallation, RecordedNativeCheckpoint, RecordedNativeProbe
from selected_summary_cases import manual_summary_record
from test_task_decisions import admit


class RecordedMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.session = self.root / 'original.jsonl'
        self.session.write_text('original fixture source\n')
        self.identity = NativeSessionIdentity('fixture-session', str(self.session))
        self.checkpoint = RecordedNativeCheckpoint(
            self.root / 'journal.sqlite3', SummaryOperationIdentity(str(self.session), 'operation'), 'commit'
        )

    def artifact(self, name, value):
        path = self.root / name
        raw = json.dumps(FieldCodec.encode(value), ensure_ascii=False).encode()
        path.write_bytes(raw)
        return FileProvenance(str(path), hashlib.sha256(raw).hexdigest())

    def test_construction_rounds_preserve_prefix_and_public_probe_boundary(self):
        # Prevent cumulative source from being resent at each cut, edited
        # prefix from being silently reconstructed, and oracle answer leakage.
        scenarios = (coding_scenario(), *(RecallScenario.read(Path(__file__).parent /
            'fixtures/retention' / f'{name}.json') for name in ('research', 'goal')))
        for scenario in scenarios:
            with self.subTest(scenario=scenario.identity):
                history = ()
                for round_, operands in zip(scenario.rounds, scenario.construction_rounds()):
                    history += operands['history_additions']
                    self.assertEqual(history, round_.history)
                    self.assertEqual(operands['round'], round_.identity)
                    self.assertEqual(operands['source_text'], '\n'.join(operands['history_additions']))
                    self.assertEqual(operands['probe_text'], round_.probe_text())
                    self.assertEqual(json.loads(operands['probe_text'].split('\n', 1)[1]),
                        {'round': round_.identity, 'questions': [q.public() for q in round_.questions]})
                with self.assertRaisesRegex(ValueError, 'preceding frozen history prefix'):
                    scenario.rounds[-1].history_after(('edited prior source',))
                shortened = replace(scenario.rounds[-1], history=scenario.rounds[0].history)
                with self.assertRaises(ValueError):
                    shortened.history_after(scenario.rounds[-2].history)

    def test_construction_plan_uses_original_oracle_and_separate_sampling_seed(self):
        # The prospective plan is derived from the exact frozen oracle once;
        # arm ordering must not accidentally depend on inference parameters.
        scenario = coding_scenario()
        original = self.artifact('construction-oracle.json', scenario)
        design = PairedRecallDesign(original, Condition.TASK_MEMORY, Condition.BOUNDED,
            'openai-codex/gpt-6.1-sol', 10, 0.95, -0.02, 10000, 20261004)
        with patch.object(RecordedNativeCheckpoint, 'read_record',
                          wraps=RecordedNativeCheckpoint.read_record) as acquired:
            plan = design.construction_plan(17)
            self.assertEqual(acquired.call_count, 1)
        self.assertEqual(plan['rounds'], scenario.construction_rounds())
        self.assertIs(plan['comparison_design'], design)
        self.assertEqual(tuple(item['sample'] for item in plan['trajectories']), tuple(range(1, 11)))
        for item in plan['trajectories']:
            self.assertEqual(set(item['condition_order']), {design.candidate, design.baseline})
        self.assertEqual(plan, design.construction_plan(17))
        self.assertEqual(plan['trajectories'],
            replace(design, bootstrap_seed=73).construction_plan(17)['trajectories'])
        self.assertNotEqual(plan['trajectories'], design.construction_plan(18)['trajectories'])
        self.assertEqual(FieldCodec.encode(plan)['comparison_design']['oracle'], FieldCodec.encode(original))
        Path(original.path).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            design.construction_plan(17)

    def test_stimulus_delivery_requires_exact_source_and_original_earlier_branch(self):
        # Detect edits, later source and sibling ancestry being credited as
        # delivered construction; native input/terminal corroboration is read's
        # existing obligation, not fabricated by this relation control.
        rows = ({'type': 'session', 'id': self.identity.session_id},
                {'type': 'message', 'id': 'source',
                 'message': {'role': 'user', 'content': 'Source: λ/path'}},
                {'type': 'message', 'id': 'answer', 'parentId': 'source',
                 'message': {'role': 'assistant', 'content': [], 'stopReason': 'stop'}},
                {'type': 'message', 'id': 'boundary', 'parentId': 'answer',
                 'message': {'role': 'user', 'content': 'Recall?'}},
                {'type': 'message', 'id': 'sibling', 'parentId': 'source',
                 'message': {'role': 'user', 'content': 'Unrelated branch'}})
        self.session.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        self.session.chmod(0o600)
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        original = {'prompt': 'Source: λ/path', 'native_input':
            NativeInputCommit(probe.input_id, self.identity.session_id, 'source'),
            'submitted_source': {'scope': 'original native user text'}}
        with NativeEntry.open_evidence(self.session) as evidence:
            observed = probe.source_delivery(original['prompt'], original, evidence, 'boundary', None)
            self.assertTrue(observed['evaluated'])
            self.assertIs(observed['input'], original['native_input'])
            with self.assertRaisesRegex(ValueError, 'frozen authored source'):
                probe.source_delivery('Edited source', original, evidence, 'boundary', None)
            for boundary in ('source', 'answer', 'sibling'):
                with self.subTest(boundary=boundary):
                    with self.assertRaisesRegex(ValueError, 'does not precede'):
                        probe.source_delivery(original['prompt'], original, evidence, boundary, None)

    def test_stimulus_inheritance_requires_existing_exact_sdk_prefix(self):
        # Equal copied IDs are insufficient: authenticate child position/bytes
        # through NativeForkCreation, then use the reader's original branch.
        child = self.root / 'child.jsonl'
        rows = ({'type': 'session', 'id': 'child', 'parentSession': self.identity.session_file},
                {'type': 'message', 'id': 'source',
                 'message': {'role': 'user', 'content': 'Source: λ/path'}},
                {'type': 'message', 'id': 'answer', 'parentId': 'source',
                 'message': {'role': 'assistant', 'content': [], 'stopReason': 'stop'}})
        prefix = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
        child.write_text(prefix)
        child.chmod(0o600)
        fork = NativeForkCreation(session_id='child', session_file=str(child), source=self.identity,
            source_revision=FileRevision.from_stat(self.session.stat()),
            revision=FileRevision.from_stat(child.stat()), prefix_digest=TextDigest.of(prefix), entry_count=3)
        with child.open('a') as stream:
            stream.write(json.dumps({'type': 'message', 'id': 'boundary', 'parentId': 'answer',
                'message': {'role': 'user', 'content': 'Recall?'}}) + '\n')
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        original = {'prompt': 'Source: λ/path', 'native_input':
            NativeInputCommit(probe.input_id, self.identity.session_id, 'source'),
            'submitted_source': {'scope': 'original native user text'}}
        with NativeEntry.open_evidence(child) as evidence:
            self.assertTrue(probe.source_delivery(original['prompt'], original,
                evidence, 'boundary', fork)['evaluated'])
            with self.assertRaisesRegex(ValueError, 'SDK ancestry'):
                probe.source_delivery(original['prompt'], original, evidence, 'boundary', None)
            with self.assertRaises(ValueError):
                probe.source_delivery(original['prompt'], original, evidence, 'boundary',
                    replace(fork, source=NativeSessionIdentity('other', str(self.root / 'other.jsonl'))))
            with self.assertRaises(ValueError):
                probe.source_delivery(original['prompt'], original, evidence, 'boundary',
                    replace(fork, entry_count=2))
            with self.assertRaisesRegex(NativePiUnavailable, 'original prefix changed'):
                probe.source_delivery(original['prompt'], original, evidence, 'boundary',
                    replace(fork, prefix_digest=TextDigest.of('edited prefix')))

    def test_stimulus_group_uses_scenario_source_and_same_acquired_reader(self):
        # Close run/scenario/reader consumers without substituting a native
        # proof. Spies are observation plumbing only; source_delivery above
        # exercises the original byte/branch owners.
        self.session.write_text(json.dumps({'type': 'session', 'id': self.identity.session_id}) + '\n')
        self.session.chmod(0o600)
        scenario = coding_scenario()
        source = RecordedNativeProbe(self.identity, 'a' * 32, 'source-answer')
        probe = replace(source, input_id='b' * 32, answer_entry_id='probe-answer')
        run = RecordedNativeProbes({'r1': probe}, stimuli={'r1': source})
        operand = scenario.construction_rounds()[0]['source_text']
        readers = []

        def read(selected, evidence, parent):
            self.assertIs(evidence, parent)
            readers.append(evidence)
            return {'prompt': operand if selected == source else scenario.rounds[0].probe_text(),
                    'native_input': NativeInputCommit(selected.input_id, selected.session.session_id,
                                                      'source' if selected == source else 'probe'),
                    'construction': {'fork': None}}

        def delivery(selected, expected, original, evidence, boundary, fork):
            self.assertIs(selected, source)
            self.assertEqual(expected, operand)
            self.assertIs(evidence, readers[0])
            self.assertEqual(boundary, 'probe')
            self.assertIsNone(fork)
            return {'evaluated': True}

        with patch.object(RecordedNativeProbe, 'read', read), patch.object(
                RecordedNativeProbe, 'source_delivery', delivery):
            cuts, probes, stimuli = run.observe(scenario)
        self.assertEqual(cuts, {})
        self.assertEqual(probes['r1']['prompt'], scenario.rounds[0].probe_text())
        self.assertTrue(stimuli['r1']['source_delivery']['evaluated'])
        self.assertTrue(all(reader.source.stream.closed for reader in readers))
        with patch.object(RecordedNativeProbe, 'read', read):
            cuts, probes, stimuli = replace(run, rounds={}).observe(scenario)
        self.assertFalse(stimuli['r1']['source_delivery']['evaluated'])
        self.assertEqual(FieldCodec.decode(RecordedNativeProbes, FieldCodec.encode(run)), run)

    def test_group_reader_shared_once_and_closed_after_second_arm_refuses(self):
        # Resource plumbing only: detect duplicated acquisition and a leaked
        # reader when the later arm refuses. This does not simulate native proof.
        self.session.write_text(json.dumps({'type': 'session', 'id': self.identity.session_id}) + '\n')
        self.session.chmod(0o600)
        probes = tuple(RecordedNativeProbe(self.identity, digit * 32, 'answer')
                       for digit in ('a', 'b'))
        stimulus = replace(probes[0], input_id='c' * 32)
        runs = (RecordedNativeProbes({'r1': probes[0]}, stimuli={'r2': stimulus}),
                RecordedNativeProbes({'r1': probes[1]}))
        readers = []
        refusal = ValueError('second original arm refused')

        def read(probe, evidence, source):
            self.assertIs(source, evidence)
            self.assertEqual(evidence.observe()[0].id, self.identity.session_id)
            readers.append(evidence)
            if probe == probes[1]:
                raise refusal
            return {}

        with patch.object(NativeEntry, 'open_evidence', wraps=NativeEntry.open_evidence) as opened:
            with patch.object(RecordedNativeProbe, 'read', read):
                with self.assertRaises(ValueError) as failed:
                    RecordedNativeProbes.observe_runs(runs, coding_scenario())
            self.assertEqual(opened.call_count, 1)
        self.assertIs(failed.exception, refusal)
        self.assertIs(readers[0], readers[1])
        self.assertIs(readers[1], readers[2])
        self.assertTrue(readers[0].source.stream.closed)
        self.assertFalse(readers[0].entries)

    def test_group_round_membership_refuses_before_reader_acquisition(self):
        # Reject unknown original inputs at their owner, before opening either arm.
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        runs = (RecordedNativeProbes({}), RecordedNativeProbes({}, stimuli={'foreign': probe}))
        with patch.object(NativeEntry, 'open_evidence') as opened:
            with self.assertRaisesRegex(ValueError, 'Unknown native rounds'):
                RecordedNativeProbes.observe_runs(runs, coding_scenario())
        opened.assert_not_called()

    def test_original_reader_group_borrows_parent_once_and_retires_on_consumer_error(self):
        # Prevent sibling-source acquisition from replacing inheritance with
        # path equality, reopening shared parents or leaking refused readers.
        self.session.write_text(json.dumps({'type': 'session', 'id': self.identity.session_id}) + '\n')
        self.session.chmod(0o600)
        child = self.root / 'child.jsonl'
        child.write_text(json.dumps({'type': 'session', 'id': 'child'}) + '\n')
        child.chmod(0o600)
        probe = RecordedNativeProbe(NativeSessionIdentity('child', str(child)),
                                    'a' * 32, 'answer', self.checkpoint)
        refusal = OSError('consumer failure, not acquisition failure')
        with self.assertRaises(OSError) as failed:
            with RecordedNativeProbe.original_readers((probe, probe), (self.checkpoint,)) as sources:
                self.assertEqual(set(sources), {self.session, child})
                readers = tuple(sources.values())
                self.assertEqual(sources[probe.checkpoint_source].observe()[0].id, self.identity.session_id)
                self.assertEqual(sources[child].observe()[0].id, 'child')
                raise refusal
        self.assertIs(failed.exception, refusal)
        self.assertTrue(all(reader.source.stream.closed and not reader.entries for reader in readers))

    def test_recorded_batch_refuses_reused_originals_not_just_changed_labels(self):
        # Prevent duplicate journals/inputs from inflating the sample count.
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        run = RecordedNativeProbes({'r1': probe})
        with self.assertRaisesRegex(ValueError, 'same original input'):
            RecordedNativeProbes({'r1': probe, 'r2': probe})
        with self.assertRaisesRegex(ValueError, 'same original input'):
            RecordedNativeProbes({'r1': probe}, stimuli={'r1': probe})
        with self.assertRaisesRegex(ValueError, 'reuse original'):
            RecordedNativeProbes.require_distinct((run, run))
        changed = RecordedNativeProbes({'r1': replace(probe, input_id='b' * 32)})
        with self.assertRaisesRegex(ValueError, 'reuse original'):
            RecordedNativeProbes.require_distinct((run, changed))
        different = RecordedNativeProbes({'r1': replace(probe,
            session=NativeSessionIdentity('different', str(self.root / 'different.jsonl')),
            input_id='b' * 32)})
        RecordedNativeProbes.require_distinct((run, different))
        with self.assertRaisesRegex(ValueError, 'at least one pair'):
            coding_scenario().compare_native_pairs(Condition.TASK_MEMORY, (), Condition.BOUNDED)
        wire = FieldCodec.encode(((run, different),))
        self.assertEqual(FieldCodec.decode(tuple[tuple[RecordedNativeProbes, RecordedNativeProbes], ...], wire),
                         ((run, different),))

    def test_paired_recall_uses_recall_questions_and_keeps_trajectory_denominators(self):
        # Equal total scores can conceal recall loss behind prohibition/action
        # answers. One cut is not an independent sample; assistance/absence must
        # not silently remove a trajectory from the batch recall denominator.
        questions = (Question('recall', 'Which source?', 'source', 'oracle'),
                     Question('rule', 'Replay?', 'no', 'oracle', measurement=Measurement.PROHIBITION),
                     Question('alternative', 'Other choice?', 'other', 'oracle', measurement=Measurement.ALTERNATIVE))
        scenario = RecallScenario('mixed', (RecallRound('r1', ('Source: source; replay: no; alternative: other.',), questions),))
        candidate = scenario.score(Condition.TASK_MEMORY, RecordedAnswers({'r1': {
            'recall': 'wrong', 'rule': 'no', 'alternative': 'other'}}))
        control = scenario.score(Condition.BOUNDED, RecordedAnswers({'r1': {
            'recall': 'source', 'rule': 'wrong', 'alternative': 'other'}}))
        unassisted = {'r1': {'answer_support': {'unassisted_recall': True}}}
        assisted = {'r1': {'answer_support': {'unassisted_recall': False}}}
        alignment = {'r1': {'evaluated': True}}
        lost = candidate.paired_quality(control, unassisted, unassisted, alignment)
        equal = control.paired_quality(control, unassisted, unassisted, alignment)
        self.assertEqual(lost['unassisted_recall']['correct_difference'], 0)
        self.assertEqual(lost['unassisted_recall']['recall_rate_difference']['value'], -1)
        pairs = ({'paired_quality': lost}, {'paired_quality': equal})
        batch = ScoredScenario.paired_batch(pairs)['unassisted_recall']
        self.assertEqual(batch['trajectories'], 2)
        self.assertEqual(batch['mean_recall_rate_difference']['value'], -0.5)
        mixed = control.paired_quality(control, unassisted, assisted, alignment)
        unavailable = ScoredScenario.paired_batch((pairs[0], {'paired_quality': mixed}))['unassisted_recall']
        self.assertEqual(unavailable['recall_trajectories'], 1)
        self.assertFalse(unavailable['mean_recall_rate_difference']['evaluated'])
        self.assertIsNone(unavailable['mean_recall_rate_difference']['value'])

    def test_paired_inference_keeps_complete_samples_and_original_model_selection(self):
        # Detect selection of only favorable/assisted subsets, cut-level
        # pseudoreplication, and admission models replaced by registry labels.
        oracle = FileProvenance(str(self.root / 'oracle.json'), '0' * 64)
        design = PairedRecallDesign(oracle, Condition.TASK_MEMORY, Condition.BOUNDED,
            'original/model', 2, 0.95, -0.02, 1000, 17)
        scenario = RecallScenario('paired', (RecallRound('r1', ('source',), (
            Question('q', 'Source?', 'source', 'oracle'),)),))
        correct = scenario.score(Condition.BOUNDED, RecordedAnswers({'r1': {'q': 'source'}}))
        lost = scenario.score(Condition.TASK_MEMORY, RecordedAnswers({'r1': {'q': 'lost'}}))
        support = {'r1': {'answer_support': {'unassisted_recall': True}}}
        progress = RequestProgress('request', 'session', 'input', 1, 2, '3',
            1, 0, 0, 0, 'budget_admission', model=ReportedModel(provider='original', id='model'))
        alignment = {'r1': {'evaluated': True, 'request_selection': {
            'candidate': {'observations': (progress,)}}}}

        def comparison(candidate, evidence=support):
            return {'candidate': candidate.public(), 'alignment': alignment,
                'paired_quality': candidate.paired_quality(correct, evidence, support, alignment)}

        pairs = (comparison(lost), comparison(correct))
        result = ScoredScenario.paired_inference(pairs, design)
        self.assertEqual((result['mean_recall_rate_difference'], result['lower'], result['upper']),
                         (-0.5, -1, 0))
        self.assertFalse(result['meets_recall_margin'])
        assisted = comparison(correct, {'r1': {'answer_support': {'unassisted_recall': False}}})
        for incomplete in (assisted, dict(pairs[1], alignment={'r1': {'evaluated': False}})):
            missing = ScoredScenario.paired_inference((pairs[0], incomplete), design)
            self.assertFalse(missing['evaluated'])
            self.assertNotIn('lower', missing)
        with self.assertRaisesRegex(ValueError, 'model does not match'):
            ScoredScenario.paired_inference(pairs, replace(design, model='another/model'))
        with self.assertRaisesRegex(ValueError, 'pair count'):
            ScoredScenario.paired_inference(pairs[:1], design)

    def test_supplied_design_binds_original_oracle_and_rejects_invalid_parameters(self):
        # Detect changed oracle bytes or an omitted sample before native reads;
        # decoding a supplied file is never a preregistration/approval grant.
        path = self.root / 'oracle.json'
        raw = json.dumps(FieldCodec.encode(coding_scenario())).encode()
        path.write_bytes(raw)
        design = PairedRecallDesign(FileProvenance(str(path), hashlib.sha256(raw).hexdigest()),
            Condition.TASK_MEMORY, Condition.BOUNDED, 'original/model', 2, 0.95, -0.02, 1000, 17)
        self.assertEqual(FieldCodec.decode(PairedRecallDesign, FieldCodec.encode(design)), design)
        with self.assertRaisesRegex(ValueError, 'pair count'):
            design.compare(())
        path.write_bytes(raw + b' ')
        with self.assertRaisesRegex(ValueError, 'changed'):
            design.compare((None, None))
        for changed in ({'sample_count': 1}, {'model': ''}, {'confidence': float('nan')},
                        {'recall_margin': float('inf')}, {'bootstrap_samples': 2},
                        {'baseline': design.candidate}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                replace(design, **changed)

    def test_admitted_budget_requires_original_request_and_turn_correlation(self):
        # Prevent same-input/time guesses and historical capacity fabrication.
        owner = Thread('original', frozenset(), str(self.root), created_at=12)
        turn = RecordedContextTurn(TurnId('turn'), TurnIdentity(owner.incarnation, 1))
        manifest = ContextManifest(owner.incarnation, turn, (), 'counter', request_id='request')
        lease = TurnLeaseFence(turn.occurrence, turn.identity.value, 3)
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        observed = RequestProgress('request', self.identity.session_id, probe.input_id,
            1, 2, '3', 1, 0, 0, 0, 'budget_admission',
            model=ReportedModel(provider='original', id='model', context_window=100, max_tokens=20),
            estimated_input_tokens=80, available_tokens=20, output_token_field='max_tokens',
            requested_output_tokens=50, admitted_output_tokens=20, minimum_output_tokens=1)

        def publication(progress, fence=lease):
            return {'turn': FieldCodec.encode(fence), 'native': FieldCodec.encode(progress)}

        def capture(records):
            path = self.root / 'requests.jsonl'
            raw = ''.join(json.dumps(item) + '\n' for item in records).encode()
            path.write_bytes(raw)
            return replace(probe, request_observations=FileProvenance(str(path), hashlib.sha256(raw).hexdigest()))

        revised = replace(observed, requested_output_tokens=20, admitted_output_tokens=10)
        selected = capture(({'acquisition': {}}, publication(replace(observed, request_id='other')),
                            publication(observed), publication(revised)))
        observations = selected.observed_request(manifest)
        result = selected.request_budget(observations)
        self.assertTrue(result['evaluated'])
        self.assertEqual(result['observations'], (observed, revised))
        self.assertFalse(selected.request_budget(selected.observed_request(replace(manifest, request_id=None)))['evaluated'])
        self.assertFalse(probe.request_budget(probe.observed_request(manifest))['evaluated'])
        for changed in (replace(observed, session_id='other'), replace(observed, input_id='b' * 32)):
            with self.assertRaises(ValueError):
                capture((publication(changed),)).observed_request(manifest)
        with self.assertRaises(ValueError):
            capture((publication(observed, replace(lease, turn_id='other')),)).observed_request(manifest)

        # Timing and budget borrow the same acquisition. Different stages and
        # real zero callback counters must not be lost or become whole-turn time.
        headers = replace(observed, stage='headers', elapsed_ms=35, observed_at_ms=36)
        first = replace(observed, stage='first_event', elapsed_ms=40, observed_at_ms=41)
        end = replace(observed, stage='stream_end', elapsed_ms=60, observed_at_ms=61,
                      callback_ms=3, callback_count=2, callback_max_ms=2)
        selected = capture(tuple(publication(point) for point in (observed, headers, first, end)))
        with patch.object(RecordedNativeCheckpoint, 'read_json_lines',
                          wraps=RecordedNativeCheckpoint.read_json_lines) as reads:
            acquired = selected.observed_request(manifest)
            timing = selected.request_timing(acquired)
            budget = selected.request_budget(acquired)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(timing['observations'], (observed, headers, first, end))
        self.assertEqual(budget['observations'], (observed,))
        self.assertIs(budget['observations'][0], timing['observations'][0])
        self.assertEqual(timing['observations'][0].callback_ms, 0)
        self.assertFalse(timing['whole_turn_evaluated'])
        self.assertFalse(selected.request_timing(())['evaluated'])

    def test_model_steps_keep_tool_step_usage_and_distinguish_missing_from_zero(self):
        # Prevent final-answer-only accounting from hiding earlier tool-step
        # cost; unavailable native counters must never become measured zeros.
        branch = (
            MessageEntry(id='user', message=UserMessage(content='original input')),
            MessageEntry(id='tool-step', timestamp='2026-10-03T01:00:00Z',
                message=AssistantMessage(stop_reason='toolUse',
                    usage=PiUsage(input=12, output=4, total_tokens=16))),
            MessageEntry(id='missing', message=AssistantMessage(usage=None)),
            MessageEntry(id='answer', message=AssistantMessage(stop_reason='stop',
                usage=PiUsage(input=0, output=0, total_tokens=0))),
        )
        steps = RecordedNativeProbe.model_steps(branch)
        self.assertEqual(tuple(step['entry_id'] for step in steps),
                         ('tool-step', 'missing', 'answer'))
        self.assertIs(steps[0]['usage']['value'], branch[1].message.usage)
        self.assertEqual(steps[0]['usage']['value'].total_tokens, 16)
        self.assertEqual(steps[0]['timestamp'], branch[1].timestamp)
        self.assertEqual(steps[1]['usage'], {'evaluated': False, 'value': None})
        self.assertTrue(steps[2]['usage']['evaluated'])
        self.assertEqual(steps[2]['usage']['value'].total_tokens, 0)
        self.assertNotIn('cacheRead', FieldCodec.encode(steps[2]['usage']['value']))

    def test_resource_totals_use_original_summary_and_all_assistant_usage(self):
        # Detect omitted tool steps, double-added reasoning and normalized cost
        # confused with actual billing. Each record remains its original type.
        summary = SummaryUsage(input=20, output=8, cache_read=0, cache_write=0,
            total_tokens=28, reasoning=3, cost=SummaryCost(0, 0, 0, 0, 0))
        assistant = PiUsage(input=4, output=6, total_tokens=10, reasoning=2,
            cost=PiCost(total=0.25))
        zero = PiUsage(input=0, output=0, total_tokens=0, reasoning=0,
            cost=PiCost(total=0))
        cuts = {'cut': {'summary_usage': {'evaluated': True, 'usage': summary}}}
        evidence = {'cut': {'model_steps': tuple({'usage': {'value': usage}}
                                             for usage in (assistant, zero))}}
        round_ = replace(coding_scenario().rounds[0], identity='cut')
        scored = RecallScenario('resource-control', (round_,)).score(Condition.TASK_MEMORY, RecordedAnswers({}))
        observed = scored.recorded_resources(cuts, evidence, {})
        combined = observed['combined']
        self.assertEqual(combined['records'], 3)
        self.assertEqual(combined['reported_total_tokens']['value'], 38)
        self.assertEqual(combined['output_tokens']['value'], 14)
        self.assertEqual(combined['reasoning_tokens']['value'], 5)
        self.assertEqual(combined['normalized_cost']['value'], 0.25)
        self.assertFalse(combined['cache_read_tokens']['evaluated'])
        self.assertIsNone(combined['cache_read_tokens']['value'])
        self.assertEqual(observed['assistants']['records'], 2)

    def test_workflow_resources_require_stimuli_and_keep_shared_preparation_separate(self):
        # Detect source-input cost disappearing from a workflow total, missing
        # evidence becoming zero, or a shared source being counted as an
        # independent paired arm. Existing combined scope remains unchanged.
        round_ = replace(coding_scenario().rounds[0], identity='cut')
        scored = RecallScenario('source-resources', (round_,)).score(Condition.TASK_MEMORY, RecordedAnswers({}))
        summary = SummaryUsage(input=1, output=2, cache_read=0, cache_write=0,
            total_tokens=3, cost=SummaryCost(0, 0, 0, 0, 0))
        probe = PiUsage(input=3, output=4, total_tokens=7)
        source = PiUsage(input=8, output=1, total_tokens=9)
        cuts = {'cut': {'summary_usage': {'evaluated': True, 'usage': summary}}}
        evidence = {'cut': {'model_steps': ({'usage': {'value': probe}},)}}
        stimuli = {'cut': {'model_steps': ({'usage': {'value': source}},)}}
        observed = scored.recorded_resources(cuts, evidence, stimuli)
        self.assertEqual(observed['combined']['reported_total_tokens']['value'], 10)
        self.assertEqual(observed['source_inputs']['reported_total_tokens']['value'], 9)
        self.assertEqual(observed['recorded_workflow']['reported_total_tokens']['value'], 19)
        self.assertEqual(observed['recorded_workflow']['records'], 3)
        missing = scored.recorded_resources(cuts, evidence, {})['recorded_workflow']
        self.assertFalse(missing['reported_total_tokens']['evaluated'])
        self.assertIsNone(missing['reported_total_tokens']['value'])
        self.assertEqual(missing['reported_total_tokens']['observed_value'], 10)
        stimuli['cut']['model_steps'] = ({'usage': {'value': PiUsage(input=0, output=0, total_tokens=0)}},)
        zero = scored.recorded_resources(cuts, evidence, stimuli)['source_inputs']
        self.assertTrue(zero['reported_total_tokens']['evaluated'])
        self.assertEqual(zero['reported_total_tokens']['value'], 0)
        stimuli['cut']['model_steps'] = ({'usage': {'value': None}},)
        self.assertFalse(scored.recorded_resources(cuts, evidence, stimuli)
                         ['recorded_workflow']['reported_total_tokens']['evaluated'])
        # Shared original source input is allowed; only recall probes establish
        # distinct experimental samples. No independence grant follows.
        stimulus = RecordedNativeProbe(self.identity, 'a' * 32, 'source-answer')
        a = replace(stimulus, input_id='b' * 32, answer_entry_id='answer')
        b = replace(a, session=NativeSessionIdentity('other', str(self.root / 'other.jsonl')), input_id='c' * 32)
        RecordedNativeProbes.require_distinct((RecordedNativeProbes({'cut': a}, stimuli={'cut': stimulus}),
                                             RecordedNativeProbes({'cut': b}, stimuli={'cut': stimulus})))

    def test_resource_totals_keep_unreported_and_empty_denominators(self):
        # Detect a perfect zero inferred from absent summary/usage/fields.
        cuts = {'cut': {'summary_usage': {'evaluated': False}}}
        evidence = {'cut': {'model_steps': ({'usage': {'value': None}},)}}
        round_ = replace(coding_scenario().rounds[0], identity='cut')
        scored = RecallScenario('resource-control', (round_,)).score(Condition.TASK_MEMORY, RecordedAnswers({}))
        unavailable = scored.recorded_resources(cuts, evidence, {})['combined']
        self.assertEqual(unavailable['records'], 2)
        self.assertEqual(unavailable['usage_records'], 0)
        self.assertFalse(unavailable['normalized_cost']['evaluated'])
        self.assertIsNone(unavailable['normalized_cost']['value'])
        empty = scored.recorded_resources({}, {}, {})['combined']
        self.assertEqual(empty['records'], 0)
        self.assertFalse(empty['reported_total_tokens']['evaluated'])

    def test_resource_totals_keep_missing_frozen_rounds_and_observed_subtotals(self):
        # Missing cuts/probes cannot turn a partial trajectory into a complete
        # cheap total. Actual reported zero is preserved independently.
        scenario = coding_scenario()
        scored = scenario.score(Condition.TASK_MEMORY, RecordedAnswers({}))
        identities = tuple(round_.identity for round_ in scenario.rounds)
        summary = SummaryUsage(input=0, output=0, cache_read=0, cache_write=0,
            total_tokens=0, reasoning=0, cost=SummaryCost(0, 0, 0, 0, 0))
        assistant = PiUsage(input=0, output=0, cache_read=0, cache_write=0,
            total_tokens=0, reasoning=0, cost=PiCost(total=0))
        cuts = {identities[0]: {'summary_usage': {'evaluated': True, 'usage': summary}}}
        evidence = {identities[0]: {'model_steps': ({'usage': {'value': assistant}},)}}
        partial = scored.recorded_resources(cuts, evidence, {})
        for group in partial['summaries'], partial['assistants'], partial['combined']:
            self.assertEqual(group['expected_rounds'], identities)
            self.assertEqual(group['observed_rounds'], identities[:1])
            self.assertEqual(group['missing_rounds'], identities[1:])
            for name in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
                         'reported_total_tokens', 'reasoning_tokens', 'normalized_cost'):
                self.assertFalse(group[name]['evaluated'])
                self.assertIsNone(group[name]['value'])
                self.assertIsNone(group[name]['expected_records'])
                self.assertEqual(group[name]['observed_value'], 0)
        cuts.update({identity: cuts[identities[0]] for identity in identities[1:]})
        cut_complete = scored.recorded_resources(cuts, evidence, {})
        self.assertTrue(cut_complete['summaries']['normalized_cost']['evaluated'])
        self.assertFalse(cut_complete['combined']['normalized_cost']['evaluated'])
        evidence.update({identity: {'model_steps': ()} for identity in identities[1:]})
        self.assertEqual(scored.recorded_resources(cuts, evidence, {})['assistants']['missing_rounds'], identities[1:])
        evidence.update({identity: evidence[identities[0]] for identity in identities[1:]})
        complete = scored.recorded_resources(cuts, evidence, {})
        self.assertTrue(complete['combined']['normalized_cost']['evaluated'])
        self.assertEqual(complete['combined']['normalized_cost']['value'], 0)
        self.assertEqual(complete['combined']['normalized_cost']['expected_records'], 6)

    def test_original_tool_pair_does_not_award_proposal_or_constraint_credit(self):
        # Prevent invented action success from an answer, an unrelated SDK
        # result, a failed result or a call with no recorded completion.
        call = ToolCallContent(id='call', name='read', arguments={'path': '/source'})
        request = MessageEntry(id='request', message=AssistantMessage(content=(call,), stop_reason='toolUse'))
        result = MessageEntry(id='result', message=ToolResultMessage(
            tool_call_id=call.id, tool_name=call.name, content=()))
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        source = JournalProvenance(self.identity.session_file, ('request', 'result'))
        question = Question('action', 'Which action?', 'inspect-source', 'oracle',
                            measurement=Measurement.ACTION, action_source=source)
        original = {'tool_steps': probe.tool_steps((request, result))}
        observation = question.executed_action(original)
        self.assertTrue(observation['evaluated'])
        self.assertTrue(observation['successful'])
        self.assertIs(observation['call'], call)
        self.assertFalse(observation['proposal_alignment']['evaluated'])
        public = question.public()
        self.assertNotIn('action_source', public)
        self.assertNotIn('expected', public)
        failed = replace(result, message=replace(result.message, is_error=True))
        self.assertFalse(question.executed_action({'tool_steps': probe.tool_steps((request, failed))})['successful'])
        reused = probe.tool_steps((request, result, replace(request, id='request2'), replace(result, id='result2')))
        self.assertEqual(tuple(item['source'].entries for item in reused),
                         (('request', 'result'), ('request2', 'result2')))
        missing, = probe.tool_steps((request,))
        self.assertFalse(missing['completion']['evaluated'])
        for branch in ((result, request), (request, result, result), (request, request),
                       (request, replace(result, message=replace(result.message, tool_name='write')))):
            with self.assertRaises(ValueError):
                probe.tool_steps(branch)
        for wrong in (JournalProvenance('/other', source.entries),
                      JournalProvenance(source.path, ('request', 'unrelated'))):
            with self.assertRaises(ValueError):
                replace(question, action_source=wrong).executed_action(original)
        with self.assertRaises(ValueError):
            replace(question, action_source=JournalProvenance(source.path, ('request', 'request')))
        self.assertFalse(replace(question, action_source=None).executed_action(original)['evaluated'])
        with self.assertRaises(ValueError):
            result.require_artifact_request(request)  # read success is not a recorded file mutation

    def test_quality_denominators_keep_assistance_and_unobserved_rounds_separate(self):
        # Prevent assisted answers or absent records from inflating recall or
        # disappearing from the frozen sample denominator.
        scenario = coding_scenario()
        answers = RecordedAnswers({r.identity: {q.identity: q.expected for q in r.questions}
                                   for r in scenario.rounds})
        score = scenario.score(Condition.TASK_MEMORY, answers)
        evidence = {'r1': {'answer_support': {'unassisted_recall': True}},
                    'r2': {'answer_support': {'unassisted_recall': False}}}
        totals = score.support_totals(evidence)
        self.assertEqual(tuple(totals[name]['questions'] for name in totals), (7, 7, 7))
        alignment = {round_.identity: {'evaluated': round_.identity == 'r1'} for round_ in scenario.rounds}
        paired = score.paired_quality(score, evidence, {'r1': evidence['r2']}, alignment)
        self.assertEqual(paired['tool_assisted_task_quality']['candidate']['questions'], 7)
        self.assertEqual(paired['unobserved']['candidate']['questions'], 14)
        self.assertFalse(paired['unobserved']['evaluated'])
        self.assertIsNone(paired['unobserved']['correct_difference'])
        unmatched = score.paired_quality(score, evidence, {'r1': evidence['r2']},
            {round_.identity: {'evaluated': False} for round_ in scenario.rounds})
        self.assertEqual(unmatched['unmatched']['candidate']['questions'], 7)
        self.assertEqual(unmatched['unobserved']['candidate']['questions'], 14)
        self.assertFalse(unmatched['unmatched']['evaluated'])
        self.assertIsNone(unmatched['unmatched']['correct_difference'])
        self.assertFalse(unmatched['unassisted_recall']['evaluated'])
        with self.assertRaisesRegex(ValueError, 'complete frozen scenario'):
            replace(score, rounds=score.rounds[:1])

    def test_alignment_requires_distinct_originals_and_keeps_missing_rounds(self):
        # Prevent changing an experimental label from manufacturing a control.
        scenario = coding_scenario()
        original = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        candidate = RecordedNativeProbes({'r1': original})
        evidence = {'r1': {}}
        with self.assertRaisesRegex(ValueError, 'reuse'):
            candidate.alignment(candidate, evidence, evidence, scenario.rounds)
        missing = candidate.alignment(RecordedNativeProbes({}), evidence, {}, scenario.rounds)
        self.assertEqual(tuple(missing), ('r1', 'r2', 'r3'))
        self.assertTrue(all(not row['evaluated'] for row in missing.values()))
        different = RecordedNativeProbes({'r1': replace(original,
            session=NativeSessionIdentity('other', str(self.root / 'other.jsonl')), input_id='b' * 32)})
        def observation(source, model='provider/model', probe=original):
            point = RequestProgress('request', probe.session.session_id, probe.input_id,
                1, 2, '3', 1, 0, 0, 0, 'budget_admission',
                model=ReportedModel(provider='provider', id='model', context_window=100, max_tokens=20))
            owner = Thread('original', frozenset(), str(self.root), created_at=12)
            turn = RecordedContextTurn(TurnId('turn'), TurnIdentity(owner.incarnation, 1))
            revision = FileRevision.from_stat(self.session.stat())
            fork = NativeForkCreation(session_id=probe.session.session_id, session_file=probe.session.session_file,
                source=NativeSessionIdentity(source, str(self.root / f'{source}.jsonl')),
                source_revision=revision, revision=revision, prefix_digest=TextDigest.of('fixture prefix'), entry_count=1)
            manifest = ContextManifest(owner.incarnation, turn,
                (SegmentManifest(ToolCatalogSegment, (JournalProvenance(probe.session.session_file, ('original',)),),
                    'c' * 64, 10, 2),), 'native', request_id='request')
            return {'construction': {'fork': fork, 'sdk_manifest': manifest,
                'request_budget': {'evaluated': True, 'observations': (point,)},
                'request_completion': RecordedNativeProbe.request_completion(
                    {'evaluated': True, 'observations': (point,)},
                    MessageEntry(id='terminal', message=AssistantMessage(provider='provider', model='model')))},
                'model_steps': ({'selection': {'evaluated': True, 'provider': 'provider', 'model': 'model',
                    'api': 'original-api', 'response_model': None, 'provider_thinking_level': None}},),
                'scoped_facts': {'configured_settings': {'evaluated': True, 'model': model, 'thinking': 'high'}}}
        a = {'r1': observation('parent')}
        control = different.rounds['r1']
        b = {'r1': observation('parent', probe=control)}
        with self.assertRaisesRegex(ValueError, 'source identity'):
            candidate.alignment(different, a, {'r1': observation('other-parent', probe=control)}, scenario.rounds)
        with self.assertRaisesRegex(ValueError, 'model/effort'):
            candidate.alignment(different, a, {'r1': observation('parent', 'other/model', probe=control)}, scenario.rounds)
        observed = candidate.alignment(different, a, b, scenario.rounds)
        self.assertTrue(observed['r1']['evaluated'])
        self.assertFalse(observed['r2']['evaluated'])
        self.assertEqual(len(observed['r1']['sdk_manifest_changes']['removed']), 1)
        self.assertEqual(len(observed['r1']['sdk_manifest_changes']['added']), 1)
        changed = observation('parent', probe=control)
        changed['construction']['fork'] = replace(changed['construction']['fork'],
            source_revision=replace(revision := changed['construction']['fork'].source_revision, size=revision.size+1))
        with self.assertRaisesRegex(ValueError, 'source revisions'):
            candidate.alignment(different, a, {'r1': changed}, scenario.rounds)
        unavailable = observation('parent', probe=control)
        unavailable['construction']['request_budget'] = {'evaluated': False, 'reason': 'Historical record absent'}
        self.assertFalse(candidate.alignment(different, a, {'r1': unavailable}, scenario.rounds)['r1']['evaluated'])
        # No-summary controls can match original request/terminal selections;
        # absent captured settings are still unevaluated, never inherited.
        no_summary = observation('parent', probe=control)
        no_summary['scoped_facts'] = {'configured_settings': {'evaluated': False}}
        match = candidate.alignment(different, a, {'r1': no_summary}, scenario.rounds)['r1']
        self.assertTrue(match['evaluated'])
        self.assertFalse(match['captured_settings']['model']['evaluated'])
        no_summary['construction']['request_completion'] = {'evaluated': False, 'reason': 'Different terminal selection'}
        self.assertFalse(candidate.alignment(different, a, {'r1': no_summary}, scenario.rounds)['r1']['evaluated'])

    def test_request_alignment_preserves_missing_intent_and_revised_allowances(self):
        # Prevent configured-model matches and missing output intent from
        # granting equal actual requests; retry allowances are observations.
        point = RequestProgress('request', self.identity.session_id, 'a' * 32,
            1, 2, '3', 1, 0, 0, 0, 'budget_admission',
            model=ReportedModel(provider='original', id='model', context_window=100, max_tokens=20),
            estimated_input_tokens=80, available_tokens=20, output_token_field='max_tokens',
            requested_output_tokens=50, admitted_output_tokens=20, minimum_output_tokens=1)

        def observed(*points):
            return {'construction': {'request_budget': {'evaluated': True, 'observations': points}}}

        terminal = MessageEntry(id='terminal', message=AssistantMessage(provider='original', model='model'))
        joined = RecordedNativeProbe.request_completion(observed(point)['construction']['request_budget'], terminal)
        self.assertTrue(joined['evaluated'])
        self.assertEqual(joined['terminal_entry'], 'terminal')
        wrong_terminal = replace(terminal, message=replace(terminal.message, model='another'))
        self.assertFalse(RecordedNativeProbe.request_completion(observed(point)['construction']['request_budget'], wrong_terminal)['evaluated'])
        self.assertFalse(RecordedNativeProbe.request_completion(observed(replace(point, model=None))['construction']['request_budget'], terminal)['evaluated'])
        a = observed(point, replace(point, requested_output_tokens=20, admitted_output_tokens=10))
        b = observed(replace(point, estimated_input_tokens=70, available_tokens=30, admitted_output_tokens=30))
        match = RecordedNativeProbes.request_alignment(a, b)
        self.assertTrue(match['evaluated'])
        self.assertEqual(len(match['candidate']['observations']), 2)
        self.assertTrue(match['model_capacity']['context_window']['same'])
        absent = observed(replace(point, requested_output_tokens=None, admitted_output_tokens=None))
        missing = RecordedNativeProbes.request_alignment(absent, absent)
        self.assertFalse(missing['request_contract']['requested_output_tokens']['evaluated'])
        self.assertIsNone(missing['request_contract']['requested_output_tokens']['same'])
        for changed in (
            replace(point, model=replace(point.model, context_window=101)),
            replace(point, model=replace(point.model, max_tokens=21)),
            replace(point, requested_output_tokens=51),
        ):
            with self.assertRaises(ValueError):
                RecordedNativeProbes.request_alignment(observed(point), observed(changed))
        with self.assertRaisesRegex(ValueError, 'Prepared native model'):
            RecordedNativeProbes.request_alignment(observed(point), observed(replace(point, model=replace(point.model, id='another'))))
        with self.assertRaisesRegex(ValueError, 'original observations'):
            RecordedNativeProbes.request_alignment(observed(), observed(point))
        self.assertFalse(RecordedNativeProbes.request_alignment(
            observed(replace(point, model=None)), observed(point))['evaluated'])

    def test_completion_metadata_is_original_and_never_filled_from_settings(self):
        # Detect decoder loss of selected/returned model and exact provider
        # effort, and prevent missing observations becoming configured values.
        wire = {'role': 'assistant', 'content': [], 'stopReason': 'stop',
                'api': 'openai-codex-responses', 'provider': 'openai-codex',
                'model': 'configured-alias', 'responseModel': 'returned-model',
                'responseId': 'response-1', 'providerThinkingLevel': 'high'}
        entry = NativeEntry.from_evidence({'type': 'message', 'id': 'answer', 'message': wire})
        selected, = RecordedNativeProbe.model_steps((entry,))
        observation = selected['selection']
        self.assertTrue(observation['evaluated'])
        self.assertEqual(observation['model'], 'configured-alias')
        self.assertEqual(observation['response_model'], 'returned-model')
        self.assertEqual(observation['provider_thinking_level'], 'high')
        self.assertTrue(all(entry.message.to_wire()[key] == value for key, value in wire.items()))
        unreported, = RecordedNativeProbe.model_steps((MessageEntry(id='missing', message=AssistantMessage()),))
        self.assertFalse(unreported['selection']['evaluated'])
        self.assertIsNone(unreported['selection']['model'])
        with self.assertRaises(ValueError):
            AssistantMessage.from_wire(dict(wire, model=True))
        a = {'model_steps': (selected,)}
        compared = RecordedNativeProbes.completion_alignment(a, a)
        self.assertTrue(compared['evaluated'])
        self.assertTrue(compared['fields']['response_model']['same'])
        missing = {'model_steps': (unreported,)}
        self.assertFalse(RecordedNativeProbes.completion_alignment(a, missing)['evaluated'])
        different = {'model_steps': (dict(selected, selection=dict(observation, model='another')),)}
        with self.assertRaisesRegex(ValueError, 'completion models'):
            RecordedNativeProbes.completion_alignment(a, different)

    def test_construction_corroborates_segment_references_on_original_branch(self):
        # Detect foreign, missing and future source coordinates before a context
        # label can imply complete history. Source references are not byte proof.
        rows = ({'type': 'session', 'id': self.identity.session_id, 'version': 3},
                {'type': 'message', 'id': 'first', 'parentId': self.identity.session_id,
                 'message': {'role': 'user', 'content': 'original history'}},
                {'type': 'message', 'id': 'probe', 'parentId': 'first',
                 'message': {'role': 'user', 'content': 'held-out question'}},
                {'type': 'message', 'id': 'answer', 'parentId': 'probe',
                 'message': {'role': 'assistant', 'content': [], 'stopReason': 'stop'}})
        self.session.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.session.chmod(0o600)
        owner = Thread('fixture', frozenset(), str(self.root))
        turn = RecordedContextTurn(TurnId('turn'), TurnIdentity(owner.incarnation, 1))
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        context = NativeContextRecord(probe.input_id, self.identity.session_id, 'probe', 1, 'b' * 64)
        def manifest(path, entries):
            segment = SegmentManifest(TranscriptSegment, (JournalProvenance(path, entries),),
                                      'b' * 64, 20, 5)
            return ContextManifest(owner.incarnation, turn, (segment,), 'counter')
        with NativeEntry.open_evidence(self.session) as evidence:
            _, entries = evidence.observe()
            branch = evidence.branch('answer', entries)
            measured = probe.construction(evidence, evidence, branch,
                                         manifest(str(self.session), ('first', 'probe')), {}, None, {'evaluated': False}, branch[-1], context)['source_coverage']
            self.assertTrue(measured['complete_message_reference_coverage'])
            self.assertEqual(measured['included_message_entries'], ('first', 'probe'))
            self.assertFalse(measured['full_context_capacity']['evaluated'])
            partial = probe.construction(evidence, evidence, branch,
                                         manifest(str(self.session), ('probe',)), {}, None, {'evaluated': False}, branch[-1], context)['source_coverage']
            self.assertEqual(partial['unreferenced_message_entries'], ('first',))
            self.assertFalse(partial['complete_message_reference_coverage'])
            for path, ids in ((str(self.root / 'foreign'), ('probe',)),
                              (str(self.session), ('missing',)), (str(self.session), ('answer',))):
                with self.assertRaises(ValueError):
                    probe.construction(evidence, evidence, branch, manifest(path, ids), {}, None, {'evaluated': False}, branch[-1], context)
            self.assertFalse(probe.construction(evidence, evidence, branch, None, {}, None, {'evaluated': False}, branch[-1], context)['source_coverage']['evaluated'])

    def test_proposed_action_uses_original_scoped_decision_not_answer_label(self):
        # Prevent exact-answer success from becoming an execution or authority
        # claim. Original scoped publications supply allowed alternatives.
        comms = Comms(self.root / 'actions')
        comms.messaging.initialize_private_initial_protocol()
        owner = admit(comms, 'agent')
        declaration = Decision(chosen='inspect-source', rejected=('open-ticket',),
            scope=owner.task_scope, source_turn=owner.turn_identity,
            source_turn_id=TurnId(owner.active_turn.id))
        message = comms.messaging.send_message(owner.name, '#team', declaration.text, task=declaration)
        retained = comms.bus.log.retained_context(owner.name, comms.registry).retained
        cut = replace(self.checkpoint, wire=comms.root / 'bus.jsonl',
            registry_scope=self.artifact('action-scope.json', comms.registry.store.read()))
        request = manual_summary_record(self.session, incarnation=owner.incarnation, retained=retained)
        attempt = SelectedSummaryAttempt('operation', str(self.session), request.journal_json(), request, ReservedSummary())
        original = {'scoped_facts': cut.scoped_facts(attempt)}
        question = Question('action', 'What action?', 'open-ticket', 'authored-label',
                            measurement=Measurement.ACTION, decision_source=message.reference)
        report = question.proposed_action('open-ticket', original)
        self.assertTrue(report['declared_alternative']['valid'])
        self.assertTrue(declaration.contains_alternative('inspect-source'))
        self.assertTrue(declaration.contains_alternative('open-ticket'))
        self.assertFalse(declaration.contains_alternative('replay-unknown'))
        self.assertFalse(declaration.contains_alternative(None))
        self.assertFalse(report['declared_alternative']['chosen'])
        self.assertFalse(report['execution']['evaluated'])
        self.assertFalse(report['constraint_validity']['evaluated'])
        self.assertFalse(question.proposed_action('replay-unknown', original)['declared_alternative']['valid'])
        self.assertTrue(question.proposed_action(None, original)['declared_alternative']['missing'])
        self.assertFalse(replace(question, decision_source=None).proposed_action('open-ticket', original)
                         ['declared_alternative']['evaluated'])
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            replace(question, expected='replay-unknown').proposed_action('replay-unknown', original)

    def test_frozen_invocation_binds_original_scoped_decision_and_tool_request(self):
        # Prevent labels, another tool/path or failed execution from becoming
        # successful intended action. Unknown prose permissions stay unknown.
        comms = Comms(self.root / 'bound-actions')
        comms.messaging.initialize_private_initial_protocol()
        owner = admit(comms, 'agent')
        decision = Decision(chosen='inspect-source', rejected=('open-ticket',),
            scope=owner.task_scope, source_turn=owner.turn_identity,
            source_turn_id=TurnId(owner.active_turn.id))
        message = comms.messaging.send_message(owner.name, '#team', decision.text, task=decision)
        retained = comms.bus.log.retained_context(owner.name, comms.registry).retained
        cut = replace(self.checkpoint, wire=comms.root / 'bus.jsonl',
            registry_scope=self.artifact('binding-scope.json', comms.registry.store.read()))
        summary = manual_summary_record(self.session, incarnation=owner.incarnation, retained=retained)
        attempt = SelectedSummaryAttempt('operation', str(self.session), summary.journal_json(), summary, ReservedSummary())
        # These oracle arguments are authored independently of the request.
        question = Question('action', 'Which action?', 'inspect-source', 'frozen-oracle',
            measurement=Measurement.ACTION, decision_source=message.reference,
            action_source=JournalProvenance(self.identity.session_file, ('request', 'result')),
            expected_tool=ReadTool({'path': '/source'}))
        original = {'scoped_facts': cut.scoped_facts(attempt)}
        call = ToolCallContent(id='native-id', name='read', arguments={'path': '/source'})
        request = MessageEntry(id='request', message=AssistantMessage(content=(call,), stop_reason='toolUse'))
        result = MessageEntry(id='result', message=ToolResultMessage(
            tool_call_id=call.id, tool_name=call.name, content=()))
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        original['tool_steps'] = probe.tool_steps((request, result))
        report = question.proposed_action('inspect-source', original)
        self.assertTrue(report['execution']['successful'])
        self.assertTrue(report['execution']['proposal_alignment']['matches'])
        self.assertEqual(report['execution']['proposal_alignment']['decision_source'], message.reference)
        self.assertFalse(report['constraint_validity']['evaluated'])
        self.assertNotIn('expected_tool', question.public())
        self.assertEqual(FieldCodec.decode(Question, FieldCodec.encode(question)), question)
        for other in (replace(call, name='write'), replace(call, arguments={'path': '/other'}),
                      replace(call, arguments={'path': '/source', 'offset': 2})):
            self.assertFalse(question.bind_execution(other, original)['matches'])
        self.assertTrue(question.bind_execution(replace(call, id='another-native-id'), original)['matches'])
        self.assertFalse(replace(question, expected_tool=None).bind_execution(call, original)['evaluated'])
        self.assertFalse(replace(question, decision_source=None).bind_execution(call, original)['evaluated'])
        failed = replace(result, message=replace(result.message, is_error=True))
        original['tool_steps'] = probe.tool_steps((request, failed))
        failure = question.executed_action(original)
        self.assertFalse(failure['successful'])
        self.assertTrue(failure['proposal_alignment']['matches'])
        self.assertFalse(WriteTool({'path': '/source', 'content': 'x'}).matches_request(call))
        with self.assertRaises(ValueError):
            replace(question, measurement=Measurement.RECALL)

    def test_absent_evidence_is_not_zero_or_three_cuts(self):
        scenario = coding_scenario()
        result = scenario.score_native(Condition.BOUNDED, RecordedNativeProbes({}))
        self.assertEqual(result['missing'], 21)
        self.assertEqual(result['original_checkpoint_count'], 0)
        self.assertFalse(result['three_original_cuts_observed'])
        self.assertFalse(result['condition_construction']['evaluated'])
        for name in ('bounded_sdk_application', 'installed_sdk_source', 'source_delivery', 'full_history_capacity'):
            self.assertEqual(result['condition_construction'][name]['unavailable_rounds'], ['r1', 'r2', 'r3'])
        paired = scenario.compare_native(Condition.TASK_MEMORY, RecordedNativeProbes({}),
                                        Condition.BOUNDED, RecordedNativeProbes({}))
        self.assertFalse(paired['condition_construction']['evaluated'])
        self.assertIs(paired['condition_construction']['candidate'], paired['candidate']['condition_construction'])
        self.assertIs(paired['condition_construction']['baseline'], paired['baseline']['condition_construction'])
        self.assertTrue(all(not item['evaluated'] for item in result['canonical_availability'].values()))
        repeated = RecordedNativeProbes(
            {'r1': RecordedNativeProbe(self.identity, 'a' * 32, 'answer', self.checkpoint)},
            {'r1': self.checkpoint})
        with self.assertRaisesRegex(ValueError, 'belongs on its RecordedNativeProbe'):
            repeated.observe(scenario)
        self.assertFalse(self.checkpoint.summary_usage(NativeSummaryPayload(summary='summary', tokens_before=12))['evaluated'])
        usage = SummaryUsage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0,
                             cost=SummaryCost(0, 0, 0, 0, 0))
        observed = self.checkpoint.summary_usage(NativeSummaryPayload(summary='summary', tokens_before=12, usage=usage))
        self.assertTrue(observed['evaluated'])
        self.assertIs(observed['usage'], usage)
        self.assertEqual(observed['usage'].cache_read, 0)

    def test_sdk_presence_requires_original_request_and_unchanged_bytes(self):
        owner = Thread('fixture-owner', frozenset(), str(self.root))
        retained = RetainedTaskFacts((GoalTaskFact(Goal('Keep λ original', 'goal-fixture')),))
        context = NativeContextRecord('a' * 32, self.identity.session_id, 'user-entry', 1, 'b' * 64)
        raw = json.dumps(retained.text, ensure_ascii=False, separators=(',', ':')).encode()
        segment = SystemLayerSegment(content=retained.text, tokens=12,
            sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw),
            provenance=(NativeProvenance(self.identity, 1, context.llm_context_digest),))
        data = NativeContextData('fixture-counter', self.identity, (segment,))
        manifest = ContextManifest(owner.incarnation,
            RecordedContextTurn(TurnId('fixture-turn'), TurnIdentity(owner.incarnation, 1)),
            (segment.measured_manifest(),), data.counter)
        probe = RecordedNativeProbe(self.identity, context.input_id, 'answer-entry',
            sdk_context=self.artifact('context.json', data.to_wire()),
            context_manifest=self.artifact('manifest.json', manifest),
            sdk_segment_bytes=self.artifact('segments.json', tuple(s.text() for s in data.segments)))
        original = probe.read_sdk_context()
        manifest = probe.request_manifest(context, original)
        texts, construction = probe.serialized_construction(original, manifest)
        report = probe.prompt_presence(retained, texts, construction)
        self.assertTrue(report['exact_envelope_present'])
        self.assertFalse(report['final_transport_evaluated'])
        self.assertTrue(construction['evaluated'])
        self.assertEqual(construction['artifact'], probe.sdk_segment_bytes)
        self.assertEqual(construction['utf8_bytes'], len(raw))
        self.assertFalse(construction['final_transport_evaluated'])
        self.assertFalse(probe.prompt_presence(None, texts, construction)['evaluated'])
        self.assertFalse(probe.prompt_presence(RetainedTaskFacts(()), texts, construction)['evaluated'])
        absent = replace(probe, sdk_segment_bytes=None)
        absent_texts, unavailable = absent.serialized_construction(original, manifest)
        self.assertIsNone(absent_texts)
        self.assertFalse(unavailable['evaluated'])
        self.assertEqual(absent.prompt_presence(retained, absent_texts, unavailable), unavailable)
        wrong = replace(probe, sdk_segment_bytes=self.artifact('changed-segments.json', ('different',)))
        with self.assertRaisesRegex(ValueError, 'measured source'):
            wrong.serialized_construction(original, manifest)
        Path(probe.sdk_segment_bytes.path).write_text('[]')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            probe.serialized_construction(original, manifest)
        with self.assertRaisesRegex(ValueError, 'original probe request'):
            probe.request_manifest(replace(context, request_generation=2), original)
        Path(probe.sdk_context.path).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            probe.read_sdk_context()

    def test_submitted_condition_keeps_partial_transform_without_claiming_request_binding(self):
        # Detect a completed transform being promoted to a submitted SDK
        # request when its converter is absent. Only the upstream fork-source
        # acquisition is a plumbing stub here; real file/SHA/frame checks run.
        owner = Thread('fixture-owner', frozenset(), str(self.root))
        manifest = ContextManifest(owner.incarnation,
            RecordedContextTurn(TurnId('turn'), TurnIdentity(owner.incarnation, 1)),
            (), 'counter', request_id='original-request')
        source = {'evaluated': True, 'summary': 'Original λ narrative',
            'source': FieldCodec.encode(self.artifact('narrative.json', 'Original λ narrative')),
            'session': FieldCodec.encode(self.identity),
            'checkpoint_session': FieldCodec.encode(self.identity), 'native_entry_id': 'commit'}
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer', self.checkpoint,
                                    fork_journal=self.checkpoint.journal)
        applied = {'stage': 'bounded-transform-applied', 'input_id': probe.input_id,
            'session': source['session'], 'checkpoint_session': source['checkpoint_session'],
            'native_entry_id': source['native_entry_id'], 'narrative_source': source['source'],
            'agent_messages_sha256': 'c' * 64}
        retired = {'stage': 'bounded-transform-restored', 'input_id': probe.input_id}
        conversion = {'stage': 'bounded-conversion-observed', 'request_id': manifest.request_id,
            'session_id': self.identity.session_id, 'input_id': probe.input_id,
            'agent_messages_sha256': applied['agent_messages_sha256'],
            'provider_messages_sha256': 'd' * 64}
        serialized = {'evaluated': True, 'provider_messages_sha256': conversion['provider_messages_sha256']}
        texts = (json.dumps(source['summary'], ensure_ascii=False),)

        def captured(name, rows):
            path = self.root / name
            path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
            return replace(probe, condition_observation=FileProvenance(str(path),
                hashlib.sha256(path.read_bytes()).hexdigest()))

        with patch.object(RecordedNativeCheckpoint, 'fork_condition_acquired', return_value=source):
            partial_probe=captured('partial.jsonl', (applied, retired))
            partial = partial_probe.applied_condition(
                object(), object(), texts, serialized, manifest,partial_probe.condition_records())
            self.assertFalse(partial['evaluated'])
            self.assertTrue(partial['transform']['evaluated'])
            self.assertFalse(partial['message_binding']['evaluated'])
            selected = captured('complete.jsonl', (applied, conversion, retired))
            complete = selected.applied_condition(object(), object(), texts, serialized, manifest,selected.condition_records())
            self.assertTrue(complete['evaluated'])
            self.assertEqual(complete['transform']['narrative_source'], source['source'])
            self.assertEqual(complete['message_binding']['request_id'], manifest.request_id)
            self.assertIs(complete['observation'], selected.condition_observation)
            with self.assertRaisesRegex(ValueError, 'has not retired'):
                unretired=captured('unretired.jsonl', (applied, conversion))
                unretired.applied_condition(
                    object(), object(), texts, serialized, manifest,unretired.condition_records())
            Path(selected.condition_observation.path).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                selected.applied_condition(object(), object(), texts, serialized, manifest,selected.condition_records())

    def test_installed_source_uses_original_witness_ancestry_and_request(self):
        # Installation must precede this input on the same source, and bind its
        # real converter result. A transform may reduce messages legitimately;
        # its output count must not be confused with the installed prefix count.
        prefix = ({'type': 'session', 'id': self.identity.session_id},
                  {'type': 'message', 'id': 'source',
                   'message': {'role': 'user', 'content': 'Original installed source'}})
        self.session.write_text(''.join(json.dumps(row) + '\n' for row in prefix))
        self.session.chmod(0o600)
        witness = NativeWitness(self.identity.session_id, str(self.session),
                                'source', 'source', FileRevision.from_stat(self.session.stat()))
        later = ({'type': 'message', 'id': 'input', 'parentId': 'source',
                  'message': {'role': 'user', 'content': 'Distinct input'}},
                 {'type': 'message', 'id': 'answer', 'parentId': 'input',
                  'message': {'role': 'assistant', 'content': [], 'stopReason': 'stop'}})
        with self.session.open('a') as stream:
            stream.write(''.join(json.dumps(row) + '\n' for row in later))
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer')
        context = NativeContextRecord(probe.input_id, self.identity.session_id, 'input', 1, 'b' * 64)
        owner = Thread('fixture-owner', frozenset(), str(self.root))
        manifest = ContextManifest(owner.incarnation,
            RecordedContextTurn(TurnId('turn'), TurnIdentity(owner.incarnation, 1)),
            (), 'counter', request_id='original-request')
        installed = RecordedConditionInstallation('installed-transform-applied', probe.input_id,
            'recent-only', witness, 'b' * 64, 2, 'c' * 64, 3, 1, 'd' * 64)
        row = FieldCodec.encode(installed)
        self.assertEqual(FieldCodec.decode(RecordedConditionInstallation, row), installed)
        conversion = {'stage': 'bounded-conversion-observed', 'request_id': manifest.request_id,
            'session_id': self.identity.session_id, 'input_id': probe.input_id,
            'agent_messages_sha256': installed.agent_messages_sha256,
            'provider_messages_sha256': 'e' * 64}
        restored = {'stage': 'installed-transform-restored', 'input_id': probe.input_id}
        serialized = {'evaluated': True, 'provider_messages_sha256': 'e' * 64}
        with NativeEntry.open_evidence(self.session) as evidence:
            _, entries = evidence.observe()
            branch = evidence.branch('answer', entries)
            partial = probe.installed_condition(evidence, branch, context, serialized,
                                                manifest, (row, restored))
            self.assertFalse(partial['evaluated'])
            complete = probe.installed_condition(evidence, branch, context, serialized,
                                                 manifest, (row, conversion, restored))
            self.assertTrue(complete['evaluated'])
            self.assertEqual(complete['installations'], (installed,))
            with self.assertRaisesRegex(ValueError, 'has not retired'):
                probe.installed_condition(evidence, branch, context, serialized,
                                          manifest, (row, conversion))
            for changed in (replace(witness, leaf_id='answer'),
                            replace(witness, session_id='other'),
                            replace(witness, revision=replace(witness.revision,
                                identity=replace(witness.revision.identity, inode=witness.revision.identity.inode + 1)))):
                with self.subTest(witness=changed):
                    with self.assertRaises(ValueError):
                        probe.installed_condition(evidence, branch, context, serialized, manifest,
                            (FieldCodec.encode(replace(installed, source_witness=changed)), conversion, restored))
            with self.assertRaisesRegex(ValueError, 'another session/input'):
                probe.installed_condition(evidence, branch, context, serialized, manifest,
                    (row, dict(conversion, input_id='f' * 32), restored))
            self.assertFalse(probe.installed_condition(evidence, branch, context, serialized,
                                                       manifest, ())['evaluated'])

    def test_condition_groups_preserve_frozen_rounds_and_do_not_promote_labels(self):
        # Keep independent source, transform and complete-history questions;
        # no preview/label or missing round may become a matched intervention.
        scenario = coding_scenario()
        scored = scenario.score(Condition.BOUNDED, RecordedAnswers({}))
        identities = tuple(item.identity for item in scenario.rounds)
        unavailable = {identity: {'evaluated': False} for identity in identities}
        original = {'construction': {'condition_application': {'evaluated': True},
            'condition_installation': {'evaluated': False},
            'source_coverage': {'full_context_capacity': {'evaluated': False}}}}
        partial = scored.condition_construction({identities[0]: original}, unavailable)
        self.assertFalse(partial['evaluated'])
        self.assertEqual(partial['bounded_sdk_application']['available_rounds'], identities[:1])
        self.assertEqual(partial['bounded_sdk_application']['unavailable_rounds'], identities[1:])
        self.assertEqual(partial['source_delivery']['unavailable_rounds'], identities)
        self.assertEqual(partial['source_delivery']['available_rounds'], ())
        self.assertEqual(partial['full_history_capacity']['unavailable_rounds'], identities)
        evidence = {identity: original for identity in identities}
        delivered = {identity: {'evaluated': True} for identity in identities}
        observed = scored.condition_construction(evidence, delivered)
        self.assertTrue(observed['bounded_sdk_application']['evaluated'])
        self.assertTrue(observed['source_delivery']['evaluated'])
        self.assertFalse(observed['installed_sdk_source']['evaluated'])
        self.assertFalse(observed['full_history_capacity']['evaluated'])
        self.assertFalse(observed['evaluated'])
        for condition in Condition:
            labelled = replace(scored, condition=condition).condition_construction(evidence, delivered)
            self.assertEqual(labelled['declared_condition'], condition)
            self.assertFalse(labelled['evaluated'])
        partial_application = {'construction': {'condition_application': {
            'evaluated': False, 'transform': {'evaluated': True}},
            'condition_installation': {'evaluated': False},
            'source_coverage': {'full_context_capacity': {'evaluated': False}}}}
        self.assertEqual(scored.condition_construction(
            {**evidence, identities[0]: partial_application}, delivered)
            ['bounded_sdk_application']['unavailable_rounds'], identities[:1])
        # Even an authored available capacity observation cannot authenticate
        # a supplied experimental label or its intended source selection.
        original['construction']['source_coverage']['full_context_capacity'] = {'evaluated': True}
        self.assertTrue(scored.condition_construction(evidence, delivered)['full_history_capacity']['evaluated'])
        original['construction']['condition_installation'] = {'evaluated': True}
        self.assertTrue(scored.condition_construction(evidence, delivered)['installed_sdk_source']['evaluated'])
        self.assertFalse(scored.condition_construction(evidence, delivered)['evaluated'])

    def test_condition_binding_uses_complete_original_messages_and_request(self):
        # A narrative in system/tools or one matching message cannot establish
        # that the entire converted SDK sequence is this request's input.
        owner = Thread('fixture-owner', frozenset(), str(self.root))
        provenance = (NativeProvenance(self.identity, 1, 'b' * 64),)
        first = '[{"role":"user","content":"Original λ 🙂","opaque":1e+21}]'
        second = '[{"role":"user","content":[{"type":"image","data":"original","mimeType":"image/png"}]}]'
        system = '"Original λ 🙂"'
        def segment(declaration, raw, **values):
            return declaration(provenance=provenance, tokens=1,
                sha256=hashlib.sha256(raw.encode()).hexdigest(), utf8_bytes=len(raw.encode()), **values)
        segments = (
            segment(SystemLayerSegment, system, content='Original λ 🙂'),
            segment(TranscriptSegment, first, messages=tuple(json.loads(first))),
            segment(InjectionMessageSegment, second, messages=tuple(json.loads(second))),
        )
        data = NativeContextData('counter', self.identity, segments)
        manifest = ContextManifest(owner.incarnation,
            RecordedContextTurn(TurnId('turn'), TurnIdentity(owner.incarnation, 1)),
            tuple(s.measured_manifest() for s in segments), data.counter, request_id='request')
        probe = RecordedNativeProbe(self.identity, 'a' * 32, 'answer',
            sdk_segment_bytes=self.artifact('segments.json', (system,first,second)))
        _, captured = probe.serialized_construction(data, manifest)
        expected = hashlib.sha256(('['+first[1:-1]+','+second[1:-1]+']').encode()).hexdigest()
        self.assertEqual(captured['provider_messages_sha256'],expected)
        applied = {'agent_messages_sha256':'c' * 64}
        conversion = {'stage':'bounded-conversion-observed','request_id':manifest.request_id,
            'session_id':self.identity.session_id,'input_id':probe.input_id,
            'agent_messages_sha256':applied['agent_messages_sha256'],
            'provider_messages_sha256':expected}
        observed = probe.condition_message_binding((conversion,), (applied,), captured, manifest)
        self.assertTrue(observed['evaluated'])
        self.assertEqual(observed['request_id'],manifest.request_id)
        self.assertFalse(probe.condition_message_binding((),(applied,),captured,manifest)['evaluated'])
        self.assertFalse(probe.condition_message_binding((conversion,),(applied,),captured,
            replace(manifest,request_id=None))['evaluated'])
        self.assertFalse(probe.condition_message_binding((dict(conversion,request_id='other'),),
            (applied,),captured,manifest)['evaluated'])
        for changed in (dict(conversion,input_id='d'*32), dict(conversion,session_id='other'),
                        dict(conversion,agent_messages_sha256='e'*64),
                        dict(conversion,provider_messages_sha256=hashlib.sha256(first.encode()).hexdigest())):
            with self.assertRaises(ValueError):
                probe.condition_message_binding((changed,),(applied,),captured,manifest)
        with self.assertRaises(ValueError):
            probe.condition_message_binding((conversion,conversion),(applied,),captured,manifest)

    def test_scope_original_publication_and_drop_own_revision_measurement(self):
        comms = Comms(self.root / 'wire')
        comms.messaging.initialize_private_initial_protocol()
        owner = comms.registry.declare(Thread('recipient', frozenset(), str(self.root)))
        subject = comms.messaging.send_user_message(owner.name, 'Never replay UNKNOWN', worktree=owner.worktree)
        pin = comms.messaging.pin_user_constraint(owner.name, subject.reference, worktree=owner.worktree)
        retained = comms.bus.log.retained_context(owner.name, comms.registry).retained
        scope = self.artifact('scope.json', comms.registry.store.read())
        first = replace(self.checkpoint, registry_scope=scope, wire=comms.root / 'bus.jsonl')

        def attempt(facts):
            source = manual_summary_record(self.session, incarnation=owner.incarnation, retained=facts)
            return SelectedSummaryAttempt('operation', str(self.session), source.journal_json(), source, ReservedSummary())

        before = attempt(retained)
        unchanged = first.revision_from(first, before, before)['constraints']
        self.assertEqual((unchanged['eligible'], unchanged['unauthorized'], unchanged['mass']), (1, 0, 0))
        # Different retained classifications still refer to the same certified
        # publication. They cannot allocate a second lineage/denominator.
        mixed = attempt(RetainedTaskFacts((*retained.facts, HumanConstraintTaskFact(pin))))
        projected = first.revision_from(first, before, mixed)['constraints']
        self.assertEqual(projected, unchanged)
        self.assertFalse(replace(first, wire=None).revision_from(first, before, before)['evaluated'])
        lost = first.revision_from(first, before, attempt(RetainedTaskFacts(())))['constraints']
        self.assertEqual((lost['eligible'], lost['unauthorized'], lost['mass']), (1, 1, 1))
        comms.messaging.send_user_message(owner.name, 'Explicitly drop this constraint',
            worktree=owner.worktree, task=UserTaskDrop(CorrectionTaskChange(pin.reference)))
        dropped = comms.bus.log.retained_context(owner.name, comms.registry).retained
        second = replace(first, registry_scope=self.artifact('scope-after.json', comms.registry.store.read()))
        authorized = second.revision_from(first, before, attempt(dropped))['constraints']
        self.assertEqual(authorized['unauthorized'], 0)
        self.assertEqual(len(authorized['scope_or_explicit_drop']), 1)
        self.assertFalse(replace(first, registry_scope=None).revision_from(first, before, before)['evaluated'])
        empty = attempt(RetainedTaskFacts(()))
        ineligible = first.revision_from(first, empty, empty)
        self.assertFalse(ineligible['evaluated'])
        self.assertIsNone(ineligible['constraints']['mass'])


if __name__ == '__main__':
    unittest.main()
