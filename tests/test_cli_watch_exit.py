"""Writer contention keeps the watch child; terminal failures end it."""
import asyncio
from contextlib import suppress
from dataclasses import replace
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

    async def show_sent(self, result):
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
        assert 'Watch stream closed' not in report
        assert len(report) < 12000
        assert 'No message was retried.' in report
        assert isinstance(failure.value.exceptions[0], ChildProcessError)
        assert not (tmp_path / 'watch.stderr.log').exists()


@pytest.mark.asyncio
async def test_route_writer_delays_delivery_without_starting_a_second_child(tmp_path, monkeypatch):
    from agent_comms.bus_route_counts import BusRouteCounts
    from agent_comms.store_files import _store_lock
    from test_bus_route_counts_locking import _case

    comms = _case(tmp_path / 'wire', monkeypatch)
    comms.messaging.send('sender', 'recipient', 'second').message
    comms.messaging.send('sender', 'recipient', 'third').message
    ready_r, ready_w = os.pipe()
    blocked = asyncio.Event()
    delivered = asyncio.Event()
    starts, children, messages = [], [], []
    start = AttachedChild.start
    # Run the actual CLI with a small rebuild bound and a pipe observing its
    # blocked state. The pipe is not part of the watch protocol.
    driver = '''
import os, sys
from agent_comms.bus_route_counts import BusRouteCounts, RouteTable
from agent_comms.child_process import PosixPlatform
from agent_comms.cli import main
BusRouteCounts.rebuild_bytes = 1
original = PosixPlatform.acquire_store_lock
def observe(self, descriptor, *, mode, blocking):
    if blocking and os.readlink(f'/proc/self/fd/{descriptor}').endswith(
        f'.bus_route_counts.v{RouteTable.schema}.sqlite3.lock'
    ):
        os.write(int(sys.argv[1]), b'B')
    return original(self, descriptor, mode=mode, blocking=blocking)
PosixPlatform.acquire_store_lock = observe
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
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(delivered.wait(), 0.1)
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


PROBE = ("from cutover.carry import Carried, Carry\n\n\n"
         "class Probe(Carry):\n    after = None\n\n"
         "    @classmethod\n    def carry(cls, root, *, apply):\n"
         "        (root / 'carried').write_text('carried')\n        return Carried(1, {}, ('carried',))\n\n\n"
         "if __name__ == '__main__':\n    Probe.main()\n")


@pytest.mark.asyncio
async def test_a_watch_pauses_for_an_upgrade_and_resumes_after_its_carry(tmp_path, monkeypatch):
    """The upgrade declares maintenance; the Core watch closes, the carry runs, and once
    maintenance clears the bridge's next child receives a message sent after it."""
    from cutover.carry import STEPS, CarryChain, CarryLedger
    from test_bus_route_counts_locking import _case
    from test_carry import REPO, CheckoutStack, Repository
    from tools.upgrade_bus import Revisions, carry_all
    from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator

    comms = _case(tmp_path / 'wire', monkeypatch)
    arrived: asyncio.Queue[str] = asyncio.Queue()
    children = []
    start = AttachedChild.start
    driver = 'import sys\nfrom agent_comms.cli import main\nraise SystemExit(main(sys.argv[1:]))\n'

    async def launch(command, **options):
        children.append(await start((sys.executable, '-c', driver, *command[1:]), **options))
        return children[-1]

    class ReceivingBridge(ReportingBridge):
        async def deliver(self, message):
            await arrived.put(message.body)
            return DeliveredOutcome

    monkeypatch.setattr(AttachedChild, 'start', launch)
    state = tmp_path / 'bridge'
    with Binding.open(state) as db:
        bridge = ReceivingBridge(Options(WatchOnly(), None, state),
                                 Binding(str(comms.root), 'recipient', 'session'), db)
        bridge.failures = []
        task = asyncio.create_task(bridge.run())
        try:
            assert await asyncio.wait_for(arrived.get(), 20) == 'first'
            maintenance = MaintenanceBarrier(comms.registry.store.path)
            held = maintenance.begin(MaintenanceOperator('upgrade-bus'))
            assert await asyncio.wait_for(children[0].wait(), 20) is not None
            (tmp_path / 'checkout/src').mkdir(parents=True)
            repo = Repository(tmp_path / 'checkout')
            head = repo.commit({'carry': (REPO / STEPS / 'carry.py').read_text(), 'carry_probe': PROBE})
            revisions = Revisions(repo.path, tmp_path / 'trees')
            try:
                carry_all(comms.root, CarryChain.at(repo.path, head).pending(()),
                          CheckoutStack(repo.path, home=tmp_path), revisions)
            finally:
                revisions.close()
            assert (comms.root / 'carried').read_text() == 'carried'
            assert CarryLedger(comms.root).applied() == ('carry_probe',)
            assert len(children) == 1 and not task.done()
            maintenance.clear(held)
            comms.messaging.send('sender', 'recipient', 'after').message
            assert await asyncio.wait_for(arrived.get(), 20) == 'after'
            assert len(children) == 2 and bridge.failures == []
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_a_bridge_started_during_a_drain_waits_and_then_receives(tmp_path, monkeypatch):
    """Registration is held during an upgrade's drain; the bridge waits for it to clear and is
    not refused."""
    from test_bus_route_counts_locking import _case
    from agent_comms.cli_bridges.shared import Inbox
    from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
    from agent_comms.thread_execution import ExternalThreadExecution

    comms = _case(tmp_path / 'wire', monkeypatch)
    recipient = comms.registry.snapshot().threads['recipient']
    comms.registry.declare(replace(recipient, execution=ExternalThreadExecution))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    maintenance = MaintenanceBarrier(comms.registry.store.path)
    held = maintenance.begin(MaintenanceOperator('upgrade-bus'))
    arrived: asyncio.Queue[str] = asyncio.Queue()
    children = []
    start = AttachedChild.start
    driver = 'import sys\nfrom agent_comms.cli import main\nraise SystemExit(main(sys.argv[1:]))\n'

    async def launch(command, **options):
        children.append(await start((sys.executable, '-c', driver, *command[1:]), **options))
        return children[-1]

    class ReceivingBridge(ReportingBridge):
        async def deliver(self, message):
            await arrived.put(message.body)
            return DeliveredOutcome

    monkeypatch.setattr(AttachedChild, 'start', launch)
    state = tmp_path / 'bridge'
    with Binding.open(state) as db:
        bridge = ReceivingBridge(Options(Inbox(), None, state),
                                 Binding(str(comms.root), 'recipient', 'session'), db)
        bridge.failures = []
        task = asyncio.create_task(bridge.run())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 1)
            assert children == [] and bridge.failures == []
            maintenance.clear(held)
            assert await asyncio.wait_for(arrived.get(), 20) == 'first'
            assert len(children) == 1 and bridge.failures == []
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
