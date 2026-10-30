"""A committed bus message reaches the native steer queue during a real tool call."""

import asyncio
import json
import os
import shlex
from functools import partial
from pathlib import Path
from time import perf_counter

from agent_comms import backend
from agent_comms.turn_inbox import SteerInputPolicy
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import InputId
from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import RootFile
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.queued_input import InitialInput
from native_backend_fixture import native_backend_fixture, wait_for


async def test_message_injects_into_next_tool_call(
    private_comms: Comms, tmp_path: Path, native_runtime_package: VerifiedNativePackage,
) -> None:
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        # Copy through the stores' declared copy operation, including SQLite WALs.
        # Neither the source fixture nor a working agent receives the message.
        practice = PracticeCopy(private_comms.root, native.root)
        practice.include(tuple(RootFile.existing_in(private_comms.root)))
        await native.author_history()
        release = native.project / 'release-tool'
        ready = native.project / 'tool-running'
        returned = native.project / 'tool-returned'
        os.mkfifo(release, mode=0o600)
        release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
        try:
            native.provider.tool_call = ('bash', {'command': (
                f'printf running > {shlex.quote(str(ready))}; '
                f'IFS= read -r released < {shlex.quote(str(release))}; '
                f'printf returned > {shlex.quote(str(returned))}; '
                'printf "%s\\n" "$released"'
            )})
            async with native.open_owner(
                runtime_enabled=True, auto_wake=True, native_options=(),
            ) as (agent, session):
                comms = agent._comms
                comms.threads.claim_thread('sender', tags=frozenset(), worktree=str(native.project))
                worker = agent.sessions.require(session)
                turn = asyncio.create_task(InitialInput.run(
                    agent.inputs, session, worker, 'Run the held bash tool, then answer.',
                    display_text='Run the held bash tool, then answer.', input_id=InputId.mint(),
                ))
                try:
                    await wait_for(ready.exists)
                    running = backend.TurnSession.active[turn]
                    forwarding = running.inputs
                    assert not returned.exists()
                    assert not turn.done()
                    marker = 'P827 private-bus message sent while the real bash tool is running'
                    # Start BEFORE send: this upper-bounds commit-to-steer latency
                    # without pretending a row-insertion timestamp is a commit clock.
                    send_started = perf_counter()
                    async with asyncio.timeout(2):
                        sent = await Coordination.run_worker(partial(
                            comms.messaging.send, 'sender', session, marker,
                        ))
                        send_returned = perf_counter()
                        while True:
                            forwarding.changed.clear()
                            accepted = tuple(
                                item for item in forwarding.pending.values()
                                if item.input_id in forwarding.accepted and marker in item.content.text
                            )
                            if accepted:
                                break
                            await forwarding.changed.wait()
                        queued = perf_counter()
                    assert len(accepted) == 1
                    assert accepted[0].when_busy is SteerInputPolicy
                    assert queued - send_started < 2
                    assert not returned.exists(), 'The native tool returned before steer acknowledgement'
                    assert not turn.done()
                    print('BUS_COMMIT_TO_NATIVE_STEER_UPPER_BOUND_SECONDS', queued - send_started,
                          'SEND_CALL_SECONDS', send_returned - send_started,
                          'MESSAGE_SEQ', sent.message.seq)
                finally:
                    # Always release the actual tool, including a latency failure.
                    os.write(release_fd, b'released\n')
                    async with asyncio.timeout(30):
                        await turn
                assert returned.exists()
                assert marker in json.dumps(native.provider.requests[-1])
                assert 'released' in json.dumps(native.provider.requests[-1])
                assert comms.registry.require(worker).turn_generation == 1
        finally:
            os.close(release_fd)
