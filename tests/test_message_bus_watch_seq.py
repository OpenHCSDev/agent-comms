"""Ordinary committed appends drive the actual recipient watch, with no writer hints."""

import asyncio
from contextlib import AsyncExitStack, aclosing

import pytest

from agent_comms.locked_store import RegistryFile
from agent_comms.comms import wire
from agent_comms.publisher import Publisher
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.registration import Registration
from bus_record_fixture import append_reply
from agent_comms.threads import Thread


@pytest.mark.asyncio
async def test_real_message_bus_watch_recipient_suffix_and_rollback(bus):
    root = bus.path.parent
    registry = Registration(RegistryFile.path_in(root))
    people = {
        name: registry.declare(Thread(
            name, frozenset(), str(root))).incarnation.require_recorded()
        for name in ("sender", "target", "idle")
    }
    messages = wire(root, Publisher)
    primers = {
        name: append_reply(bus, people["sender"], people[name], f"prime {name}")
        for name in ("target", "idle")
    }
    tasks = []
    async with AsyncExitStack() as resources:
        streams = {
            name: await resources.enter_async_context(aclosing(messages.watch(
                name, acknowledgment=ExplicitWatchAcknowledgment)))
            for name in ("target", "idle")
        }
        try:
            # A real first output proves native registration and the entire
            # inbox path are live before testing a subsequent committed wake.
            for name, stream in streams.items():
                first = await asyncio.wait_for(anext(stream), 5)
                assert first.reference == primers[name].reference
            target = asyncio.create_task(anext(streams["target"]))
            idle = asyncio.create_task(anext(streams["idle"]))
            tasks.extend((target, idle))
            await asyncio.sleep(0)
            assert not target.done() and not idle.done()

            first = append_reply(bus, people["sender"], people["target"], "new first")
            second = append_reply(bus, people["sender"], people["target"], "new second")
            assert (await asyncio.wait_for(target, 5)).reference == first.reference
            assert (await asyncio.wait_for(anext(streams["target"]), 5)).reference == second.reference
            target = asyncio.create_task(anext(streams["target"]))
            tasks.append(target)
            await asyncio.sleep(0)

            before = bus.revision()

            def rolled_back(seq, db):
                raise ValueError("controlled append rollback")

            with pytest.raises(ValueError, match="controlled append rollback"):
                bus.append(rolled_back)
            assert bus.revision() == before
            done, _ = await asyncio.wait((target, idle), timeout=0.2)
            assert not done, "unaddressed or rolled-back append yielded a message"
            print("real Comms.watch: exact new recipient suffix; unaddressed and rollback silence")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
