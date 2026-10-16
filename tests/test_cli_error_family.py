"""Core's error declaration controls encoding and read-watch recovery."""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from agent_comms.bus_route_counts import RouteIndexRebuilding
from agent_comms.cli import _fail, main
from agent_comms.cli_bridges.shared import (
    Binding,
    DeliveryJournal,
    Inbox,
    LineMonitorBridge,
    Options,
    Outcome,
)
from agent_comms.cli_protocol import CORE_ERROR, FailedCoreError, UnavailableCoreError
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.messages import InfoKind, Message


@pytest.mark.parametrize('error,expected', [
    (CoordinationReadUnavailable('wording does not decide recovery'), UnavailableCoreError),
    (RouteIndexRebuilding(12, 4096), UnavailableCoreError),
    (RuntimeError('database is locked'), FailedCoreError),
    (ValueError('first\nsecond'), FailedCoreError),
])
def test_exception_declaration_encodes_one_line(error, expected, capsys):
    assert _fail(error) == 1
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    decoded = CORE_ERROR.validate_json(output)
    assert type(decoded) is expected
    assert decoded.error == str(error)
    assert json.loads(output)['type'] == expected.wire_tag


def test_error_keeps_cause_without_using_cause_text_as_policy(capsys):
    try:
        raise ValueError('bad\ninput')
    except ValueError as cause:
        error = CoordinationReadUnavailable('not ready')
        error.__cause__ = cause
    _fail(error)
    decoded = CORE_ERROR.validate_json(capsys.readouterr().out)
    assert isinstance(decoded, UnavailableCoreError)
    assert 'ValueError: bad\ninput' in decoded.error


def test_unknown_error_kind_is_not_a_retry():
    with pytest.raises(ValidationError):
        CORE_ERROR.validate_python({'type': 'new-unknown', 'error': 'database is locked'})
    with pytest.raises(RuntimeError, match='Core send'):
        UnavailableCoreError(error='read unavailable').raise_failure('send')


@pytest.mark.parametrize('arguments', [[], ['not-a-command'], ['watch']])
def test_argument_errors_use_the_same_single_line(arguments, capsys):
    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert isinstance(CORE_ERROR.validate_json(output), FailedCoreError)


@pytest.mark.asyncio
async def test_watch_resubscribes_only_for_declared_unavailable(tmp_path, capsys):
    journal = DeliveryJournal(tmp_path)
    binding = Binding(str(tmp_path), 'viewer', 'external-session')
    bridge = LineMonitorBridge(Options(Inbox(), None, None, None, tmp_path), binding, journal)
    message = Message(sender='peer', target='viewer', type=InfoKind,
                      body='one delivery', timestamp=1.0, seq=1)
    delivered = asyncio.get_running_loop().create_future()
    subscriptions = []
    closed = []
    waiting = asyncio.Event()

    class ReadStream:
        async def watch(self, state):
            subscriptions.append(state)
            try:
                if len(subscriptions) == 1:
                    yield message, json.dumps(message.to_wire())
                    yield UnavailableCoreError(error='Route index covers 1 of 100 bytes')
                else:
                    yield message, json.dumps(message.to_wire())
                    delivered.set_result(True)
                    await waiting.wait()
            finally:
                closed.append(state)

    bridge.core = ReadStream()
    task = asyncio.create_task(bridge.watch())
    try:
        await asyncio.wait_for(delivered, 3)
        assert len(subscriptions) == 2
        assert journal.db.execute('SELECT outcome FROM delivery').fetchone()[0] == Outcome.EMITTED.value
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        journal.close()
    assert len(closed) == 2
    assert capsys.readouterr().out.count('one delivery') == 1


@pytest.mark.asyncio
async def test_watch_failed_error_raises_without_resubscribing(tmp_path):
    journal = DeliveryJournal(tmp_path)
    bridge = LineMonitorBridge(Options(Inbox(), None, None, None, tmp_path),
                              Binding(str(tmp_path), 'viewer', 'external-session'), journal)
    subscriptions = []

    class ReadStream:
        async def watch(self, state):
            subscriptions.append(state)
            yield FailedCoreError(error='database is locked')

    bridge.core = ReadStream()
    try:
        with pytest.raises(RuntimeError, match='Core watch: database is locked'):
            await bridge.watch()
        assert len(subscriptions) == 1
        assert journal.db.execute('SELECT count(*) FROM delivery').fetchone()[0] == 0
    finally:
        journal.close()
