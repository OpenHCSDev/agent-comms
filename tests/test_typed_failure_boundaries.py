"""Failure ownership across the optional worker and observation task boundary."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from acp import RequestError

from agent_comms.context_segments.optional_awareness import OmittedAwareness
from agent_comms.optional_awareness_projection import OptionalAwarenessProjection
from agent_comms.ui_model.observation import OwnerRead, ReadOwnerGoal, ReadRefused


@pytest.mark.asyncio
async def test_awareness_future_delivers_original_programming_defect(monkeypatch):
    error = TypeError("awareness implementation defect")
    monkeypatch.setattr(OptionalAwarenessProjection, "_build", Mock(side_effect=error))
    projection = OptionalAwarenessProjection(object(), 0, 1, 1, 1, registry=object())
    with pytest.raises(TypeError) as failed:
        await projection.segments(None, None, None)
    assert failed.value is error


@pytest.mark.asyncio
async def test_awareness_timeout_does_not_release_running_read(monkeypatch):
    release = threading.Event()
    exited = threading.Event()
    slot = threading.BoundedSemaphore(1)

    def read(*_):
        try:
            assert release.wait(2)
            return OmittedAwareness.capture(reason="finished")
        finally:
            exited.set()

    monkeypatch.setattr(OptionalAwarenessProjection, "_build", read)
    monkeypatch.setattr(OptionalAwarenessProjection, "_build_slot", slot)
    monkeypatch.setattr(OptionalAwarenessProjection, "build_seconds", 0.01)
    projection = OptionalAwarenessProjection(object(), 0, 1, 1, 1, registry=object())
    try:
        assert await projection.segments(None, None, None)
        assert not slot.acquire(blocking=False)
        assert await projection.segments(None, None, None)
    finally:
        release.set()
        assert await asyncio.to_thread(exited.wait, 2)
    # Future callbacks release the slot after the operation actually returns.
    assert await asyncio.to_thread(slot.acquire, True, 2)
    slot.release()


@pytest.mark.asyncio
async def test_owner_read_programming_defect_reaches_service_task_owner(monkeypatch):
    error = TypeError("owner read implementation defect")
    monkeypatch.setattr(OwnerRead, "require_root", Mock())
    monkeypatch.setattr(OwnerRead, "ask", AsyncMock(side_effect=error))
    service = SimpleNamespace(comms=None, owner_reads={}, tasks=asyncio.TaskGroup(), send=Mock())
    with pytest.raises(ExceptionGroup) as failed:
        async with service.tasks:
            ReadOwnerGoal(root="/unused", owner=object()).receive(service)
    assert failed.value.exceptions == (error,)
    assert not service.owner_reads
    service.send.assert_not_called()
