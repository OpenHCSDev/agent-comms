"""A real selected owner and child pipes, with a recorded RPC executable."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.thread_identity import RecordedThreadIncarnation


async def recorded_request(control, agent):
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    fd = os.open(control / 'entered', os.O_RDONLY | os.O_NONBLOCK)
    try:
        async with asyncio.timeout(15):
            loop.add_reader(fd, entered.set_result, None)
            await entered
    except TimeoutError:
        from delivery_owner_fixture import drain_diagnostic

        raise AssertionError(f'Scripted input never arrived: {drain_diagnostic(agent._comms, "beta")!r}') from None
    finally:
        loop.remove_reader(fd)
        os.close(fd)


def requests(control):
    path = control / 'requests.jsonl'
    return tuple(map(json.loads, path.read_text().splitlines())) if path.exists() else ()
