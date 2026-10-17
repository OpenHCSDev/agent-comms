"""Writer contention keeps the watch child; terminal failures end it."""
import asyncio
from contextlib import suppress
import os
import sys

import pytest

from agent_comms.child_process import AttachedChild
from agent_comms.cli_bridges.shared import Binding, CliBridge, Operation, Options
from agent_comms.cli_protocol import DeliveredOutcome, FailedCoreError


class WatchOnly(Operation):
    async def run(self, bridge):
        await bridge.watch()


class ReportingBridge(CliBridge):
    async def deliver(self, message):
        raise AssertionError('No message may be delivered in an exit check')

    async def show_sent(self, result, target, body):
        raise AssertionError('No send in an exit check')

    async def report_failure(self, text):
        self.failures.append(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('frame,code', [
    ('', 0), ('', 17),
    (FailedCoreError(error='bad root').model_dump_json(), 1),
])
async def test_watch_exit_is_reported_once_with_exit_and_log_tail(tmp_path, monkeypatch, frame, code):
    start = AttachedChild.start
    starts = []

    async def launch(command, **options):
        starts.append(command)
        script = ('import sys; '
                  'sys.stderr.write("old-prefix" + "x" * 20000 + "tail-evidence\\n"); '
                  f'print({frame!r}, flush=True) if {bool(frame)!r} else None; '
                  f'sys.exit({code})')
        return await start((sys.executable, '-c', script), **options)

    monkeypatch.setattr(AttachedChild, 'start', launch)
    with Binding.open(tmp_path) as db:
        bridge = ReportingBridge(Options(WatchOnly(), None, tmp_path),
                                 Binding(str(tmp_path), 'viewer', 'session'), db)
        bridge.failures = []
        with pytest.raises(ExceptionGroup) as failure:
            await bridge.run()
        assert len(starts) == 1
        assert len(bridge.failures) == 1
        report = bridge.failures[0]
        assert f'exit {code}' in report
        assert 'tail-evidence' in report
        assert 'old-prefix' not in report
        assert 'No message was retried.' in report
        assert isinstance(failure.value.exceptions[0], ChildProcessError)
        assert (tmp_path / 'watch.stderr.log').read_bytes().endswith(b'tail-evidence\n')


@pytest.mark.asyncio
async def test_route_writer_delays_delivery_without_starting_a_second_child(tmp_path, monkeypatch):
    from agent_comms.bus_route_counts import BusRouteCounts
    from agent_comms.store_files import _store_lock
    from test_bus_route_counts_locking import _case

    comms = _case(tmp_path / 'wire', monkeypatch)
    comms.messaging.send_message('sender', 'recipient', 'second')
    comms.messaging.send_message('sender', 'recipient', 'third')
    ready_r, ready_w = os.pipe()
    blocked = asyncio.Event()
    delivered = asyncio.Event()
    starts, children, messages = [], [], []
    start = AttachedChild.start
    # Run the actual CLI with a small rebuild bound and a pipe observing its
    # blocked state. The pipe is not part of the watch protocol.
    driver = '''
import os, sys
from agent_comms.bus_route_counts import BusRouteCounts, RouteIndexBlocked
from agent_comms.cli import main
BusRouteCounts.rebuild_bytes = 1
original = RouteIndexBlocked.defer_read
def observe(self, through, size):
    os.write(int(sys.argv[1]), b'B')
    return original(self, through, size)
RouteIndexBlocked.defer_read = observe
raise SystemExit(main(sys.argv[2:]))
'''

    async def launch(command, **options):
        starts.append(command)
        child = await start((sys.executable, '-c', driver, str(ready_w), *command[1:]),
                            pass_fds=(ready_w,), **options)
        children.append(child)
        return child

    class ReceivingBridge(ReportingBridge):
        async def deliver(self, message):
            messages.append(message.body)
            if len(messages) == 3:
                delivered.set()
            return DeliveredOutcome

    monkeypatch.setattr(AttachedChild, 'start', launch)
    loop = asyncio.get_running_loop()

    def observed():
        assert os.read(ready_r, 1) == b'B'
        loop.remove_reader(ready_r)
        blocked.set()

    loop.add_reader(ready_r, observed)
    state = tmp_path / 'bridge'
    with Binding.open(state) as db:
        bridge = ReceivingBridge(Options(WatchOnly(), None, state),
                                 Binding(str(comms.root), 'recipient', 'session'), db)
        bridge.failures = []
        task = None
        try:
            with _store_lock(BusRouteCounts(comms.bus.log.path).path):
                task = asyncio.create_task(bridge.run())
                await asyncio.wait_for(blocked.wait(), 10)
                assert messages == [] and not task.done()
                assert len(starts) == 1 and children[0].returncode is None
            await asyncio.wait_for(delivered.wait(), 10)
            assert messages == ['first', 'second', 'third']
            assert len(starts) == 1 and children[0].returncode is None
            assert bridge.failures == []
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            loop.remove_reader(ready_r)
            os.close(ready_r)
            os.close(ready_w)
        assert children[0].returncode is not None
