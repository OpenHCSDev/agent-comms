"""Retained board evidence never disguises a recorded failed service pass."""

import asyncio
import time
from contextlib import nullcontext, suppress
from dataclasses import replace

import pytest

from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.coordinator import Coordination
from agent_comms.commander_hud import CommanderFrame
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import ToolCallId
from agent_comms.locked_store import DocumentChange
from agent_comms.order_tools import CommsCommanderLoadTool, CommsHudTool, CommsUnjudgedTool
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.service_passes import ServicePassStore
from agent_comms.todos import ProgressRead
from agent_comms.tool_caller import ThreadCaller
from agent_comms.ui_model.unit_state import UnitState
from commander_heartbeat_fixture import ObservedHeartbeat, capture_frame

pytest_plugins = ('test_commander_forks',)


@pytest.fixture
def published_board(fork_bus, installed_runtime):
    comms, source, _ = fork_bus
    from test_commander_forks import record_edits
    record_edits(comms, source, 1)
    parent = comms.registry.require('lead')
    frame = capture_frame(comms, parent.name, ProgressRead())
    states = UnitState.current(comms, parent.name, frame)
    observation = CommanderObservation(parent, frame, (), states)
    asyncio.run(ObservedHeartbeat((observation,)).run(lambda: nullcontext(comms)))
    return comms


def test_recorded_failure_precedes_stale_board_and_load_without_changing_retained_data(published_board):
    comms = published_board
    owner = comms.registry.require('lead').incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    original = CommanderFrame.current(comms, 'lead')
    old_completion = time.time() - 3600
    services.update(lambda passes: DocumentChange.to(replace(
        passes, commander_completed=passes.commander_completed | {owner: old_completion})))
    services.failed('CommanderHeartbeat', KeyError('original branch read failed'))
    failure = services.read().failing['CommanderHeartbeat']
    hud = ThreadCaller('lead').invoke(CommsHudTool(), comms)
    load = ThreadCaller('lead').invoke(CommsCommanderLoadTool(), comms)
    assert hud.units == original.units and hud.load == original.load
    assert hud.heartbeat == RecordedEventTime(old_completion)
    assert load.load == original.load and load.heartbeat == hud.heartbeat
    for lines in (hud.frame, load.frame):
        assert 'original branch read failed' in lines[0]
        assert 'elapsed' in lines[0]
        assert 'last completed' in lines[1]
        assert any('STALE retained' in line for line in lines)
    edits = ThreadCaller('lead').invoke(CommsUnjudgedTool(), comms)
    assert 'original branch read failed' in edits.service_status[0]
    assert edits.complete and edits.unavailable == ()
    assert len(edits.edits) == 1 and edits.edits[0].id == ToolCallId('parent-edit-0')
    assert services.read().failing['CommanderHeartbeat'] == failure
    assert services.read().commander_completed[owner] == old_completion
    services.failed('CommanderHeartbeat', ValueError('newest failure'))
    assert services.read().failing['CommanderHeartbeat'].since == failure.since
    assert 'newest failure' in CommanderFrame.current(comms, 'lead').frame[0]
    published_before = tuple((path, path.read_bytes()) for path in CommanderFrame.matching_in(comms.root))

    async def recover():
        task = asyncio.create_task(Coordination.watch_worker(
            CommanderHeartbeat(), lambda: nullcontext(comms), services))
        try:
            async with asyncio.timeout(15):
                while 'CommanderHeartbeat' in services.read().failing:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(recover())
    recovered = CommanderFrame.current(comms, 'lead')
    assert recovered.services.failing == {}
    assert recovered.heartbeat.since(old_completion)
    assert any(path.read_bytes() != before for path, before in published_before)
    assert recovered.heartbeat == RecordedEventTime(services.read().commander_completed[owner])


def test_missing_frame_reports_recorded_failure_without_fabricating_a_board(fork_bus):
    comms, _, _ = fork_bus
    services = ServicePassStore.at(comms.root)
    services.failed('CommanderHeartbeat', KeyError('no readable publication'))
    with pytest.raises(ToolRefusedError) as refused:
        CommanderFrame.current(comms, 'lead')
    lines = str(refused.value).splitlines()
    assert 'no readable publication' in lines[0]
    assert 'Board unavailable:' in lines[-1]
    assert not CommanderFrame.matching_in(comms.root)
    with pytest.raises(ToolRefusedError, match='no readable publication'):
        CommanderFrame.read_all(comms.root)
