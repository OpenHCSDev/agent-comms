"""Socket and delivery owners exercised by the real Claude probe."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import signal
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pytest
import pytest_asyncio

from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.monitor import AttachMonitor, EndSession, MonitorService, NoReader, REPLIES, VerifySession
from agent_comms.cli_bridges.shared import Binding, BridgeLog, DeliveryRow, MONITOR_FRAME, ObservedMonitor, Options, Serve
from agent_comms.cli_protocol import DeliveredOutcome, EmittedOutcome, UnconfirmedOutcome
from agent_comms.messages import InfoKind, Message
from agent_comms_hooks import ClaudeHook
from agent_comms_hooks.watch import WatchClaim, WatchRecord


def test_bridge_log_timestamps_each_line_including_fragmented_writes_and_tracebacks():
    import io
    import traceback
    output = io.StringIO()
    log = BridgeLog(output)
    log.write('partial ')
    print('line\nnext line', file=log, flush=True)
    traceback.print_exception(ValueError('terminal failure'), file=log)
    bodies = []
    for line in output.getvalue().splitlines():
        stamp, body = line.split(' ', 1)
        assert datetime.fromisoformat(stamp).tzinfo is not None
        bodies.append(body)
    assert bodies == ['partial line', 'next line', 'ValueError: terminal failure']


@pytest.mark.asyncio
async def test_bridge_survives_its_watch_child_being_signalled(tmp_path, monkeypatch):
    """Production serve owns a real socket and replaces only its dead Core reader."""
    from agent_comms.field_codec import FieldCodec
    from agent_comms.launch_runtime import ThreadLaunch
    from agent_comms.thread_execution import ExternalThreadExecution
    from test_bus_route_counts_locking import _case
    from dataclasses import replace

    home = tmp_path.parent / 'home'
    home.mkdir(exist_ok=True)
    monkeypatch.setenv('HOME', str(home))
    endpoint_directory = tmp_path.parent / 'w'
    monkeypatch.setattr(WatchClaim, 'directory', classmethod(lambda cls: endpoint_directory))
    comms = _case(tmp_path / 'wire', monkeypatch)
    recipient = comms.registry.require('recipient')
    comms.registry.declare(replace(recipient, execution=ExternalThreadExecution))
    owner = ProcessIdentity.capture(os.getpid())
    session = 'signal-watch-session'
    transcript = tmp_path / f'{session}.jsonl'
    transcript.touch()
    claim = WatchClaim(session)
    state = tmp_path / 'state'
    log_path = tmp_path / 'serve.log'
    environment = ThreadLaunch.strip(dict(os.environ))
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')

    def observed(line, record_id):
        record = dict(type='user', uuid=record_id,
                      timestamp=datetime.now(timezone.utc).isoformat(), sessionId=session,
                      promptId='prompt', origin=dict(kind='task-notification'),
                      message=dict(content='<task-notification><event>' + escape(line)
                                   + '</event></task-notification>'))
        with transcript.open('a') as output:
            output.write(json.dumps(record) + '\n')

    def watch_process():
        children = Path(f'/proc/{bridge.pid}/task/{bridge.pid}/children').read_text().split()
        assert len(children) == 1, children
        return ProcessIdentity.capture(int(children[0]))

    # Only the socket locator is controlled: the normal HOME-based path exceeds
    # sockaddr_un under the mandated persistent scratch directory.
    driver = ('import sys\nfrom pathlib import Path\n'
              'from agent_comms_hooks.watch import WatchClaim\n'
              'directory = Path(sys.argv.pop(1))\n'
              'WatchClaim.directory = classmethod(lambda cls: directory)\n'
              'from agent_comms.cli_bridges.claude import main\nmain()\n')
    command = (sys.executable, '-c', driver, str(endpoint_directory), 'serve',
               '--root', str(comms.root), '--bus-thread', 'recipient', '--session', session,
               '--state', str(state), '--owner', json.dumps(FieldCodec.encode_record(owner)),
               '--transcript', str(transcript))
    with log_path.open('wb') as log:
        bridge = ParentedProcess.launch(command, env=environment, output=log)
        try:
            async with asyncio.timeout(20):
                while not claim.socket_path.exists():
                    assert bridge.alive(), log_path.read_text()
                    await asyncio.sleep(0.01)
            await asyncio.to_thread(VerifySession(owner=owner).exchange, claim)
            async with reader(claim) as (stream, writer, probe):
                observed(probe.model_dump_json(), 'probe')
                async with asyncio.timeout(20):
                    first_line = (await stream.readline()).decode().strip()
                first = ClaudeMonitorFrame.decode_line(first_line)
                assert first.body == 'first'
                observed(first_line, 'first-part')
                child = watch_process()
                child.signal(signal.SIGTERM)
                await asyncio.wait_for(child.wait_exited(), 10)
                await asyncio.to_thread(VerifySession(owner=owner).exchange, claim)
                comms.messaging.send('sender', 'recipient', 'after signal')
                async with asyncio.timeout(20):
                    after_line = (await stream.readline()).decode().strip()
                after = ClaudeMonitorFrame.decode_line(after_line)
                assert after.body == 'after signal' and after.id != first.id
                observed(after_line, 'after-part')
                assert bridge.alive() and watch_process() != child
                await no_line(stream)
            with Binding.open(state) as db:
                assert len(DeliveryRow.select(db)) == 2
        finally:
            await asyncio.to_thread(bridge.close)
    lines = log_path.read_text().splitlines()
    assert any('SIGTERM' in line and 'reopening its subscription' in line for line in lines)
    for line in lines:
        assert datetime.fromisoformat(line.split(' ', 1)[0]).tzinfo is not None


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
        bridge = ClaudeBridge(Options(Serve(owner=owner, transcript=tmp_path / 'session.jsonl'), binding, tmp_path / 'state'), binding, db)
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
async def test_delivery_waits_for_current_probe_then_delivers_once(endpoint):
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
        hook = ClaudeHook('session', 'PostToolUse')
        claim.state().report(hook)
        assert 'Delivery is paused' in capsys.readouterr().out
        with claim.reader_held():
            claim.state().report(hook)
            assert capsys.readouterr().out == ''
