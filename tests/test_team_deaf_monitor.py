"""The real session endpoint publishes whether its Monitor has proved delivery."""

import asyncio
import os
import time
from contextlib import nullcontext, suppress
from dataclasses import replace

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.monitor import AttachMonitor, SessionReady
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.cli_bridges.shared import (
    Binding,
    MonitorProbe,
    MonitorService,
    Options,
    Serve,
    StoredBinding,
)
from agent_comms.execution.child_process import (
    NoProcess,
    ProcessIdentity,
)
from agent_comms.messaging.channel_targets import CommsTag
from agent_comms.messaging.messages import Addressed
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.heartbeat_delivery import OnHeartbeatDelivery
from agent_comms.registry.thread_status import ConnectedThreadStatus
from agent_comms.registry.threads import ExternalThread
from agent_comms.task_attachments.owner_notices import TeamNotice
from agent_comms.task_attachments.team_pings import TeamUpdate
from agent_comms.team.team_heartbeat import TeamHeartbeat

pytest_plugins = ("test_holder_standing_rules",)


@pytest.mark.asyncio
async def test_unconfirmed_monitor_reports_team_deaf_once(
    held_bus, tmp_path, monkeypatch,
):
    # A relative HOME keeps the real AF_UNIX endpoint below its path-length limit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    owner = ProcessIdentity.capture(os.getpid())
    # The reconnect assertion reads the published board packet; publication is off by default.
    colleague = held_bus.registry.require("colleague").as_member(ExternalThread,
        process_identity=owner, heartbeat_delivery=OnHeartbeatDelivery)
    held_bus.registry.register(colleague)
    state = tmp_path / "bridge-state"
    binding = Binding(root=str(held_bus.root), owner=held_bus.registry.require('colleague').incarnation,
                      thread='deaf-session', start=Binding.bus_head(str(held_bus.root)))
    operation = Serve(owner=owner, transcript=tmp_path / "deaf-session.jsonl")
    heartbeat = TeamHeartbeat()
    owner_thread = held_bus.messaging.user_identity(Checkout(held_bus.root)).name

    async def beat():
        await heartbeat.run(lambda: nullcontext(held_bus))

    def alarms():
        with held_bus.bus.bus.observing() as db:
            sequences = tuple(row.seq for row in Addressed.table.select(
                db, sender=owner_thread, target=CommsTag.channel(),
            ))
        return tuple(message for sequence in sequences
                     if (message := held_bus.bus.bus.lookup(sequence)).task == TeamNotice())

    with Binding.open(state) as db:
        binding.bind(db)
        bridge = ClaudeBridge(Options(operation, StoredBinding(), state, state.parent), binding.address(), db)
        await operation.prepare(bridge)
        service = MonitorService(bridge, owner, lambda: None)
        endpoint = WatchClaim(binding.thread).socket_path
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        serving = asyncio.create_task(service.run())
        try:
            async with asyncio.timeout(10):
                while not endpoint.exists():
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
                reader, writer = await asyncio.open_unix_connection(str(endpoint))
                request = FieldCodec.compact(FieldCodec.encode(AttachMonitor())) + "\n"
                writer.write(request.encode())
                await writer.drain()
                FieldCodec.decode(SessionReady, FieldCodec.parse(await reader.readline()))
                probe = MonitorProbe.model_validate_json(await reader.readline())
                unready = bridge.reader
            try:
                snapshot = held_bus.registry.snapshot()
                status = snapshot.status("colleague")
                assert not status.receiving
                assert status.stopped
                paused = snapshot.seen_at("colleague")
                assert status.paused_since(paused) == (paused,)
                assert type(snapshot.statuses["colleague"]) is type(status)
                await beat()
                assert alarms() == ()
                monkeypatch.setattr(time, "time", lambda: paused + 301)
                await beat()
                await beat()
                alarm, = alarms()
                assert alarm.body.startswith("colleague: ")
                assert "agent-comms-claude-bridge watch" in alarm.body

                await bridge.confirm_probe(probe)
                snapshot = held_bus.registry.snapshot()
                assert type(snapshot.status("colleague")) is ConnectedThreadStatus
                assert type(snapshot.statuses["colleague"]) is ConnectedThreadStatus
                assert snapshot.status("colleague").paused_since(snapshot.seen_at("colleague")) == ()
                await beat()
                assert alarms() == (alarm,)
                update, = held_bus.bus.bus.latest_addressed(
                    frozenset({owner_thread}), frozenset({"colleague"}),
                )
                # The board's open lines wake every beat (TeamUpdate.derive); reconnecting
                # must add no line of its own.
                assert isinstance(update.task, TeamUpdate)
                assert update.task.new_pings() == ()
            finally:
                writer.close()
                await writer.wait_closed()
            async with asyncio.timeout(10):
                while held_bus.registry.snapshot().status("colleague").receiving:
                    await asyncio.sleep(0.01)
            snapshot = held_bus.registry.snapshot()
            assert snapshot.status("colleague").paused_since(snapshot.seen_at("colleague"))
            await unready.confirm_probe(bridge, probe)
            assert not held_bus.registry.snapshot().status("colleague").receiving
            dead = replace(colleague, process_identity=NoProcess())
            assert (
                snapshot.statuses["colleague"].registered(dead, paused).paused_since(paused) == (paused,)
            )
        finally:
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving
