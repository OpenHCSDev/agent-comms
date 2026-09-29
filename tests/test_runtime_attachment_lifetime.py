"""An unsent socket attachment ends with its real owner/root/caller lifetime."""

import asyncio
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_comms.child_process import DetachedProcess, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread


@pytest.mark.parametrize("ending", ["exit", "lease", "process", "root", "close", "cancel"])
async def test_unsent_attachment_ends_with_actual_lifetime(tmp_path, ending):
    process = DetachedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    comms = Comms(tmp_path / "wire")
    comms.registry.declare(
        Thread("cold", frozenset(), str(tmp_path), process_identity=process.identity)
    )
    proxy = RuntimeProxy(
        SimpleNamespace(_comms=comms), "cold", socket_path(comms.root, process.pid)
    )
    pending = asyncio.create_task(proxy._connect_current())
    try:
        await asyncio.sleep(0.1)
        assert not pending.done()
        if ending == "exit":
            await process.stop()
            expected = "exited before attachment; request not sent"
        elif ending == "lease":
            comms.registry.register(comms.registry.require("cold"), new_owner=True)
            expected = "Owner lease changed"
        elif ending == "process":
            comms.registry.register(
                replace(
                    comms.registry.require("cold"),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                )
            )
            expected = "Owner lease changed"
        elif ending == "root":
            comms.root.rename(tmp_path / "retired")
            comms.root.mkdir()
            expected = "Wire root changed"
        elif ending == "close":
            await proxy.close()
            expected = "closed before request dispatch"
        else:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            return
        async with asyncio.timeout(2):
            with pytest.raises((ConnectionError, RuntimeError), match=expected):
                await pending
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await proxy.close()
        await process.stop()
