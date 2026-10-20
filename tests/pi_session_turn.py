"""Native test turns use the production backend's launch and delivery owners."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, contextmanager
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from agent_comms import backend
from agent_comms.agent_backend import InputContent, TurnDelivery, TurnInbox
from agent_comms.comms import Comms
from agent_comms.native_arguments import NativeArguments
from agent_comms.launch_root import LaunchRoot
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.request_progress import NO_REQUEST_OBSERVER
from agent_comms.selected_actions import NoSelectedTools
from agent_comms.send_outcome import GrantedSend


async def observe(thread, state, info):
    pass


@contextmanager
def permit(*args):
    yield GrantedSend()


def native_backend(agent_bin, agent_args, root):
    """Use the same backend constructor and private launch declaration as ACP."""
    comms = Comms(Path(root))
    with comms.bus.log.locked():
        private = comms.bus.log.read_metadata_unlocked().private
    if not private:
        comms.messaging.initialize_private_initial_protocol()
    if 'native-fixture' not in comms.registry.snapshot().threads:
        comms.registry.declare(comms.registry.new_thread(
            'native-fixture', frozenset(), str(root), backend=PiNativeBackend))
    return PiNativeBackend(agent_bin, NativeArguments.parse(agent_args), comms, observe)


async def backend_events(owner, task, cwd, env_extra=None, *, session_file=None,
                         steering_queue=None, finish_event=None, native_start=lambda *args: True,
                         send_boundary=permit, interrupt_boundary=permit, ui_request=None,
                         tool_mode=NoSelectedTools(), images=(), context_contributions=(),
                         request_observer=NO_REQUEST_OBSERVER, **policy):
    """Launch fields are assembled by PiNativeBackend.run_turn, never by a test."""
    from agent_comms.pi_native_backend import PiSessionFile
    thread = replace(owner.comms.registry.require('native-fixture'), worktree=str(cwd))
    if session_file is not None:
        thread = thread.with_runtime(PiSessionFile(str(session_file)))
    delivery = TurnDelivery(
        worktree=str(cwd), environment=dict(env_extra or {}),
        inbox=steering_queue if steering_queue is not None else TurnInbox(),
        finish_event=finish_event if finish_event is not None else asyncio.Event(),
        send_boundary=send_boundary, interrupt_boundary=interrupt_boundary,
        input_started=native_start, request_progress=request_observer,
        ui_request=ui_request, tool_mode=tool_mode,
    )
    content = InputContent(text=task, images=tuple(images), contributions=context_contributions)
    original = backend.stream_agent_events

    async def configured(*args, **kwargs):
        async for event in original(*args, **kwargs, **policy):
            yield event

    # Only watchdog budgets are controlled; launch fields still come from run_turn.
    with LaunchRoot.from_path(owner.comms.root).selected(), patch.object(backend, 'stream_agent_events', configured):
        async with aclosing(owner.run_turn(thread, content, delivery)) as stream:
            async for event in stream:
                yield event


async def one_turn_events(agent_bin, agent_args, task, cwd, env_extra=None, **kwargs):
    """An independent turn retains its child only for this fixture's lifetime."""
    owner = native_backend(agent_bin, agent_args, cwd)
    try:
        async for event in backend_events(owner, task, cwd, env_extra, **kwargs):
            yield event
    finally:
        await owner.close()
