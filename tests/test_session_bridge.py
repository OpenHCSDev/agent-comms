"""Socket/admission owners already exercised by the real Claude probe."""
import asyncio
from contextlib import asynccontextmanager
import json
import os

import pytest
import pytest_asyncio

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.monitor import AttachMonitor, EndSession, MonitorService, NoReader, REPLIES, VerifySession
from agent_comms.cli_bridges.shared import Binding, DeliveryRow, MONITOR_FRAME, ObservedMonitor, Options, Serve
from agent_comms.cli_protocol import DeliveredOutcome, EmittedOutcome, UnconfirmedOutcome
from agent_comms.messages import InfoKind, Message
from agent_comms_hooks.watch import WatchClaim, WatchRecord


@pytest_asyncio.fixture
async def endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(WatchClaim, 'path', property(lambda self: tmp_path / 'session.lock'))
    monkeypatch.setattr(WatchClaim, 'socket_path', property(lambda self: tmp_path.parent / 'session.sock'))
    claim = WatchClaim('session')
    owner = ProcessIdentity.capture(os.getpid())
    acknowledgments = []
    class Core:
        def __init__(self):
            self.messaging = self
        def acknowledge(self, thread, references):
            acknowledgments.extend(references)
    with Binding.open(tmp_path / 'state') as db, claim.held(WatchRecord('viewer', str(tmp_path), ('bridge', 'watch'))):
        binding = Binding(str(tmp_path), 'viewer', 'session')
        bridge = ClaudeBridge(Options(Serve(owner, tmp_path / 'session.jsonl'), binding, tmp_path / 'state'), binding, db)
        bridge.comms = Core()
        async def run():
            await MonitorService(bridge, owner, asyncio.current_task().cancel).run()
        task = asyncio.create_task(run())
        async with asyncio.timeout(3):
            while not claim.socket_path.exists():
                await asyncio.sleep(0)
        try:
            yield bridge, claim, owner, acknowledgments, task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def reader(claim):
    stream, writer = await asyncio.open_unix_connection(str(claim.socket_path))
    writer.write((AttachMonitor().model_dump_json() + '\n').encode())
    await writer.drain()
    REPLIES.validate_json(await stream.readline()).require()
    probe = MONITOR_FRAME.validate_json(await stream.readline())
    try:
        yield stream, writer, probe
    finally:
        writer.close()
        await writer.wait_closed()


def message(body, seq=1):
    return Message(sender='peer', target='viewer', type=InfoKind, body=body, timestamp=float(seq), seq=seq)


async def detached(bridge):
    async with asyncio.timeout(3):
        async with bridge.reader_changed:
            await bridge.reader_changed.wait_for(lambda: isinstance(bridge.reader, NoReader))


async def no_line(stream):
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.03):
            await stream.readline()


@pytest.mark.asyncio
async def test_admission_waits_for_current_probe_then_delivers_once(endpoint):
    bridge, claim, owner, acknowledgments, task = endpoint
    first = message('first')
    delivery = asyncio.create_task(bridge.accept(first))
    await asyncio.sleep(0)
    assert DeliveryRow.select(bridge.db) == []
    async with reader(claim) as (stream, writer, old_probe):
        assert not delivery.done()
        assert DeliveryRow.select(bridge.db) == []
        await old_probe.confirm(bridge, ObservedMonitor('probe-1', 1.))
        part = ClaudeMonitorFrame.decode_line((await stream.readline()).decode().strip())
        await delivery
        assert DeliveryRow.select(bridge.db)[0].outcome is EmittedOutcome
        await part.confirm(bridge, ObservedMonitor('part-1', 2.))
        assert acknowledgments == [first.reference]
    await detached(bridge)
    assert not task.done()
    second = message('second', 2)
    delivery = asyncio.create_task(bridge.accept(second))
    await asyncio.sleep(0)
    assert DeliveryRow.select(bridge.db, id=second.message_id) == []
    async with reader(claim) as (stream, writer, fresh_probe):
        assert fresh_probe != old_probe
        await old_probe.confirm(bridge, ObservedMonitor('old-history', 3.))
        await asyncio.sleep(0)
        assert not delivery.done()
        assert DeliveryRow.select(bridge.db, id=second.message_id) == []
        await fresh_probe.confirm(bridge, ObservedMonitor('probe-2', 4.))
        part = ClaudeMonitorFrame.decode_line((await stream.readline()).decode().strip())
        await delivery
        await part.confirm(bridge, ObservedMonitor('part-2', 5.))
        await bridge.accept(first)
        await no_line(stream)
        assert acknowledgments == [first.reference, second.reference, first.reference]
        assert all(row.outcome is DeliveredOutcome for row in DeliveryRow.select(bridge.db))


