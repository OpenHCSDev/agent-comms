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
from agent_comms.cli_bridges.shared import (
    Binding,
    DeliveryJournal,
    DeliveryRow,
    MonitorAttemptRow,
    Inbox,
    CliBridge,
    Options,
    UnconfirmedOutcome, DeliveredOutcome,
)
from agent_comms.cli_protocol import CORE_ERROR, FailedCoreError, UnavailableCoreError
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
async def test_journal_concurrent_initialization_and_reopen_preserve_rows(tmp_path):
    # Pause both real processes immediately before their first table CREATE.
    # The old present()/create() implementation then deterministically races:
    # both have observed absence before either is allowed to create the table.
    worker = r'''
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch
from agent_comms.cli_bridges.shared import DeliveryJournal

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
    journal = DeliveryJournal(Path(sys.argv[1]))
    journal.close()
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
    journal = DeliveryJournal(tmp_path)
    try:
        assert journal.bind(binding) == binding
        assert journal.begin(message, 'original payload', UnconfirmedOutcome)
    finally:
        journal.close()
    await open_together()
    journal = DeliveryJournal(tmp_path)
    try:
        assert journal.bind(None) == binding
        assert DeliveryRow.select(journal.db) == [
            DeliveryRow(message.message_id, 'original payload', UnconfirmedOutcome)]
    finally:
        journal.close()


@pytest.mark.parametrize('conflicting', [False, True])
def test_concurrent_initial_binding_preserves_one_identity_and_pending_delivery(tmp_path, conflicting):
    from journal_binding_cases import concurrent_bindings

    first = Binding(str(tmp_path), 'viewer', 'external-session')
    other = Binding(str(tmp_path), 'other-viewer', 'external-session')
    message = Message(sender='peer', target='viewer', type=InfoKind,
                      body='retained delivery', timestamp=1.0, seq=1)
    journal = DeliveryJournal(tmp_path)
    try:
        assert journal.begin(message, 'original payload', UnconfirmedOutcome)
    finally:
        journal.close()
    results = concurrent_bindings(tmp_path, (first, other if conflicting else first))
    assert sum(kind == 'bound' for kind, _ in results) == (1 if conflicting else 2)
    assert sum(kind == 'rejected' for kind, _ in results) == int(conflicting)
    journal = DeliveryJournal(tmp_path)
    try:
        rows = Binding.select(journal.db)
        assert len(rows) == 1
        winner, = rows
        assert winner in (first, other)
        assert journal.bind(None) == journal.bind(winner) == winner
        loser = other if winner == first else first
        with pytest.raises(ValueError, match='another Core/CLI conversation'):
            journal.bind(loser)
        assert not journal.begin(message, 'must not replace', DeliveredOutcome)
        assert DeliveryRow.select(journal.db) == [
            DeliveryRow(message.message_id, 'original payload', UnconfirmedOutcome)]
        assert Binding.select(journal.db) == [winner]
    finally:
        journal.close()


def test_existing_binding_lookup_does_not_acquire_a_writer(tmp_path):
    binding = Binding(str(tmp_path), 'viewer', 'external-session')
    journal = DeliveryJournal(tmp_path)
    try:
        assert journal.bind(binding) == binding
        statements = []
        journal.db.set_trace_callback(statements.append)
        with closing(CoordinationStore.writer(journal.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            assert journal.bind(None) == binding
            assert writer.in_transaction
        assert not any(sql.startswith(('BEGIN', 'COMMIT', 'ROLLBACK')) for sql in statements)
    finally:
        journal.close()


def test_legacy_journal_reopen_preserves_schema_binding_and_delivery(tmp_path):
    binding = Binding(str(tmp_path), 'viewer', 'external-session')
    pending = DeliveryRow('pending-id', 'original payload', UnconfirmedOutcome)
    with closing(CoordinationStore.writer(tmp_path / 'delivery.sqlite3')) as db:
        db.execute('CREATE TABLE binding (root TEXT, bus TEXT, thread TEXT)')
        db.execute('CREATE TABLE delivery (id TEXT PRIMARY KEY, payload TEXT NOT NULL, outcome TEXT NOT NULL)')
        binding.insert(db)
        pending.insert(db)
        db.commit()
        schema = db.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall()
    journal = DeliveryJournal(tmp_path)
    try:
        assert journal.bind(binding) == journal.bind(None) == binding
        assert DeliveryRow.select(journal.db) == [pending]
        current = dict(journal.db.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall())
        assert {name: current[name] for name, _ in schema} == dict(schema)
        assert MonitorAttemptRow.select(journal.db) == []
    finally:
        journal.close()


def test_journal_initialization_failure_closes_owned_connection(tmp_path, monkeypatch):
    path = tmp_path / 'delivery.sqlite3'
    path.write_bytes(b'not a SQLite database')
    connection = CoordinationStore.writer(path)
    monkeypatch.setattr(CoordinationStore, 'writer', lambda *_args, **_options: connection)
    with pytest.raises(sqlite3.DatabaseError):
        DeliveryJournal(tmp_path)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')


class AcceptedBridge(CliBridge):
    """Controlled API acknowledgment; these checks concern Core read recovery."""

    async def deliver(self, message):
        for part in self.incoming_frame(message).parts:
            print(part)
        return DeliveredOutcome

    async def show_sent(self, result, target, body):
        pass

    async def report_failure(self, text):
        pass


@pytest.mark.asyncio
async def test_watch_resubscribes_only_for_declared_unavailable(tmp_path, capsys):
    journal = DeliveryJournal(tmp_path)
    binding = Binding(str(tmp_path), 'viewer', 'external-session')
    bridge = AcceptedBridge(Options(Inbox(), None, None, None, tmp_path), binding, journal)
    message = Message(sender='peer', target='viewer', type=InfoKind,
                      body='one delivery', timestamp=1.0, seq=1)
    delivered = asyncio.get_running_loop().create_future()
    subscriptions = []
    closed = []
    waiting = asyncio.Event()

    class ReadStream:
        async def acknowledge(self, message):
            assert message.body == 'one delivery'

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
        assert journal.db.execute('SELECT outcome FROM delivery').fetchone()[0] == DeliveredOutcome.declared_name
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
    bridge = AcceptedBridge(Options(Inbox(), None, None, None, tmp_path),
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
