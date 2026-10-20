"""Core's error declaration controls encoding and read-watch recovery."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import closing

import pytest
from pydantic import ValidationError

from agent_comms.bus_route_counts import RouteIndexRebuilding
from agent_comms.cli import _fail, main
from agent_comms.cli_bridges.shared import Binding, DeliveryRow
from agent_comms.cli_protocol import (
    WATCH_LINE, FailedCoreError, UnavailableCoreError, UnconfirmedOutcome, DeliveredOutcome,
)
from agent_comms.coordination_database import CoordinationStore
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
    decoded = WATCH_LINE.validate_json(output)
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
    decoded = WATCH_LINE.validate_json(capsys.readouterr().out)
    assert isinstance(decoded, UnavailableCoreError)
    assert 'ValueError: bad\ninput' in decoded.error


def test_unknown_error_kind_is_not_a_retry():
    with pytest.raises(ValidationError):
        WATCH_LINE.validate_python({'type': 'new-unknown', 'error': 'database is locked'})


@pytest.mark.parametrize('arguments', [[], ['not-a-command'], ['watch']])
def test_argument_errors_use_the_same_single_line(arguments, capsys):
    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert isinstance(WATCH_LINE.validate_json(output), FailedCoreError)


@pytest.mark.asyncio
async def test_journal_concurrent_initialization_and_reopen_preserve_rows(tmp_path):
    worker = r'''
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch
from agent_comms.cli_bridges.shared import Binding

connect = sqlite3.connect
class PausedConnection(sqlite3.Connection):
    first_create = True
    def execute(self, statement, *args, **kwargs):
        if self.first_create and statement.startswith('CREATE TABLE '):
            self.first_create = False
            print('ready', flush=True)
            assert sys.stdin.readline() == 'go\n'
        return super().execute(statement, *args, **kwargs)

with patch('sqlite3.connect', side_effect=lambda *a, **k: connect(*a, factory=PausedConnection, **k)):
    with Binding.open(Path(sys.argv[1])):
        pass
print('opened', flush=True)
'''

    async def open_together():
        children = []
        try:
            for _ in range(2):
                children.append(await asyncio.create_subprocess_exec(
                    sys.executable, '-c', worker, str(tmp_path),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE))
            ready = await asyncio.wait_for(asyncio.gather(
                *(child.stdout.readline() for child in children)), 20)
            assert ready == [b'ready\n', b'ready\n']
            results = await asyncio.wait_for(asyncio.gather(
                *(child.communicate(b'go\n') for child in children)), 20)
            assert [(child.returncode, out, err) for child, (out, err)
                    in zip(children, results)] == [(0, b'opened\n', b'')] * 2
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                await child.wait()

    await open_together()
    binding = Binding(str(tmp_path), 'viewer', 'external-session')
    message = Message(sender='peer', target='viewer', type=InfoKind,
                      body='retained delivery', timestamp=1.0, seq=1)
    pending = DeliveryRow(message.message_id, json.dumps(message.to_wire()), UnconfirmedOutcome)
    with Binding.open(tmp_path) as db:
        assert Binding.bind(db, binding) == binding
        with db:
            assert pending.insert_if_absent(db).rowcount
    await open_together()
    with Binding.open(tmp_path) as db:
        assert Binding.bind(db, None) == binding
        assert DeliveryRow.select(db) == [pending]


@pytest.mark.parametrize('conflicting', [False, True])
def test_concurrent_initial_binding_preserves_one_identity_and_pending_delivery(tmp_path, conflicting):
    from journal_binding_cases import concurrent_bindings

    first = Binding(str(tmp_path), 'viewer', 'external-session')
    other = Binding(str(tmp_path), 'other-viewer', 'external-session')
    message = Message(sender='peer', target='viewer', type=InfoKind,
                      body='retained delivery', timestamp=1.0, seq=1)
    pending = DeliveryRow(message.message_id, json.dumps(message.to_wire()), UnconfirmedOutcome)
    with Binding.open(tmp_path) as db, db:
        pending.insert(db)
    results = concurrent_bindings(tmp_path, (first, other if conflicting else first))
    assert sum(kind == 'bound' for kind, _ in results) == (1 if conflicting else 2)
    assert sum(kind == 'rejected' for kind, _ in results) == int(conflicting)
    with Binding.open(tmp_path) as db:
        winner, = Binding.select(db)
        assert winner in (first, other)
        assert Binding.bind(db, None) == Binding.bind(db, winner) == winner
        loser = other if winner == first else first
        with pytest.raises(ValueError, match='another Core/CLI conversation'):
            Binding.bind(db, loser)
        with db:
            assert not DeliveryRow(message.message_id, 'unused', DeliveredOutcome).insert_if_absent(db).rowcount
        assert DeliveryRow.select(db) == [pending]
        assert Binding.select(db) == [winner]


def test_existing_binding_lookup_does_not_acquire_a_writer(tmp_path):
    binding = Binding(str(tmp_path), 'viewer', 'external-session')
    with Binding.open(tmp_path) as db:
        assert Binding.bind(db, binding) == binding
        statements = []
        db.set_trace_callback(statements.append)
        with closing(CoordinationStore.writer(tmp_path / 'delivery.sqlite3')) as writer:
            writer.execute('BEGIN IMMEDIATE')
            assert Binding.bind(db, None) == binding
            assert writer.in_transaction
        assert not any(sql.startswith(('BEGIN', 'COMMIT', 'ROLLBACK')) for sql in statements)


