"""Real resident bridge admission and request evaluation shared by hook-family tests."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import JsonValue

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.hook_requests import HookReply, HookRequest, ResidentHookOutput
from agent_comms.cli_bridges.shared import Binding, GivenBinding, Options, StoredBinding, Watch
from agent_comms.comms import Comms
from agent_comms_hooks import ClaudeHook


@contextmanager
def resident_session(comms: Comms, state: Path, actor: str, session: str) -> Iterator[ClaudeBridge]:
    """Admit through the actual binding owner, never a manually reconstructed actor."""
    with Binding.open(state) as db:
        binding = GivenBinding({
            "root": str(comms.root), "bus_thread": actor, "session": session,
        }).bind(db).registered(comms).bind(db)
        bridge = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        asyncio.run(bridge.options.operation.prepare(bridge))
        yield bridge


def resident_hook[Event: ClaudeHook](
        bridge: ClaudeBridge, program: type[HookRequest[Event]], transcript: Path,
        **fields: JsonValue) -> HookReply:
    """Evaluate a declared single-event request; root exercises socket transport separately."""
    event_name, = program.events
    payload = {"session_id": bridge.binding.thread, "hook_event_name": event_name,
               "transcript_path": str(transcript), **fields}
    request = program(payload=payload)
    output = ResidentHookOutput()
    event = request.decode_event(bridge.comms, bridge.binding, output)
    return request.evaluate(event, bridge.comms, output, bridge.binding)


def resident_stdout[Event: ClaudeHook](
        bridge: ClaudeBridge, program: type[HookRequest[Event]], transcript: Path,
        **fields: JsonValue) -> str:
    """Positive scenarios must not mistake a failed resident evaluation for empty output."""
    reply = resident_hook(bridge, program, transcript, **fields)
    assert reply.status == 0, reply.stderr
    return reply.stdout
