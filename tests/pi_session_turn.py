"""Native recorded-protocol turns share the transport's required launch fields."""

from __future__ import annotations

from contextlib import aclosing
from pathlib import Path

import agent_comms.backend as backend
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.selected_actions import NoSelectedTools


async def session_turn_events(session, agent_bin, agent_args, task, cwd, env_extra=None,
                              *, tool_mode=NoSelectedTools(), **kwargs):
    """Use the real transport on a caller-owned session, without replacing methods."""
    async with aclosing(backend.stream_agent_events(
        agent_bin, agent_args, task, cwd, env_extra,
        persistent_session=session, root=Path(cwd), tool_mode=tool_mode,
        package=VerifiedNativePackage.acquire(pinned_native_package()),
        session_directory=Path(cwd), **kwargs,
    )) as stream:
        async for event in stream:
            yield event


async def one_turn_events(agent_bin, agent_args, task, cwd, env_extra=None, **kwargs):
    """Close the independent session after its actual native stream has ended."""
    session = PersistentPiSession()
    try:
        async with aclosing(session_turn_events(
            session, agent_bin, agent_args, task, cwd, env_extra, **kwargs,
        )) as stream:
            async for event in stream:
                yield event
    finally:
        await session.close()
