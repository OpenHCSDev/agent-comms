"""Retained board evidence never disguises a recorded failed service pass."""

import asyncio
import time
from contextlib import nullcontext, suppress
from dataclasses import replace

import pytest

from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.nominal.field_codec import ToolCallId
from agent_comms.planning.orders import ProgressRead
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.team_heartbeat import TeamHeartbeat, TeamObservation
from agent_comms.team.team_hud import TeamFrame
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.orders import (
    CommsBoardTool,
    CommsHudTool,
    CommsTeamLoadTool,
    CommsUnjudgedTool,
)
from agent_comms.transport.tools.reads import TeammateRead
from agent_comms.ui_model.observation import ReadOperation
from agent_comms.ui_model.teammate_state import TeammateState
from agent_comms_hooks import ToolRefusedError
from team_heartbeat_fixture import ObservedHeartbeat, capture_frame

pytest_plugins = ('test_forks',)


@pytest.fixture
def published_board(fork_bus, installed_runtime):
    comms, source, _ = fork_bus
    from test_forks import record_edits
    record_edits(comms, source, 1)
    frame = capture_frame(comms, ProgressRead())
    states = TeammateState.current(comms, frame)
    observation = TeamObservation(frame, (), states)
    asyncio.run(ObservedHeartbeat(observation).run(lambda: nullcontext(comms)))
    return comms


def test_recorded_failure_precedes_stale_board_and_load_without_changing_retained_data(published_board):
    comms = published_board
    owner = comms.registry.require('colleague').incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    original = TeamFrame.current(comms, 'colleague')
    old_completion = time.time() - 3600
    services.update(lambda passes: DocumentChange.to(replace(
        passes, teammate_completed=passes.teammate_completed | {owner: old_completion})))
    services.failed('TeamHeartbeat', KeyError('original branch read failed'))
    failure = services.read().failing['TeamHeartbeat']
    hud = ThreadCaller('colleague').invoke(CommsHudTool(), comms)
    load = ThreadCaller('colleague').invoke(CommsTeamLoadTool(), comms)
    assert hud.units == original.units and hud.load == original.load
    assert hud.heartbeat == RecordedEventTime(old_completion)
    assert load.load == original.load and load.heartbeat == hud.heartbeat
    board = ThreadCaller('colleague').invoke(CommsBoardTool(), comms)
    for lines in (hud.frame, load.header, board.lines):
        assert 'original branch read failed' in lines[0]
        assert lines[0].endswith(' ago)')  # the failure's age, painted when the line is read
        assert 'last completed' in lines[1]
        assert any('Nothing is refreshing these rows' in line for line in lines)
    edits = ThreadCaller('colleague').invoke(CommsUnjudgedTool(), comms)
    assert 'original branch read failed' in edits.service_status[0]
    assert edits.complete and edits.unavailable == ()
    assert len(edits.edits) == 1 and edits.edits[0].id == ToolCallId('parent-edit-0')
    assert services.read().failing['TeamHeartbeat'] == failure
    assert services.read().teammate_completed[owner] == old_completion
    services.failed('TeamHeartbeat', ValueError('newest failure'))
    assert services.read().failing['TeamHeartbeat'].since == failure.since
    assert 'newest failure' in TeamFrame.current(comms, 'colleague').frame[0]
    published_before = tuple((path, path.read_bytes()) for path in TeamFrame.matching_in(comms.root))

    async def recover():
        task = asyncio.create_task(Coordination.watch_worker(
            TeamHeartbeat(), lambda: nullcontext(comms), services))
        try:
            async with asyncio.timeout(15):
                while 'TeamHeartbeat' in services.read().failing:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(recover())
    recovered = TeamFrame.current(comms, 'colleague')
    assert recovered.services.failing == {}
    assert recovered.heartbeat.since(old_completion)
    assert any(path.read_bytes() != before for path, before in published_before)
    assert recovered.heartbeat == RecordedEventTime(services.read().teammate_completed[owner])


