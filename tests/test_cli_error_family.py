"""Core's error declaration controls encoding and read-watch recovery."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import closing

import pytest
from pydantic import ValidationError

from agent_comms.cli import _fail, main
from agent_comms.cli_bridges.shared import Binding, DeliveryRow, GivenBinding, StoredBinding
from agent_comms.cli_protocol import (
    WATCH_LINE,
    DeliveredOutcome,
    FailedCoreError,
    UnavailableCoreError,
    UnconfirmedOutcome,
)
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.field_codec import RecordProblems
from agent_comms.locked_store import SQLiteFile
from agent_comms.messages import InfoKind, Message
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread


@pytest.mark.parametrize('error,expected', [
    (CoordinationReadUnavailable('wording does not decide recovery'), UnavailableCoreError),
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
    assert json.loads(output)['type'] == expected.declared_name


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
async def test_journal_concurrent_initialization_and_reopen_preserve_rows(tmp_path, bus):
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

    async def open_together(*, fresh):
        children = []
        try:
            for _ in range(2):
                children.append(await asyncio.create_subprocess_exec(
                    sys.executable, '-c', worker, str(tmp_path),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE))
                if fresh and len(children) == 1:
                    # The first opener holds its write transaction at CREATE TABLE
                    # while the second opens; the second waits, then finds the schema.
                    ready = await asyncio.wait_for(children[0].stdout.readline(), 20)
                    assert ready == b'ready\n'
            results = await asyncio.wait_for(asyncio.gather(
                *(child.communicate(b'go\n') for child in children)), 20)
            assert [(child.returncode, out, err) for child, (out, err)
                    in zip(children, results)] == [(0, b'opened\n', b'')] * 2
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                await child.wait()

    await open_together(fresh=True)
    binding = Binding(root=str(tmp_path), owner=RecordedThreadIncarnation('viewer', 1.0),
                      thread='external-session', start=Binding.bus_head(str(tmp_path)))
    message = Message(sender='peer', target='viewer', type=InfoKind,
                      body='retained delivery', timestamp=1.0, seq=1)
    pending = DeliveryRow(message.message_id, json.dumps(message.to_wire()), UnconfirmedOutcome)
    with Binding.open(tmp_path) as db:
        assert binding.bind(db) == binding
        with db:
            assert pending.insert_if_absent(db).rowcount
    await open_together(fresh=False)
    with Binding.open(tmp_path) as db:
        assert StoredBinding().bind(db) == binding
        assert DeliveryRow.select(db) == [pending]


@pytest.mark.parametrize('conflicting', [False, True])
def test_concurrent_initial_binding_preserves_one_identity_and_pending_delivery(
        tmp_path, conflicting, bus):
    from journal_binding_cases import concurrent_bindings

    first = Binding(root=str(tmp_path), owner=RecordedThreadIncarnation('viewer', 1.0),
                    thread='external-session', start=Binding.bus_head(str(tmp_path)))
    other = Binding(root=str(tmp_path), owner=RecordedThreadIncarnation('other-viewer', 2.0),
                    thread='external-session', start=Binding.bus_head(str(tmp_path)))
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
        assert StoredBinding().bind(db) == winner.bind(db) == winner
        loser = other if winner == first else first
        with pytest.raises(ValueError):
            loser.bind(db)
        with db:
            assert not DeliveryRow(message.message_id, json.dumps(message.to_wire()), DeliveredOutcome).insert_if_absent(db).rowcount
        assert DeliveryRow.select(db) == [pending]
        assert Binding.select(db) == [winner]


def test_existing_binding_lookup_does_not_acquire_a_writer(tmp_path, bus):
    binding = Binding(root=str(tmp_path), owner=RecordedThreadIncarnation('viewer', 1.0),
                      thread='external-session', start=Binding.bus_head(str(tmp_path)))
    with Binding.open(tmp_path) as db:
        assert binding.bind(db) == binding
        statements = []
        db.set_trace_callback(statements.append)
        with closing(SQLiteFile.writer(tmp_path / 'delivery.sqlite3')) as writer:
            writer.execute('BEGIN IMMEDIATE')
            assert StoredBinding().bind(db) == binding
            assert writer.in_transaction
        assert not any(sql.startswith(('BEGIN', 'COMMIT', 'ROLLBACK')) for sql in statements)




@pytest.mark.asyncio
async def test_watched_record_crosses_the_watch_line_as_its_envelope(root):
    """The bridge's watch child writes what bus.watch yields; the bridge decodes that same line."""
    from contextlib import aclosing

    from goal_owner_fixture import canonical_goal_wire

    from agent_comms.cli_protocol import ArrivedMessage
    from agent_comms.read_basis import ExplicitWatchAcknowledgment
    from agent_comms.threads import Thread

    comms = canonical_goal_wire(root)
    for name in ("a", "b"):
        comms.registry.declare(Thread(name=name, tags=frozenset(), worktree='/wt'))
    sent = comms.messaging.send("a", "b", "crosses").message
    async with aclosing(comms.watch("b", acknowledgment=ExplicitWatchAcknowledgment)) as watched:
        async with asyncio.timeout(5):
            record = await anext(watched)
    line = WATCH_LINE.validate_json(ArrivedMessage(message=record).model_dump_json())
    assert isinstance(line, ArrivedMessage)
    assert line.message.to_wire() == sent.to_wire() == record.to_wire()


def test_given_binding_fields_override_the_stored_binding(tmp_path, comms):
    """A partial binding takes each missing field from the stored one; a field absent from
    both is refused by name, and a changed field still claims another conversation."""
    for name in ('viewer', 'other-viewer'):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    type_key = {Binding.family_discriminator: Binding.declared_name}
    with Binding.open(tmp_path) as db:
        with pytest.raises(RecordProblems, match='bus_thread'):
            GivenBinding({**type_key, 'root': str(comms.root)}).bind(db)
        stored = Binding(
            root=str(comms.root), owner=comms.registry.require('viewer').incarnation,
            thread='external-session', start=Binding.bus_head(str(comms.root)),
        ).bind(db)
        address = GivenBinding({**type_key, 'root': str(comms.root)}).bind(db)
        assert address == stored.address()
        assert address.registered(comms).bind(db) == stored
        other = GivenBinding({**type_key, 'bus_thread': 'other-viewer'}).bind(db)
        with pytest.raises(ValueError, match='another Core/CLI conversation'):
            other.registered(comms).bind(db)
