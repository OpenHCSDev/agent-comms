"""Actual private retained restart and concurrent automatic inbox drains.

Only the localhost provider is controlled. Worker, registry, bus, selected
admission, native process, source proof and response publication remain real.
"""
import argparse
import hashlib
import shutil
import shlex
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import subprocess
import threading

from compaction_loopback import LoopbackProvider
from agent_comms.comms import Comms
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import decode_updates
from agent_comms.messages import Message, MessageType
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest
from agent_comms.threads import Thread
from agent_comms.thread_identity import ThreadRole
from agent_comms.bus_publication import HumanOrigin, stable_thread_lookup
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.native_runtime_input import NativeRuntimeInput, CurrentNativeCursor
from agent_comms.native_input_record import TriageNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from contextlib import closing, contextmanager
from abc import abstractmethod
import sqlite3
from agent_comms.declared_family import DeclaredFamily
from agent_comms.native_prompt_binding import PromptBinding, binding_store_path
from agent_comms.private_sidecar import sidecar_connection
from agent_comms.coordinator import Coordination
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.bus_publication import stable_thread_lookup


def configured_stage(arguments, configured, snapshot, *, continue_existing=False):
    """Share the original installed/configured private receiving preparation."""
    from agent_comms.native_package import verify_native_package

    verify_native_package(arguments.package)
    stage = arguments.stage.absolute()
    assert stage.is_relative_to('/home/ts/wt')
    if not continue_existing:
        stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    from agent_comms.owner_launch import RetainedOwnerLaunch
    from agent_comms.native_pi import NativePiRpcLaunch

    project = stage/'project'
    if not continue_existing:
        project.mkdir(mode=0o700)
    retained = RetainedOwnerLaunch.capture(configured, snapshot)
    # The original owner owns auth/settings/extension selection. Bootstrap is
    # its existing OS-environment decoder, not a second fixture configuration.
    _, environment = NativePiRpcLaunch.bootstrap(
        arguments.package/'dist/cli.js', (), Path(configured.worktree), retained.environment, retained.configuration)
    source_profile = retained.configuration.native_config
    source_hashes = {}
    for filename in ('auth.json', 'models.json', 'settings.json'):
        original = source_profile/filename
        if original.exists():
            source_hashes[str(original)] = hashlib.sha256(original.read_bytes()).hexdigest()
    if not continue_existing:
        (project/'batch-values.txt').write_text('PUBLIC_VALUE=17\n')
    service = Comms(stage/'wire', private_initial_writes=True)
    if continue_existing:
        with service.bus.log.locked():
            root_id = service.bus.log._private_marker_unlocked().root_id
    else:
        root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, arguments.package)
    binary = Path(sys.executable).with_name('pi-comms-native')
    environment.update(AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(arguments.package),
        AGENT_COMMS_AGENT_BIN=str(binary),
        AGENT_COMMS_AGENT_ARGS=shlex.join(retained.arguments or ()),
        AGENT_COMMS_RUNTIME_ROOT=str(binary.parent),
        VIRTUAL_ENV=str(binary.parent.parent),
        AGENT_COMMS_DEBUG_LOG=str(stage/'acp.log'),
        PATH=str(binary.parent)+os.pathsep+environment.get('PATH', ''))
    for key in ('PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY', 'PYTHONPATH'):
        environment.pop(key, None)
    os.environ.clear(); os.environ.update(environment)
    return stage, project, service, root_id, source_hashes


