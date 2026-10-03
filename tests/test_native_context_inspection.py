"""Existing real native/ACP owner; inspect through its original Unix socket."""

import asyncio
import os
import json
import sys
import time
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.native_turn_context import NativeContextData
from agent_comms.runtime import RuntimeConnection, socket_path
from agent_comms.threads import Thread
from agent_comms.goal_actions import GoalPrecondition, OwnerInvocable, RuntimeInvocable, SetGoalAction, StandbyGoalAction
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEntry
from agent_comms.task_sources import CurrentTaskScopeSelection, OriginalTaskChange
from agent_comms.tools import tool_catalog
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


@pytest.mark.parametrize('recorded_readers', [True])
async def test_original_context_query_preserves_native_journal_and_dispatches_no_prompt(
    native_backend, recorded_readers=False
):
    fixture = native_backend
    sdk_source = None
    if recorded_readers:
        import agent_comms

        assert 'site-packages' in Path(agent_comms.__file__).resolve().parts
        seed_root = fixture.root.parent / 'recorded-sdk-source'
        seed = await asyncio.create_subprocess_exec(
            'node', str(Path(__file__).with_name('native_turn_context_contract.mjs')),
            os.environ['PI_COMPACTION_TEST_PACKAGE'], str(seed_root), '--recorded-readers',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await seed.communicate()
        assert seed.returncode == 0, error.decode()
        sdk_source = json.loads(output)
        (fixture.root.parent / 'original-recorded-sdk-source.json').write_bytes(output)
        fixture.session = Path(sdk_source['session_file'])
        fixture.project = seed_root / 'project'
    before = fixture.session.read_bytes()
    original_inputs = fixture.saved_inputs()
    owner = canonical_agent(
        Comms(fixture.root), agent_bin="pi",
        agent_args=["--model", "response-local/fixture", "--offline"],
        auto_wake=False, runtime_enabled=True,
    )
    # This fixture owns the root; the existing owner launch and RPC reader remain real.
    try:
        await owner._runtime.start()
        thread = Thread("context-source", frozenset(), str(fixture.project),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(fixture.session), model="response-local/fixture")
        owner._comms.registry.declare(thread)
        await owner.load_session(str(fixture.project), thread.name)
        connection = RuntimeConnection(owner._comms, thread.name,
            socket_path(owner._comms.root, thread.require_process().pid))
        try:
            async with asyncio.timeout(20):
                assert thread.name not in owner.turns.persistent_backends
                assert fixture.session.read_bytes() == before
                # Cold browsing asks the runtime owner to acquire its saved
                # source through the original selected startup, without input.
                first = FieldCodec.decode(NativeContextData, await connection.request("context"))
                selected_before = fixture.session.read_bytes()
                prepared_child = owner.turns.persistent_backends[thread.name].custody.idle().child.proc
                assert prepared_child.alive()
                second = FieldCodec.decode(NativeContextData, await connection.request("context"))
            assert first.identity == second.identity
            assert first.segments == second.segments
            assert {segment.declared_name for segment in first.segments} >= {"system_layer", "tool_catalog"}
            selected = Path(first.identity.session_file)
            assert selected == Path(thread.require_saved_session())
            assert owner._comms.bus.log.context_manifests(thread.name, owner._comms.registry) == ()
            assert fixture.session.read_bytes() == selected_before
            if recorded_readers:
                from agent_comms.native_turn_context import NativeContextManifestData
                from agent_comms.turn_context import ContextSourceText

                observed = NativeContextManifestData.from_wire(sdk_source['recorded_observation'])
                # This is the SDK contract's original authored observation, not
                # a claim that a model request/onContextReady event happened.
                leased = owner._comms.agents.begin_turn(thread.name, 'authored-sdk-context-read')
                try:
                    await observed.record(owner._comms.bus.log, leased.thread, leased.turn_lease)
                finally:
                    owner._comms.agents.finish_turn(leased.turn_lease)
                original, = owner._comms.bus.log.context_manifests(thread.name, owner._comms.registry)
                assert original.require_request_id() == 'authored-sdk-source-request'
                position = next(i for i, segment in enumerate(original.segments)
                                if segment.kind == 'transcript' and len(segment.contributors) > 1)
                group = original.selected_segment(position)
                child = original.selected_segment(position, (0,))
                assert group.sha256 != child.sha256
                source = NativeContextData.from_wire(sdk_source['full'])
                expected_group = source.segments[position]
                params = dict(turn=FieldCodec.encode(original.turn),
                              request_id=original.require_request_id(), segment=position)
                root_text = FieldCodec.decode(ContextSourceText,
                    await connection.request('context_recorded_segment', **params))
                child_text = FieldCodec.decode(ContextSourceText,
                    await connection.request('context_recorded_segment', **params, contributors=[0]))
                from agent_comms.pi_payloads import PiMessage

                assert root_text.text == expected_group.public_text()
                assert child_text.text == PiMessage.from_wire(expected_group.messages[0]).text
                assert child_text.text != root_text.text
                assert fixture.session.read_bytes() == selected_before
                assert owner._comms.bus.log.latest_sequence() == 0
                receipt = {'scope':'Installed authenticated reads of an original authored SDK capture',
                    'model_request_capture':False, 'provider_calls':fixture.provider.posts,
                    'new_native_inputs':0, 'original_user_rows':len(original_inputs),
                    'original_request':original.require_request_id(),
                    'root_text':root_text.text, 'child_text':child_text.text,
                    'root_and_child_differ':True, 'original_native_bytes_unchanged':True}
                (fixture.root.parent / 'recorded-reader-receipt.json').write_text(json.dumps(receipt, indent=2))
        finally:
            await connection.close()
    finally:
        await owner.shutdown()
        assert fixture.provider.posts == 0
        assert fixture.session.read_bytes().startswith(before)
        assert fixture.saved_inputs() == original_inputs
    assert not prepared_child.alive()
    print("cold_context_runtime", json.dumps({"pid": prepared_child.pid,
        "source": str(fixture.session), "cold_acquisition": True,
        "provider_requests": fixture.provider.posts, "new_inputs": 0,
        "repeat_source_unchanged": True, "original_source_prefix_preserved": True,
        "child_exited": not prepared_child.alive()}), flush=True)


async def test_context_manifest_native_acp_and_cli_continuous(
    native_backend, receiving_only=False, authored_operations_only=False
):
    """Actual Toad originals/followup, native Core tool and CLI on one source.

    The existing SDK source contract seeds real history/summary/image/resource
    entries. Provider transport is localhost; no application/protocol substitute.
    Run with the paired installed interpreter to establish installed acceptance.
    """
    fixture = native_backend
    from retained_input_origin_observer import actual_s2_ingress, until
    from agent_comms.input_origin import HumanInputOrigin
    from agent_comms.goals import AbsentGoalCheckpoint, PresentGoalCheckpoint
    from agent_comms.goal_actions import ClearGoalAction
    from agent_comms.native_fork import ForkSessionRequest
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskSupersession
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.compaction_states import ManualCommittedSummary
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    started = time.monotonic()
    seed = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("native_turn_context_contract.mjs")),
        str(package), str(fixture.root.parent / "sdk-source"), '--retained-history',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await seed.communicate()
    assert seed.returncode == 0, err.decode()
    source = json.loads(out)
    fresh_session, fresh_project = fixture.session, fixture.project
    fixture.session = Path(source["session_file"])
    # The SDK session's declared worktree is the source contract's project.
    project = fixture.root.parent / "sdk-source" / "project"
    models = fixture.config / "models.json"
    document = json.loads(models.read_text())
    document["providers"]["response-local"]["models"][0]["input"] = ["text", "image"]
    models.write_text(json.dumps(document))
    original_source = fixture.session.read_bytes()
    owner = canonical_agent(
        Comms(fixture.root), agent_bin="pi",
        agent_args=["--model", "response-local/fixture", "--thinking", "off", "--offline",
                    "--no-extensions", "--no-skills", "--no-prompt-templates",
                    "--no-builtin-tools", "--extension",
                    str(package / "agent-comms-extensions/global-agent-comms/index.mjs")],
        auto_wake=False, runtime_enabled=True,
    )
    failures, updates = [], []

    class View:
        async def session_update(self, session_id, update):
            updates.append(update)
            failures.extend(fact for fact in decode_updates(update.field_meta)
                            if isinstance(fact, (InputFailedUpdate, RequestFailedUpdate)))

    owner.on_connect(View())
    thread = Thread("context-source", frozenset({'team'}), str(project),
        process_identity=ProcessIdentity.capture(os.getpid()),
        session_file=str(fixture.session), model="response-local/fixture", thinking_level="off")
    owner._comms.registry.declare(thread)
    if authored_operations_only:
        from retained_context_native_journey import authored_context_operations

        try:
            await owner._runtime.start()
            await owner.load_session(str(project), thread.name)
            await owner.turns.prepare_selected_session(thread.name, thread)
            await authored_context_operations(fixture, owner, thread, fresh_session, fresh_project)
            assert failures == [], failures
        finally:
            await owner.shutdown()
        return
    peer = Thread("context-peer", frozenset({'team'}), str(project),
                  process_identity=ProcessIdentity.capture(os.getpid()))
    owner._comms.registry.declare(peer)
    c_identity = await CompactionJournal(owner._comms.root / 'compaction-commits.sqlite3').private_inputs.fork(ForkSessionRequest(str(package),
        str(fixture.session), str(project), str(fixture.root.parent / 'c-native-fork')),
        cwd=project, env=dict(os.environ))
    receiver = Thread('context-receiver', frozenset({'team'}), str(project),
        process_identity=ProcessIdentity.capture(os.getpid()),
        session_file=c_identity.session_file, model='response-local/fixture', thinking_level='off')
    owner._comms.registry.declare(receiver)
    peer_lease = owner._comms.agents.begin_turn(peer.name, "context-fixture-peer", "Independent context fixture").turn_lease
    output = fixture.root.parent / "context-journey"
    output.mkdir(mode=0o700)

    async def cli(*options):
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "agent_comms.cli", "--root", str(fixture.root),
            "context", thread.name, *options,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        data, error = await child.communicate()
        assert child.returncode == 0, (data.decode(), error.decode())
        return json.loads(data)

    try:
        await owner._runtime.start()
        await owner.load_session(str(project), thread.name)
        absent_origin = HumanInputOrigin.capture(owner._comms,
            owner._comms.registry.snapshot().admission_identity(thread.name))
        assert isinstance(absent_origin.goal, AbsentGoalCheckpoint)
        assert FieldCodec.decode(HumanInputOrigin, FieldCodec.encode(absent_origin)) == absent_origin
        # Seed an authentic active-but-waiting goal through its declaration and
        # original grant/wait owners. A bare ActiveGoal without a READY grant is
        # correctly blocked by the live observer; never patch that scheduler.
        goal = owner._comms.goals.update_goal(thread.name, SetGoalAction(
            text="Preserve the exact retained source; never replay an uncertain input.",
            expect=GoalPrecondition(expected_owner=thread.require_process())),
            actor=OwnerInvocable, owner_store=owner.turns.goals.open_goal_store())
        owner._comms.goals.update_goal(thread.name, StandbyGoalAction(
            wait_for=(peer.name,), expect=GoalPrecondition(goal_id=goal.id)),
            actor=RuntimeInvocable)
        await owner.turns.prepare_selected_session(thread.name, thread)
        before_query = fixture.session.read_bytes()
        preview = await cli()
        (output / "next-context.json").write_text(json.dumps(preview))
        assert preview["input_supplied"] is False
        assert {row["kind"] for row in preview["segments"]} >= {"coordination", "goal", "user_input"}
        assert "Original source summary." in json.dumps(preview["native_provider_context"])
        assert fixture.session.read_bytes() == before_query
        baseline_sequence = owner._comms.bus.log.latest_sequence()
        inputs = ("S5_CONTEXT_FIRST_ORIGINAL", "S5_CONTEXT_QUEUED_ORIGINAL", "S5_CONTEXT_SECOND_ORIGINAL")
        declaration = next(tool for tool in tool_catalog() if tool['name'] == 'comms_decision')
        choice = 'PRIVATE_ORIGINAL_CONTEXT_CHOICE'
        fixture.provider.tool_call = ('comms_decision', {
            'chosen': choice, 'rejected': ['PRIVATE_ORIGINAL_CONTEXT_ALTERNATIVE'],
            'to': peer.name, 'scope': FieldCodec.encode(CurrentTaskScopeSelection()),
            'change': FieldCodec.encode(OriginalTaskChange()),
        })
        fixture.provider.response_gate = asyncio.Event()
        originals = []

        async def queue_during_original(observer, original):
            assert original.has_started
            originals.append(await observer.queue_followup(inputs[1]))
            fixture.provider.response_gate.set()

        async with actual_s2_ingress(owner, thread, output) as observer:
            steps = (inputs[0],) if receiving_only else (inputs[0], inputs[2])
            for index, text in enumerate(steps, 1):
                if index == 2:
                    private_choice, = owner._comms.bus.log.full_history()
                    supersession = owner._comms.messaging.send_user_message('#team',
                        'PUBLIC_USER_CORRECTION_WITHOUT_PRIVATE_BODY', worktree=str(project),
                        task=UserTaskSupersession(CorrectionTaskChange(private_choice.reference)))
                    fixture.provider.tool_call = ('comms_decision', {
                        'chosen': 'PUBLIC_CORRECTED_CONTEXT_CHOICE',
                        'rejected': ['PUBLIC_CONTEXT_ALTERNATIVE'], 'to': '#team',
                        'scope': FieldCodec.encode(CurrentTaskScopeSelection()),
                        'change': FieldCodec.encode(CorrectionTaskChange(private_choice.reference)),
                    })
                previous = owner._comms.bus.log.context_manifests(thread.name, owner._comms.registry)
                image = {'mimeType': 'image/png',
                    'data': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='} if index == 1 else None
                original = await observer.submit(text, image=image,
                    while_running=queue_during_original if index == 1 else None)
                originals.append(original)
                assert isinstance(original.origin.require_human().goal, PresentGoalCheckpoint)
                assert not failures, failures
                manifests = owner._comms.bus.log.context_manifests(thread.name, owner._comms.registry)
                assert len(manifests) > len(previous)
                manifest = manifests[-1]
                assert {segment.kind for segment in manifest.segments} >= {
                    'system_layer', 'transcript', 'compaction_summary', 'injection_message', 'tool_catalog'}
                assert all(segment.provenance for segment in manifest.segments)
                assert manifest.counter == 'pi.estimateTokens'
                selected = await cli('--turn', str(manifest.turn.occurrence.generation))
                assert selected['manifests'] == FieldCodec.encode(tuple(item for item in manifests
                    if item.turn.matches_generation(manifest.turn.occurrence.generation)))
                assert selected['text_recorded'] is False and text not in json.dumps(selected)
                (output / f'recorded-turn-{index}.json').write_text(json.dumps(selected))
            contributors = tuple(part for manifest in manifests for segment in manifest.segments
                                 for part in segment.contributors)
            # Manual owner inputs intentionally omit goal instructions unless
            # an original goal permit owns this turn. The unchanged CLI preview
            # above covers declared goal contributors; never inject new bytes
            # merely to satisfy this manual-input oracle.
            assert {part.kind for part in contributors} >= {'coordination', 'user_input', 'user_followup'}
            assert all(part.provenance for part in contributors)
            stored = InputDispositions(owner._comms.root / InputDispositions.filename).read()
            terminals = tuple(stored.lookup(original.key) for original in originals)
            assert all(item.has_started for item in terminals)
            assert len({item.native_id for item in terminals}) == len(originals)
            for item in terminals:
                item.origin.require_human()
                assert any(item.context_provenance() in part.provenance for part in contributors)
            with NativeEntry.open_evidence(fixture.session) as reader:
                _, entries = reader.observe()
            for item in terminals:
                user, = [entry for entry in entries if entry.input_id == item.native_id]
                assert user.message.user
            (output / 'original-inputs.json').write_text(json.dumps(FieldCodec.encode(terminals)))
        if receiving_only:
            assert len(terminals) == 2
            assert len({item.native_id for item in terminals}) == 2
            assert fixture.provider.posts == 2
            assert fixture.session.read_bytes().startswith(original_source)
            queued_receipt, = (item for item in observer.observer.receipts
                if 'queued_original' in item)
            assert queued_receipt['pre_delivery_queue_paint']['native_started'] is False
            assert queued_receipt['queue_to_chat_handoff']['native_chat_claims'] == 1
            print('S5_RECEIVING_INPUT_JOURNEY', json.dumps({
                'elapsed_seconds': time.monotonic() - started,
                'actual_toad_originals': 1, 'actual_controller_followups': 1,
                'local_posts': fixture.provider.posts,
                'original_inputs': FieldCodec.encode(terminals),
                'original_scope_captured_by_queue_owner': True,
                'source_render_bytes_identical': source['provider_bytes_identical'],
                'pre_delivery_queue_paint': queued_receipt['pre_delivery_queue_paint'],
                'queue_to_chat_handoff': queued_receipt['queue_to_chat_handoff'],
                'manual_compactions': 0, 'public_inputs': 0,
                'artifact_root': str(output), 'python': sys.executable,
                'visual_limit': 'Compositor source and raw export only; readable physical pixels not claimed',
            }), flush=True)
            return
        # C's independent real SDK source was forked before private publication.
        # Drive the same production ACP manual selected writer through actual Toad.
        c_output = output / 'receiver'
        c_output.mkdir(mode=0o700)
        journal = CompactionJournal(owner._comms.root / 'compaction-commits.sqlite3')
        await owner.load_session(str(project), receiver.name)
        async with actual_s2_ingress(owner, receiver, c_output) as observer:
            async def compact_receiver():
                app = observer.observer.app
                view = app.selected_session.conversation
                assert view.agent.session_id == receiver.name
                await view.compact_context('Preserve the original certified public source only.').wait()
                attempts = journal.summaries.history(receiver.session_file)
                assert len(attempts) == 1
                assert isinstance(attempts[0].state, ManualCommittedSummary)
                def committed_frame():
                    return 'Context compacted' in '\n'.join(
                        strip.text for strip in app.screen._compositor.render_strips())
                await until(observer.observer.pilot, committed_frame)
                (c_output / 'actual-committed-summary.svg').write_text(app.export_screenshot())
                return attempts[0]
            attempt = await observer.run(compact_receiver())
        captured = attempt.request
        snapshot = owner._comms.registry.snapshot()
        assert captured.retained.current_authored_sources(snapshot.threads[receiver.name], snapshot) == ()
        assert {fact.source.reference for fact in captured.retained.facts} == {
            supersession.reference, owner._comms.bus.log.full_history()[-1].reference}
        with NativeEntry.open_evidence(Path(receiver.session_file)) as reader:
            _, c_entries = reader.observe()
        # Read the SDK external compaction record; never synthesize a witness/leaf.
        c_records = [json.loads(line) for line in Path(receiver.session_file).read_text().splitlines()]
        c_compactions = [row for row in c_records if row['type'] == 'compaction']
        captured.retained.require_summary(c_compactions[-1]['summary'])
        assert 'PRIVATE_ORIGINAL_CONTEXT_CHOICE' not in Path(receiver.session_file).read_text()
        assert 'PRIVATE_ORIGINAL_CONTEXT_ALTERNATIVE' not in Path(receiver.session_file).read_text()
        assert 'PRIVATE_ORIGINAL_CONTEXT_CHOICE' not in json.dumps(fixture.provider.requests[-1])
        assert 'PRIVATE_ORIGINAL_CONTEXT_ALTERNATIVE' not in json.dumps(fixture.provider.requests[-1])
        operation = journal.operations.get(attempt.state.commit_id)
        assert operation.state.committed
        (c_output / 'selected-commit.json').write_text(json.dumps({
            'operation_id': attempt.operation_id, 'commit_id': attempt.state.commit_id,
            'source': FieldCodec.encode(captured), 'native_entry': c_compactions[-1]['id'],
            'private_body_absent': True, 'native_prepared_entry_count': len(c_entries),
        }))
        original_origin = originals[-1].origin.require_human()
        owner._comms.goals.update_goal(thread.name, ClearGoalAction(
            expect=GoalPrecondition(goal_id=goal.id)), actor=RuntimeInvocable)
        replacement = owner._comms.goals.update_goal(thread.name, SetGoalAction(
            text='Replacement acceptance scope', expect=GoalPrecondition(expected_owner=thread.require_process())),
            actor=OwnerInvocable, owner_store=owner.turns.goals.open_goal_store())
        owner._comms.goals.update_goal(thread.name, StandbyGoalAction(
            wait_for=(peer.name,), expect=GoalPrecondition(goal_id=replacement.id)), actor=RuntimeInvocable)
        assert replacement.id != goal.id
        assert not original_origin.applies(owner._comms.registry.require(thread.name),
            owner._comms.registry.snapshot())
        assert InputDispositions(owner._comms.root / InputDispositions.filename).read().lookup(
            originals[-1].key).origin == original_origin
        (output / 'goal-boundaries.json').write_text(json.dumps({
            'absent': FieldCodec.encode(absent_origin.goal), 'present': FieldCodec.encode(original_origin.goal),
            'replacement': FieldCodec.encode(owner._comms.registry.require(thread.name).goal_checkpoint),
            'historical_origin_unchanged': True, 'historical_scope_inapplicable': True,
        }))
        difference = await cli("--diff")
        assert difference["turn"] != difference["previous_turn"]
        (output / "context-diff.json").write_text(json.dumps(difference))
        # Pi drains a queued followup after the tool result into the same next
        # synthesis request; one request serves both originals in this turn.
        assert fixture.provider.posts == 5
        assert owner._comms.bus.log.latest_sequence() == baseline_sequence + 3
        decision, user_correction, correction = owner._comms.bus.log.full_history()
        assert decision.task.chosen == choice
        assert decision.task.author == thread.incarnation
        first_request = fixture.provider.requests[0]
        actual_tool = next(tool['function'] for tool in first_request['tools']
                           if tool['function']['name'] == declaration['name'])
        assert actual_tool['parameters'] == declaration['parameters']
        assert any(message['role'] == 'tool' and decision.reference.message_id in message['content']
                   for request in fixture.provider.requests for message in request['messages'])
        assert fixture.session.read_bytes().startswith(original_source)
        rows = list(map(json.loads, fixture.session.read_text().splitlines()))
        users = [json.dumps(row["message"]) for row in rows
                 if row.get("type") == "message" and row["message"].get("role") == "user"]
        assert all(sum(marker in text for text in users) == 1 for marker in inputs)
        requests = fixture.provider.requests
        assert "Original source summary." in json.dumps(requests[0])
        assert "Original kept question" in json.dumps(requests[0])
        assert "image_url" in json.dumps(requests[0])
        (output / "provider-requests.json").write_text(json.dumps(requests))
        print("S5_CONTEXT_JOURNEY", json.dumps({"elapsed_seconds": time.monotonic()-started,
            "local_posts": fixture.provider.posts, "original_inputs": inputs,
            "manifests": len(manifests), "query_preserved_journal": True,
            "actual_toad_originals": 2, "actual_controller_followups": 1,
            "nested_core_tool": 'comms_decision', "original_decision": FieldCodec.encode(decision.reference),
            'public_correction': FieldCodec.encode(correction.reference),
            'selected_receiver_commit': attempt.state.commit_id,
            'goal_absent_present_replaced': True, 'private_receiver_body_absent': True,
            "source_render_bytes_identical": source["provider_bytes_identical"],
            "public_changes": [], "failures": [], "artifact_root": str(output),
            "python": sys.executable}), flush=True)
    finally:
        await owner.shutdown()
        owner._comms.agents.finish_turn(peer_lease)
