"""Declared case/arm execution through configured saved forks and original probes.

Uses the existing configured ACP/native resource and scorer. Source publications
are real private USER constraints; expected answers never enter model prompts.
Full SDK objects are observed at their original manifest publication, not queried
later. No public input, original replay, policy activation or comparative study.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

from acp.agent.router import build_agent_router
from agent_comms.acp_extension import CompactRequest, encode_request
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.native_entries import NativeEntry
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.turn_context import FileProvenance

from original_owner_capture import CurrentTypedCapture
from compaction_source_successor_installed_journey import configured_saved_agent, digest
from compaction_retention_fixture import (
    Condition, PairedRecallDesign, RecallScenario, RecordedNativeProbes,
)
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe
from summary_prefix_configured_installed_journey import observe_native_requests


def record(path, value):
    """Pin one original typed observation; keep private SDK/source bodies private."""
    path.write_text(json.dumps(FieldCodec.encode(value), ensure_ascii=False) + '\n')
    path.chmod(0o600)
    return FileProvenance(str(path), digest(path))


async def condition_application(application_stage, package, agent, owner, fork,
                                selected_condition: Condition, checkpoint: RecordedNativeCheckpoint,
                                *, prompt_text: str, receipt, retire_selected):
    """Observe one selected arm and retire its exact acquired child on any exit.

    Source selection/restoration belongs to SessionLifecycle. The yielded
    retirement capability owns custody checks and cancellation-safe joining;
    this consumer neither selects a current backend nor repeats those checks.
    """
    with observe_native_requests(package,application_stage/'condition-observation.jsonl',
            contexts=application_stage/'sdk-contexts',
            condition_source=application_stage/'fork-condition-source.private.json',
            condition=selected_condition) as launch:
        environment=dict(os.environ)
        launch(environment)
        with patch.dict(os.environ,environment,clear=True):
            try:
                return await apply_condition_input(application_stage,agent,owner,fork,
                    selected_condition,checkpoint,prompt_text=prompt_text,receipt=receipt)
            finally:
                await retire_selected()


async def apply_condition_input(application_stage, agent, owner, fork,
                                selected_condition: Condition, checkpoint: RecordedNativeCheckpoint,
                                *, prompt_text: str, receipt):
    """One prepare/prompt/original-capture algorithm serves both resources.

    The paired selection and the standalone owned agent have different
    retirement authorities. Their resource scopes surround this shared body;
    neither creates a second USER/registry or reconstructs a missing receipt.
    """
    contexts=application_stage/'sdk-contexts'
    observer_output=application_stage/'condition-observation.jsonl'
    condition_file=application_stage/'fork-condition-source.private.json'
    service=agent._comms
    contexts.mkdir(mode=0o700)
    condition=checkpoint.fork_condition_source(
        service.root/'compaction-commits.sqlite3',Path(fork.session_file))
    record(condition_file,condition)
    await agent.turns.prepare_selected_session(owner.name,owner)
    before=set(InputDispositions(service.root/InputDispositions.filename).read().rows)
    print('CONFIGURED_FORK_INSTALLED_DISTINCT_INPUT',flush=True)
    result=await build_agent_router(agent)('session/prompt',{'sessionId':owner.name,
        'prompt':[{'type':'text','text':prompt_text}]},False)
    assert result.stop_reason=='end_turn'
    document=InputDispositions(service.root/InputDispositions.filename).read()
    row,=(row for key,row in document.rows.items() if key not in before)
    assert row.has_started and row.source_text==prompt_text
    session=NativeSessionIdentity(fork.session_id,fork.session_file)
    probe=RecordedNativeProbe.capture_input(service,owner,session,row,contexts,
        application_stage,checkpoint,observer_output)
    record(application_stage/'recorded-probe.private.json',probe)
    measured=probe.observe()
    record(application_stage/'recorded-application.private.json',measured)
    construction=measured['construction']
    installed=construction['condition_installation']
    assert installed['evaluated']
    for item in installed['installations']:
        item.require_condition(selected_condition.value)
    assert construction['request_budget']['evaluated']
    assert construction['request_completion']['evaluated']
    assert service.registry.require(owner.name).active_turn is None
    receipt.update(complete=True,original_cut_correlated=True,
        SDK_child_binding=True,installed_source_in_actual_SDK_request=True,
        installed_narrative_source_evaluated=installed['narrative_source']['evaluated'],
        constructed_source_prefix=installed['constructed_prefix'],
        full_history_sdk_admission=construction['full_history_sdk_admission'],
        canonical_request_budget_and_terminal=True,distinct_answer=True,new_original_inputs=1,
        model_steps=len(measured['model_steps']),model_recall_evaluated=False,
        final_HTTP_bytes_evaluated=False)
    return probe, measured


async def verify_condition_application(stage, package, original_python, selected_condition,
                                       checkpoint, *, core_source, core_artifacts=(), prompt_text):
    """Acquire one outer private owner for the standalone functional command.

    Paired arms do not use this acquisition: they borrow the source trajectory's
    same owner through selected_native_fork and the shared application body.
    """
    captured=CurrentTypedCapture(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'),original_python
        ).read('openhcs-architecture-memory')
    original=captured.require_current()
    assert original.model=='openai-codex/gpt-6.1-sol'
    assert ThinkingLevel.optional_name(original.thinking_level)=='high'
    with checkpoint.original_source() as (identity,evidence):
        checkpoint.capture(identity,evidence)
        captured_source=identity.path
    stage.mkdir(mode=0o700,exist_ok=False)

    def capture_source():
        return captured.require_current(),captured.retained

    class Receiver:
        async def session_update(self,**value):
            pass

    receipt={'complete':False,'public_inputs':0,'input_replays':0,'paid_comparison':False,
        'installed_UI':False,'acceptance_scope':'original completed cut/SDK fork/installed SDK input and distinct answer',
        'selected_condition':selected_condition}
    application_stage=stage/'application'
    with observe_native_requests(package,application_stage/'condition-observation.jsonl',
            contexts=application_stage/'sdk-contexts',
            condition_source=application_stage/'fork-condition-source.private.json',
            condition=selected_condition) as launch:
        async with configured_saved_agent(application_stage,package,captured_source,Receiver(),receipt,
                capture_source=capture_source,observe_launch=launch,
                core_source=core_source,core_artifacts=core_artifacts) as (agent,owner,fork):
            return await apply_condition_input(application_stage,agent,owner,fork,
                selected_condition,checkpoint,prompt_text=prompt_text,receipt=receipt)


async def request_construction(stage, package, original_python, *, core_source, core_artifacts=()):
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
                capture_source=capture_source, observe_launch=observe_launch,
                core_source=core_source,core_artifacts=core_artifacts) as (agent, owner, fork):
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


async def run(stage, package, original_python, *, design: PairedRecallDesign,
              sampling_seed: int, trajectory: int, core_source, core_artifacts=()):
    """Execute one explicitly selected prospective pair, never an entire study.

    This callable supplies no spending permission. Actual execution requires
    the operator's separate configured/holder/artifact purpose. A new private
    root is mandatory; uncertain or completed roots are never resumed here.
    Source and summary work are shared between the arms, not independent costs.
    """
    plan = design.construction_plan(sampling_seed, trajectory=trajectory)
    selected, = plan['trajectories']
    scenario = RecordedNativeCheckpoint.read_record(design.oracle, RecallScenario)
    stage.mkdir(mode=0o700, exist_ok=False)
    record(stage / 'construction-plan.private.json', plan)
    record(stage / 'frozen-oracle.private.json', scenario)
    captured = CurrentTypedCapture(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'),
                                   original_python).read('openhcs-architecture-memory')
    original = captured.require_current()
    if original.model != design.model:
        raise ValueError('Declared design differs from the selected configured model')
    source_file = Path(original.require_saved_session())
    receipt = {'complete': False, 'public_inputs': 0, 'input_replays': 0,
               'comparative_study': False, 'policy_activation': False,
               'acceptance_scope': 'one declared shared-source trajectory and original installed-arm probes',
               'sample': selected['sample'], 'condition_order': selected['condition_order'],
               'completed_rounds': [], 'shared_preparation': True}
    record(stage / 'receipt.json', receipt)

    class Receiver:
        async def session_update(self, **value):
            pass

    def capture_source():
        return captured.require_current(), captured.retained

    parent_stage = stage / 'source'
    contexts = parent_stage / 'sdk-contexts'
    summaries = parent_stage / 'summary-assemblies'
    probes = {condition: {} for condition in selected['condition_order']}
    stimuli, checkpoints, task_events = {}, {}, ()
    workflow_started = monotonic()
    with observe_native_requests(package, parent_stage / 'request-observation.jsonl',
                                 contexts=contexts, summaries=summaries) as observe_launch:
        async with configured_saved_agent(parent_stage, package, source_file, Receiver(), receipt,
                capture_source=capture_source, observe_launch=observe_launch,
                core_source=core_source, core_artifacts=core_artifacts) as (agent, owner, creation):
            contexts.mkdir(mode=0o700)
            summaries.mkdir(mode=0o700)
            session = NativeSessionIdentity(creation.session_id, creation.session_file)
            service = agent._comms
            router = build_agent_router(agent)
            journal = CompactionJournal(service.root / 'compaction-commits.sqlite3')
            inputs = InputDispositions(service.root / InputDispositions.filename)
            # The source is frozen study data, not an instruction to modify the
            # inherited project. Publish through the existing USER pin owner.
            restriction = service.messaging.send_user_message(owner.name,
                scenario.rounds[0].evaluation_instructions(),
                worktree=owner.worktree)
            service.messaging.pin_user_constraint(owner.name, restriction.reference,
                                                 worktree=owner.worktree)
            for number, operands in enumerate(plan['rounds'], 1):
                identity = operands['round']
                round_ = scenario.rounds[number - 1]
                round_stage = stage / f'round-{number}'
                round_stage.mkdir(mode=0o700)
                task_events = round_.publish_task_sources(service,owner.name,task_events)
                record(round_stage/'authored-task-events.private.json',
                       round_.observe_task_events(service,owner.name,task_events))
                # Each source body is the original construction operand, not a
                # caller-built cumulative history or oracle answer payload.
                before = set(inputs.read().rows)
                result = await router('session/prompt', {'sessionId': owner.name,
                    'prompt': [{'type': 'text', 'text': operands['source_text']}]}, False)
                assert result.stop_reason == 'end_turn'
                document = inputs.read()
                row, = (row for key, row in document.rows.items() if key not in before)
                assert row.has_started and row.source_text == operands['source_text']
                stimulus = RecordedNativeProbe.capture_input(service, owner, session, row,
                    contexts, round_stage)
                stimuli[identity] = stimulus
                record(round_stage / 'source-input.private.json', stimulus)
                # Retain the original terminal before refusing an unexpected
                # tool attempt; never erase or retry that source input.
                measured = stimulus.observe()
                record(round_stage / 'source-measurement.private.json', measured)
                assert measured['answer_support']['unassisted_recall']
                assert service.registry.require(owner.name).active_turn is None
                scope = record(round_stage / 'registry.private.json', service.registry.store.read())
                prior = {attempt.identity.operation_id for attempt in journal.summaries.history(session.session_file)}
                result = await router('session/prompt', {'sessionId': owner.name,
                    'prompt': [{'type': 'text', 'text': ' '}], '_meta': encode_request(CompactRequest(
                        'Preserve the exact supplied case history and its explicit corrections. '
                        'Do not use tools or resume inherited work.'))}, False)
                assert result.stop_reason == 'end_turn'
                attempt, = (attempt for attempt in journal.summaries.history(session.session_file)
                            if attempt.identity.operation_id not in prior)
                assert isinstance(attempt.state, ManualCommittedSummary)
                operation = journal.operations.get(attempt.state.commit_id)
                operation.committed_outcome()
                checkpoint = RecordedNativeCheckpoint(journal.path, attempt.identity,
                    operation.commit_id, scope, service.root / 'bus.jsonl').capture_summary_observation(summaries)
                checkpoints[identity] = checkpoint
                record(round_stage / 'checkpoint.private.json', checkpoint)
                for position, condition in enumerate(selected['condition_order'], 1):
                    arm_stage=round_stage/f'arm-{position}'
                    arm_stage.mkdir(mode=0o700)
                    original_owner=RegistryOwner.capture_local(service.registry.snapshot(),owner.name)
                    child=await journal.private_inputs.fork(ForkSessionRequest(
                        str(package),original_owner.thread.require_saved_session(),owner.worktree,
                        str(arm_stage/'forks')),cwd=Path(owner.worktree))
                    arm_receipt={'complete':False,'public_inputs':0,'input_replays':0,
                        'paid_comparison':False,'selected_condition':condition,
                        'acceptance_scope':'same-owner selected SDK fork and original installed input; not study'}
                    try:
                        async with agent.sessions.selected_native_fork(owner.name,original_owner,child) as (selected_owner,retire_selected):
                            probe, observed = await condition_application(arm_stage,package,agent,
                                selected_owner.thread,child,condition,checkpoint,
                                prompt_text=operands['probe_text'],receipt=arm_receipt,
                                retire_selected=retire_selected)
                        arm_receipt['original_source_restored']=True
                    finally:
                        record(arm_stage/'receipt.json',arm_receipt)
                    probes[condition][identity] = probe
                    record(round_stage / f'arm-{position}-measurement.private.json', observed)
                    record(stage / f'arm-{position}-run.private.json',
                           RecordedNativeProbes(dict(probes[condition]), stimuli=dict(stimuli)))
                receipt['completed_rounds'].append(identity)
                record(stage / 'receipt.json', receipt)

    # One actual span includes source work and BOTH arms, through owned cleanup.
    # Keep it in the existing receipt; arm records borrow it rather than copy a
    # duration or subtract the other arm's request clocks. Its complete input
    # membership lets the reader derive that it is shared preparation.
    workflow_elapsed_seconds = monotonic() - workflow_started
    runs = {condition: RecordedNativeProbes(originals, stimuli=stimuli)
            for condition, originals in probes.items()}
    # Corroborate and score all originals before completing the existing receipt.
    # The clock ended with runtime cleanup; reader/scorer work is excluded.
    report = scenario.compare_native(design.candidate, runs[design.candidate],
                                     design.baseline, runs[design.baseline])
    receipt.update(complete=True, original_sources=len(stimuli), original_cuts=len(checkpoints),
                   original_arm_probes=sum(len(values) for values in probes.values()),
                   whole_study_evaluated=False, capacity_HTTP_billing_evaluated=False,
                   shared_source_and_summary_cost_not_independent=True,
                   workflow_elapsed_seconds=workflow_elapsed_seconds,
                   workflow_inputs=tuple(dict.fromkeys(
                       original for run in runs.values() for original in run.workflow_inputs)))
    workflow = record(stage / 'receipt.json', receipt)
    runs = {condition: RecordedNativeProbes(run.rounds, stimuli=run.stimuli, workflow=workflow)
            for condition, run in runs.items()}
    for position, condition in enumerate(selected['condition_order'], 1):
        record(stage / f'arm-{position}-run.private.json', runs[condition])
    for arm, condition in (('candidate', design.candidate), ('baseline', design.baseline)):
        report[arm]['workflow_timing'] = runs[condition].workflow_timing()
    record(stage / 'paired-measurements.private.json', report)


if __name__ == '__main__':
    from publish_retained_summary import InstalledSource

    source, artifacts, arguments = InstalledSource.command_arguments(sys.argv[1:])
    stage = Path(arguments[0]).absolute()
    package, original = Path(arguments[1]).resolve(), Path(arguments[2]).absolute()
    if arguments[3:] == ['--request-construction']:
        asyncio.run(request_construction(stage, package, original,
            core_source=source,core_artifacts=artifacts))
    elif arguments[3:4]==['--condition-application']:
        if len(arguments)!=6:
            raise ValueError('--condition-application requires Condition and original checkpoint file')
        marker=f'ORIGINAL_INSTALLED_S4_APPLICATION_{stage.name.upper().replace("-", "_")}_VERIFIED'
        _, measured = asyncio.run(verify_condition_application(stage,package,original,
            FieldCodec.decode(Condition,arguments[4]),
            FieldCodec.decode(RecordedNativeCheckpoint,json.loads(Path(arguments[5]).read_text())),
            core_source=source,core_artifacts=artifacts,
            prompt_text=f'New distinct isolated verification input. Do not use tools or resume inherited work. Reply exactly {marker}.'))
        assert marker in measured['answer_text']
    elif arguments[3:4] == ['--paired-construction']:
        if len(arguments) != 7:
            raise ValueError('--paired-construction requires original design file, sampling seed and trajectory')
        design = FieldCodec.decode(PairedRecallDesign, json.loads(Path(arguments[4]).read_text()))
        asyncio.run(run(stage, package, original, design=design,
            sampling_seed=int(arguments[5]), trajectory=int(arguments[6]),
            core_source=source, core_artifacts=artifacts))
    else:
        raise ValueError('Select --request-construction, --condition-application or --paired-construction explicitly; existing outputs are never resumed')