async def configured_pure_channel(arguments):
    """Actual saved settings, native forks and overlapping pure channel owners.

    No mention or direct message forces FULL. Original proofs/UNKNOWNs are never
    copied into the private coordinator or submitted as new inputs.
    """
    from agent_comms.comms import wire
    from agent_comms.native_fork import ForkSessionRequest
    from agent_comms.native_input_record import FullNativeExecution
    from agent_comms.selected_triage import FullSelectedTriage
    from agent_comms.coordination_tables.publications import PublicationReceipts
    from agent_comms.field_codec import FieldCodec
    from agent_comms.store_files import _store_lock
    from agent_comms.native_entries import NativeEntry
    from agent_comms.coordination_cohort import _receipt_matches
    from agent_comms.acp_extension import CursorAdvancedUpdate, VerifiedCursorObservation

    public = wire()
    original_names = (arguments.configured_owner, *arguments.configured_peers)
    assert arguments.collective and len(original_names) == arguments.owners >= 1
    if arguments.configured_saved_preparation:
        assert arguments.owners == 1 and not arguments.configured_peers
    assert len(set(original_names)) == len(original_names)
    snapshot = public.registry.snapshot()
    sources = tuple(snapshot.require(name) for name in original_names)
    assert all(source.model and source.session_file for source in sources)
    if arguments.saved_source is not None:
        assert len(sources) == 1, 'An explicit saved source belongs to one configured fork'
    common_tags = set.intersection(*(set(source.tags) for source in sources))
    assert 'openhcs' in common_tags
    stage, project, service, root_id, source_hashes = configured_stage(arguments, sources[0], snapshot)
    assert len(str(service.root/'native-sessions'/('0'*32)/'s')) < 108
    names, settings = [], []
    for index, source in enumerate(sources):
        original = arguments.saved_source or Path(source.session_file)
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        # Existing SessionManager fork owns strict saved-history creation under
        # its native source lock. Its output stays under the owned profile.
        from agent_comms.compaction_journal import CompactionJournal
        fork = await CompactionJournal(service.root / 'compaction-commits.sqlite3').private_inputs.fork(
            ForkSessionRequest(str(arguments.package), str(original), source.worktree, str(stage / 'forks')),
            cwd=Path(source.worktree), env=dict(os.environ),
        )
        assert Path(fork.session_file).is_relative_to(stage)
        assert hashlib.sha256(original.read_bytes()).hexdigest() == before
        source_hashes[str(original)] = before
        name = f'purechannel-owner-{index}'
        child = Thread(name, source.tags, source.worktree, parent=source.name,
            task=source.task, session_file=fork.session_file, model=source.model,
            thinking_level=source.thinking_level, execution=source.execution)
        service.registry.declare(child)
        names.append(name)
        settings.append({'source_owner':source.name, 'private_owner':name,
            'model':source.model, 'thinking':source.thinking_level.declared_name,
            'worktree':source.worktree, 'tags':sorted(source.tags),
            'source_session_bytes':original.stat().st_size,
            'source_session_file': str(original),
            'source_session_sha256':before, 'owned_fork':fork.session_file,
            'original_task_sha256':hashlib.sha256((source.task or '').encode()).hexdigest()})
    service.registry.declare(Thread('human', frozenset(), str(project), role=ThreadRole.USER))
    if arguments.busy_reader:
        service.registry.declare(Thread('history-sender', frozenset(), str(project)))
        service.registry.declare(Thread('history-recipient', frozenset(), str(project)))
        for index in range(arguments.history):
            service.messaging.send_initial_cohort('history-sender', 'history-recipient',
                f'Original private retained history {index}: ' + 'history ' * 400)
    packets, timeline, compaction_events = [], [], []
    class Observation:
        async def session_update(self, **kwargs):
            packets.append(kwargs)
            from agent_comms.acp_extension import CompactionChangedUpdate
            for item in decode_updates(kwargs['update'].get('_meta')):
                if isinstance(item, CompactionChangedUpdate):
                    compaction_events.append({'elapsed_seconds': time.perf_counter()-begun,
                        'phase': item.event.phase})
    attachment = CommsClient(service, runtime_enabled=True,
        private_nk_native_package=arguments.package, private_nk_wire_root_id=root_id)
    attachment.on_connect(Observation())
    originals, proof, failure = [], {}, None
    if arguments.configured_task_timing:
        assert arguments.owners == arguments.wave_size == 1
        policy = json.loads(os.environ.get('AGENT_COMMS_COMPACTION_POLICY', '{}'))
        policy['taskAware'] = True
        os.environ['AGENT_COMMS_COMPACTION_POLICY'] = json.dumps(policy)
    begun = time.perf_counter()
    reading = asyncio.Event()
    read_spans = []

    def read_original_bus():
        started = time.perf_counter()
        history = service.bus.log.full_history()
        service.bus.channel_activity()
        service.bus.last_sent_timestamps()
        service.bus.pending_counts_all(names)
        return {'seconds': time.perf_counter() - started, 'messages': len(history)}

    async def observe_original_bus():
        while not reading.is_set():
            read_spans.append(await asyncio.to_thread(read_original_bus))
            await asyncio.sleep(.05)

    observer = asyncio.create_task(observe_original_bus()) if arguments.busy_reader else None
    try:
        sender = service.registry.require('human')
        with _store_lock(service._wire_lock_path):
            originals = [service.bus.publisher.publish_ordinary(Message(sender.name, '#openhcs',
                f'{stage.name} original {ordinal}: New isolated user channel question to all participants: '
                'please answer here with the value of 10+2. This is a fresh question, '
                'not continuation or retry of any previous input. No project edits are requested.' + (
                    ' Before answering, publish one authored subtask notice through the ordinary '
                    'CLI: agent-comms invoke --tool comms_subtask --arguments '
                    '\'{"to":"#openhcs","text":"Verified 10 plus 2","completed":true}\'. '
                    'Use those exact arguments once, without retry or another target. The existing '
                    'wire forbids sending to yourself; this channel notice records only that small '
                    'subtask, not a completed goal. Do not inspect or dump environment, auth or '
                    'settings. Use no file-writing tools.'
                    if arguments.configured_task_timing else ''),
                MessageType.INFO),
                _human_origin=HumanOrigin(sender.name,sender.created_at,sender.worktree))
                for ordinal in range(arguments.wave_size)]
        assert originals and len({row.message_id for row in originals}) == arguments.wave_size
        # All originals exist before dispatch. No coalescing timer or forced FULL
        # changes the channel policy; owners snapshot the already pending wave.
        await asyncio.gather(*(asyncio.to_thread(service.owners.start, name) for name in names))
        await asyncio.gather(*(attach(service, name) for name in names))
        await asyncio.gather(*(attachment.load_session(cwd=source.worktree, session_id=name)
            for name, source in zip(names, sources, strict=True)))
        overlap = False
        async with asyncio.timeout(arguments.observation_seconds):
            while True:
                with Coordination(str(service.root/'coordination.sqlite3')) as store:
                    with store.session.read():
                        db = store.session._connection
                        claims = WakeAssignment.select(db,
                            where="wire_seq IN (" + ",".join("?" for _ in originals) + ")",
                            parameters=tuple(row.seq for row in originals),order_by=('wire_seq',))
                        all_inputs = NativeRuntimeInput.select(db)
                        original_ids = {row.assignment_id for row in claims}
                        membership = {row.input_id: row.execution.source_assignment_ids(db,row.input_id)
                                      for row in all_inputs}
                        inputs = [row for row in all_inputs
                                  if original_ids.intersection(membership[row.input_id])]
                        dispatched = tuple(row for row in inputs if
                            row.sent_owner_admission_generation.reservation_violation()
                            and not row.reference.recorded)
                    # SQLite is last in the original lock order. Release its
                    # read snapshot before registry/bus/history observations;
                    # otherwise an observer can block publisher COMMIT while
                    # awaiting a registry lock held by that publisher.
                    active = tuple(name for name in names
                        if service.registry.require(name).active_turn is not None)
                    observed = {'elapsed_seconds':time.perf_counter()-begun,
                            'active_owners':active,
                            'dispatched_unproven_inputs':[{'owner':row.owner_thread,
                                'stage':row.reference_stage.declared_name,'input_id':row.input_id}
                                for row in dispatched],
                            'claims':[{'owner':row.recipient,'revision':row.revision,
                                'disposition':row.lifecycle.declared_name} for row in claims]}
                    if not timeline or observed['dispatched_unproven_inputs'] != timeline[-1]['dispatched_unproven_inputs'] or observed['claims'] != timeline[-1]['claims']:
                        timeline.append(observed)
                    overlap |= len({row.owner_thread for row in dispatched}) == len(names)
                    triage = [row for row in inputs if row.reference_stage is TriageNativeExecution]
                    full = [row for row in inputs if row.reference_stage is FullNativeExecution]
                    if len(claims) == len(names)*len(originals) and all(row.lifecycle.completed for row in claims) and not active:
                        assert len(triage) == len(full) == len(names)
                        assert all(row.verdict is FullSelectedTriage for row in triage)
                        assert all(row.session_id and row.session_entry_id for row in inputs)
                        assert all(len({row.accepted_at_ms for row in claims if row.wire_seq == original.seq}) == 1
                                   for original in originals)
                        for native in inputs:
                            expected = tuple(row.assignment_id for row in claims if row.recipient == native.owner_thread)
                            assert membership[native.input_id] == expected, "Native input did not capture the complete ordered wave"
                            with NativeEntry.open_evidence(Path(native.session_file)) as evidence:
                                _, entries = evidence.observe()
                            tracked = NativeEntry.tracked_users(entries)[native.input_id]
                            assert all(assignment_id in tracked.message.text for assignment_id in expected)
                            assert all(json.dumps(original.body,ensure_ascii=True) in tracked.message.text
                                       for original in originals)
                        assert all(row.lifecycle.mode.triage for row in claims)
                        receipts = []
                        for original in originals:
                            initial = service.bus.log.read_delivery_cohort(root_id,original.seq)
                            with store.session.read():
                                sealed = _receipt_matches(db,initial)
                            assert {row.assignment_id for row in sealed.assignments} == {
                                row.assignment_id for row in claims if row.wire_seq == original.seq}
                        for claim in claims:
                            rows = PublicationReceipts.select(db, where='execution_id=?',
                                parameters=(claim.lifecycle.execution_id,))
                            assert len(rows) == 1 and rows[0].exact_target == '#openhcs'
                            reply = service.bus.log.message_by_id(rows[0].message_id)
                            assert reply.sender == claim.recipient and '12' in reply.body
                            receipts.extend(rows)
                            history = read_historical_native_inputs(store,wire_root_id=root_id,
                                recipient_lookup=claim.recipient_lookup,source_seq=claim.wire_seq)
                            assert len(history) == 2 and all(
                                item.expected_prompt_equality_established for item in history)
                        cursors = CurrentNativeCursor.select(db,where='input_id IS NOT NULL')
                        recorded_inputs = {row.input_id: row for row in all_inputs}
                        published_coverage = {}
                        for packet in packets:
                            for fact in decode_updates(packet['update'].get('_meta')):
                                if (isinstance(fact, CursorAdvancedUpdate)
                                        and isinstance(fact.envelope.observation, VerifiedCursorObservation)):
                                    cursor = fact.envelope.observation.cursor
                                    if cursor.owner_thread in names and cursor.covered_seq >= originals[-1].seq:
                                        native = recorded_inputs[cursor.input_id]
                                        assert cursor.owner_identity == native.owner_identity
                                        assert cursor.reference == native.reference
                                        assert native.sent_owner_admission_generation.matches(cursor.owner_admission_generation)
                                        assert cursor.injected_seq >= originals[-1].seq
                                        assert fact.envelope.scope.owner_pid == service.registry.require(native.owner_thread).pid
                                        published_coverage[native.owner_thread] = fact.envelope
                        # Original completion and informational ACP publication
                        # are separate asynchronous boundaries. Await the actual
                        # installed publisher within the existing journey budget;
                        # never replay a native input to make its projection appear.
                        # Later peer-reply triage legitimately advances this SAME
                        # cursor family. Original FULL proof is checked above;
                        # current coverage must not be pinned to that past stage.
                        if (len(cursors) != len(names) or len(published_coverage) != len(names)
                                or any(row.covered_seq < originals[-1].seq for row in cursors)):
                            await asyncio.sleep(.1)
                            continue
                        if len(names) > 1:
                            assert overlap, 'No overlapping actual dispatched native inputs observed'
                        proof = {'recipients':len(names),'pure_channel':True,
                            'pending_originals_per_owner':len(originals),
                            'complete_ordered_native_membership':True,
                            'all_original_sealed_receipts':True,
                            'original_claim_count':len(claims),
                            'dm_or_mention_forcing_full':False,'triage_inputs':len(triage),
                            'full_inputs':len(full),'overlapping_dispatched_native_inputs':len(names) > 1 and overlap,
                            'all_originals_completed':True,'common_accepted_time':True,
                            'channel_receipts':FieldCodec.encode(tuple(dict.fromkeys(receipts))),
                            'all_original_historical_proofs':True,'all_current_cursors_cover_source':True,
                            'actual_coverage_cursor_envelopes':FieldCodec.encode(tuple(published_coverage.values())),
                            'actual_current_cursors':FieldCodec.encode(tuple(cursors)),
                            'current_original_owner_epochs_alive':True,
                            'refresh_native_input_replays':0}
                        break
                diagnostics = list((service.root/'diagnostics').glob('*.json'))
                if diagnostics:
                    diagnostic = json.loads(diagnostics[0].read_text())
                    raise AssertionError(diagnostic.get('source_error',diagnostic.get('reason')))
                await asyncio.sleep(.1)
        if arguments.configured_task_timing:
            proof['task_timing'] = await configured_task_timing(
                arguments, service, attachment, names[0], sender, stage
            )
            assert {'start', 'progress', 'end'} <= {event['phase'] for event in compaction_events}, \
                'Optional compaction did not publish continuous ACP progress and completion'
        if arguments.configured_saved_preparation:
            from agent_comms.compaction_journal import CompactionJournal
            from agent_comms.compaction_records import SelectedSummarySource
            name, = names
            selected = service.registry.require(name)
            assert selected.session_file == settings[0]['owned_fork'], 'Ordinary ACP selected a different saved journal'
            journal = CompactionJournal(service.root/'compaction-commits.sqlite3')
            summaries = journal.summaries.history(selected.session_file)
            proof['saved_preparation'] = {
                'ordinary_acp_selected_original_fork': True,
                'explicit_selected_execution_override': False,
                'selected_summary_states': [attempt.state.declared_name for attempt in summaries],
                'summary_source_digests': [hashlib.sha256(attempt.source_json.encode()).hexdigest()
                                           for attempt in summaries],
                'naturally_triggered_compaction': bool(summaries),
                'configured_budget_changed': False,
            }
            # Current typed requests and immutable native proof bytes are distinct.
            # No decoding, transformation or replay of historical proof strings.
            assert all(isinstance(attempt.request, SelectedSummarySource) for attempt in summaries)
            proof['saved_preparation']['cancel_continue'] = await configured_cancel_continue(
                arguments, service, attachment, name, sender, stage
            )
        facts = [fact for packet in packets for fact in decode_updates(packet['update'].get('_meta'))]
        assert facts and {packet['session_id'] for packet in packets} == set(names)
        proof['acp_fact_count'] = len(facts)
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
        raise
    finally:
        reading.set()
        try:
            if observer is not None:
                await observer
        finally:
            await attachment.shutdown()
            for name in reversed(names):
                await asyncio.to_thread(service.owners.stop, name)
            (stage/'acp-observer.json').write_text(json.dumps(packets,indent=2)+'\n')
            (stage/'native-overlap-timeline.json').write_text(json.dumps(timeline,indent=2)+'\n')
            with Coordination(str(service.root/'coordination.sqlite3')) as store:
                final_inputs = NativeRuntimeInput.select(store.session._connection)
            receipt = {'final_native_inputs':FieldCodec.encode(final_inputs),'elapsed_seconds' :time.perf_counter()-begun,'failure':failure,
                'settings':settings,'installed_interpreter':sys.executable,
                'original_sequences':[row.seq for row in originals],'proof':proof,
                'public_inputs':0,'original_seq326_replays':0,'provider':'actual configured provider',
                'all_owned_workers_retired':all(not service.registry.require(name).process_alive for name in names),
                'configured_sources_unchanged':all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
                    for path,digest in source_hashes.items()),
                'bus_reads': read_spans, 'retained_bus_bytes': service.bus.log.path.stat().st_size}
            receipt['compaction_events'] = compaction_events
            (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
            print(json.dumps(receipt,indent=2),flush=True)


async def configured_task_timing(arguments, service, attachment, name, sender, stage, *, addend=11):
    """One new original after a real authored marker on the same saved owner.

    Reuses the ordinary selected/native/ACP path and original captured provider
    configuration. The sole fixture opt-in is the declared compaction policy.
    """
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_entries import NativeEntry

    snapshot = service.registry.snapshot()
    owner = snapshot.require(name)
    retained = service.bus.log.retained_context(name, service.registry).retained
    boundary = retained.optional_boundary(owner, snapshot)
    assert len(boundary) == 1, 'Configured owner did not author the requested explicit completed subtask'
    journal = CompactionJournal(service.root/'compaction-commits.sqlite3')
    before = journal.summaries.history(owner.session_file)
    assert not journal.summaries.attempted_boundary(owner.session_file, boundary)
    original = service.messaging.send_user_message('#openhcs',
        f'New independent question: what is {addend} plus 2? Answer here. Do not record another '
        'subtask, complete a goal, edit files, or retry any earlier question.',
        worktree=sender.worktree)
    async with asyncio.timeout(arguments.observation_seconds):
        while True:
            current = service.registry.require(name)
            with Coordination(str(service.root/'coordination.sqlite3')) as store:
                claims = WakeAssignment.select(store.session._connection,
                    where='wire_seq=?', parameters=(original.seq,))
            if len(claims) == 1 and claims[0].lifecycle.completed and current.active_turn is None:
                break
            diagnostics = list((service.root/'diagnostics').glob('*.json'))
            if diagnostics:
                raise AssertionError(json.loads(diagnostics[0].read_text()).get('source_error'))
            await asyncio.sleep(.1)
    after = journal.summaries.history(owner.session_file)
    attempts = tuple(attempt for attempt in after if attempt.operation_id not in
                     {previous.operation_id for previous in before})
    assert len(attempts) == 1, 'Explicit boundary did not cause exactly one journaled optional attempt'
    attempt, = attempts
    assert attempt.request.retained.contains_source(boundary[0])
    assert attempt.state.commit_id
    assert journal.operations.get(attempt.state.commit_id).state.committed
    assert journal.summaries.attempted_boundary(owner.session_file, boundary)
    replies = tuple(message for message in service.bus.log.full_history()
                    if message.sender == name and message.target == '#openhcs' and message.seq > original.seq)
    assert len(replies) == 1 and str(addend + 2) in replies[0].body
    with NativeEntry.open_evidence(Path(owner.session_file)) as evidence:
        _, entries = evidence.observe()
    return {'explicit_authored_boundary': FieldCodec.encode(boundary),
            'new_original_reference': FieldCodec.encode(original.reference),
            'optional_attempt': attempt.operation_id, 'committed': True,
            'same_saved_owner_continued': current.session_file == owner.session_file,
            'once_channel_reply': replies[0].reference.message_id,
            'native_saved_entries': len(entries), 'provider_configuration_copied': False,
            'old_input_replays': 0, 'default_activation': False}


async def configured_task_timing_continuation(arguments):
    """Continue the preserved private owner with new inputs, never replay its failed task."""
    from agent_comms.comms import wire
    from agent_comms.field_codec import FieldCodec
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.coordination_tables.publications import PublicationReceipts
    from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
    from agent_comms.native_input_record import FullNativeExecution

    stage = arguments.stage.absolute()
    original_receipt = stage/'receipt.json'
    original_bytes = original_receipt.read_bytes()
    previous = json.loads(original_bytes)
    assert previous['all_owned_workers_retired'] and previous['proof']['all_originals_completed']
    setting, = previous['settings']
    assert setting['source_owner'] == arguments.configured_owner
    existing = Comms(stage/'wire', private_initial_writes=True)
    name = setting['private_owner']
    owner = existing.registry.require(name)
    assert not owner.process_alive and owner.active_turn is None
    assert owner.session_file == setting['owned_fork']
    with Coordination(str(existing.root/'coordination.sqlite3')) as store:
        original_inputs = NativeRuntimeInput.select(store.session._connection)
    original_journal = CompactionJournal(existing.root/'compaction-commits.sqlite3')
    original_attempts = original_journal.summaries.history(owner.session_file)
    original_summaries = FieldCodec.encode(original_attempts)
    public_snapshot = wire().registry.snapshot()
    configured = public_snapshot.require(arguments.configured_owner)
    assert owner.model == configured.model and owner.thinking_level == configured.thinking_level
    output = stage/'task-timing-continuation'
    output.mkdir(mode=0o700, exist_ok=False)
    stage, project, service, root_id, source_hashes = configured_stage(
        arguments, configured, public_snapshot, continue_existing=True)
    source = Path(configured.session_file)
    source_hashes[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
    policy = json.loads(os.environ.get('AGENT_COMMS_COMPACTION_POLICY', '{}'))
    policy['taskAware'] = True
    os.environ['AGENT_COMMS_COMPACTION_POLICY'] = json.dumps(policy)
    packets, proof, originals = [], {}, []
    class Observation:
        async def session_update(self, **kwargs):
            packets.append(kwargs)
    attachment = CommsClient(service, runtime_enabled=True,
        private_nk_native_package=arguments.package, private_nk_wire_root_id=root_id)
    attachment.on_connect(Observation())
    failure = None
    begun = time.perf_counter()
    try:
        await asyncio.to_thread(service.owners.start, name)
        await attach(service, name)
        await attachment.load_session(cwd=owner.worktree, session_id=name)
        sender = service.registry.require('human')
        original = service.messaging.send_user_message('#openhcs',
            'A distinct new isolated task: verify 20 plus 2 and answer here. This is not a retry '
            'of the earlier 10 plus 2 request or its failed self-targeted notice. Before answering, '
            'publish one completed subtask through the ordinary CLI: '
            'agent-comms invoke --tool comms_subtask --arguments '
            '\'{"to":"#openhcs","text":"Verified 20 plus 2","completed":true}\'. '
            'Use those arguments once; do not retry tools or old inputs. This records only this '
            'small subtask, not goal completion. Do not inspect or dump environment, auth or '
            'settings; do not edit files. Then answer 20 plus 2 in this channel.',
            worktree=sender.worktree)
        originals.append(original.reference)
        async with asyncio.timeout(arguments.observation_seconds):
            while True:
                with Coordination(str(service.root/'coordination.sqlite3')) as store:
                    db = store.session._connection
                    claims = WakeAssignment.select(db, where='wire_seq=?', parameters=(original.seq,))
                    executions = {link.execution_id for claim in claims for link in
                        ExecutionAssignmentLink.select(db, where='assignment_id=?',
                                                       parameters=(claim.assignment_id,))}
                    receipts = tuple(receipt for execution in executions for receipt in
                        PublicationReceipts.select(db, where='execution_id=?', parameters=(execution,)))
                current = service.registry.require(name)
                if len(claims) == 1 and claims[0].lifecycle.completed and current.active_turn is None:
                    assert len(receipts) == 1 and receipts[0].exact_target == '#openhcs'
                    reply = service.bus.log.message_by_id(receipts[0].message_id)
                    assert reply.sender == name and '22' in reply.body
                    proof['new_authored_task_answer'] = FieldCodec.encode(reply.reference)
                    proof['new_task_original'] = FieldCodec.encode(original.reference)
                    break
                diagnostics = list((service.root/'diagnostics').glob('*.json'))
                if diagnostics:
                    raise AssertionError(json.loads(diagnostics[0].read_text()).get('source_error'))
                await asyncio.sleep(.1)
        proof['task_timing'] = await configured_task_timing(
            arguments, service, attachment, name, sender, stage, addend=21)
        from agent_comms.acp_extension import CompactionChangedUpdate
        events = tuple(item.event for packet in packets
                       for item in decode_updates(packet['update'].get('_meta'))
                       if isinstance(item, CompactionChangedUpdate))
        assert {'start', 'progress', 'end'} <= {event.phase for event in events}, \
            'Optional compaction did not publish continuous ACP progress and completion'
        proof['continuous_compaction_phases'] = [event.phase for event in events]
        # The original shared effect records the second new reference in its receipt.
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
        raise
    finally:
        await attachment.shutdown()
        await asyncio.to_thread(service.owners.stop, name)
        with Coordination(str(service.root/'coordination.sqlite3')) as store:
            final_inputs = NativeRuntimeInput.select(store.session._connection)
        final_by_id = {row.input_id: row for row in final_inputs}
        new_inputs = tuple(row for row in final_inputs if row.input_id not in
                           {original.input_id for original in original_inputs})
        (output/'acp-observer.json').write_text(json.dumps(packets, indent=2)+'\n')
        receipt = {
            'elapsed_seconds': time.perf_counter()-begun, 'failure': failure,
            'installed_interpreter': sys.executable, 'continued_private_root': str(service.root),
            'same_saved_session': service.registry.require(name).session_file == owner.session_file,
            'original_receipt_sha256': hashlib.sha256(original_bytes).hexdigest(),
            'original_receipt_unchanged': original_receipt.read_bytes() == original_bytes,
            'original_native_inputs_unchanged': all(final_by_id[row.input_id] == row for row in original_inputs),
            'original_summaries_unchanged': FieldCodec.encode(tuple(attempt for attempt in
                original_journal.summaries.history(owner.session_file) if attempt.operation_id in
                {entry.operation_id for entry in original_attempts})) == original_summaries,
            'new_native_inputs': FieldCodec.encode(new_inputs),
            'new_full_inputs_recorded': all(row.reference.recorded for row in new_inputs
                                          if row.reference_stage is FullNativeExecution),
            'new_originals': FieldCodec.encode(originals), 'proof': proof,
            'all_owned_workers_retired': not service.registry.require(name).process_alive,
            'configured_sources_unchanged': all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
                                              for path,digest in source_hashes.items()),
            'fresh_sdk_forks': 0, 'old_input_replays': 0, 'public_inputs': 0,
        }
        (output/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt, indent=2), flush=True)


