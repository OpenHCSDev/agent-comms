"""One independent Pi turn on its own session child, closed when the turn ends."""

from __future__ import annotations

from contextlib import aclosing

from agent_comms import backend
from agent_comms.pi_native_backend import PersistentPiSession


async def one_turn_events(*args, **kwargs):
    """Run ``backend.stream_agent_events`` on a fresh session and close its child after."""
    session = PersistentPiSession()
    try:
        async with aclosing(
            backend.stream_agent_events(*args, persistent_session=session, **kwargs)
        ) as stream:
            async for event in stream:
                yield event
    finally:
        await session.close()
