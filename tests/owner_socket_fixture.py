"""Registered owner threads, their runtime sockets and their child processes for owner-read tests.

A native owner's socket here is a real Unix-socket peer that receives exactly one request
and closes without replying: it proves dispatch and then reply loss, never the domain
answer. An external owner has a live process and no socket. Child processes are real
spawned processes that a test may stop through their own owner.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest

from agent_comms.child_process import ProcessIdentity, SpawnedProcess
from agent_comms.comms import Comms
from agent_comms.private_path import PrivateSocketRole
from agent_comms.repo import Checkout
from agent_comms.owner_connection import socket_path
from agent_comms.thread_execution import NativeThreadExecution, ThreadExecution
from agent_comms.threads import Thread


@pytest.fixture
def runtime_processes() -> Iterator[tuple[SpawnedProcess, SpawnedProcess]]:
    children = []
    try:
        for _ in range(2):
            children.append(
                SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
            )
        yield tuple(children)
    finally:
        for child in children:
            child.stop_sync()


def declare_owner(
    comms: Comms, name: str, process: ProcessIdentity,
    execution: type[ThreadExecution] = NativeThreadExecution,
) -> Thread:
    return comms.registry.declare(Thread(
        name, frozenset(), Checkout(comms.root.parent), process_identity=process, execution=execution,
    ))


@asynccontextmanager
async def silent_socket(comms: Comms, process: ProcessIdentity) -> AsyncIterator[list[dict[str, object]]]:
    """The owner's socket path served by a peer that reads one request and hangs up."""
    path = socket_path(comms.root, process.pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    received: list[dict[str, object]] = []

    async def disconnect_after_request(reader, writer):
        received.append(json.loads(await reader.readline()))
        writer.close()
        await writer.wait_closed()

    with PrivateSocketRole.address(path) as address:
        server = await asyncio.start_unix_server(disconnect_after_request, path=address)
        try:
            yield received
        finally:
            server.close()
            await server.wait_closed()
            path.unlink(missing_ok=True)
