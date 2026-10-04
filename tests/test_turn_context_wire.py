"""Original sealed messaging survives silent context rows through every projection."""
from dataclasses import replace
import hashlib
import json

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.turn_context import ContextManifest, RecordedContextTurn, SegmentManifest, OwnerProvenance, TurnContext, TranscriptSegment, UserInputSegment
from agent_comms.pi_commands import Prompt
from agent_comms.image_inputs import ImageInput
from agent_comms.wire_record import WireRecord, ObservationWireRecord, ContextManifestWireObservation
from agent_comms.cli_commands import ContextCliCommand
from agent_comms.comms import Comms
from test_private_bus_checkpoint import _root, _page
from agent_comms.bus_publication import stable_thread_lookup


def manifest(owner, generation=1):
    source=OwnerProvenance(owner.incarnation, 'original-input-source')
    segment=SegmentManifest(TranscriptSegment,(source,),hashlib.sha256(b'PRIVATE INPUT').hexdigest(),13,4)
    return ContextManifest(owner.incarnation,RecordedContextTurn(TurnId('original-turn'),TurnIdentity(owner.incarnation,generation)),(segment,),'pi.estimateTokens')


@pytest.mark.asyncio
async def test_installed_context_callbacks_share_original_writer_custody(tmp_path):
    """Both real event owners publish original observations without blocking.

    Run once with the shared receiving interpreter and an original native root.
    This provider-free recording fixture does not replay an input or claim a new
    native observation. Its source segments remain the original SDK metadata.
    """
    import asyncio
    from contextlib import ExitStack
    import os
    from pathlib import Path
    import sys
    import threading
    import time

    import agent_comms
    from agent_comms.acp import CommsAgent
    from agent_comms.agent_events import ContextObserved
    from agent_comms.channel_input_batch import SingleInputBatch
    from agent_comms.coordination_cohort import accept_delivery_cohort
    from agent_comms.coordinator import Coordination
    from agent_comms.native_turn_context import NativeContextManifestData
    from agent_comms.pi_events import TurnContextObserved
    from agent_comms.routing import TurnRouting
    from agent_comms.selected_participant import SelectedParticipant
    from agent_comms.turn_goal_account import TurnGoalAccount
    from agent_comms.turn_goal_permission import InactiveGoalPermission
    from agent_comms.turn_input_source import NoInputDependency, RoutedOriginalInput
    from agent_comms.turn_progress import TurnProgress
    from agent_comms.owned_turn import OwnedTurn

    installed = Path(agent_comms.__file__).resolve()
    assert 'site-packages' in installed.parts, 'This journey requires installed application source'
    original = Comms(Path(os.environ['CONTEXT_MANIFEST_ORIGINAL_ROOT']))
    source_files = (original.root / 'registry.json', original.root / 'bus.jsonl')
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    source_snapshot = original.registry.snapshot()
    observations = tuple((thread, item) for thread in source_snapshot.threads.values()
        for item in original.bus.log.context_manifests(thread.name, original.registry))
    observed_owner, observed = observations[0]
    data = NativeContextManifestData(observed.counter, observed.segments, request_id=observed.request_id)
    comms, root_id = _root(tmp_path)
    comms.agents.set_agent_info('Bob', model=observed_owner.model)
    original_message = comms.messaging.send_initial_cohort('sender', '#team', '@Bob recording fixture')
    owner = CommsAgent(comms, auto_wake=False)
    turn = OwnedTurn(owner.turns, 'Alice', 'Alice', '')
    turn.registry_owner = comms.agents.begin_turn('Alice', 'owned-context-recording515')
    lease, thread = turn.turn_lease, turn.thread
    turn.routing = TurnRouting()
    turn.checkpoint = comms.transcripts.transcript_checkpoint('Alice')
    turn.original = RoutedOriginalInput(accepted_id=None, goal_permission=InactiveGoalPermission(),
        prompt='', original_display=None, origins=(), dependency=NoInputDependency(),
        batch=SingleInputBatch(()))
    resources = ExitStack()
    goals = TurnGoalAccount(comms=comms, owner=thread, turn=TurnId(lease.turn_id),
        lease=lease, permit=None, open_store=owner.turns.goals.open_goal_store,
        pending_origins=owner.turns.goals.pending_goal_origins, claims=resources)
    progress = TurnProgress(comms=comms, sessions=owner.sessions, inputs=owner.inputs,
        effects=owner, runtime=owner._runtime, emitted_errors=owner.turns.emitted_errors,
        session_id='Alice', turn=turn, finish_event=asyncio.Event(),
        goals=goals, sync_goals=owner.turns.goals.sync_goal_execution)
    results = []

    async def publish_while_contended(consumer, event, *, cancel):
        acquired, release = threading.Event(), threading.Event()
        def hold_original_writer():
            with comms.bus.log.locked():
                acquired.set()
                release.wait()
        holder = threading.Thread(target=hold_original_writer)
        holder.start()
        assert await asyncio.to_thread(acquired.wait, 2)
        watchdog = threading.Timer(2, release.set)
        watchdog.start()
        operation = asyncio.create_task(consumer.dispatch(event))
        try:
            started = time.monotonic()
            await asyncio.sleep(0.03)
            heartbeat = time.monotonic() - started
            assert heartbeat < 0.5, 'Canonical writer blocked the application event loop'
            assert not operation.done(), 'Publication must await the original locked writer'
            if cancel:
                operation.cancel()
                await asyncio.sleep(0.03)
                assert not operation.done(), 'Cancellation escaped before worker custody retired'
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await operation
            else:
                await operation
            results.append({'consumer':type(consumer).__name__, 'heartbeat_seconds':heartbeat,
                            'cancelled_after_join':cancel})
        finally:
            release.set()
            watchdog.cancel()
            await asyncio.to_thread(holder.join)
            if not operation.done():
                operation.cancel()
                try:
                    await operation
                except asyncio.CancelledError:
                    pass

    try:
        with Coordination(str(comms.root / 'coordination.sqlite3')) as store:
            for registered in comms.registry.snapshot().threads.values():
                store.participants.register(stable_thread_lookup(registered.created_at),
                    registered.name, registered.name, committed=True)
            accept_delivery_cohort(comms.bus, root_id, original_message.seq, store)
            await publish_while_contended(progress, ContextObserved(data), cancel=True)
            async with SelectedParticipant.select(comms, store, root_id, 'Bob', 0) as selected:
                await publish_while_contended(selected, TurnContextObserved(context=data), cancel=False)
            selected_rows = comms.bus.log.context_manifests('Bob', comms.registry)
            ordinary_rows = comms.bus.log.context_manifests('Alice', comms.registry)
            assert len(selected_rows) == len(ordinary_rows) == 1
            assert selected_rows[0].segments == ordinary_rows[0].segments == observed.segments
            assert ordinary_rows[0].turn == RecordedContextTurn(TurnId(lease.turn_id), lease.identity)
            assert comms.bus.log.latest_sequence() == original_message.seq
            assert comms.bus.log.full_history() == [original_message]
            from subprocess import run
            command = [str(Path(sys.executable).with_name('agent-comms')), '--root', str(comms.root),
                       'context', 'Alice', '--turn', str(lease.identity.generation)]
            output = run(command, capture_output=True, text=True, check=True)
            cli = json.loads(output.stdout)
            assert cli['manifests'] == FieldCodec.encode(ordinary_rows)
            assert cli['text_recorded'] is False
            assert {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files} == source_hashes
            (tmp_path / 'installed-context-callback-receipt.json').write_text(json.dumps({
                'state':'PASS', 'installed_source':str(installed), 'original_root':str(original.root),
                'original_source_hashes':source_hashes, 'source_owner':FieldCodec.encode(observed.thread),
                'source_turn':FieldCodec.encode(observed.turn), 'recorded_segments_unchanged':True,
                'callbacks':results, 'manifest_cli':command, 'manifest_cli_returncode':output.returncode,
                'public_sequence_unchanged':True, 'original_files_unchanged':True,
                'provider_calls':0, 'native_inputs':0,
                'scope':'Actual installed recording owners/locks/CLI; captured SDK metadata, no fresh native/provider qualification'
            }, indent=2) + '\n')
    finally:
        resources.close()
        comms.agents.finish_turn(lease)
        await owner.shutdown()


