"""One independent Pi turn on its own session child, closed when the turn ends."""

from __future__ import annotations

from contextlib import aclosing
from pathlib import Path

import agent_comms.backend as backend
from agent_comms.comms import Comms
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.selected_actions import NoSelectedTools


async def one_turn_events(agent_bin, agent_args, task, cwd, env_extra=None,
                          *, tool_mode=NoSelectedTools(), **kwargs):
    """Run ``backend.stream_agent_events`` on a fresh session and close its child after."""
    session = PersistentPiSession()
    comms = Comms(Path(cwd))
    owner = comms.registry.declare(comms.registry.new_thread(
        "independent-turn", frozenset(), cwd,
    )).incarnation.require_recorded()
    try:
        async with aclosing(
            backend.stream_agent_events(agent_bin, agent_args, task, cwd, env_extra,
                                        persistent_session=session, root=comms.root, owner=owner, tool_mode=tool_mode,
                                        package=VerifiedNativePackage.acquire(pinned_native_package()),
                                        session_directory=Path(cwd), **kwargs)
        ) as stream:
            async for event in stream:
                yield event
    finally:
        await session.close()
