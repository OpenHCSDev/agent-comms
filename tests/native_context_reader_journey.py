"""Original acquired SDK context reader through the installed owner RPC."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.native_turn_context import NativeContextData
from agent_comms.runtime import RuntimeConnection, socket_path
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent
from native_backend_fixture import native_backend_fixture


async def read_original_context(
    fixture, recorded_readers=False, sdk_source_path=None
):
    sdk_source = None
    if recorded_readers:
        import agent_comms

        assert 'site-packages' in Path(agent_comms.__file__).resolve().parts
        if sdk_source_path is None:
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
        else:
            sdk_source = json.loads(sdk_source_path.read_bytes())
            seed_root = Path(sdk_source['session_file']).parents[1]
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
        from agent_comms.bus_publication import stable_thread_lookup
        from agent_comms.coordinator import Coordination

        # An in-process fixture registration has the same private participant
        # relation as the original owner launcher; a registry row alone does not.
        with Coordination(str(owner._comms.root / 'coordination.sqlite3')) as store:
            store.participants.register(stable_thread_lookup(thread.created_at),
                                        thread.name, thread.name, committed=True)
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
                mixed_observed = NativeContextManifestData.from_wire(sdk_source['mixed_observation'])
                # This is the SDK contract's original authored observation, not
                # a claim that a model request/onContextReady event happened.
                leased = owner._comms.agents.begin_turn(thread.name, 'authored-sdk-context-read')
                try:
                    await observed.record(owner._comms.bus.log, leased.thread, leased.turn_lease)
                    await mixed_observed.record(owner._comms.bus.log, leased.thread, leased.turn_lease)
                finally:
                    owner._comms.agents.finish_turn(leased.turn_lease)
                original, mixed_original = owner._comms.bus.log.context_manifests(
                    thread.name, owner._comms.registry)
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
                mixed_source = NativeContextData.from_wire(sdk_source['mixed_full'])
                mixed_position = next(i for i, segment in enumerate(mixed_original.segments)
                                      if segment.kind == 'transcript' and len(segment.contributors) > 1)
                mixed_group = mixed_original.selected_segment(mixed_position)
                assert len(mixed_group.requested_parts()) == 1
                assert any(part.public_text_recorded for part in mixed_group.recorded_parts())
                mixed_text = FieldCodec.decode(ContextSourceText,
                    await connection.request('context_recorded_segment',
                        turn=FieldCodec.encode(mixed_original.turn),
                        request_id=mixed_original.require_request_id(), segment=mixed_position))
                assert mixed_text.text == mixed_source.segments[mixed_position].public_text()
                assert 'Authored transformed SDK part.' in mixed_text.text
                assert mixed_text.text != root_text.text
                assert fixture.session.read_bytes() == selected_before
                assert owner._comms.bus.log.latest_sequence() == 0
                receipt = {'scope':'Installed authenticated reads of an original authored SDK capture',
                    'model_request_capture':False, 'provider_calls':fixture.provider.posts,
                    'new_native_inputs':0, 'original_user_rows':len(original_inputs),
                    'original_request':original.require_request_id(),
                    'root_text':root_text.text, 'child_text':child_text.text,
                    'mixed_text':mixed_text.text, 'mixed_request':mixed_original.require_request_id(),
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


async def run(root, sdk_source_path=None):
    root.mkdir(mode=0o700)
    started = time.monotonic()
    receipt = {"python": sys.executable, "scope": "Authored SDK recorded root/mixed/child installed RPC reads",
               "model_request_capture": False, "new_native_inputs": 0}
    try:
        async with native_backend_fixture(root) as fixture:
            await read_original_context(fixture, recorded_readers=True, sdk_source_path=sdk_source_path)
            receipt["provider_calls"] = fixture.provider.posts
        receipt["state"] = "SCOPED_PASS"
    except BaseException as error:
        receipt.update(state="FAILED_NO_REPLAY", error=repr(error))
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (root / "terminal-receipt.json").write_text(json.dumps(receipt, indent=2)+"\n")
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--sdk-source", type=Path, help="Borrow an already completed original SDK capture without rerunning it")
    args = parser.parse_args()
    asyncio.run(run(args.root.resolve(), args.sdk_source))
