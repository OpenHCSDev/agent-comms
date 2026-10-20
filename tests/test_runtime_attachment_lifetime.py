"""An unsent socket attachment ends with its real owner/root/caller lifetime."""

import asyncio
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.owner_protocol import (
    IncompatibleOwner,
    OtherCoreOwner,
    OwnerAnnouncement,
    UnannouncedOwner,
)
from agent_comms.session_load import FailedSessionLoadPlan
from agent_comms.runtime import socket_path


# The 496609b95 owner's first line: an update whose context_usage predates
# the ContextReading family. A frontend must never decode it.
_UNANNOUNCED_FIRST_LINE = {"update": {"_meta": {"agentComms": {"updates": [
    {"kind": "coordination_changed", "context_usage": {"used": 1, "size": 2}},
]}}}}
_OTHER_COMMIT = "496609b95" + "0" * 31


@pytest.mark.parametrize("ending", ["invalid_ready", "cancel", "unannounced", "other_core"])
async def test_unaccepted_session_load_retires_original_socket(tmp_path, ending):
    """Exercise lifecycle cleanup at the actual subscription protocol boundary."""
    import json
    from agent_comms.acp import CommsAgent
    from acp import RequestError

    comms = Comms(tmp_path / "wire")
    thread = comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(thread)
    path = socket_path(comms.root, os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    entered = asyncio.Event()
    closed = asyncio.get_running_loop().create_future()

    async def subscribe(reader, writer):
        try:
            request = json.loads(await reader.readline())
            assert request["action"] == "subscribe"
            entered.set()
            if ending == "invalid_ready":
                writer.write(OwnerAnnouncement().wire())
                writer.write(b'{"kind": "owner_ready", "ready": {}, "configOptions": []}\n')
                await writer.drain()
            elif ending == "unannounced":
                writer.write((json.dumps(_UNANNOUNCED_FIRST_LINE) + "\n").encode())
                await writer.drain()
            elif ending == "other_core":
                writer.write(OwnerAnnouncement(core_commit=_OTHER_COMMIT).wire())
                await writer.drain()
            assert await reader.read() == b""
        except BaseException as error:
            closed.set_exception(error)
        else:
            closed.set_result(None)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(subscribe, path=path)
    agent = CommsAgent(comms, auto_wake=False)
    loading = asyncio.create_task(agent.sessions.attach_owner(thread, "owner"))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            if ending == "cancel":
                loading.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loading
            else:
                with pytest.raises(RequestError) as raised:
                    await loading
                owner = IncompatibleOwner.from_failure(raised.value.data)
                if ending == "invalid_ready":
                    assert owner is None
                else:
                    expected = (
                        UnannouncedOwner(thread="owner")
                        if ending == "unannounced"
                        else OtherCoreOwner(thread="owner", core_commit=_OTHER_COMMIT)
                    )
                    assert owner == expected
                    assert raised.value.data["reason"] == expected.line()
                    assert FailedSessionLoadPlan.from_failure(raised.value.data) is not None
            await closed
        assert not agent.sessions.proxies
    finally:
        loading.cancel()
        await asyncio.gather(loading, return_exceptions=True)
        await agent.shutdown()
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
