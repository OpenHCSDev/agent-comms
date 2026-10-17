"""Watch terminal records and real child exits never start another subscription."""
import sys

import pytest

from agent_comms.child_process import AttachedChild
from agent_comms.cli_bridges.shared import Binding, CliBridge, Operation, Options
from agent_comms.cli_protocol import FailedCoreError, UnavailableCoreError


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
    (UnavailableCoreError(error='read snapshot unavailable').model_dump_json(), 1),
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
