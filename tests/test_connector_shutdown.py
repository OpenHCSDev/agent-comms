"""SIGTERM reaches the real HTTP/MCP server, not a mocked cleanup callback."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession

from agent_comms.connector_store import ConnectorStore
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from connector_readiness import ConnectorReadiness
from connector_shutdown_fixture import ShutdownReadTool, ShutdownWriteTool


@pytest.mark.parametrize("tool", (ShutdownReadTool, ShutdownWriteTool))
async def test_sigterm_drops_a_read_but_finishes_a_bus_write(comms, tmp_path, tool):
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
        [sys.executable, "-m", "connector_shutdown_fixture", str(comms.root), str(auth), str(port)],
        cwd=project, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        await asyncio.to_thread(readiness.wait, process, port)
        async with ClientSession() as client:
            request = asyncio.create_task(client.post(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": tool.declared_name, "arguments": {"directory": str(control)},
                }},
            ))
            async with asyncio.timeout(10):
                while not (control / "entered").exists():
                    assert process.poll() is None
                    await asyncio.sleep(.01)
            started = time.monotonic()
            process.terminate()
            if tool is ShutdownWriteTool:
                await asyncio.sleep(1.2)
                assert process.poll() is None, "The admitted write was abandoned or capped"
                (control / "release").touch()
            await asyncio.to_thread(process.wait, timeout=5)
            elapsed = time.monotonic() - started
            replies = await asyncio.gather(request, return_exceptions=True)
            for reply in replies:
                if not isinstance(reply, BaseException):
                    reply.release()
        assert process.returncode == 0, process.stdout.read().decode()
        print(f"{tool.declared_name}: SIGTERM to process exit {elapsed:.3f}s")
        if tool is ShutdownWriteTool:
            assert elapsed >= 1.2
            assert (control / "finished").exists()
            assert any(message.body == "shutdown write committed"
                       for message in comms.bus.inbox("worker").page.messages)
        else:
            assert elapsed < 1, f"Read-only connector shutdown took {elapsed:.3f}s"
            assert not (control / "finished").exists()
            assert not (control / "release").exists()
    finally:
        (control / "release").touch()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        process.stdout.close()
        readiness.close()