async def configured_cancel_continue(arguments, service, attachment, name, sender, stage):
    """Use ordinary originals and the existing joined ACP cancellation boundary."""
    from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
    from agent_comms.coordination_tables.publications import PublicationReceipts
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_input_record import FullNativeExecution
    from agent_comms.store_files import _store_lock

    def publish(label):
        with _store_lock(service._wire_lock_path):
            return service.bus.publisher.publish_ordinary(Message(sender.name, '#openhcs',
                f'{stage.name} {label}: Fresh isolated acceptance question, not a retry. '
                'Please answer here with 10+2 only; do not resume inherited work or edit files.',
                MessageType.INFO),
                _human_origin=HumanOrigin(sender.name, sender.created_at, sender.worktree))

    def observe(original):
        with Coordination(str(service.root/'coordination.sqlite3')) as store:
            with store.session.read():
                db = store.session._connection
                claims = WakeAssignment.select(db, where='wire_seq=?', parameters=(original.seq,))
                ids = {claim.assignment_id for claim in claims}
                inputs = tuple(row for row in NativeRuntimeInput.select(db)
                    if ids.intersection(row.execution.source_assignment_ids(db, row.input_id)))
                execution_ids = {link.execution_id for claim in claims
                    for link in ExecutionAssignmentLink.select(
                        db, where='assignment_id=?', parameters=(claim.assignment_id,))}
                receipts = tuple(receipt for execution_id in sorted(execution_ids)
                    for receipt in PublicationReceipts.select(
                        db, where='execution_id=?', parameters=(execution_id,)))
                return claims, inputs, receipts

    cancelled = publish('CANCEL_ORIGINAL')
    async with asyncio.timeout(arguments.observation_seconds):
        while True:
            claims, inputs, _ = observe(cancelled)
            admitted = tuple(row for row in inputs
                if row.sent_owner_admission_generation.reservation_violation()
                and not row.reference.recorded)
            if admitted:
                break
            assert not (claims and all(claim.lifecycle.completed for claim in claims)), \
                'Original completed before cancellation; cancellation was not exercised'
            await asyncio.sleep(.03)
        await attachment.cancel(name)
        while service.registry.require(name).active_turn is not None:
            await asyncio.sleep(.03)
        cancelled_claims, cancelled_inputs, cancelled_receipts = observe(cancelled)
        assert {row.input_id for row in admitted}.issubset({row.input_id for row in cancelled_inputs})
        retained = {row.input_id: row for row in cancelled_inputs}
        continuation = publish('NEW_CONTINUATION_ORIGINAL')
        while True:
            claims, inputs, receipts = observe(continuation)
            if claims and all(claim.lifecycle.completed for claim in claims) and \
                    service.registry.require(name).active_turn is None:
                full = tuple(row for row in inputs if row.reference_stage is FullNativeExecution)
                assert len(full) == 1 and full[0].reference.recorded
                assert len(receipts) == 1 and receipts[0].exact_target == '#openhcs'
                reply = service.bus.log.message_by_id(receipts[0].message_id)
                assert reply.sender == name and '12' in reply.body
                break
            await asyncio.sleep(.1)
        final_claims, final_inputs, final_receipts = observe(cancelled)
        assert {row.input_id: row for row in final_inputs} == retained, \
            'Cancellation was followed by replay or mutation of the original native attempt'
        assert final_receipts == cancelled_receipts, 'Cancellation later published an extra original reply'
        return {'cancelled_original': FieldCodec.encode(cancelled.reference),
            'continued_original': FieldCodec.encode(continuation.reference),
            'cancelled_admission_stages': [row.reference_stage.declared_name for row in admitted],
            'cancelled_native_inputs': FieldCodec.encode(cancelled_inputs),
            'cancelled_claims': FieldCodec.encode(cancelled_claims),
            'cancelled_publications': FieldCodec.encode(cancelled_receipts),
            'continuation_native_inputs': FieldCodec.encode(inputs),
            'continuation_publications': FieldCodec.encode(receipts),
            'joined_cancel_originals_retained': True,
            'uncertain_original_replays': 0,
            'new_original_completed_once': True}


