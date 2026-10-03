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
from agent_comms.compaction_records import SelectedSummaryAttempt
from agent_comms.compaction_states import ReservedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.native_compaction_request import NativeSummaryPayload
from agent_comms.native_pi import NativeContextRecord
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_turn_context import NativeContextData
from agent_comms.pi_summary_payloads import SummaryCost, SummaryUsage
from agent_comms.retained_task_facts import GoalTaskFact, RetainedTaskFacts
from agent_comms.task_sources import CorrectionTaskChange, UserTaskDrop
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.threads import Thread
from agent_comms.turn_context import (
    ContextManifest, FileProvenance, NativeProvenance, RecordedContextTurn, SystemLayerSegment,
)
from compaction_retention_fixture import Condition, RecordedNativeProbes, coding_scenario
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe
from selected_summary_cases import manual_summary_record


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
        self.assertEqual(observed['usage']['cacheRead'], 0)

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
        report = probe.prompt_presence(context, retained, original)
        self.assertTrue(report['exact_envelope_present'])
        self.assertFalse(report['final_transport_evaluated'])
        with self.assertRaisesRegex(ValueError, 'original probe request'):
            probe.prompt_presence(replace(context, request_generation=2), retained, original)
        Path(probe.sdk_context.path).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            probe.read_sdk_context()

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