@pytest.mark.asyncio
async def test_detached_partial_attempt_is_retained_without_replay_or_old_window(endpoint):
    bridge, claim, owner, acknowledgments, task = endpoint
    uncertain = message('x' * 10000)
    async with reader(claim) as (stream, writer, probe):
        await probe.confirm(bridge, ObservedMonitor('probe-1', 1.))
        delivery = asyncio.create_task(bridge.accept(uncertain))
        await stream.readline()
    await detached(bridge)
    async with asyncio.timeout(3):
        await delivery
    assert DeliveryRow.select(bridge.db)[0].outcome is UnconfirmedOutcome
    assert not acknowledgments and not task.done()
    async with reader(claim) as (stream, writer, probe):
        await probe.confirm(bridge, ObservedMonitor('probe-2', 2.))
        await bridge.accept(uncertain)
        await no_line(stream)
        fresh = message('new message after the uncertain one', 2)
        await bridge.accept(fresh)
        part = ClaudeMonitorFrame.decode_line((await stream.readline()).decode().strip())
        assert part.id == fresh.message_id
        await part.confirm(bridge, ObservedMonitor('fresh-part', 3.))
        assert acknowledgments == [fresh.reference]
        assert DeliveryRow.select(bridge.db, id=uncertain.message_id)[0].outcome is UnconfirmedOutcome


@pytest.mark.asyncio
async def test_session_control_fences_process_incarnation(endpoint):
    bridge, claim, owner, acknowledgments, task = endpoint
    stale = ProcessIdentity(owner.pid, owner.start_time + 1)
    with pytest.raises(ValueError, match='different Claude process incarnation'):
        await asyncio.to_thread(EndSession(owner=stale).exchange, claim)
    assert not task.done()
    await asyncio.to_thread(VerifySession(owner=owner).exchange, claim)
    await asyncio.to_thread(EndSession(owner=owner).exchange, claim)
    async with asyncio.timeout(3):
        await asyncio.gather(task, return_exceptions=True)
    assert not claim.socket_path.exists()


@pytest.mark.asyncio
async def test_confirmed_outcome_cannot_be_downgraded_by_socket_completion(endpoint):
    bridge, claim, owner, acknowledgments, task = endpoint
    original = message('observed before the final drain returned')
    row = DeliveryRow(original.message_id, json.dumps(original.to_wire()), DeliveredOutcome)
    row.insert(bridge.db)
    bridge.db.commit()
    await row.finish(bridge, EmittedOutcome)
    assert DeliveryRow.select(bridge.db)[0].outcome is DeliveredOutcome


def test_enrol_is_idempotent_and_guard_reports_only_missing_reader(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(WatchClaim, 'path', property(lambda self: tmp_path / 'session.lock'))
    claim = WatchClaim('session')
    record = WatchRecord('viewer', str(tmp_path), ('bridge', 'watch'))
    claim.enrol(record)
    assert claim.state().record == record
    with claim.held(record):
        claim.enrol(record)
        claim.state().report('PostToolUse')
        assert 'Delivery is paused' in capsys.readouterr().out
        with claim.reader_held():
            claim.state().report('PostToolUse')
            assert capsys.readouterr().out == ''
