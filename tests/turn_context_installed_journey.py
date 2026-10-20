"""Run the existing continuous context case without source/conftest substitutes."""
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import agent_comms
from agent_comms.cli_commands import ContextCliCommand
from stored_inputs import stored_inputs
from agent_comms.goal_waits import NoGoalWait


async def configured_terminal(owner, thread, output, acknowledgement, marker):
    """One installed stdio ACP attachment on the existing configured SDK fork."""
    from acp import spawn_agent_process
    from acp.schema import TextContentBlock
    from agent_comms.coordinator import Coordination
    from agent_comms.field_codec import FieldCodec
    from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
    from agent_comms.threads import Thread
    from explicit_private_owner_installed_journey import Subscriber

    service = owner._comms
    subscriber = Subscriber()
    text = ('Bounded acceptance only. Do not resume inherited tasks or goals, use tools, '
            f'or change files. Reply exactly {marker}, then stop.')
    started = time.monotonic()
    async with spawn_agent_process(subscriber, sys.executable, '-m', 'agent_comms.acp',
        cwd=thread.worktree, env=dict(os.environ)) as (connection, process):
        acknowledgement['acp_pid'] = process.pid
        await connection.initialize(protocol_version=1)
        await connection.load_session(cwd=thread.worktree, session_id=thread.name, mcp_servers=[])
        acknowledgement['state'] = 'ONE_DISTINCT_CONFIGURED_ACP_PROMPT_NO_RETRY'
        (output.parent / 'terminal-acknowledgement.json').write_text(json.dumps(acknowledgement, indent=2))
        pending = asyncio.create_task(connection.prompt(thread.name,
            [TextContentBlock(type='text', text=text)]))
        async with asyncio.timeout(300):
            while not service.registry.require(thread.name).active_turn.executing:
                if pending.done():
                    await pending
                    raise AssertionError('Original turn completed before waiter ownership capture')
                await asyncio.sleep(.02)
            lease = service.registry.require(thread.name).turn_lease

            def wait_on_original():
                service.registry.declare(service.registry.new_thread('terminal-waiter', frozenset(), thread.worktree))
                goal = service.goals.update_goal('terminal-waiter', SetGoalAction(text='Wait for the original configured turn'))
                service.goals.update_goal('terminal-waiter', StandbyGoalAction(
                    expect=GoalPrecondition(goal_id=goal.id), wait_for=(thread.name,)))
                return goal.id

            waiter_goal = await Coordination.run_worker(wait_on_original)
            response = await pending
        state = service.registry.require(thread.name).turn_state
        assert not state.active.phase.busy and state.finished_turn_id == lease.turn_id
        assert isinstance(service.goals.goal_wait('terminal-waiter'), NoGoalWait)
        assert service.registry.require('terminal-waiter').goal.state.active
        assert response.stop_reason == 'end_turn'
        assert any(marker in str(update) for update in subscriber.updates)
        child = owner.turns.persistent_backends[thread.name].child_state.idle().child
        acknowledgement.update(original_lease=FieldCodec.encode(lease), terminal_state=FieldCodec.encode(state),
            original_waiter_goal=waiter_goal, waiter_released=True,
            acp_response=response.model_dump(by_alias=True, exclude_none=True),
            native_process=FieldCodec.encode(child.proc.identity),
            prompt_through_return_seconds=time.monotonic() - started)
        (output / 'original-acp-updates.json').write_text(json.dumps(subscriber.updates, indent=2))
    assert process.returncode is not None
    acknowledgement['acp_exited'] = True
    return marker, child


