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
from agent_comms.order_tools import (
    CommanderUnitRead,
    CommsCommanderLoadTool,
    CommsHudTool,
    CommsUnjudgedTool,
)
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.service_passes import ServicePassStore
from agent_comms.todos import ProgressRead
from agent_comms.tool_caller import ThreadCaller
from agent_comms.ui_model.observation import ReadOperation
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
    for lines in (hud.frame, load.header):
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


def test_stopped_heartbeat_without_failure_explains_stale_board_and_refuses_details(published_board):
    comms = published_board
    owner = comms.registry.require('lead').incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    services.update(lambda passes: DocumentChange.to(replace(
        passes, commander_completed=passes.commander_completed | {owner: time.time() - 3600})))
    hud = CommanderFrame.current(comms, 'lead')
    assert not hud.services.failing
    assert hud.header[0] == 'no failure recorded, so no heartbeat is running'
    assert 'STALE retained' in hud.retained_status()
    card = hud.units[0]
    request = ReadOperation(str(comms.root), CommanderUnitRead(
        commander=owner, unit=card.unit,
        orders=tuple(dict.fromkeys(order.order for order in card.orders))))
    with pytest.raises(ToolRefusedError, match='no failure recorded, so no heartbeat is running'):
        request.read(comms)


def test_a_pass_still_inside_its_declared_duration_is_not_called_dead(published_board):
    comms = published_board
    services = ServicePassStore.at(comms.root)
    owner = comms.registry.require('lead').incarnation.require_recorded()
    services.update(lambda passes: DocumentChange.to(replace(
        passes, commander_completed=passes.commander_completed | {
            owner: time.time() - CommanderHeartbeat.pass_duration})))
    hud = CommanderFrame.current(comms, 'lead')
    assert hud.header[0] != 'no failure recorded, so no heartbeat is running'
    assert hud.retained_status() == 'Retained board rows and load:'
    hud.require_current()


def test_failing_watch_does_not_make_a_fresh_heartbeat_stale(published_board):
    comms = published_board
    services = ServicePassStore.at(comms.root)
    owner = comms.registry.require('lead').incarnation.require_recorded()
    services.heartbeat_passed((owner,), (owner,))
    services.failed('another-watch', OSError('independent watch failed'))
    hud = CommanderFrame.current(comms, 'lead')
    assert 'independent watch failed' in hud.header[0]
    assert hud.retained_status() == 'Retained board rows and load:'
    hud.require_current()


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


def test_one_unreadable_source_preserves_the_other_original_edit(published_board, fork_bus, tmp_path):
    from agent_comms.repo import Checkout
    from agent_comms.storage_rules import StorageRule
    from agent_comms.thread_execution import ExternalThreadExecution
    from agent_comms.thread_model import DeclaredModel
    from agent_comms.threads import Thread
    from test_edit_allocations import issue

    comms, source, template = fork_bus
    worker = comms.registry.declare(Thread(
        'unreadable-worker', frozenset({'comms'}), Checkout(source.checkout),
        model=DeclaredModel('anthropic/claude-opus-4-6'), execution=ExternalThreadExecution))
    rule = StorageRule.load(comms.root)
    checkout = rule.order_checkout(rule.roots_of(worker), 'P41')
    checkout.parent.mkdir(parents=True, exist_ok=True)
    source.git(source.checkout, 'worktree', 'add', '-q', '-b', 'unreadable-edit-source', str(checkout))
    issue(comms, source, replace(template, check=replace(template.check, directory=checkout)),
          worker.name, 'P41')
    metadata = checkout / '.git'
    retained_metadata = tmp_path / 'unreadable-source-metadata'
    metadata.rename(retained_metadata)
    try:
        result = ThreadCaller('lead').invoke(CommsUnjudgedTool(), comms)
    finally:
        retained_metadata.rename(metadata)
    assert not result.complete
    assert len(result.unavailable) == 1 and str(result.unavailable[0].problem) == 'P41'
    assert tuple(edit.id for edit in result.edits) == (ToolCallId('parent-edit-0'),)
