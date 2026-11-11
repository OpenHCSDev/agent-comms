"""Socket and delivery owners exercised by the real Claude probe."""
import asyncio
import socket
from datetime import datetime
from pathlib import Path

import pytest

from agent_comms.cli_bridges.monitor import AttachMonitor, InspectSession, SessionReady
from agent_comms.cli_bridges.session_watch import (
    SessionCommandError,
    SessionConnectionClosed,
    SessionRequestTimeout,
    WatchClaim,
)
from agent_comms.cli_bridges.shared import Binding, BridgeLog, GivenBinding
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms_hooks.watch import WatchRecord


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
@pytest.mark.parametrize('wire_reply', (
    b'',
    (FieldCodec.compact(FieldCodec.encode(SessionReady())) + '\n').encode(),
))
async def test_monitor_startup_eof_is_loud_before_reply_or_probe(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire_reply: bytes) -> None:
    # A real endpoint is short (WatchClaim.socket_path); a pytest temp directory need not be.
    monkeypatch.chdir(tmp_path)
    endpoint = Path('startup.sock')

    async def close_at_startup(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(wire_reply)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(close_at_startup, path=endpoint)
    async with server:
        with pytest.raises(SessionConnectionClosed):
            await anext(AttachMonitor().lines(endpoint))


def test_reply_read_translates_timeout_before_the_borrowed_connection_exits():
    reader, writer = socket.socketpair()
    with reader, writer, reader.makefile('rb') as stream:
        reader.settimeout(.025)
        with pytest.raises(SessionRequestTimeout):
            InspectSession().read_reply(stream)


def test_enrol_is_idempotent_and_refuses_another_command(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    claim = WatchClaim('session')
    record = WatchRecord('viewer', str(tmp_path), ('bridge', 'watch'), tmp_path)
    claim.enrol(record)
    assert claim.state().record == record
    with claim.held(record):
        claim.enrol(record)
        with pytest.raises(SessionCommandError, match='bridge watch.*Stop that'):
            claim.enrol(WatchRecord('viewer', str(tmp_path), ('other', 'watch'), tmp_path))
        assert not claim.has_reader()
        with claim.reader_held():
            assert claim.has_reader()


def test_a_first_binding_without_a_root_is_refused_by_name(tmp_path):
    with Binding.open(tmp_path) as db:
        with pytest.raises(Exception, match="root") as refused:
            GivenBinding({"bus": "unit-a"}).bind(db)
    assert not isinstance(refused.value, KeyError)