async def read_actual_context_publication(owner, thread, child, original_input, output, acknowledgement):
    """Read the manifest emitted by the one actual model turn, never publish it."""
    import hashlib
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_pi import NativeContextProof
    from agent_comms.runtime import RuntimeConnection, socket_path
    from agent_comms.turn_context import (
        ContextSourceText, NativeProvenance, SystemLayerSegment, ToolCatalogSegment, TranscriptSegment,
    )

    service = owner._comms
    page = service.contexts.page(thread.name, service.registry)
    while True:
        selected = tuple(resource.value for resource in page.resources
                         if resource.value.turn.identity.value == original_input.turn_id)
        if selected or not page.has_older():
            break
        page = service.contexts.page(thread.name, service.registry, cursor=page.older)
    manifest, = selected
    record = FieldCodec.encode(page.reference(manifest))
    request_id = manifest.require_request_id()
    proof = NativeContextProof.read_evidence(Path(thread.require_saved_session()), original_input.native_id)
    native_sources = tuple(source for segment in manifest.segments
                           for source in segment.provenance if isinstance(source, NativeProvenance))
    assert native_sources
    assert all(source.request_generation == proof.request_generation
               and source.context_digest == proof.llm_context_digest
               and source.identity.session_id == proof.session_id for source in native_sources)
    system = next(segment for segment in manifest.segments if segment.kind is SystemLayerSegment)
    assert system.captured_text and system.public_text_recorded
    assert any(segment.captured_text for segment in manifest.segments
               if segment.kind is ToolCatalogSegment)
    position = next(i for i, segment in enumerate(manifest.segments)
                    if segment.kind is TranscriptSegment and len(segment.contributors) > 1)
    group = manifest.selected_segment(position)
    part = next(i for i, part in enumerate(group.contributors) if not part.public_text_recorded)
    selected = manifest.selected_segment(position, (part,))
    assert group.sha256 != selected.sha256
    before = Path(thread.require_saved_session()).read_bytes()
    connection = RuntimeConnection(service, thread.name,
        socket_path(service.root, thread.require_process().pid))
    try:
        params = dict(turn=FieldCodec.encode(manifest.turn), request_id=request_id,
                      record=record, segment=position)
        root = FieldCodec.decode(ContextSourceText,
            await connection.request('context_recorded_segment', **params))
        exact_child = FieldCodec.decode(ContextSourceText,
            await connection.request('context_recorded_segment', **params, contributors=[part]))
        original_system = FieldCodec.decode(ContextSourceText,
            await connection.request('context_recorded_segment', turn=FieldCodec.encode(manifest.turn),
                request_id=request_id, record=record, segment=manifest.segments.index(system)))
        assert root.text != exact_child.text
        assert exact_child.text in root.text
        assert original_system.text == '\n'.join(system.captured_text)
        assert Path(thread.require_saved_session()).read_bytes() == before
        assert owner.turns.persistent_backends[thread.name].child_state.idle().child is child
    finally:
        await connection.close()
    updates = json.loads((output / 'original-acp-updates.json').read_text())
    usage = [value for value in updates if value.get('update', value).get('sessionUpdate') == 'usage_update']
    # Manifest values are the existing public renderer's captured strings; source
    # message content is recovered and filtered by the same PiMessage renderer.
    acknowledgement.update(model_onContextReady_publication=True, request_id=request_id,
        native_request_generation=proof.request_generation, actual_manifest_lease=True,
        captured_system_text_read=True, transcript_original_root_and_child_read=True,
        public_renderer_only=True, same_native_child=True, source_query_bytes_unchanged=True,
        captured_system_utf8_bytes=len(original_system.text.encode()),
        captured_system_sha256=hashlib.sha256(original_system.text.encode()).hexdigest(),
        recorded_root_utf8_bytes=len(root.text.encode()),
        recorded_child_utf8_bytes=len(exact_child.text.encode()),
        original_acp_usage_updates=len(usage), final_HTTP_evaluated=False)


async def run(root, receiving_only=False, *, package: VerifiedNativePackage):
    from pytest import MonkeyPatch
    from native_backend_fixture import native_backend_fixture
    from test_native_context_inspection import test_context_manifest_native_acp_and_cli_continuous

    started = time.monotonic()
    root.mkdir(mode=0o700)
    original = None
    acknowledgement = {"python": sys.executable, "core": agent_comms.__file__, "fixture": str(root),
               "public_inputs": 0, "paid_provider_calls": 0}
    try:
        with MonkeyPatch.context() as monkey:
            monkey.delenv("AGENT_COMMS_THREAD", raising=False)
            async with native_backend_fixture(root, package=package) as original:
                async with asyncio.timeout(90):
                    await test_context_manifest_native_acp_and_cli_continuous(
                        original, receiving_only=receiving_only)
        acknowledgement["state"] = "SCOPED_PASS"
    except BaseException as error:
        acknowledgement["state"] = "FAILED_NO_REPLAY"
        acknowledgement["error"] = repr(error)
        raise
    finally:
        if original is not None:
            acknowledgement["local_provider_posts"] = original.provider.posts
            requests = root / 'original-provider-requests.json'
            requests.write_text(json.dumps(original.provider.requests))
            acknowledgement['original_provider_requests'] = str(requests)
        acknowledgement["elapsed_seconds"] = time.monotonic() - started
        (root / "terminal-acknowledgement.json").write_text(json.dumps(acknowledgement, indent=2))
        print(json.dumps(acknowledgement), flush=True)