async def configured_mixed_routes(arguments):
    """The same installed owner/ACP path, using the actual configured provider.

    Only fresh private originals are submitted. The public owner supplies its
    configured model/level, not a saved input or permission to restart it.
    """
    from agent_comms.comms import wire
    from agent_comms.native_input_record import FullNativeExecution
    from agent_comms.native_package import verify_native_package
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_entries import NativeEntry
    from agent_comms.pi_payloads import ToolResultMessage
    from agent_comms.coordination_tables.publications import PublicationReceipts
    from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
    from agent_comms.store_files import _store_lock

    assert arguments.owners == 1 and arguments.collective
    assert not (arguments.saved_source or arguments.contention or arguments.cancel_before_grant)
    snapshot = wire().registry.snapshot()
    configured = snapshot.require(arguments.configured_owner)
    assert configured.model, "Configured owner has no selected model"
    stage, project, service, root_id, source_hashes = configured_stage(arguments, configured, snapshot)
    name = 'batch-receiver'
    service.registry.declare(Thread('human',frozenset(),str(project),role=ThreadRole.USER))
    service.registry.declare(Thread(name,frozenset({'team'}),str(project),
        model=configured.model,thinking_level=configured.thinking_level,
        task='Answer each original route accurately once; do not expose direct-message content to a channel.'))
    sender = service.registry.require('human')
    with _store_lock(service._wire_lock_path):
        originals = [service.bus.publisher.publish_ordinary(Message(sender.name, target, body,
            MessageType.INFO), _human_origin=HumanOrigin(sender.name,sender.created_at,sender.worktree))
            for target, body in (
                ('#team', f'{stage.name}: @{name} Read batch-values.txt with the normal read tool and report PUBLIC_VALUE.'),
                ('#team', f'{stage.name}: @{name} Also compute 10+2. Combine this with the other channel request.'),
                (name, f'{stage.name}: Direct/private only: compute 7+8 and include PRIVATE_ROUTE_490 in this direct reply only.'))]
    packets = []
    class Observation:
        async def session_update(self, **kwargs):
            packets.append(kwargs)
    attachment = CommsClient(service,runtime_enabled=True,
        private_nk_native_package=arguments.package,private_nk_wire_root_id=root_id)
    attachment.on_connect(Observation())
    begun = time.perf_counter()
    failure, late = None, None
    proof = {}
    try:
        await asyncio.to_thread(service.owners.start,name)
        await attach(service,name)
        await attachment.load_session(cwd=str(project),session_id=name)
        async with asyncio.timeout(90):
            while True:
                diagnostics = list((service.root/'diagnostics').glob('*.json'))
                if diagnostics:
                    diagnostic = json.loads(diagnostics[0].read_text())
                    raise AssertionError(diagnostic.get('source_error',diagnostic.get('reason')))
                with Coordination(str(service.root/'coordination.sqlite3')) as store:
                    db = store.session._connection
                    inputs = NativeRuntimeInput.select(db)
                    if inputs and late is None:
                        assert len(inputs) == 1 and inputs[0].reference_stage is FullNativeExecution
                        membership = inputs[0].execution.source_assignment_ids(db,inputs[0].input_id)
                        assert len(membership) == len(originals), "First native input did not capture the whole wave"
                        with _store_lock(service._wire_lock_path):
                            late = service.bus.publisher.publish_ordinary(Message(sender.name,name,
                                f'{stage.name}: Separate late batch: compute 20+3; reply directly and once.',MessageType.INFO),
                                _human_origin=HumanOrigin(sender.name,sender.created_at,sender.worktree))
                        print('ORIGINAL_WAVE_RESERVED_LATE_BATCH_PUBLISHED',flush=True)
                    assignments = WakeAssignment.select(db,order_by=('wire_seq',))
                    cursors = CurrentNativeCursor.select(db,where='input_id IS NOT NULL')
                    if late is not None and len(assignments) == len(originals)+1 and all(
                            row.lifecycle.completed for row in assignments) and len(cursors) == 1 and (
                            cursors[0].covered_seq >= late.seq) and service.registry.require(name).active_turn is None:
                        assert len(inputs) == 2 and all(row.reference_stage is FullNativeExecution for row in inputs)
                        links = ExecutionAssignmentLink.select(db,order_by=('execution_id','ordinal'))
                        original_claims = {row.assignment_id for row in assignments if row.wire_seq != late.seq}
                        execution_ids = {row.execution_id for row in links if row.assignment_id in original_claims}
                        assert len(execution_ids) == 1
                        first_execution = execution_ids.pop()
                        receipts = PublicationReceipts.select(db,where='execution_id=?',parameters=(first_execution,))
                        assert {row.exact_target for row in receipts} == {'#team','human'}
                        replies = [service.bus.log.message_by_id(row.message_id) for row in receipts]
                        channel = next(row for row in replies if row.target == '#team')
                        direct = next(row for row in replies if row.target == 'human')
                        assert '17' in channel.body and '12' in channel.body
                        assert 'PRIVATE_ROUTE_490' not in channel.body
                        assert '15' in direct.body and 'PRIVATE_ROUTE_490' in direct.body
                        late_assignment = next(row for row in assignments if row.wire_seq == late.seq)
                        late_receipts = PublicationReceipts.select(db,where='execution_id=?',
                            parameters=(late_assignment.lifecycle.execution_id,))
                        assert len(late_receipts) == 1 and late_receipts[0].exact_target == 'human'
                        assert '23' in service.bus.log.message_by_id(late_receipts[0].message_id).body
                        lookup = stable_thread_lookup(service.registry.require(name).created_at)
                        for original in (*originals,late):
                            history = read_historical_native_inputs(store,wire_root_id=root_id,
                                recipient_lookup=lookup,source_seq=original.seq)
                            assert len(history) == 1 and history[0].expected_prompt_equality_established
                        assert len(cursors) == 1 and cursors[0].covered_seq >= late.seq
                        proof = {'captured_originals':len(originals),'routes':['#team','human'],
                            'original_wave_native_inputs':1,'late_wave_native_inputs':1,
                            'per_original_historical_proof':True,'route_receipts':FieldCodec.encode(receipts),
                            'private_route_not_leaked':True,'all_originals_completed':True,
                            'late_answer_published_on_original_route':True,
                            'original_cursor_covers_late':True}
                        break
                await asyncio.sleep(.03)
        with Coordination(str(service.root/'coordination.sqlite3')) as store:
            history = read_historical_native_inputs(store,wire_root_id=root_id,
                recipient_lookup=lookup,source_seq=originals[0].seq)
        with NativeEntry.open_evidence(history[0].context.session_file) as evidence:
            _, entries = evidence.observe()
        tool_results = [entry.message for entry in entries if entry.is_message and
                        isinstance(entry.message,ToolResultMessage)]
        assert any(result.tool_name == 'read' and not result.is_error for result in tool_results), 'No ordinary native read tool ran'
        facts = [fact for packet in packets for fact in decode_updates(packet['update'].get('_meta'))]
        assert facts, 'No actual ACP facts observed'
        proof['ordinary_read_tool_result'] = True
        proof['acp_fact_count'] = len(facts)
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
        raise
    finally:
        (stage/'acp-observer.json').write_text(json.dumps(packets,indent=2)+'\n')
        await attachment.shutdown()
        await asyncio.to_thread(service.owners.stop,name)
        receipt = {'elapsed_seconds':time.perf_counter()-begun,'failure':failure,
            'model':configured.model,'thinking':configured.thinking_level.declared_name,
            'installed_interpreter':sys.executable,'original_sequences':[row.seq for row in originals],
            'late_original_seq':late.seq if late else None,'proof':proof,'public_mutations':0,
            'original_replays':0,'provider':'actual configured provider, no substitute',
            'all_owned_workers_retired':not service.registry.require(name).process_alive,
            'configured_source_files_unchanged':all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
                for path,digest in source_hashes.items())}
        (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2),flush=True)


