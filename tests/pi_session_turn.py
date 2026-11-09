"""One independent Pi turn on its own session child, closed when the turn ends."""

from __future__ import annotations

from contextlib import AsyncExitStack, aclosing
from pathlib import Path

import agent_comms.execution.backend as backend
from agent_comms.agent_backend import NoInputStartListener
from agent_comms.comms import wire
from agent_comms.execution.cursor_publication import CursorDelivery
from agent_comms.execution.selected.actions import NoSelectedTools
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.execution.persistent_pi_session import PersistentPiSession
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread


async def one_turn_events(agent_bin, agent_args, task, cwd, env_extra=None,
                          *, package: Path, tool_mode=NoSelectedTools(), **kwargs):
    """Run ``backend.stream_agent_events`` on a fresh session and close its child after."""
    session = PersistentPiSession()
    comms = wire(Path(cwd))
    owner = comms.registry.declare(NativeThread(
        "independent-turn", frozenset(), Checkout(Path(cwd)))).incarnation.require_recorded()
    async with AsyncExitStack() as resources:
        cursor_delivery = CursorDelivery()
        resources.callback(cursor_delivery.close)
        resources.push_async_callback(session.close)
        async with aclosing(
            backend.stream_agent_events(agent_bin, agent_args, task, cwd, env_extra,
                                        persistent_session=session, root=comms.root, owner=owner, tool_mode=tool_mode,
                                        input_start=NoInputStartListener(), cursor_delivery=cursor_delivery,
                                        package=VerifiedNativePackage.acquire(package),
                                        session_directory=Path(cwd), **kwargs)
        ) as stream:
            async for event in stream:
                yield event