async def run_configured(options):
    """Original configured saved fork; context-only does not submit input."""
    import hashlib
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
    from agent_comms.native_entries import NativeEntry
    from agent_comms.native_package import verify_native_package
    from agent_comms.threads import Thread
    from delivery_owner_fixture import canonical_agent
    from agent_comms.owner_launch import RetainedOwnerLaunch
    from agent_comms.owner_lifecycle import OwnerRestartSelection

    started = time.monotonic()
    root = options.root.resolve()
    root.mkdir(mode=0o700)
    output = root / 'configured-context-journey'
    output.mkdir(mode=0o700)
    acknowledgement = {'state': 'PREPARING_ORIGINAL_CONFIGURED_SOURCE',
        'python': sys.executable, 'core': agent_comms.__file__,
        'fixture': str(root), 'public_inputs': 0, 'original_inputs_retried': 0}
    owner = None
    try:
        original_service = Comms(options.configured_source_root)
        original_snapshot = original_service.registry.snapshot()
        selection = OwnerRestartSelection.capture(original_snapshot, options.configured_source_name)
        source = selection.require_current(original_snapshot)
        retained = RetainedOwnerLaunch.capture(selection, original_snapshot)
        original_file = Path(source.require_saved_session())
        original_digest = hashlib.sha256(original_file.read_bytes()).hexdigest()
        acknowledgement.update(model=source.model, thinking=source.thinking_level.declared_name,
            original_name=source.name, original_worktree=source.worktree,
            source_file=str(original_file), source_bytes=original_file.stat().st_size,
            original_process=FieldCodec.encode(source.require_process()),
            original_sha256=original_digest)
        package = pinned_native_package()
        verify_native_package(package)
        project = root / 'project'
        project.mkdir(mode=0o700)
        environment = dict(retained.environment)
        service = Comms(root / 'wire')
        identity = await ForkSessionHelper.run(ForkSessionRequest(str(package),
            str(original_file), str(project), str(root / 'native-forks')), cwd=project, env=environment)
        assert Path(identity.session_file).is_relative_to(root)
        selection.require_current(original_service.registry.snapshot())
        root_id = service.messaging.initialize_private_initial_protocol()
        runtime = Path(sys.executable).parent
        environment.update(AGENT_COMMS_ROOT=str(service.root),
            AGENT_COMMS_AGENT_BIN=str(runtime / 'pi-comms-native'),
            PATH=str(runtime) + os.pathsep + environment.get('PATH', os.defpath),
            XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
            XDG_DATA_HOME=str(root / 'data'), AGENT_COMMS_DEBUG_LOG=str(root / 'owner-debug.log'))
        for name in ('PYTHONPATH', 'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY',
            'PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK'):
            environment.pop(name, None)
        # Credentials remain only in the acquired launch environment. The
        # explicit fork path owns all native writes; original accounts stay intact.
        os.environ.clear()
        os.environ.update(environment)
        owner = canonical_agent(service, agent_bin=str(runtime / 'pi-comms-native'),
            agent_args=list(retained.arguments or ()), auto_wake=False,
            runtime_enabled=True)
        thread = service.registry.new_thread('configured-source', frozenset(), str(project), process_identity=ProcessIdentity.capture(os.getpid()), session_file=identity.session_file, model=source.model, thinking_level=source.thinking_level, task='Bounded acceptance only. Do not resume inherited work or use tools.')
        service.registry.declare(thread)
        if options.terminal_only or options.recorded_publication:
            # The original stopped-source owner populates private participant
            # membership before this controller acquires its own process.
            service.threads.restore_stopped(service.registry.snapshot(), (thread.name,))
            thread = service.owners.acquire_thread(thread.name, owner_pid=os.getpid())
        await owner._runtime.start()
        await owner.load_session(str(project), thread.name)
        assert thread.name not in owner.turns.persistent_backends

        if options.terminal_only or options.recorded_publication:
            token, child = await configured_terminal(owner, thread, output, acknowledgement,
                                                     options.terminal_marker)
            stored = stored_inputs(service.root)
            original, = stored.rows.values()
            assert original.has_started
            with NativeEntry.open_evidence(Path(identity.session_file)) as reader:
                _, entries = reader.observe()
            user, = (entry for entry in entries if entry.input_id == original.native_id)
            replies = tuple(entry for entry in entries[entries.index(user) + 1:] if entry.final_reply)
            assert replies[-1].message.authoritative_text.strip() == token
            assert hashlib.sha256(original_file.read_bytes()).hexdigest() == original_digest
            acknowledgement.update(state='SCOPED_CONFIGURED_SDK_ACP_NATIVE_TERMINAL_PASS',
                original_input=FieldCodec.encode(original), original_native_user=user.id,
                original_native_reply=replies[-1].id, source_unchanged=True,
                private_root=str(service.root), fork_file=identity.session_file)
            if options.recorded_publication:
                await read_actual_context_publication(owner, service.registry.require(thread.name), child, original, output, acknowledgement)
                acknowledgement['state'] = 'CONFIGURED_MODEL_ON_CONTEXT_READY_PUBLISHED_AND_READ_PASS'
            return

        async def query(*arguments):
            child = await asyncio.create_subprocess_exec(sys.executable, '-m',
                'agent_comms.cli', '--root', str(service.root), 'context', thread.name,
                *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            data, error = await child.communicate()
            assert child.returncode == 0, error.decode()
            return json.loads(data)

        selected = Path(identity.session_file)
        before = selected.read_bytes()
        preview = await query()
        (output / 'next-context.json').write_text(json.dumps(preview))
        assert selected.read_bytes() == before
        if options.context_only:
            persistent = owner.turns.persistent_backends[thread.name]
            child = persistent.child_state.idle().child
            assert child.proc.alive()
            repeated = await query()
            assert repeated == preview
            assert persistent.child_state.idle().child is child
            assert selected.read_bytes() == before
            assert not stored_inputs(service.root).rows
            assert hashlib.sha256(original_file.read_bytes()).hexdigest() == original_digest
            selection.require_current(original_service.registry.snapshot())
            acknowledgement.update(state='SCOPED_COLD_CONFIGURED_SAVED_CONTEXT_PASS',
                priming_calls=0, native_prompts=0, provider_requests=0,
                private_root=str(service.root), fork_file=str(selected),
                fork_sha256=hashlib.sha256(before).hexdigest(), source_unchanged=True,
                query_preserved_journal=True, repeated_same_child=True,
                native_process=FieldCodec.encode(child.proc.identity),
                context_segments=len(preview['native_manifest']))
            return
        from retained_input_origin_observer import actual_s2_ingress
        baseline = service.bus.log.latest_sequence()
        acknowledgement['state'] = 'ONE_CONFIGURED_INPUT_ABOUT_TO_BE_SUBMITTED_NO_RETRY'
        (root / 'terminal-acknowledgement.json').write_text(json.dumps(acknowledgement, indent=2))
        token = 'S5_CONFIGURED_RETAINED_ONCE_473'
        async with actual_s2_ingress(owner, thread, output) as observer:
            original = await observer.submit('Bounded acceptance only. Do not resume '
                f'inherited work, use tools or change files. Reply exactly {token}.',
                observation_seconds=300)
        stored = stored_inputs(service.root)
        assert len(stored.rows) == 1
        assert stored.lookup(original.key).has_started
        with NativeEntry.open_evidence(selected) as reader:
            _, entries = reader.observe()
        user, = (entry for entry in entries if entry.input_id == original.native_id)
        terminal_replies = tuple(entry for entry in entries[entries.index(user) + 1:]
            if entry.final_reply)
        assert terminal_replies[-1].message.authoritative_text.strip() == token
        manifests = tuple(reversed(tuple(ContextCliCommand(thread=thread.name).manifests(service))))
        assert manifests
        historical = await query('--turn', str(manifests[-1].turn.occurrence.generation))
        (output / 'recorded-context.json').write_text(json.dumps(historical))
        assert historical['text_recorded'] is False
        assert service.bus.log.latest_sequence() == baseline
        assert selected.read_bytes().startswith(before)
        assert hashlib.sha256(original_file.read_bytes()).hexdigest() == original_digest
        selection.require_current(original_service.registry.snapshot())
        acknowledgement.update(state='SCOPED_CONFIGURED_SAVED_FORK_TOAD_ACP_NATIVE_CONTEXT_PASS',
            original_inputs=FieldCodec.encode(tuple(stored.rows.values())),
            manifests=len(manifests), source_unchanged=True, query_preserved_journal=True,
            original_native_user=user.id, original_native_reply=terminal_replies[-1].id)
    except BaseException as error:
        acknowledgement.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        if owner is not None:
            acknowledgement['original_dispositions'] = FieldCodec.encode(
                stored_inputs(owner._comms.root))
            await owner.shutdown()
            if (options.context_only or options.terminal_only or options.recorded_publication) and 'child' in locals():
                acknowledgement['native_child_retired'] = child.proc.retired
                acknowledgement['native_child_exited'] = not child.proc.alive()
                assert child.proc.retired and not child.proc.alive()
        acknowledgement['elapsed_seconds'] = time.monotonic() - started
        (root / 'terminal-acknowledgement.json').write_text(json.dumps(acknowledgement, indent=2))
        print(json.dumps(acknowledgement), flush=True)


def complete_goal_controls(root):
    """Authorized local controls on completed state; no native/input dispatcher."""
    import hashlib
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.input_attempt import InputAttempt
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.goals import AbsentGoalCheckpoint, PresentGoalCheckpoint
    from agent_comms.goal_actions import ClearGoalAction, SetGoalAction, StandbyGoalAction, GoalPrecondition, RuntimeInvocable
    from agent_comms.cli_commands import TurnContextCliCommand, DiffContextCliCommand

    started = time.monotonic()
    service = Comms(root / 'wire')
    originals = FieldCodec.decode(tuple[InputAttempt, ...], json.loads(
        (root / 'context-journey' / 'original-inputs.json').read_text()))
    dispositions = InputDispositions(service.root)
    assert all(stored_inputs(dispositions.root).lookup(item.key) == item and item.has_started for item in originals)
    source = service.registry.require('context-source')
    receiver = service.registry.require('context-receiver')
    retained = [Path(source.require_saved_session()), Path(receiver.require_saved_session()),
                root / 'original-provider-requests.json', service.root / 'input_dispositions.sqlite3']
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in retained}
    original_origin = originals[-1].origin.require_human()
    if isinstance(source.goal_checkpoint, PresentGoalCheckpoint):
        assert original_origin.applies(source, service.registry.snapshot())
    else:
        assert isinstance(source.goal_checkpoint, AbsentGoalCheckpoint)
        assert not original_origin.applies(source, service.registry.snapshot())
    acknowledgement = {'scope': 'authorized post-terminal goal controls and recorded CLI; continuous49 remains FAILED',
               'python': sys.executable, 'core': agent_comms.__file__, 'root': str(root),
               'native_prompts': 0, 'provider_calls': 0, 'input_replays': 0, 'owner_restarts': 0,
               'original_goal': FieldCodec.encode(source.goal_checkpoint)}
    try:
        if isinstance(source.goal_checkpoint, PresentGoalCheckpoint):
            service.goals.update_goal(source.name, ClearGoalAction(
                expect=GoalPrecondition(goal_id=source.goal.id)), actor=RuntimeInvocable)
        snapshot = service.registry.snapshot()
        absent = snapshot.threads[source.name].goal_checkpoint
        assert isinstance(absent, AbsentGoalCheckpoint)
        assert not original_origin.applies(snapshot.threads[source.name], snapshot)
        replacement = service.goals.update_goal(source.name, SetGoalAction(
            text='Replacement acceptance scope'), actor=RuntimeInvocable)
        assert replacement.id != original_origin.goal.revision.id
        snapshot = service.registry.snapshot()
        current = snapshot.threads[source.name].goal_checkpoint
        assert isinstance(current, PresentGoalCheckpoint)
        assert current.revision.id == replacement.id
        assert not original_origin.applies(snapshot.threads[source.name], snapshot)
        # The completed peer has no live turn. The canonical standby owner must
        # reject this; never fabricate process/lease activity to satisfy a test.
        try:
            service.goals.update_goal(source.name, StandbyGoalAction(
                wait_for=('context-peer',), expect=GoalPrecondition(goal_id=replacement.id)),
                actor=RuntimeInvocable)
        except ValueError as error:
            assert 'No declared dependency is working' in str(error)
            acknowledgement['standby_refused_without_active_dependency'] = str(error)
        else:
            raise AssertionError('Standby accepted a completed, non-active dependency')
        durable = stored_inputs(dispositions.root)
        assert all(durable.lookup(item.key) == item for item in originals)
        compare = DiffContextCliCommand(thread=source.name)
        difference = compare.apply(service).to_wire()
        manifests = tuple(reversed(tuple(ContextCliCommand(thread=source.name).manifests(service))))
        assert difference['turn'] != difference['previous_turn']
        command = TurnContextCliCommand(thread=source.name,
            turn=manifests[-1].turn.occurrence.generation)
        recorded = command.apply(service).to_wire()
        assert recorded['text_recorded'] is False
        assert recorded['manifests'] == FieldCodec.encode(tuple(item for item in manifests
            if item.turn.matches_generation(manifests[-1].turn.occurrence.generation)))
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in retained}
        assert before == after
        acknowledgement.update(state='SCOPED_POST_TERMINAL_CONTROLS_PASS',
            absent=FieldCodec.encode(absent), replacement=FieldCodec.encode(current),
            old_scope_inapplicable=True, original_inputs_durably_unchanged=True,
            retained_hashes=after, recorded_cli=recorded, context_diff=difference)
    except BaseException as error:
        acknowledgement.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        acknowledgement['elapsed_seconds'] = time.monotonic() - started
        (root / 'goal-completion-receipt51.json').write_text(json.dumps(acknowledgement, indent=2))
        print(json.dumps(acknowledgement), flush=True)


