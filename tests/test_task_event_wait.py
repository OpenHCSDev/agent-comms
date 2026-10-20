"""A test event cannot outlive the failure of the task that produces it."""
import asyncio

import pytest

from conftest import await_event_or_task


async def test_original_task_failure_ends_the_event_wait():
    error = ValueError('original fake failure')

    async def fail():
        raise error

    async with asyncio.timeout(1):
        with pytest.raises(ValueError) as raised:
            await await_event_or_task(asyncio.Event().wait(), asyncio.create_task(fail()))
    assert raised.value is error


async def test_successful_task_without_event_is_a_failed_expectation():
    async def complete():
        return None

    async with asyncio.timeout(1):
        with pytest.raises(AssertionError, match='without the expected event'):
            await await_event_or_task(asyncio.Event().wait(), asyncio.create_task(complete()))


async def test_event_does_not_cancel_its_still_running_producer():
    arrived, release = asyncio.Event(), asyncio.Event()
    arrived.set()
    producer = asyncio.create_task(release.wait())
    try:
        assert await await_event_or_task(arrived.wait(), producer) is True
        assert not producer.done()
    finally:
        release.set()
        await producer
