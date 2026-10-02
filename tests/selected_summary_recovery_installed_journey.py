"""Installed original-source recovery; actual SDK fork/preparation, no prompt send."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from agent_comms.backend import PersistentPiSession
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.compaction_records import SelectedSummaryAttempt, SelectedSummarySource
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import NotSentInput
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_package import verify_native_package
from agent_comms.native_arguments import NativeArguments
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.retained_task_facts import InputTaskFact, RetainedTaskFacts
from agent_comms.selected_pi_route import read_selected_compaction_decision
from agent_comms.selected_source import ManualSource, SelectedAdmissionSource, SessionRevision
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


async def run(stage, package):
    import agent_comms
    import sqlite3

    start = time.monotonic()
    installed = Path(agent_comms.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[1]
    assert installed.is_relative_to(Path(sys.prefix))
    for original in (checkout / 'src/agent_comms').rglob('*'):
        if original.is_file() and '__pycache__' not in original.parts:
            assert original.read_bytes() == (installed / original.relative_to(checkout / 'src/agent_comms')).read_bytes()
    stage.mkdir(mode=0o700, exist_ok=False)
    receipt = {'completed': False, 'provider_inputs': 0, 'prompt_writes': 0, 'cases': []}
    public = wire()
    snapshot = public.registry.snapshot()
    original = snapshot.require_active('openhcs-architecture-memory')
    launch = RetainedOwnerLaunch.capture(original, snapshot)
    original_hash = digest(original.require_saved_session())
    # Read original attempts through their current declaration. No writer/init.
    with sqlite3.connect((public.root / 'compaction-commits.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        rows = SelectedSummaryAttempt.select(db)
        with sqlite3.connect(stage/'original-summary-journal.sqlite3') as private:
            db.backup(private)
    (stage/'original-summary-journal.sqlite3').chmod(0o600)
    private_originals = CompactionJournal(stage/'original-summary-journal.sqlite3')
    # InputDispositions is the existing owner, not a local recovered input list.
    from agent_comms.input_disposition import InputDispositions
    input_document = InputDispositions(public.root / InputDispositions.filename).read()
    receipt['original_classification'] = []
    for row in rows:
        if row.state.retire_unchanged_source() == row.state:
            continue
        current_owner = snapshot.require(row.request.source.incarnation.resolved(snapshot).name)
        check = row.request.interrupted_check(SessionRevision.observe(row.session_file),
            input_document, current_owner.incarnation,
            TurnId(current_owner.last_finished_turn_id))
        check.require_valid()
        private_originals.summaries.retire_unchanged(row, check)
        recovered = private_originals.summaries.get(row.operation_id)
        assert recovered.request == row.request
        assert not recovered.state.blocks_input(recovered, input_document)
        assert not recovered.state.original_eligible
        receipt['original_classification'].append({
            'operation_id': row.operation_id, 'state': row.state.declared_name,
            'owner': current_owner.name, 'original_keys': row.request.source.pending_input_keys,
            'native_and_proof_revision_equal': True, 'native_binding_absent': True,
            'private_exact_original_retirement': recovered.state.declared_name,
            'original_request_unchanged': True})
    assert len(receipt['original_classification']) == 4
    verify_native_package(package)
    service = Comms(stage/'w')
    fork = await CompactionJournal(service.root/'compaction-commits.sqlite3').private_inputs.fork(ForkSessionRequest(
        str(package), original.require_saved_session(), original.worktree, str(stage/'forks')),
        cwd=Path(original.worktree), env=dict(launch.environment))
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    environment = dict(launch.environment)
    binary = Path(sys.executable).with_name('pi-comms-native')
    environment.update(AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        AGENT_COMMS_AGENT_BIN=str(binary), AGENT_COMMS_RUNTIME_ROOT=str(binary.parent),
        PATH=str(binary.parent)+os.pathsep+environment.get('PATH',''))
    for key in ('PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY', 'PYTHONPATH'):
        environment.pop(key, None)
    service.registry.declare(Thread('recovery523', original.tags, original.worktree,
        parent=original.name, session_file=fork.session_file, model=original.model,
        thinking_level=original.thinking_level,
        process_identity=ProcessIdentity.capture(os.getpid())))
    private_snapshot = service.registry.snapshot()
    private_owner = private_snapshot.require_active('recovery523')
    environment.update(private_owner.native_environment(service.root,
        private_snapshot, original.worktree))
    arguments = NativeArguments.parse(launch.arguments or ()).with_model(
        private_owner.model).with_thinking(
        ThinkingLevel.optional_name(private_owner.thinking_level)).argv
    os.environ.clear()
    os.environ.update(environment)
    persistent = PersistentPiSession()
    children = []  # Acquired process resources, not semantic session state.

    class PreparationObservation(NativeSessionPreparation):
        async def receive_record(self):
            async for event in super().receive_record():
                yield event
            if self.line and not self.skip and not self.finished:
                with (stage/'startup-events.jsonl').open('a') as stream:
                    stream.write(json.dumps(FieldCodec.encode(self.payload))+'\n')
                (stage/'startup-events.jsonl').chmod(0o600)

    try:
        state = await PreparationObservation.open(persistent, str(binary),
            arguments, worktree=original.worktree,
            environment=environment, session_file=fork.session_file)
        state.model.require_selection(original.model)
        children.append(persistent.custody.child.proc)
        selected = state.model.for_compaction(original.model)
        decision = await read_selected_compaction_decision(persistent,
            session_file=fork.session_file, expected_package=package, selected=selected)
        prepared = await asyncio.to_thread(prepare_native_source, package, fork.session_file,
            settings=decision.summary_settings(), context_window=selected.context_window)
        witness = prepared.require_ready().witness
        bridge = await asyncio.to_thread(OwnerCompactionCommit, service.registry.store.path, package)
        baseline = digest(fork.session_file)
        receipt.update(model=original.model, thinking=ThinkingLevel.optional_name(original.thinking_level),
            original_bytes=Path(original.session_file).stat().st_size,
            fork_bytes=Path(fork.session_file).stat().st_size)
        for kind in ('reserved', 'unknown', 'refused'):
            prior = service.agents.begin_turn('recovery523', f'prior-{kind}')
            owner = service.registry.require('recovery523')
            revision = SessionRevision.observe(fork.session_file).require_available()
            source = ManualSource(owner=owner.process_identity, incarnation=owner.incarnation,
                turn=TurnId(prior.turn_id), reserved_revision=revision)
            retained = RetainedTaskFacts(())
            if kind == 'refused':
                key = 'acp:known-refusal-private'
                bridge.inputs.record(key, seq=None, owner=owner.name, admission=prior.admission_generation,
                    target=owner.name, text='Distinct private refusal original; never send')
                inputs = bridge.inputs.read()
                source = SelectedAdmissionSource.capture(owner, TurnId(prior.turn_id),
                    prior.admission_generation, (key,), inputs,
                    inputs.lookup(key).source_text, revision)
                retained = RetainedTaskFacts((InputTaskFact(inputs.lookup(key)),))
            op = bridge.journal.summaries.reserve(fork.session_file, SelectedSummarySource(
                source, selected, decision.summary_settings(), retained))
            if kind == 'unknown': bridge.journal.summaries.mark_unknown(op)
            if kind == 'refused':
                bridge.journal.summaries.refuse(op, 'context_requires_compaction')
                bridge.inputs.settle_unbound((key,))
            service.agents.finish_turn(prior)
            fresh = service.agents.begin_turn('recovery523', f'distinct-recovery-{kind}')
            owner, generation = bridge.registry.live_owner_with_generation('recovery523')
            original_inputs = bridge.inputs.read()
            await asyncio.to_thread(bridge.reconcile_interrupted_summaries, owner, generation, witness)
            result = bridge.journal.summaries.get(op)
            assert result.state.declared_name == ('retired_refusal' if kind=='refused' else 'retired_unknown')
            assert not result.state.original_eligible
            assert native_input_admitted(service.root, fork.session_file)
            assert bridge.inputs.read() == original_inputs
            assert digest(fork.session_file) == baseline
            if kind == 'refused': assert isinstance(bridge.inputs.read().lookup(key), NotSentInput)
            receipt['cases'].append({'original': kind, 'operation_id': op,
                'recovered': result.state.declared_name, 'original_inputs_unchanged': True,
                'native_bytes_unchanged': True, 'new_input_admission_barrier_open': True})
            service.agents.finish_turn(fresh)
        receipt['original_public_hash_unchanged'] = digest(original.session_file) == original_hash
        assert receipt['original_public_hash_unchanged']
        receipt['completed'] = True
    finally:
        await persistent.close()
        receipt['native_child_closed'] = all(child.returncode is not None and not child.identity.alive() for child in children)
        receipt['elapsed_seconds'] = time.monotonic()-start
        (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    assert receipt['native_child_closed']
    print(json.dumps(receipt))


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).absolute(), Path(sys.argv[2]).absolute()))
