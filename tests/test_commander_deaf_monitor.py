"""The real session endpoint publishes whether its Monitor has proved delivery."""

import asyncio
import os
import time
from contextlib import suppress
from dataclasses import replace

import pytest

from agent_comms.channel_targets import CommsTag
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.task_attachments.commander_pings import CommanderNotice
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.monitor import AttachMonitor, MonitorService, SessionReady
from agent_comms.cli_bridges.shared import Binding, MonitorProbe, Options, Serve, StoredBinding
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Addressed
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import ConnectedThreadStatus
from agent_comms_hooks.watch import WatchClaim

pytest_plugins = ("test_holder_standing_rules",)


@pytest.mark.asyncio
async def test_unconfirmed_monitor_reports_commander_deaf_once(
    held_bus, installed_runtime, tmp_path, monkeypatch,
):
    # A relative HOME keeps the real AF_UNIX endpoint below its path-length limit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "home")
    owner = ProcessIdentity.capture(os.getpid())
    commander = replace(
        held_bus.registry.require("lead"),
        execution=ExternalThreadExecution,
        process_identity=owner,
    )
    held_bus.registry.register(commander)
    state = tmp_path / "bridge-state"
    binding = Binding(root=str(held_bus.root), owner=held_bus.registry.require('lead').incarnation,
                      thread='deaf-session', start=Binding.bus_head(str(held_bus.root)))
    operation = Serve(owner=owner, transcript=tmp_path / "deaf-session.jsonl")
    heartbeat = CommanderHeartbeat()

    def beat():
        heartbeat.pass_body(held_bus, heartbeat.observe(held_bus))

    def alarms():
        with held_bus.bus.bus.observing() as db:
            sequences = tuple(row.seq for row in Addressed.table.select(
                db, sender=heartbeat.sender, target=CommsTag.channel(),
            ))
        return tuple(message for sequence in sequences
                     if "COMMANDER DEAF" in (message := held_bus.bus.bus.lookup(sequence)).body)

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
                status = snapshot.status("lead")
                assert not status.receiving
                assert status.stopped
                paused = snapshot.seen_at("lead")
                assert status.paused_since(paused) == (paused,)
                assert type(snapshot.statuses["lead"]) is type(status)
                beat()
                assert alarms() == ()
                monkeypatch.setattr(time, "time", lambda: paused + 301)
                beat()
                beat()
                alarm, = alarms()
                assert alarm.body.startswith("lead: COMMANDER DEAF")
                assert "agent-comms-claude-bridge watch" in alarm.body

                await bridge.confirm_probe(probe)
                snapshot = held_bus.registry.snapshot()
                assert type(snapshot.status("lead")) is ConnectedThreadStatus
                assert type(snapshot.statuses["lead"]) is ConnectedThreadStatus
                assert snapshot.status("lead").paused_since(snapshot.seen_at("lead")) == ()
                beat()
                assert alarms() == (alarm,)
                update, = held_bus.bus.bus.latest_addressed(
                    frozenset({heartbeat.sender}), frozenset({"lead"}),
                )
                assert isinstance(update.task, CommanderNotice)
            finally:
                writer.close()
                await writer.wait_closed()
            async with asyncio.timeout(10):
                while held_bus.registry.snapshot().status("lead").receiving:
                    await asyncio.sleep(0.01)
            snapshot = held_bus.registry.snapshot()
            assert snapshot.status("lead").paused_since(snapshot.seen_at("lead"))
            await unready.confirm_probe(bridge, probe)
            assert not held_bus.registry.snapshot().status("lead").receiving
            dead = replace(commander, process_identity=NoProcess())
            assert (
                snapshot.statuses["lead"].registered(dead, paused).paused_since(paused) == (paused,)
            )
        finally:
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving
