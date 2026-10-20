"""ACP lock callbacks retain the Context in which they acquired their resource."""

import asyncio
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import pytest

from agent_comms.acp_ingress import AcpIngress
from agent_comms.active_route import LocalRoute
from agent_comms.route_selection import RouteSelection
from agent_comms.store_files import _store_lock


def ingress(root: Path) -> AcpIngress:
    root.mkdir()
    return AcpIngress(RouteSelection(LocalRoute(root), root), root)


@pytest.mark.asyncio
async def test_preflight_releases_the_real_store_lock_in_its_acquisition_context(tmp_path):
    client = ingress(tmp_path / "preflight")
    await client.preflight()
    with _store_lock(client.selection.root / "wire", blocking=False):
        pass


@pytest.mark.asyncio
async def test_prompt_holds_the_real_gate_through_write_and_then_releases(tmp_path):
    client = ingress(tmp_path / "prompt")

    def exclusive():
        with _store_lock(client.selection.root / "wire", blocking=False):
            pass

    async with client.prompt():
        with pytest.raises(BlockingIOError):
            await asyncio.to_thread(exclusive)
    await asyncio.to_thread(exclusive)


@pytest.mark.asyncio
async def test_abandoned_acquisition_closes_in_the_same_context():
    value = ContextVar("acp_test_context", default="caller")
    entered = threading.Event()
    finish = threading.Event()
    closed = asyncio.Event()
    loop = asyncio.get_running_loop()

    @contextmanager
    def resource():
        token = value.set("acquired")
        try:
            yield
        finally:
            assert value.get() == "acquired"
            value.reset(token)
            loop.call_soon_threadsafe(closed.set)

    def acquire(locks):
        locks.enter_context(resource())
        entered.set()
        assert finish.wait(3)

    task = asyncio.create_task(AcpIngress._acquire(acquire))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        finish.set()
    await asyncio.wait_for(closed.wait(), 3)
    assert value.get() == "caller"