def test_stopped_heartbeat_without_failure_explains_stale_board_and_refuses_details(published_board):
    comms = published_board
    owner = comms.registry.require('colleague').incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    services.update(lambda passes: DocumentChange.to(replace(
        passes, teammate_completed=passes.teammate_completed | {owner: time.time() - 3600})))
    hud = TeamFrame.current(comms, 'colleague')
    assert not hud.services.failing
    # One line says it, not two. The header no longer opens with the double negative
    # service_status used to add beside this; retained_status is the only place the
    # board says whether it is refreshing, so the two cannot disagree.
    assert hud.retained_status() == 'Nothing is refreshing these rows. Last time they were read:'
    assert 'no failure recorded' not in '\n'.join(hud.header)
    card = hud.units[0]
    request = ReadOperation(str(comms.root), TeammateRead(
        frame_owner=owner, unit=card.unit,
        orders=tuple(dict.fromkeys(order.order for order in card.orders))))
    # The refusal carries its own reason rather than the deleted header line.
    with pytest.raises(ToolRefusedError, match='the teammate heartbeat is stale'):
        request.read(comms)


def test_a_pass_still_inside_its_declared_duration_is_not_called_dead(published_board):
    comms = published_board
    services = ServicePassStore.at(comms.root)
    owner = comms.registry.require('colleague').incarnation.require_recorded()
    services.update(lambda passes: DocumentChange.to(replace(
        passes, teammate_completed=passes.teammate_completed | {
            owner: time.time() - TeamHeartbeat.pass_duration})))
    hud = TeamFrame.current(comms, 'colleague')
    assert hud.retained_status() == 'The team, right now:'
    hud.require_current()


def test_failing_watch_does_not_make_a_fresh_heartbeat_stale(published_board):
    comms = published_board
    services = ServicePassStore.at(comms.root)
    owner = comms.registry.require('colleague').incarnation.require_recorded()
    services.heartbeat_passed((owner,), (owner,))
    services.failed('another-watch', OSError('independent watch failed'))
    hud = TeamFrame.current(comms, 'colleague')
    assert 'independent watch failed' in hud.header[0]
    assert hud.retained_status() == 'The team, right now:'
    hud.require_current()


def test_missing_frame_reports_recorded_failure_without_fabricating_a_board(fork_bus):
    comms, _, _ = fork_bus
    services = ServicePassStore.at(comms.root)
    services.failed('TeamHeartbeat', KeyError('no readable publication'))
    with pytest.raises(ToolRefusedError) as refused:
        TeamFrame.current(comms, 'colleague')
    lines = str(refused.value).splitlines()
    assert 'no readable publication' in lines[0]
    assert 'Board unavailable:' in lines[-1]
    assert not TeamFrame.matching_in(comms.root)
    with pytest.raises(ToolRefusedError, match='no readable publication'):
        TeamFrame.read_all(comms.root)


def test_one_unreadable_source_preserves_the_other_original_edit(
        published_board, fork_bus, configured_repository, tmp_path):
    from agent_comms.projects.checkouts import Checkout
    from agent_comms.projects.repository_name import RepositoryName
    from agent_comms.registry.thread_model import DeclaredModel
    from agent_comms.registry.threads import ExternalThread
    from source_revision_fixture import SourceRepository
    from test_edit_allocations import issue

    comms, source, template = fork_bus
    # P41 checks a second registered repository, so breaking it leaves the parent's source readable.
    unreadable = SourceRepository.create(
        tmp_path / 'unreadable-source', RepositoryName.from_wire('example/unreadable'))
    configured_repository(comms, source, unreadable)
    worker = comms.registry.declare(ExternalThread(
        'unreadable-worker', frozenset({'comms'}), Checkout(source.checkout),
        model=DeclaredModel('anthropic/claude-opus-4-6')))
    issue(comms, unreadable, replace(template, check=replace(template.check, directory=unreadable.checkout)),
          worker.name, 'P41')
    # A HEAD Git cannot parse keeps the folder a checkout (Checkout.git_roots) whose reads fail;
    # removing `.git` would instead declare the folder retired.
    head = unreadable.checkout / '.git' / 'HEAD'
    retained_head = head.read_text()
    head.write_text('not a reference\n')
    try:
        result = ThreadCaller('colleague').invoke(CommsUnjudgedTool(), comms)
    finally:
        head.write_text(retained_head)
    assert not result.complete
    assert len(result.unavailable) == 1 and str(result.unavailable[0].problem) == 'P41'
    assert tuple(edit.id for edit in result.edits) == (ToolCallId('parent-edit-0'),)