def complete_history_controls(root):
    """Installed historical CLI only; original sealed records, no native input."""
    import hashlib
    import subprocess
    from dataclasses import replace
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.thread_identity import TurnId, TurnIdentity
    from agent_comms.threads import Thread
    from agent_comms.turn_context import ContextManifest, RecordedContextTurn, TurnContext

    root.mkdir(mode=0o700)
    wire = root / 'wire'
    wire.mkdir(mode=0o700)
    service = Comms(wire)
    owner = service.registry.new_thread('history-original', frozenset(), str(root), process_identity=ProcessIdentity.capture(os.getpid()), created_at=18002.0)
    service.registry.register(owner)
    service.messaging.initialize_private_initial_protocol()
    originals = []
    for generation in (1, 2):
        turn = RecordedContextTurn(TurnId(f'original-history-{generation}'),
                                   TurnIdentity(owner.incarnation, generation))
        context = TurnContext.for_owner(owner, turn, f'Original private source {generation} π', (), (), service.goals)
        observation = ContextManifest(context.thread, context.turn,
            tuple(segment.manifest(0) for segment in context.segments), 'fixture-original-estimate')
        service.contexts.record(observation)
        originals.append(observation)
    service.registry.rename(owner.name, 'history-renamed')
    renamed = service.registry.require('history-renamed')
    second = originals[-1]
    continuation = replace(second, thread=renamed.incarnation,
        turn=replace(second.turn, occurrence=replace(second.turn.occurrence,
                                                   incarnation=renamed.incarnation)))
    service.contexts.record(continuation)
    future_context = TurnContext.for_owner(renamed,
        RecordedContextTurn(TurnId('original-history-3'), TurnIdentity(renamed.incarnation, 3)),
        'Later original private source', (), (), service.goals)
    future = ContextManifest(future_context.thread, future_context.turn,
        tuple(segment.manifest(0) for segment in future_context.segments), 'fixture-original-estimate')
    service.contexts.record(future)
    expected = (*originals, continuation, future)
    protected = tuple(path for path in (service.contexts.path, service.bus.log.path) if path.exists())
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    outputs = []

    def query(name, *arguments, accepted=True):
        result = subprocess.run([str(Path(sys.executable).parent / 'agent-comms'),
            '--root', str(wire), 'context', name, *arguments], env=environment,
            text=True, capture_output=True, timeout=15)
        assert result.returncode == (0 if accepted else 1), result.stderr + result.stdout
        payload = json.loads(result.stdout)
        outputs.append(dict(name=name, arguments=arguments, result=payload))
        return payload

    assert query('history-renamed', '--turn', '1')['manifests'] == FieldCodec.encode((originals[0],))
    assert query('history-original', '--turn', '2')['manifests'] == FieldCodec.encode((originals[1], continuation))
    difference = query('history-renamed', '--turn', '2', '--diff')
    assert difference == continuation.changed_since(originals[0]).to_wire()
    assert query('history-original', '--diff') == future.changed_since(continuation).to_wire()
    assert 'No preceding recorded turn' in query('history-renamed', '--turn', '1', '--diff', accepted=False)['error']
    reopened = Comms(wire)
    assert tuple(reversed(tuple(ContextCliCommand(thread='history-original').manifests(reopened)))) == expected
    service.registry.unregister('history-renamed')
    service.registry.remove('history-renamed')
    service.registry.register(replace(renamed, created_at=19002.0))
    assert 'No original context manifest' in query('history-renamed', '--turn', '1', accepted=False)['error']
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    assert before == after
    acknowledgement = dict(state='INSTALLED_HISTORICAL_CLI_PASS', python=sys.executable,
        core=agent_comms.__file__, original_records=FieldCodec.encode(expected),
        queries=outputs, original_file_hashes=after, original_files_unchanged=True,
        native_prompts=0, provider_calls=0, public_mutations=0, original_input_replays=0)
    (root / 'context-history-acknowledgement.json').write_text(json.dumps(acknowledgement, indent=2))
    print(json.dumps({key: value for key, value in acknowledgement.items()
                      if key not in ('original_records', 'queries')}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--test-support-site", type=Path)
    parser.add_argument("--toad-driver-dir", type=Path)
    parser.add_argument('--configured-source-root', type=Path)
    parser.add_argument('--configured-source-name', default='nra-architecture')
    parser.add_argument('--context-only', action='store_true')
    parser.add_argument('--terminal-only', action='store_true')
    parser.add_argument('--recorded-publication', action='store_true')
    parser.add_argument('--terminal-marker', default='597_CONFIGURED_ORIGINAL_TERMINAL_ONCE')
    parser.add_argument('--complete-goal-controls', action='store_true')
    parser.add_argument('--complete-history-controls', action='store_true')
    parser.add_argument('--receiving-only', action='store_true')
    options = parser.parse_args()
    if options.context_only and not options.configured_source_root:
        parser.error('Context-only requires the original configured saved source')
    if (options.terminal_only or options.recorded_publication) and (not options.configured_source_root or options.context_only):
        parser.error('Terminal-only requires a distinct configured saved fork')
    if "site-packages" not in Path(agent_comms.__file__).parts:
        raise RuntimeError("This acceptance requires the paired installed Core wheel")
    # Only pytest's MonkeyPatch is borrowed. Import installed
    # Core first and append the support directory; do not process donor .pth files.
    if options.test_support_site is not None:
        sys.path.append(str(options.test_support_site))
    if options.toad_driver_dir is not None:
        sys.path.append(str(options.toad_driver_dir))
    os.environ['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), os.environ.get('PATH', os.defpath)))
    if options.complete_history_controls:
        complete_history_controls(options.root)
    elif options.complete_goal_controls:
        complete_goal_controls(options.root)
    else:
        asyncio.run(run_configured(options) if options.configured_source_root
                    else run(options.root, receiving_only=options.receiving_only,
                             package=VerifiedNativePackage.acquire(pinned_native_package())))