def test_silent_manifest_continuous_original_message_and_cold_projection(tmp_path, monkeypatch):
    comms,root_id=_root(tmp_path)
    first=comms.messaging.send_initial_cohort('sender','#team','@Alice original question')
    lookup=stable_thread_lookup(17002.0)
    prior=_page(comms,lookup)
    raw=comms.bus.log.path.read_bytes()
    activity=comms.bus.channel_activity()
    pending=comms.bus.pending_counts_all(['Alice','Bob'])
    awareness=comms.bus.awareness_segments(comms.registry.require('Alice'))
    comms.bus.log.record_context(manifest(comms.registry.require('Alice')))
    assert comms.bus.log.path.read_bytes().startswith(raw)
    assert b'PRIVATE INPUT' not in comms.bus.log.path.read_bytes()[len(raw):]
    assert comms.bus.log.latest_sequence()==first.seq
    assert comms.bus.log.total_messages()==1
    assert _page(comms,lookup)[0].offset>prior[0].offset
    assert _page(comms,lookup)[1:]==prior[1:]
    assert comms.bus.channel_activity()==activity
    assert comms.bus.pending_counts_all(['Alice','Bob'])==pending
    assert comms.bus.awareness_segments(comms.registry.require('Alice'))==awareness
    assert comms.bus.log.full_history()==[first]
    with comms.bus.log.full_history_snapshot() as (_, messages):
        assert list(messages)==[first]
    second=comms.messaging.send_initial_cohort('sender','#team','@Alice next original message')
    assert second.seq==first.seq+1
    comms.bus.log.record_context(manifest(comms.registry.require('Alice'),2))
    reopened=Comms(comms.root)
    assert reopened.bus.log.full_history()==[first,second]
    assert len(_page(reopened,lookup)[1])==2
    monkeypatch.setattr(type(reopened.relationships), "RECENT_MESSAGES", 2)
    recent, limited = reopened.relationships._recent_messages()
    assert recent == (first, second) and not limited
    assert reopened.bus.pending_counts_all(['Alice','Bob'])['Alice']==2
    recorded = ContextCliCommand(thread='Alice',turn=1)
    assert len(recorded.encode_result(recorded.apply(reopened))['manifests'])==1
    difference = ContextCliCommand(thread='Alice',diff=True)
    assert difference.encode_result(difference.apply(reopened))['turn']['occurrence']['generation']==2


