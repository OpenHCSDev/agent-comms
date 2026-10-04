"""One configured saved fork, three genuine cuts and original recorded probes.

Uses the existing configured ACP/native resource and scorer. Source publications
are real private USER constraints; expected answers never enter model prompts.
Full SDK objects are observed at their original manifest publication, not queried
later. No public input, original replay, policy activation or comparative study.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import sys

from acp.agent.router import build_agent_router
from agent_comms.acp_extension import CompactRequest, encode_request
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import NativeContextProof
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.message_reference import MessageReference
from agent_comms.registry_document import RegistryDocument
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.task_sources import CorrectionTaskChange, UserTaskDrop
from agent_comms.turn_context import FileProvenance, NativeProvenance

from original_owner_capture import CurrentTypedCapture
from compaction_source_successor_installed_journey import configured_saved_agent, digest
from compaction_retention_fixture import (
    Condition, Measurement, Question, RecallRound, RecallScenario, RecordedNativeProbes,
)
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe
from summary_prefix_configured_installed_journey import observe_native_requests


def record(path, value):
    """Pin one original typed observation; keep private SDK/source bodies private."""
    path.write_text(json.dumps(FieldCodec.encode(value), ensure_ascii=False) + '\n')
    path.chmod(0o600)
    return FileProvenance(str(path), digest(path))


async def bounded_application(stage,package,original_python):
    """One new configured cut, original raw capture, SDK child and distinct input.

    This is functional S4 verification, not a comparative provider study. The
    inspector uses Agent's existing context transform, preserving the original
    transform and recording its input binding. Original histories are read only.
    """
    captured=CurrentTypedCapture(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'),original_python
        ).read('openhcs-architecture-memory')
    original=captured.require_current()
    assert original.model=='openai-codex/gpt-6.1-sol'
    assert ThinkingLevel.optional_name(original.thinking_level)=='high'
    stage.mkdir(mode=0o700,exist_ok=False)
    capture_stage=stage/'capture'
    summaries=capture_stage/'summary-assemblies'
    source_file=Path(original.require_saved_session())
    def capture_source():
        return captured.require_current(),captured.retained
    chunks=[]
    class Receiver:
        async def session_update(self,**value):
            update=value['update']
            if update.session_update=='agent_message_chunk' and update.content.type=='text':
                chunks.append(update.content.text)

    capture_receipt={'complete':False,'public_inputs':0,'input_replays':0,'paid_comparison':False}
    # Release the inspector before the original native EOF/child join, including
    # on refusal. ExitStack owns this resource order; no new lifetime authority.
    with ExitStack() as observations:
        launch=observations.enter_context(observe_native_requests(package,
            capture_stage/'summary-observation.jsonl',summaries=summaries))
        async with configured_saved_agent(capture_stage,package,source_file,Receiver(),capture_receipt,
                capture_source=capture_source,observe_launch=launch) as (agent,owner,fork):
            try:
                summaries.mkdir(mode=0o700)
                service=agent._comms
                scope=record(capture_stage/'original-registry.private.json',service.registry.store.read())
                print('CONFIGURED_FRESH_NARRATIVE_COMPACTION',flush=True)
                await build_agent_router(agent)('session/prompt',{'sessionId':owner.name,
                    'prompt':[{'type':'text','text':' '}],'_meta':encode_request(CompactRequest(
                        'Create a fresh concise narrative of the selected history. Preserve exact original decisions, '
                        'source coordinates and authorized changes. Do not resume inherited work.'))},False)
                journal=CompactionJournal(service.root/'compaction-commits.sqlite3')
                attempt,=journal.summaries.history(fork.session_file)
                assert isinstance(attempt.state,ManualCommittedSummary)
                operation=journal.operations.get(attempt.state.commit_id)
                operation.committed_outcome()
                checkpoint=RecordedNativeCheckpoint(journal.path,attempt.identity,operation.commit_id,
                    scope,service.root/'bus.jsonl').capture_summary_observation(summaries)
                record(capture_stage/'original-checkpoint.private.json',checkpoint)
                with checkpoint.original_source() as (session,evidence):
                    narrative=checkpoint.condition_source(session,evidence)
                record(capture_stage/'original-narrative-source.private.json',narrative)
                assert narrative['evaluated'],narrative['reason'] if 'reason' in narrative else narrative
                assert not InputDispositions(service.root/InputDispositions.filename).read().rows
                captured_source=Path(fork.session_file)
                capture_receipt.update(complete=True,original_summary_operation=attempt.operation_id,
                    original_commit=operation.commit_id,raw_summary_bytes=narrative['utf8_bytes'],
                    new_original_inputs=0)
            finally:
                observations.close()

    application_stage=stage/'application'
    contexts=application_stage/'sdk-contexts'
    observer_output=application_stage/'condition-observation.jsonl'
    condition_file=application_stage/'fork-condition-source.private.json'
    receipt={'complete':False,'public_inputs':0,'input_replays':0,'paid_comparison':False,
        'installed_UI':False,'acceptance_scope':'original configured cut/capture/SDK fork/bounded SDK input and distinct answer'}
    with ExitStack() as observations:
        launch=observations.enter_context(observe_native_requests(package,observer_output,
            contexts=contexts,condition_source=condition_file))
        async with configured_saved_agent(application_stage,package,captured_source,Receiver(),receipt,
                capture_source=capture_source,observe_launch=launch) as (agent,owner,fork):
            try:
                contexts.mkdir(mode=0o700)
                service=agent._comms
                condition=checkpoint.fork_condition_source(
                    service.root/'compaction-commits.sqlite3',Path(fork.session_file))
                record(condition_file,condition)
                assert condition['evaluated']
                assert condition['session']!=condition['checkpoint_session']
                chunks.clear()
                marker='ORIGINAL_BOUNDED_S4_APPLICATION_VERIFIED'
                text=f'New distinct isolated verification input. Do not use tools or resume inherited work. Reply exactly {marker}.'
                print('CONFIGURED_FORK_BOUNDED_DISTINCT_INPUT',flush=True)
                result=await build_agent_router(agent)('session/prompt',{'sessionId':owner.name,
                    'prompt':[{'type':'text','text':text}]},False)
                assert result.stop_reason=='end_turn' and marker in ''.join(chunks)
                document=InputDispositions(service.root/InputDispositions.filename).read()
                row,=document.rows.values()
                assert row.has_started and row.source_text==text
                session=NativeSessionIdentity(fork.session_id,fork.session_file)
                probe=RecordedNativeProbe.capture_input(service,owner,session,row,contexts,
                    application_stage,checkpoint,observer_output)
                record(application_stage/'recorded-probe.private.json',probe)
                measured=probe.observe()
                record(application_stage/'recorded-application.private.json',measured)
                construction=measured['construction']
                assert construction['condition_application']['evaluated']
                assert construction['condition_application']['message_binding']['evaluated']
                assert construction['request_budget']['evaluated']
                assert construction['request_completion']['evaluated']
                assert service.registry.require(owner.name).active_turn is None
                receipt.update(complete=True,original_cut_correlated=True,
                    SDK_child_binding=True,bounded_original_narrative_in_actual_SDK_input=True,
                    canonical_request_budget_and_terminal=True,distinct_answer=True,new_original_inputs=1,
                    model_steps=len(measured['model_steps']),model_recall_evaluated=False,
                    final_HTTP_bytes_evaluated=False)
            finally:
                observations.close()


def frozen_scenario(root_reference):
    """Freeze the oracle before any summary or answer, separate from prompts."""
    rounds = []
    for number, path in enumerate(('/artifacts/S4/α/source', '/artifacts/S4/β/corrected', 'NONE'), 1):
        rounds.append(RecallRound(f'cut-{number}', (f'private-source-cut-{number}',), (
            Question('archive', 'What archive root is currently binding? Answer NONE if it was explicitly dropped.',
                     path, root_reference, obsolete=tuple(value for value in
                        ('/artifacts/S4/α/source', '/artifacts/S4/β/corrected') if value != path)),
            Question('replay', 'May an uncertain input be replayed? Answer yes or no.',
                     'no', 'persistent-prohibition', measurement=Measurement.PROHIBITION),
            Question('alternative', 'What valid review alternative remained available although warm-review was chosen?',
                     'cold-review', 'persistent-alternative', measurement=Measurement.ALTERNATIVE),
        )))
    return RecallScenario('configured-original-three-cut', tuple(rounds))


async def request_construction(stage, package, original_python):
    """One distinct configured input qualifies observation, not a new study.

    The existing saved-agent owner makes the isolated fork. The native SDK
    publisher, diagnostic owner and recorded probe own each measurement. No
    authored answers or condition labels can replace the original request facts.
    """
    captured = CurrentTypedCapture(
        Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'), original_python,
    ).read('openhcs-architecture-memory')
    original = captured.require_current()
    source = Path(original.require_saved_session())
    contexts = stage / 'sdk-contexts'
    summaries = stage / 'summary-assemblies'
    receipt = {'complete': False, 'public_inputs': 0, 'input_replays': 0,
               'paid_comparison': False, 'acceptance_scope': 'original configured request construction observations'}
    chunks = []

    class Receiver:
        async def session_update(self, **value):
            update = value['update']
            if update.session_update == 'agent_message_chunk' and update.content.type == 'text':
                chunks.append(update.content.text)

    def capture_source():
        return captured.require_current(), captured.retained

    with observe_native_requests(package, stage / 'request-observation.jsonl',
                                 contexts=contexts, summaries=summaries) as observe_launch:
        async with configured_saved_agent(stage, package, source, Receiver(), receipt,
                capture_source=capture_source, observe_launch=observe_launch) as (agent, owner, fork):
            contexts.mkdir(mode=0o700)
            summaries.mkdir(mode=0o700)
            marker = f'REQUEST_CONSTRUCTION_{stage.name.upper().replace("-", "_")}'
            result = await build_agent_router(agent)('session/prompt', {
                'sessionId': owner.name, 'prompt': [{'type': 'text',
                    'text': f'New isolated verification input. Do not use tools or resume inherited work. Reply exactly {marker}.'}],
            }, False)
            assert result.stop_reason == 'end_turn' and marker in ''.join(chunks)
            service = agent._comms
            document = InputDispositions(service.root / InputDispositions.filename).read()
            row, = document.rows.values()
            assert row.has_started
            session = NativeSessionIdentity(fork.session_id, fork.session_file)
            probe=RecordedNativeProbe.capture_input(service,owner,session,row,contexts,stage)
            measured=probe.observe()
            record(stage / 'recorded-probe.private.json', probe)
            record(stage / 'request-construction.private.json', measured)
            budget = measured['construction']['request_budget']
            manifest=measured['construction']['sdk_manifest']
            assert budget['evaluated'] and manifest.request_id
            assert measured['construction']['source_coverage']['evaluated']
            assert all(point.request_id == manifest.request_id for point in budget['observations'])
            assert service.registry.require(owner.name).active_turn is None
            receipt.update(complete=True, original_input_count=1,
                exact_manifest_request=True, original_budget_observations=len(budget['observations']),
                condition_intervention_evaluated=False, final_HTTP_bytes_evaluated=False,
                action_validity_evaluated=False, model_recall_evaluated=False)


def committed_checkpoint(stage):
    """Explicitly locate a known successful cut; never infer input replay safety."""
    from agent_comms.comms import Comms
    service = Comms(stage / 'wire')
    session_file = service.registry.require('source529').require_saved_session()
    journal = CompactionJournal(service.root / 'compaction-commits.sqlite3')
    attempt, = journal.summaries.history(session_file)
    assert isinstance(attempt.state, ManualCommittedSummary)
    scope = stage / 'cut-1-registry.private.json'
    checkpoint = RecordedNativeCheckpoint(journal.path, attempt.identity, attempt.state.commit_id,
                                         FileProvenance(str(scope), digest(scope)), service.root / 'bus.jsonl'
                                         ).capture_summary_observation(stage / 'summary-assemblies')
    checkpoint.inspect()
    return RecordedNativeProbes({}, {'cut-1': checkpoint})


def completed_continuation(stage):
    """Read the two original answers without consuming their ACP outcome."""
    from agent_comms.comms import Comms
    service = Comms(stage / 'wire')
    scenario = RecallScenario.read(stage / 'frozen-oracle.private.json')
    recorded = FieldCodec.decode(RecordedNativeProbes,
                                json.loads((stage / 'original-run.private.json').read_text()))
    document = InputDispositions(service.root / InputDispositions.filename).read()
    submitted = record(stage / 'continuation-original-inputs.private.json', document)
    probes = {identity: replace(probe, submitted_inputs=submitted)
              for identity, probe in recorded.rounds.items()}
    session_file = service.registry.require('source529').require_saved_session()
    with NativeEntry.open_evidence(Path(session_file)) as evidence:
        header, _ = evidence.observe()
        session = NativeSessionIdentity(header.id, session_file)
        for round_ in scenario.rounds[:2]:
            if round_.identity in probes:
                continue
            checkpoint = FieldCodec.decode(RecordedNativeCheckpoint,
                json.loads((stage / f'{round_.identity}-checkpoint.private.json').read_text()))
            row, = (row for row in document.rows.values() if row.source_text == round_.probe_text())
            assert row.has_started
            context = NativeContextProof.read_evidence(Path(session_file), row.native_id, evidence=evidence)
            answer, _ = RecordedNativeProbe.answer_for_input(evidence, context)
            sdk = stage / 'sdk-contexts' / f'context-{context.llm_context_digest}.json'
            provenance = NativeProvenance(session, context.request_generation, context.llm_context_digest)
            manifest, = (manifest for manifest in service.bus.log.context_manifests('source529', service.registry)
                if manifest.segments and all(provenance in segment.provenance for segment in manifest.segments))
            probes[round_.identity] = RecordedNativeProbe(session, row.native_id, answer.id, checkpoint,
                FileProvenance(str(sdk), digest(sdk)),
                record(stage / f'{round_.identity}-original-manifest.private.json', manifest),
                submitted_inputs=submitted)
    resumed = RecordedNativeProbes(probes)
    resumed.observe(scenario)
    record(stage / 'continuation-original-run.private.json', resumed)
    return resumed


async def run(stage, package, original_python, *, continuation=None):
    captured = CurrentTypedCapture(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'),
                                   original_python).read('openhcs-architecture-memory')
    original = captured.require_current()
    assert ThinkingLevel.optional_name(original.thinking_level) == 'high'
    assert 'sol' in original.model.lower(), 'Use the configured Sol model; do not select an alternative'
    source_file = Path(original.require_saved_session())
    receipt = {'complete': False, 'public_inputs': 0, 'input_replays': 0,
               'comparative_study': False, 'policy_activation': False,
               'acceptance_scope': 'three sequential configured SDK/ACP/native cuts and recorded retention probes',
               'completed_rounds': []}

    class Receiver:
        async def session_update(self, **value):
            # ACP is the real delivery path; scoring uses the original native answer.
            pass

    def capture_source():
        return captured.require_current(), captured.retained

    observation = stage / 'request-observation.jsonl'
    contexts = stage / 'sdk-contexts'
    summaries = stage / 'summary-assemblies'
    with observe_native_requests(package, observation, contexts=contexts,
                                 summaries=summaries) as observe_launch:
        async with configured_saved_agent(stage, package, source_file, Receiver(), receipt,
                capture_source=capture_source, observe_launch=observe_launch,
                continuation=continuation) as (agent, owner, creation):
            contexts.mkdir(mode=0o700, exist_ok=continuation is not None)
            summaries.mkdir(mode=0o700, exist_ok=continuation is not None)
            session = NativeSessionIdentity(creation.session_id, creation.session_file)
            service = agent._comms
            router = build_agent_router(agent)
            journal = CompactionJournal(service.root / 'compaction-commits.sqlite3')
            inputs = InputDispositions(service.root / InputDispositions.filename)

            def pin(wording, **changes):
                subject = service.messaging.send_user_message(owner.name, wording, worktree=owner.worktree)
                declaration = service.messaging.pin_user_constraint(owner.name, subject.reference,
                                worktree=owner.worktree, **changes)
                return declaration

            if continuation is None:
                archive = pin('For this private retention task, the binding archive root is /artifacts/S4/α/source.')
                pin('Never replay an uncertain input. This prohibition remains binding throughout this task.')
                pin('warm-review was chosen; cold-review remains a valid review alternative and must remain available.')
                scenario = frozen_scenario(json.dumps(FieldCodec.encode(archive.reference), sort_keys=True))
                record(stage / 'frozen-oracle.private.json', scenario)
                record(stage / 'public-questions.json', scenario.public())
            else:
                scenario = RecallScenario.read(stage / 'frozen-oracle.private.json')
                reference = FieldCodec.decode(MessageReference, json.loads(scenario.rounds[0].questions[0].evidence_ref))
                with service.bus.log.certified_read() as source:
                    original, = source.references((reference,))
                    archive = original.message
            probes = {} if continuation is None else dict(continuation.rounds)
            if probes:
                # The original retained fact owner resolves the correction; a
                # current body/time lookup cannot select the dropped subject.
                checkpoint = probes[scenario.rounds[len(probes)-1].identity].checkpoint
                with NativeEntry.open_evidence(Path(session.session_file)) as evidence:
                    attempt, _, _, _ = checkpoint.capture(session, evidence)
                snapshot = checkpoint.read_record(checkpoint.registry_scope, RegistryDocument).snapshot()
                captured_owner = snapshot.require_active(attempt.request.source.incarnation.name)
                _, archive = next((root, current) for root, current in
                    attempt.request.retained.current_authored_lineages(captured_owner, snapshot)
                    if root.reference == reference)
            for number, round_ in enumerate(scenario.rounds, 1):
                if round_.identity in probes:
                    receipt['completed_rounds'].append(round_.identity)
                    print(f'{round_.identity}: recorded original answer; no summary or input replay', flush=True)
                    continue
                if number == 2:
                    archive = pin('Authorized correction: the binding archive root is now /artifacts/S4/β/corrected.',
                                  change=CorrectionTaskChange(archive.reference))
                elif number == 3:
                    service.messaging.send_user_message(owner.name,
                        'Explicitly drop the archive-root constraint. No binding archive root remains for this task.',
                        worktree=owner.worktree, task=UserTaskDrop(CorrectionTaskChange(archive.reference)))

                if continuation is not None and round_.identity in continuation.checkpoints:
                    checkpoint = continuation.checkpoints[round_.identity]
                    print(f'{round_.identity}: original committed cut, no summary replay', flush=True)
                else:
                    scope = record(stage / f'{round_.identity}-registry.private.json', service.registry.store.read())
                    print(f'{round_.identity}: configured canonical compaction', flush=True)
                    prior_operations = {attempt.identity.operation_id for attempt in journal.summaries.history(session.session_file)}
                    await router('session/prompt', {'sessionId': owner.name,
                        'prompt': [{'type': 'text', 'text': ' '}], '_meta': encode_request(CompactRequest(
                        'Preserve exact original facts and authorized corrections/drops. Do not resume inherited work.'))}, False)
                    attempt, = (attempt for attempt in journal.summaries.history(session.session_file)
                                if attempt.identity.operation_id not in prior_operations)
                    assert isinstance(attempt.state, ManualCommittedSummary)
                    operation = journal.operations.get(attempt.state.commit_id)
                    operation.committed_outcome()
                    checkpoint = RecordedNativeCheckpoint(journal.path, attempt.identity,
                        operation.commit_id, scope, service.root / 'bus.jsonl'
                        ).capture_summary_observation(summaries)
                # Persist the committed cut before admitting the new, distinct probe.
                record(stage / f'{round_.identity}-checkpoint.private.json', checkpoint)
                print(f'{round_.identity}: distinct held-out probe', flush=True)
                result = await router('session/prompt', {'sessionId': owner.name,
                    'prompt': [{'type': 'text', 'text': round_.probe_text()}]}, False)
                assert result.stop_reason == 'end_turn'
                row, = (row for row in inputs.read().rows.values() if row.source_text == round_.probe_text())
                assert row.has_started
                probes[round_.identity]=RecordedNativeProbe.capture_input(
                    service,owner,session,row,contexts,stage,checkpoint)
                record(stage / ('original-run.private.json' if continuation is None else
                                'continued-run.private.json'), RecordedNativeProbes(dict(probes)))
                receipt['completed_rounds'].append(round_.identity)
                assert service.registry.require(owner.name).active_turn is None

            record(stage / 'source-publications.private.json', service.bus.log.full_history())
            report = scenario.score_native(Condition.TASK_MEMORY, RecordedNativeProbes(probes))
            record(stage / 'original-measurements.private.json', report)
            assert report['three_original_cuts_observed']
            assert all(value['evaluated'] for value in report['canonical_availability'].values())
            measured = report['provider_prompt_presence']
            # Previously captured SDK objects cannot supply missing original
            # serialization. Keep that metric unavailable, not zero or credit.
            assert all(value['exact_envelope_present'] for value in measured.values() if value['evaluated'])
            assert measured['cut-3']['evaluated']
            for cut in ('cut-2', 'cut-3'):
                revision = report['checkpoints'][cut]['revision_mass']
                assert revision['evaluated'] and revision['constraints']['unauthorized'] == 0
            receipt.update(complete=True, three_cuts_observed=True, actual_probe_count=len(probes),
                           recall=report['correct'], questions=report['questions'], stale=report['stale'],
                           missing=report['missing'], measurements=report['measurements'],
                           original_scope_and_sdk_captures=True,
                           prompt_presence_unavailable=[key for key,value in measured.items() if not value['evaluated']],
                           prior_ACP_outcome='UNCONFIRMED' if continuation is not None and continuation.rounds else 'not applicable')


if __name__ == '__main__':
    stage = Path(sys.argv[1]).absolute()
    modes = {'--continue-committed': committed_checkpoint, '--continue-completed': completed_continuation}
    if sys.argv[4:] == ['--request-construction']:
        asyncio.run(request_construction(stage, Path(sys.argv[2]).resolve(), Path(sys.argv[3]).absolute()))
    elif sys.argv[4:]==['--bounded-application']:
        asyncio.run(bounded_application(stage,Path(sys.argv[2]).resolve(),Path(sys.argv[3]).absolute()))
    else:
        continuation = modes[sys.argv[4]](stage) if sys.argv[4:] else None
        asyncio.run(run(stage, Path(sys.argv[2]).resolve(), Path(sys.argv[3]).absolute(), continuation=continuation))