async def retained_owner():
    """Original installed ACP owner with an explicit saved SelectedExecution."""
    from agent_comms.acp import CommsAgent
    from agent_comms.coordinated_runtime import SelectedExecution
    from agent_comms.private_nk_entrypoint import PrivateNkLaunch
    from agent_comms.native_source_cursor import NativeSourceCursor
    import traceback
    original_advance = NativeSourceCursor._advance_proven
    def observe_advance(*args, **kwargs):
        begun = time.perf_counter_ns()
        try:
            value = original_advance(*args, **kwargs)
        except BaseException as error:
            print(json.dumps({'cursor_probe_ms':(time.perf_counter_ns()-begun)/1e6,
                'error':type(error).__name__, 'site':traceback.extract_tb(error.__traceback__)[-1].name}), flush=True)
            raise
        print(json.dumps({'cursor_probe_ms':(time.perf_counter_ns()-begun)/1e6,
                         'result':'original_completed'}), flush=True)
        return value
    NativeSourceCursor._advance_proven = observe_advance
    launch = PrivateNkLaunch.from_environment(Path(os.environ['AGENT_COMMS_ROOT']), os.environ)
    launch.validate()
    service = Comms(launch.validated_root)
    name = os.environ['AGENT_COMMS_THREAD']
    agent = CommsAgent(service, runtime_enabled=True,
        private_nk_wire_root_id=launch.wire_root_id,
        private_nk_native_package=launch.native_package)
    # This receiving case supplies the original saved-session capability to
    # SelectedExecution. Automatic drains currently omit that argument; their
    # complete restart/triage path is covered separately by the ordinary mode.
    agent.inputs.auto_wake = False
    try:
        owner = service.registry.require(name)
        await agent.load_session(owner.worktree, name)
        assert await asyncio.to_thread(sys.stdin.readline) == 'GO\n'
        execution = SelectedExecution(root=service.root,
            wire_root_id=launch.wire_root_id, owner_name=name,
            native_package=launch.native_package, session_file=Path(owner.session_file))
        result = await agent.turns.run_selected(name, execution)
        assert result.cursor_status == 'proven', result
        await agent.cursors.publish(name, name, selected_status=result.cursor_status)
        print('RETAINED_CURSOR_COMPLETE', flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await agent.shutdown()


class AdmissionContention(DeclaredFamily, affix='Contention'):
    """Real held resource in the existing continuous native journey."""

    @classmethod
    @abstractmethod
    def custody(cls, service, owners): ...


class WireReadContention(AdmissionContention, declared_name='wire'):
    @classmethod
    @contextmanager
    def custody(cls, service, owners):
        with service.bus.log.certified_read():
            yield True


class PromptBindingContention(AdmissionContention, declared_name='binding'):
    @classmethod
    @contextmanager
    def custody(cls, service, owners):
        with Coordination(str(service.root/'coordination.sqlite3')) as store:
            path = binding_store_path(store)
            if not path.exists():
                yield False
                return
            with sidecar_connection(path, PromptBinding) as db:
                # All burst reservations must have their genuine immutable
                # binding before holding its snapshot; no writer is blocked in
                # an earlier bind transaction instead of the affected send seam.
                yield len(PromptBinding.select(db)) >= owners


async def attach(service, name):
    owner = service.registry.require(name)
    async with asyncio.timeout(30):
        while not socket_path(service.root, owner.pid).exists():
            assert owner.process_alive
            await asyncio.sleep(.03)
        reader, writer = await asyncio.open_unix_connection(
            socket_path(service.root, owner.pid), limit=8 * 1024 * 1024)
        try:
            writer.write((json.dumps(SubscribeRuntimeRequest(thread=name).to_wire())+'\n').encode())
            await writer.drain()
            while line := await reader.readline():
                packet = json.loads(line)
                assert 'error' not in packet, packet
                if 'ready' in packet:
                    return
            raise AssertionError('Worker closed before complete attachment')
        finally:
            writer.close()
            await writer.wait_closed()


class PublishOriginalsAtStoppedBatch(StoppedOwnerInstallation):
    def __init__(self, service, names, *, collective=False, after_attachment=False, wave_size=1):
        self.service, self.names, self.collective = service, names, collective
        self.after_attachment = after_attachment
        self.wave_size = wave_size
        self.originals = []

    def require_selection(self, snapshot, owners):
        assert {owner.name for owner in owners} == set(self.names)

    def after_stopped(self, lifecycle):
        assert all(not lifecycle.registry.require(name).process_alive for name in self.names)
        if self.after_attachment:
            return
        self.publish()

    def publish(self):
        # Batch already owns the wire lock. The canonical publication owner
        # acquires bus/registry custody; Messaging's outer wire scope would nest.
        if self.collective:
            sender = self.service.registry.require('human')
            self.originals = [self.service.bus.publisher.publish_ordinary(
                Message(sender.name, '#team',
                        f'Batch wave original {index}: compute 10+{index}.'
                        if self.wave_size > 1 else 'New collective channel original: test',
                        MessageType.INFO),
                _human_origin=HumanOrigin(sender.name, sender.created_at, sender.worktree))
                for index in range(self.wave_size)]
            return
        self.originals = [self.service.bus.publisher.publish_initial_cohort(Message(
            'fixture-sender', name, f'Unique new inbox input for {name}: reply ONCE.', MessageType.INFO))
            for name in self.names]

    def publish_cancel_and_pending(self):
        from agent_comms.store_files import _store_lock

        # Both original publications precede admission, under the same existing
        # wire custody. No observer can reserve the first between the two.
        with _store_lock(self.service._wire_lock_path):
            sender = self.service.registry.require('human')
            self.originals = [self.service.bus.publisher.publish_ordinary(
                Message(sender.name, '#team', body, MessageType.INFO),
                _human_origin=HumanOrigin(sender.name, sender.created_at, sender.worktree))
                for body in ('Cancel THIS original before grant.',
                             'Independent saved channel original: reply ONCE.')]


async def run(arguments):
    if arguments.continue_task_timing:
        return await configured_task_timing_continuation(arguments)
    if arguments.configured_pure_channel or arguments.configured_saved_preparation or arguments.configured_task_timing:
        return await configured_pure_channel(arguments)
    if arguments.configured_owner:
        return await configured_mixed_routes(arguments)
    if arguments.wave_size > 1:
        assert arguments.collective and arguments.owners == 1 and not arguments.cancel_before_grant
    if arguments.cancel_before_grant:
        assert arguments.collective and arguments.contention and arguments.owners == 1
    if arguments.cursor_contention:
        assert arguments.collective and arguments.saved_source and not arguments.contention
    source_hashes = {}
    if arguments.saved_source:
        for path in (arguments.saved_source, Path(str(arguments.saved_source)+'.input-proof')):
            source_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    stage = arguments.stage.absolute()
    assert stage.is_relative_to('/home/ts/wt')
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    assert len(str(stage/'wire'/'native-sessions'/('0'*32)/'s')) < 108
    project, config = stage/'project', stage/'config'
    project.mkdir(); config.mkdir(mode=0o700)
    class ChannelReplyProvider(LoopbackProvider):
        def response_chunks(self):
            # Only the provider is controlled; select its bounded triage or
            # ordinary answer from the actual request envelope.
            messages = self.requests[-1]['messages']
            latest = json.dumps(next(message for message in reversed(messages)
                                     if message['role']=='user'))
            text = ('{"decision":"FULL"}' if 'one key decision' in latest
                    else 'Unique controlled native reply.')
            yield {'content':text}, None
            yield {}, 'stop'

    class BatchReplyProvider(LoopbackProvider):
        def response_chunks(self):
            latest = next(message for message in reversed(self.requests[-1]['messages'])
                          if message['role'] == 'user')
            text = json.dumps(latest)
            if 'one key decision' in text:
                answer = '{"decision":"FULL"}'
            elif 'Batch wave late original' in text:
                answer = 'Separate late-wave answer: 20+3=23.'
            else:
                answer = 'Combined wave answer: ' + '; '.join(
                    f'10+{index}={10+index}' for index in range(arguments.wave_size)) + '.'
            yield {'content':answer}, None
            yield {}, 'stop'

    provider_type = (BatchReplyProvider if arguments.wave_size > 1 else
                     ChannelReplyProvider if arguments.cancel_before_grant else LoopbackProvider)
    provider = provider_type(status=200, text=(
        '{"decision":"IGNORE"}' if arguments.collective else 'Unique controlled native reply.'),
        response_timeout=60 if arguments.saved_source else 15)
    provider.response_gate = asyncio.Event()
    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task(); connections.add(task)
        try:
            await provider.handle(reader, writer)
        finally:
            connections.remove(task)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    (config/'models.json').write_text(json.dumps({'providers':{'restart-local':{
        'baseUrl':f'http://127.0.0.1:{port}/v1','api':'openai-completions',
        'apiKey':'local-only','models':[{'id':'fixture','name':'Private restart fixture',
            'contextWindow':2000000,'maxTokens':8192}]}}}))
    (config/'auth.json').write_text(json.dumps({'restart-local': {
        'type': 'api_key', 'key': 'local-only'}}))
    (config/'settings.json').write_text(json.dumps({'compaction':{'enabled':False},
        'retry':{'enabled':False,'maxRetries':0,'provider':{'maxRetries':0}}}))
    service = Comms(stage/'wire', private_initial_writes=True)
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, arguments.package)
    env = dict(os.environ, AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(arguments.package),
        AGENT_COMMS_NATIVE_CONFIG_DIR=str(config), PI_CODING_AGENT_DIR=str(config),
        AGENT_COMMS_AGENT_MODELS='restart-local/fixture')
    for key in ('PYTHONPATH','PI_PROMPT','PI_TASK','PI_PARENT_ID','PI_AGENT_ID',
                'AGENT_COMMS_STARTUP_INPUT_KEY'):
        env.pop(key, None)
    os.environ.clear();os.environ.update(env)
    names = [f'restart-worker-{index}' for index in range(arguments.owners)]
    service.registry.declare(Thread('fixture-sender',frozenset(),str(project)))
    service.registry.declare(Thread('history-recipient',frozenset(),str(project)))
    if arguments.collective:
        service.registry.declare(Thread('human',frozenset(),str(project),role=ThreadRole.USER))
    for index in range(arguments.registry_size):
        service.registry.declare(Thread(f'unrelated-{index}',frozenset(),str(project)))
    for name in names:
        service.registry.declare(Thread(name,frozenset({'team'} if arguments.collective else ()),str(project),
            model='restart-local/fixture',thinking_level='off',
            task='Provider-free isolated restart acceptance; reply to each original once'))
    if arguments.saved_source:
        for name in names:
            owner = service.registry.require(name)
            directory = service.root/'native-sessions'/stable_thread_lookup(owner.created_at)
            directory.mkdir(mode=0o700, parents=True)
            saved = directory/'retained.jsonl'
            shutil.copyfile(arguments.saved_source, saved); saved.chmod(0o600)
            with closing(sqlite3.connect(Path(str(arguments.saved_source)+'.input-proof').as_uri()+
                                         '?mode=ro',uri=True)) as original:
                with closing(sqlite3.connect(str(saved)+'.input-proof')) as copied:
                    original.backup(copied)
            Path(str(saved)+'.input-proof').chmod(0o600)
            service.registry.register(replace(owner, session_file=str(saved)))
    for index in range(arguments.history):
        service.messaging.send_initial_cohort('fixture-sender','history-recipient',
            f'Retained unrelated canonical source {index}: ' + 'history '*250)
    started = time.perf_counter()
    failure = None
    packets = []
    class Observation:
        async def session_update(self, **kwargs):
            packets.append(kwargs)
    attachment = CommsClient(service, runtime_enabled=True,
        private_nk_native_package=arguments.package, private_nk_wire_root_id=root_id)
    attachment.on_connect(Observation())
    contention_release = threading.Event()
    contention_held = threading.Event()
    contention_observations = []
    cancel_observation = {}
    late_original = None
    batch_proof = {}

    def hold_original_reader():
        deadline = time.monotonic()+30
        while time.monotonic()<deadline and not contention_release.is_set():
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                reserved = NativeRuntimeInput.select(db)
            if reserved:
                # The family holds the actual resource, not a replaced runtime
                # or invented Busy response. Each gate keeps its original IDs.
                with AdmissionContention.decode(arguments.contention_resource).custody(
                    service, len(names)
                ) as ready:
                    if not ready:
                        continue
                    begun = time.monotonic()
                    contention_held.set()
                    contention_release.wait(arguments.contention_seconds)
                    contention_observations.append({'held_seconds':time.monotonic()-begun,
                        'original_reserved_input':reserved[0].input_id,
                        'resource':arguments.contention_resource})
                return
            contention_release.wait(.01)

    def hold_cursor_publication():
        from agent_comms.store_files import _store_lock

        deadline = time.monotonic()+90
        while time.monotonic()<deadline and not contention_release.is_set():
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                ignored = {row.recipient for row in WakeAssignment.select(db)
                           if row.lifecycle.declared_name == 'ignored'}
            if not set(names) <= ignored:
                contention_release.wait(.02)
                continue
            opened_owners = []
            for name in names:
                owner = service.registry.require(name)
                if not owner.process_alive:
                    continue
                open_files = []
                for descriptor in Path(f'/proc/{owner.pid}/fd').iterdir():
                    try:
                        open_files.append(os.readlink(descriptor))
                    except FileNotFoundError:
                        pass
                if owner.session_file not in open_files:
                    break
                opened_owners.append(owner)
            if len(opened_owners) != len(names):
                contention_release.wait(.02)
                continue
            for owner in opened_owners:
                # The real ignored claim and still-open original journal place
                # this hold after native result settlement, inside corroboration.
                with _store_lock(service.root/'wire'):
                    begun = time.monotonic()
                    while not contention_release.is_set() and time.monotonic()-begun < 15:
                        stack = subprocess.run(
                            ['sudo','-n','/home/ts/.local/bin/py-spy','dump','--pid',
                             str(owner.pid),'--nonblocking'],capture_output=True,text=True,timeout=3)
                        frames = stack.stdout
                        if ('acquire_posix (' in frames and '_response_boundary (' in frames
                                and 'native_source_cursor.py' in frames):
                            (stage/'cursor-busy-original-scope.txt').write_text(frames)
                            contention_observations.append({
                                'resource':'cursor-publication-wire',
                                'held_seconds':time.monotonic()-begun,
                                'original_native_file_open':True,
                                'ignored_owner':owner.name,
                                'all_original_sources_open_after_ignore':len(opened_owners),
                                'original_cursor_lock_refusal_observed':True})
                            return
                        contention_release.wait(.03)
                    raise AssertionError('No actual bounded physical cursor acquisition witnessed')
            contention_release.wait(.02)

    contender = None
    retained_children = []
    cutover = PublishOriginalsAtStoppedBatch(service,names,collective=arguments.collective,
        after_attachment=arguments.cancel_before_grant)
    try:
        for name in names:
            if arguments.saved_source:
                output = (stage/(name+'-retained-owner.log')).open('w')
                child = subprocess.Popen([sys.executable, __file__, '--retained-owner'],
                    env=dict(env, AGENT_COMMS_THREAD=name), stdin=subprocess.PIPE,
                    stdout=output, stderr=subprocess.STDOUT, text=True)
                retained_children.append((child, output))
                async with asyncio.timeout(30):
                    while service.registry.require(name).pid != child.pid:
                        assert child.poll() is None, (stage/(name+'-retained-owner.log')).read_text()
                        await asyncio.sleep(.03)
            else:
                await asyncio.to_thread(service.owners.start,name,
                    agent_args=['--offline','--no-extensions','--no-skills',
                                '--no-context-files','--no-prompt-templates','--no-tools'])
            await attach(service,name)
        assert provider.posts == 0
        originals = [service.registry.require(name) for name in names]
        cutover = PublishOriginalsAtStoppedBatch(service,names,collective=arguments.collective,
            after_attachment=arguments.cancel_before_grant, wave_size=arguments.wave_size)
        if arguments.contention or arguments.cursor_contention:
            contender = threading.Thread(
                target=hold_cursor_publication if arguments.cursor_contention else hold_original_reader,
                name='original-cursor-publication' if arguments.cursor_contention else 'original-wire-reader')
            contender.start()
        if arguments.saved_source:
            from agent_comms.store_files import _store_lock
            with _store_lock(service.root/'wire'):
                sender = service.registry.require('human')
                cutover.originals = [service.bus.publisher.publish_ordinary(
                    Message(sender.name, '#team',
                        'New isolated retained-source triage: ignore THIS original once.', MessageType.INFO),
                    _human_origin=HumanOrigin(sender.name, sender.created_at, sender.worktree))]
            from agent_comms.coordination_cohort import accept_delivery_cohort
            initial = service.bus.log.read_delivery_cohort(root_id, cutover.originals[0].seq)
            with Coordination(str(service.root/'coordination.sqlite3')) as store:
                for audience in initial.audience.recipients:
                    store.participants.register(audience.recipient_lookup,
                        audience.canonical_thread, audience.canonical_thread, committed=True)
                accept_delivery_cohort(service.bus, root_id, cutover.originals[0].seq, store)
        else:
            result = await asyncio.to_thread(service.owners.restart_owners,names,cutover=cutover)
            assert len(result)==len(names)
            assert all(not owner.process_alive for owner in originals)
        if not arguments.contention or arguments.cancel_before_grant:
            await asyncio.gather(*(attachment.load_session(
                cwd=str(project), session_id=name) for name in names))
        for child, _ in retained_children:
            child.stdin.write('GO\n'); child.stdin.flush()
        if arguments.cancel_before_grant:
            await asyncio.to_thread(cutover.publish_cancel_and_pending)
            async with asyncio.timeout(30):
                while not contention_held.is_set():
                    await asyncio.sleep(.03)
                # Verify the real dedicated writer is waiting at admission;
                # readiness/lease alone would not prove the affected seam.
                while True:
                    owner = service.registry.require(names[0])
                    stack = await asyncio.to_thread(subprocess.run,
                        ['sudo','-n','/home/ts/.local/bin/py-spy','dump','--pid',
                         str(owner.pid),'--nonblocking'],capture_output=True,text=True,timeout=5)
                    if '_enter_admission' in stack.stdout:
                        (stage/'writer-wait-before-cancel.txt').write_text(stack.stdout)
                        break
                    await asyncio.sleep(.05)
            assert provider.posts == 0
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                cancelled_input, = NativeRuntimeInput.select(db)
            await asyncio.wait_for(attachment.cancel(names[0]), 15)
            assert provider.posts == 0
            cancel_observation = {'cancelled_input':cancelled_input.input_id,
                'localhost_posts_at_joined_cancel':0,'writer_wait_observed':True}
            contention_release.set()
            provider.response_gate.set()
        diagnosed = False
        concurrent_native = False
        async with asyncio.timeout(90 if arguments.saved_source else 45):
            while True:
                diagnostics = list((service.root/'diagnostics').glob('*.json'))
                for activity in service.agents.all_activity().values():
                    activity.readiness.require_available()
                if diagnostics:
                    observed = json.loads(diagnostics[0].read_text())
                    raise AssertionError(observed.get('source_error',observed.get('reason')))
                expected_posts = (4 if arguments.wave_size > 1 else
                                  2 if arguments.cancel_before_grant else len(names))
                if arguments.wave_size > 1 and provider.posts == 1 and late_original is None:
                    assert service.registry.require(names[0]).active_turn is not None
                    # All original batch members were reserved before this late
                    # arrival, while the real provider's first response is held.
                    with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                                 '?mode=ro',uri=True)) as db:
                        reserved = NativeRuntimeInput.select(db)
                        assert len(reserved) == 1
                        members = reserved[0].execution.source_assignment_ids(db, reserved[0].input_id)
                        assert len(members) == arguments.wave_size
                    sender = service.registry.require('human')
                    late_original = service.bus.publisher.publish_ordinary(Message(
                        sender.name, '#team', 'Batch wave late original: compute 20+3.', MessageType.INFO),
                        _human_origin=HumanOrigin(sender.name,sender.created_at,sender.worktree))
                    concurrent_native = True
                    provider.response_gate.set()
                if provider.posts == expected_posts:
                    if not provider.response_gate.is_set():
                        assert all(service.registry.require(name).active_turn is not None
                                   for name in names)
                        concurrent_native = True
                        provider.response_gate.set()
                    replies = service.bus.inbox('fixture-sender')
                    completed = len(replies)==len(names)
                    if arguments.collective:
                        with closing(sqlite3.connect(
                            (service.root/'coordination.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                            rows = WakeAssignment.select(db,where='wire_seq=?',
                                parameters=(cutover.originals[0].seq,))
                            if arguments.cancel_before_grant:
                                rows = WakeAssignment.select(db,where='wire_seq=?',
                                    parameters=(cutover.originals[1].seq,))
                                completed = len(rows)==1 and rows[0].lifecycle.declared_name=='completed'
                            elif arguments.wave_size > 1:
                                rows = WakeAssignment.select(db, where='wire_seq IN ('+
                                    ','.join('?' for _ in (*cutover.originals, late_original))+')',
                                    parameters=tuple(message.seq for message in (*cutover.originals, late_original)))
                                completed = len(rows) == arguments.wave_size+1 and all(
                                    row.lifecycle.completed for row in rows)
                            else:
                                completed = len(rows)==len(names) and all(
                                    row.lifecycle.declared_name=='ignored' for row in rows)
                    if completed and all(service.registry.require(name).active_turn is None
                                         for name in names):
                        break
                if not diagnosed and time.perf_counter()-started > 25:
                    diagnosed = True
                    for name in names:
                        owner = service.registry.require(name)
                        stack = await asyncio.to_thread(subprocess.run,
                            ['sudo','-n','/home/ts/.local/bin/py-spy','dump','--pid',
                             str(owner.pid),'--nonblocking'],capture_output=True,text=True,timeout=5)
                        (stage/(name+'-stack.txt')).write_text(stack.stdout+stack.stderr)
                await asyncio.sleep(.05)
        if arguments.cancel_before_grant:
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                retained = NativeRuntimeInput.one(db,input_id=cancelled_input.input_id)
                cancelled_assignment = WakeAssignment.one(db,assignment_id=retained.assignment_id)
                assert retained == cancelled_input
                assert cancelled_assignment.lifecycle.declared_name=='deferred'
            replies = service.bus.channel_history('#team')
            assert any(reply.sender==names[0] and reply.body=='Unique controlled native reply.'
                       for reply in replies)
            assert provider.posts == 2
        elif arguments.wave_size > 1:
            replies = [message for message in service.bus.channel_history('#team')
                       if message.sender == names[0]]
            assert len(replies) == 2
            assert replies[0].body == 'Combined wave answer: ' + '; '.join(
                f'10+{index}={10+index}' for index in range(arguments.wave_size)) + '.'
            assert replies[1].body == 'Separate late-wave answer: 20+3=23.'
            assert provider.posts == 4
            lookup = stable_thread_lookup(service.registry.require(names[0]).created_at)
            with Coordination(str(service.root/'coordination.sqlite3')) as store:
                inputs = NativeRuntimeInput.select(store.session._connection)
                assert len(inputs) == 4
                memberships = [row.execution.source_assignment_ids(store.session._connection, row.input_id)
                               for row in inputs]
                assert sorted(len(members) for members in memberships) == [1,1,arguments.wave_size,arguments.wave_size]
                for original in (*cutover.originals, late_original):
                    evidence = read_historical_native_inputs(store, wire_root_id=root_id,
                        recipient_lookup=lookup,source_seq=original.seq)
                    assert len(evidence) == 2
                    assert all(proof.expected_prompt_equality_established for proof in evidence)
                    assert len({proof.assignment_id for proof in evidence}) == 1
                batch_proof = {'captured_originals':arguments.wave_size,
                    'native_workflows':2,'original_wave_provider_inputs':2,
                    'late_wave_provider_inputs':2,'combined_original_wave_answers':1,
                    'per_original_native_source_proof':True,
                    'late_arrival_separate_batch':True}
        else:
            assert concurrent_native
            assert provider.posts==len(names),(provider.posts,len(names))
        if arguments.collective and not arguments.cancel_before_grant and arguments.wave_size == 1:
            assert replies == []
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                inputs = NativeRuntimeInput.select(db)
                assert len(inputs)==len(names)
                assert all(row.reference_stage is TriageNativeExecution and
                           row.verdict is IgnoreSelectedTriage for row in inputs)
                cursors = CurrentNativeCursor.select(db, where="input_id IS NOT NULL")
                assert len(cursors) == len(names), "Settled triage lost an auxiliary cursor"
                assert {row.input_id for row in cursors} == {row.input_id for row in inputs}
                assert all(row.covered_seq >= cutover.originals[0].seq for row in cursors)
        elif not arguments.cancel_before_grant and arguments.wave_size == 1:
            assert {reply.sender for reply in replies}==set(names)
        facts = [fact for packet in packets
                 for fact in decode_updates(packet['update'].get('_meta'))]
        if arguments.cursor_contention:
            assert contention_observations, 'No actual cursor publication contention observed'
        if arguments.contention and not arguments.cancel_before_grant:
            assert contention_held.is_set() and contention_observations
        else:
            assert {packet['session_id'] for packet in packets} == set(names)
            assert facts, 'No authoritative ACP notifications observed'
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
        raise
    finally:
        contention_release.set()
        if contender is not None:
            await asyncio.to_thread(contender.join,35)
            assert not contender.is_alive()
        await attachment.shutdown()
        for child, output in retained_children:
            if child.poll() is None:
                child.stdin.close()
                await asyncio.to_thread(child.wait, 15)
            output.close()
        for name in reversed(names):
            await asyncio.to_thread(service.owners.stop,name)
        server.close();await server.wait_closed()
        for task in tuple(connections):task.cancel()
        await asyncio.gather(*connections,return_exceptions=True)
        native_inputs=[]
        for source, expected in source_hashes.items():
            assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == expected
        for path in (service.root/'native-sessions').rglob('*.jsonl'):
            for row in map(json.loads,path.read_text().splitlines()):
                if row.get('type')=='message' and row['message'].get('role')=='user' and any(
                        original.body in json.dumps(row['message'].get('content')) for original in cutover.originals):
                    native_inputs.append({'session':str(path.relative_to(stage)),
                                          'entry_id':row['id']})
        if failure is None:
            assert len(native_inputs) == (4 if arguments.wave_size > 1 else
                                         2 if arguments.cancel_before_grant else len(names)), native_inputs
        receipt={'elapsed_seconds':time.perf_counter()-started,'owners':len(names),
            'history_rows':arguments.history,'saved_original_hashes':source_hashes,
            'saved_source_bytes':arguments.saved_source.stat().st_size if arguments.saved_source else 0,
            'bus_bytes':(service.root/'bus.jsonl').stat().st_size,
            'localhost_provider_posts':provider.posts,'native_originals':native_inputs,
            'simultaneous_triage_cursor_proofs':len(cursors) if arguments.collective and failure is None and not arguments.cancel_before_grant and arguments.wave_size == 1 else None,
            'failure':failure,'all_owned_workers_retired':all(
                not service.registry.require(name).process_alive for name in names),
            'public_mutations':0,'paid_provider_calls':0,'original_replays':0,
            'installed_interpreter':sys.executable,
            'acp_notification_count':len(packets),'contention':contention_observations,
            'cancellation':cancel_observation,
            'batch':batch_proof,
            'all_native_turns_active_before_provider_release':concurrent_native if failure is None else False,
            'acp_fact_counts':{kind:sum(type(fact).__name__ == kind for fact in facts)
                for kind in sorted({type(fact).__name__ for fact in facts})} if failure is None else {}}
        (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2),flush=True)


if __name__=='__main__':
    if sys.argv[1:] == ['--retained-owner']:
        asyncio.run(retained_owner())
        raise SystemExit(0)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--owners',type=int,default=3)
    parser.add_argument('--saved-source',type=Path)
    parser.add_argument('--cursor-contention',action='store_true')
    parser.add_argument('--history',type=int,default=259)
    parser.add_argument('--collective',action='store_true')
    parser.add_argument('--registry-size',type=int,default=0)
    parser.add_argument('--contention',action='store_true')
    parser.add_argument('--contention-seconds',type=float,default=12)
    parser.add_argument('--contention-resource',default='wire',
                        choices=[kind.declared_name for kind in AdmissionContention.members_with(AdmissionContention)])
    parser.add_argument('--cancel-before-grant',action='store_true')
    parser.add_argument('--wave-size',type=int,default=1)
    parser.add_argument('--configured-owner',help='Read only this original model/level; submit fresh private mixed-route originals')
    parser.add_argument('--configured-pure-channel',action='store_true')
    parser.add_argument('--continue-task-timing', action='store_true',
                        help='Two distinct new inputs on the existing stage owner; preserve its original receipt')
    parser.add_argument('--configured-task-timing',action='store_true',
                        help='One configured saved fork: explicit subtask then optional compaction and new original continuation')
    parser.add_argument('--configured-saved-preparation',action='store_true',
                        help='One saved configured fork: ordinary ACP admission, cancel, then a distinct new original')
    parser.add_argument('--busy-reader',action='store_true')
    parser.add_argument('--configured-peers',nargs='*',default=[])
    parser.add_argument('--observation-seconds',type=float,default=180,
                        help='Bound this private observer; never alters native turn/provider policy')
    asyncio.run(run(parser.parse_args()))
