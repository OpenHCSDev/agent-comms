"""Final controls for unavailable evidence, exact SDK bytes and scoped loss.

Authored records exercise measurement plumbing only, never model recall.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agent_comms.comms import Comms
from agent_comms.compaction_identity import SummaryOperationIdentity
from agent_comms.compaction_records import NativeForkCreation, SelectedSummaryAttempt
from agent_comms.compaction_states import ReservedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.native_compaction_request import NativeSummaryPayload
from agent_comms.native_entries import MessageEntry, NativeEntry
from agent_comms.native_pi import NativeContextRecord
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_turn_context import NativeContextData
from agent_comms.native_tools import ReadTool, WriteTool
from agent_comms.pi_summary_payloads import SummaryCost, SummaryUsage
from agent_comms.pi_payloads import AssistantMessage, PiCost, PiUsage, ToolCallContent, ToolResultMessage, UserMessage
from agent_comms.request_progress import RequestProgress
from agent_comms.private_path import FileRevision
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
from compaction_retention_fixture import Condition, Measurement, Question, RecordedAnswers, RecordedNativeProbes, ScoredScenario, coding_scenario
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe
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
        result = selected.request_budget(manifest)
        self.assertTrue(result['evaluated'])
        self.assertEqual(result['observations'], (observed, revised))
        self.assertFalse(selected.request_budget(replace(manifest, request_id=None))['evaluated'])
        self.assertFalse(probe.request_budget(manifest)['evaluated'])
        for changed in (replace(observed, session_id='other'), replace(observed, input_id='b' * 32)):
            with self.assertRaises(ValueError):
                capture((publication(changed),)).request_budget(manifest)
        with self.assertRaises(ValueError):
            capture((publication(observed, replace(lease, turn_id='other')),)).request_budget(manifest)

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
        observed = ScoredScenario.recorded_resources(cuts, evidence)
        combined = observed['combined']
        self.assertEqual(combined['records'], 3)
        self.assertEqual(combined['reported_total_tokens']['value'], 38)
        self.assertEqual(combined['output_tokens']['value'], 14)
        self.assertEqual(combined['reasoning_tokens']['value'], 5)
        self.assertEqual(combined['normalized_cost']['value'], 0.25)
        self.assertFalse(combined['cache_read_tokens']['evaluated'])
        self.assertIsNone(combined['cache_read_tokens']['value'])
        self.assertEqual(observed['assistants']['records'], 2)

    def test_resource_totals_keep_unreported_and_empty_denominators(self):
        # Detect a perfect zero inferred from absent summary/usage/fields.
        cuts = {'cut': {'summary_usage': {'evaluated': False}}}
        evidence = {'cut': {'model_steps': ({'usage': {'value': None}},)}}
        unavailable = ScoredScenario.recorded_resources(cuts, evidence)['combined']
        self.assertEqual(unavailable['records'], 2)
        self.assertEqual(unavailable['usage_records'], 0)
        self.assertFalse(unavailable['normalized_cost']['evaluated'])
        self.assertIsNone(unavailable['normalized_cost']['value'])
        empty = ScoredScenario.recorded_resources({}, {})['combined']
        self.assertEqual(empty['records'], 0)
        self.assertFalse(empty['reported_total_tokens']['evaluated'])

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
        def manifest(path, entries):
            segment = SegmentManifest(TranscriptSegment, (JournalProvenance(path, entries),),
                                      'b' * 64, 20, 5)
            return ContextManifest(owner.incarnation, turn, (segment,), 'counter')
        with NativeEntry.open_evidence(self.session) as evidence:
            _, entries = evidence.observe()
            branch = evidence.branch('answer', entries)
            measured = probe.construction(evidence, branch,
                                         manifest(str(self.session), ('first', 'probe')), {}, None, {'evaluated': False}, branch[-1])['source_coverage']
            self.assertTrue(measured['complete_message_reference_coverage'])
            self.assertEqual(measured['included_message_entries'], ('first', 'probe'))
            self.assertFalse(measured['full_context_capacity']['evaluated'])
            partial = probe.construction(evidence, branch,
                                         manifest(str(self.session), ('probe',)), {}, None, {'evaluated': False}, branch[-1])['source_coverage']
            self.assertEqual(partial['unreferenced_message_entries'], ('first',))
            self.assertFalse(partial['complete_message_reference_coverage'])
            for path, ids in ((str(self.root / 'foreign'), ('probe',)),
                              (str(self.session), ('missing',)), (str(self.session), ('answer',))):
                with self.assertRaises(ValueError):
                    probe.construction(evidence, branch, manifest(path, ids), {}, None, {'evaluated': False}, branch[-1])
            self.assertFalse(probe.construction(evidence, branch, None, {}, None, {'evaluated': False}, branch[-1])['source_coverage']['evaluated'])

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
        self.assertTrue(all(not item['evaluated'] for item in result['canonical_availability'].values()))
        repeated = RecordedNativeProbes(
            {'r1': RecordedNativeProbe(self.identity, 'a' * 32, 'answer', self.checkpoint)},
            {'r1': self.checkpoint})
        with self.assertRaisesRegex(ValueError, 'belongs on its RecordedNativeProbe'):
            repeated.observe(scenario.rounds)
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