def test_observation_family_rejects_message_fields_and_preview(tmp_path):
    comms,root_id=_root(tmp_path)
    row=ObservationWireRecord(ContextManifestWireObservation(manifest(comms.registry.require('Alice'))))
    assert WireRecord.from_wire(row.to_wire(),root_id).messages()==()
    assert row.sequence_after(39)==39
    with pytest.raises(ValueError):
        WireRecord.from_wire({**row.to_wire(),'seq':40},root_id)


def test_recorded_context_history_retains_rename_and_original_predecessor(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    original = manifest(owner)
    second = manifest(owner, 2)
    comms.bus.log.record_context(original)
    comms.bus.log.record_context(second)
    comms.registry.rename('Alice', 'Renamed-Alice')
    renamed = comms.registry.require('Renamed-Alice')
    same_turn = replace(second, thread=renamed.incarnation,
                        turn=replace(second.turn,
                            occurrence=replace(second.turn.occurrence,
                                               incarnation=renamed.incarnation)))
    comms.bus.log.record_context(same_turn)
    future = manifest(renamed, 3)
    comms.bus.log.record_context(future)
    raw = comms.bus.log.path.read_bytes()
    reopened = Comms(comms.root)
    history = reopened.bus.log.context_manifests('Alice', reopened.registry)
    assert history == (original, second, same_turn, future)
    assert reopened.bus.log.context_manifests('Renamed-Alice', reopened.registry) == history
    recorded = ContextCliCommand(thread='Renamed-Alice', turn=1)
    assert recorded.encode_result(recorded.apply(reopened))['manifests'] == FieldCodec.encode((original,))
    compare = ContextCliCommand(thread='Alice', turn=2, diff=True)
    difference = compare.encode_result(compare.apply(reopened))
    assert difference['previous_turn'] == FieldCodec.encode(original.turn)
    assert difference['turn'] == FieldCodec.encode(same_turn.turn)
    latest = ContextCliCommand(thread='Renamed-Alice', diff=True)
    assert latest.encode_result(latest.apply(reopened))['previous_turn'] == FieldCodec.encode(same_turn.turn)
    with pytest.raises(ValueError, match='No preceding recorded turn'):
        ContextCliCommand(thread='Alice', turn=1, diff=True).apply(reopened)
    with pytest.raises(ValueError, match='outside the original history'):
        replace(future, counter='unrecorded').changed_from_history(history)
    comms.registry.unregister('Renamed-Alice')
    comms.registry.remove('Renamed-Alice')
    comms.registry.register(replace(renamed, created_at=18002.0))
    assert comms.bus.log.context_manifests('Renamed-Alice', comms.registry) == ()
    assert comms.bus.log.path.read_bytes() == raw


def test_context_pointer_family_rebuilds_and_decodes_only_selected_originals(tmp_path, monkeypatch):
    """Original wire lookup and cold recovery share declaration-owned pointers."""
    import cProfile
    import agent_comms.private_bus_checkpoint as checkpoint
    from agent_comms.errors import RelationViolationError

    comms, _ = _root(tmp_path)
    for index in range(12):
        comms.messaging.send_initial_cohort('sender', '#team', f'@Alice original-{index}')
    alice = comms.registry.require('Alice')
    selected = manifest(alice)
    other = manifest(comms.registry.require('Bob'))
    comms.bus.log.record_context(selected)
    comms.bus.log.record_context(other)
    original = comms.bus.log.path.read_bytes()
    profile = cProfile.Profile()
    profile.enable()
    assert comms.bus.log.context_manifests('Alice', comms.registry) == (selected,)
    profile.disable()
    calls = profile.getstats()
    assert not any(getattr(call.code, 'co_name', '') == '_snapshot_records' for call in calls)
    decodes = [call for call in calls if getattr(call.code, 'co_name', '') == 'decode_bytes']
    assert sum(call.callcount for call in decodes) == 1
    assert comms.bus.log.path.read_bytes() == original

    # A durable append before index publication is UNKNOWN. The canonical cold
    # recovery must replace ALL derived members, including prior observations.
    with monkeypatch.context() as patch:
        patch.setattr(checkpoint, 'append_private_bus_checkpoint_unlocked',
                      lambda *_: (_ for _ in ()).throw(OSError('interrupted index publication')))
        with pytest.raises(RelationViolationError, match='outcome UNKNOWN'):
            comms.bus.log.record_context(replace(selected, counter='original-second-observation'))
    expected = (selected, replace(selected, counter='original-second-observation'))
    assert Comms(comms.root).bus.log.context_manifests('Alice', comms.registry) == expected
    assert comms.bus.log.path.read_bytes().startswith(original)
    assert comms.bus.log.latest_sequence() == 12
    assert len(comms.bus.log.full_history()) == 12

    # Changing a sealed pointer file cannot authorize a rebuilt or empty view.
    index = comms.root / 'private_bus_checkpoint.sqlite3'
    index.write_bytes(index.read_bytes() + b'changed sealed index')
    with pytest.raises(RelationViolationError):
        comms.bus.log.context_manifests('Alice', comms.registry)


def test_rendered_contributors_remain_original_bytes_through_prompt_boundary(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    images = (ImageInput('YQ==', 'image/png'),)
    context = TurnContext.for_owner(owner, manifest(owner).turn, 'Original unicode task π 🙂', ())
    rendered = context.render(images=images)
    expected = ''.join(segment.text() for segment in context.segments)
    assert rendered.text == expected
    raw = expected.encode()
    for source in rendered.contributions:
        assert hashlib.sha256(raw[source.offset:source.offset+source.length]).hexdigest() == source.sha256
    assert sum(source.length for source in rendered.contributions) == len(raw)
    assert next(source for source in rendered.contributions if source.kind is UserInputSegment).images == (0,)
    command = Prompt(input_id='a'*32, message=rendered.text, images=images,
                     context_contributions=rendered.contributions)
    decoded = Prompt.from_wire(command.to_rpc())
    assert decoded == command
    assert decoded.message.encode() == raw and decoded.images == images


def test_current_relevance_resource_is_frozen_with_coordination_provenance(tmp_path, monkeypatch):
    from agent_comms.turn_context import CoordinationSegment, InstructionFile
    from agent_comms.wake_policy import WakePolicy

    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    original = WakePolicy.relevance_instruction()
    captured = CoordinationSegment.capture(owner, ())
    assert original.source in captured.provenance
    assert hashlib.sha256(original.content.encode()).hexdigest() == original.source.sha256

    def changed_instruction(cls, name):
        raise AssertionError('Captured instructions must not reread the current file')

    monkeypatch.setattr(InstructionFile, 'read', classmethod(changed_instruction))
    assert original.content in captured.text()
    assert original.content in captured.summary_instructions(None)
    assert captured.response_instruction.source == original.source
