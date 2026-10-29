"""SIGTERM reaches the real HTTP/MCP server and scheduler, not a cleanup fake."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import ClientSession

from agent_comms.comms import Comms
from agent_comms.connector_store import ConnectorStore
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from connector_readiness import ConnectorReadiness
from connector_shutdown_fixture import ShutdownReadTool, ShutdownWriteTool
from watch_shutdown_fixture import ShutdownWatch


@dataclass(frozen=True)
class ShutdownServer:
    """One owner for each proof's process, readiness pipe and release files."""

    process: subprocess.Popen[bytes]
    port: int
    token: str
    directory: Path

    @classmethod
    @asynccontextmanager
    async def launch(cls, comms: Comms, tmp_path: Path, module: str, *arguments: str) -> AsyncIterator[ShutdownServer]:
        for name in ("worker", "claude-ai"):
            comms.registry.declare(Thread(name, frozenset(), Checkout(comms.root)))
        auth = tmp_path / "auth"
        token = ConnectorStore.at(auth).thread_granted("claude-ai")
        control = tmp_path / "call"
        control.mkdir()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        project = Path(__file__).resolve().parents[1]
        readiness = ConnectorReadiness()
        environment = readiness.environment({**os.environ, "PYTHONPATH": os.pathsep.join((
            str(project / "src"), str(project / "tools"), str(project / "tests"),
            os.environ.get("PYTHONPATH", ""),
        ))})
        process = subprocess.Popen(
            [sys.executable, "-m", module, str(comms.root), str(auth), str(port), *arguments],
            cwd=project, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            await asyncio.to_thread(readiness.wait, process, port)
            yield cls(process, port, token, control)
        finally:
            (control / "release").touch()
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)
            process.stdout.close()
            readiness.close()

    async def entered(self) -> None:
        async with asyncio.timeout(10):
            while not (self.directory / "entered").exists():
                assert self.process.poll() is None
                await asyncio.sleep(.01)

    async def exited(self) -> str:
        await asyncio.to_thread(self.process.wait, timeout=5)
        output = self.process.stdout.read().decode()
        assert self.process.returncode == 0, output
        return output


@pytest.mark.parametrize("tool", (ShutdownReadTool, ShutdownWriteTool))
async def test_sigterm_drops_a_read_but_finishes_a_bus_write(comms, tmp_path, tool):
    async with ShutdownServer.launch(comms, tmp_path, "connector_shutdown_fixture") as server:
        async with ClientSession() as client:
            request = asyncio.create_task(client.post(
                f"http://127.0.0.1:{server.port}/mcp",
                headers={"Authorization": f"Bearer {server.token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": tool.declared_name, "arguments": {"directory": str(server.directory)},
                }},
            ))
            await server.entered()
            started = time.monotonic()
            server.process.terminate()
            if tool is ShutdownWriteTool:
                await asyncio.sleep(1.2)
                assert server.process.poll() is None, "The admitted write was abandoned or capped"
                (server.directory / "release").touch()
            output = await server.exited()
            elapsed = time.monotonic() - started
            replies = await asyncio.gather(request, return_exceptions=True)
            for reply in replies:
                if not isinstance(reply, BaseException):
                    reply.release()
        assert f"pending tools: {tool.declared_name}" in output
        print(f"{tool.declared_name}: SIGTERM to process exit {elapsed:.3f}s")
        if tool is ShutdownWriteTool:
            assert elapsed >= 1.2
            assert (server.directory / "finished").exists()
            assert any(message.body == "shutdown write committed"
                       for message in comms.bus.inbox("worker").page.messages)
        else:
            assert elapsed < 1, f"Read-only connector shutdown took {elapsed:.3f}s"
            assert not (server.directory / "finished").exists()
            assert not (server.directory / "release").exists()


@pytest.mark.parametrize("watch", ShutdownWatch.members_with(ShutdownWatch))
async def test_sigterm_drops_observe_but_joins_and_names_pass_body(comms, tmp_path, watch):
    async with ShutdownServer.launch(
        comms, tmp_path, "watch_shutdown_fixture", watch.declared_name, str(tmp_path / "call"),
    ) as server:
        await server.entered()
        started = time.monotonic()
        server.process.terminate()
        await watch.release_on_stop(server.process, server.directory)
        output = await server.exited()
        elapsed = time.monotonic() - started
        holders = [line for line in output.splitlines() if "Connector stopping;" in line]
        assert len(holders) == 1, output
        assert f"pending watches: {watch.__name__}" in holders[0]
        watch.check_exit(comms, server.directory, elapsed)
        print(f"{watch.__name__}: SIGTERM to process exit {elapsed:.3f}s; {holders[0]}")
