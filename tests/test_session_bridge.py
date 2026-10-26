"""Socket and delivery owners exercised by the real Claude probe."""
import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from agent_comms.cli_bridges.monitor import AttachMonitor, SessionReady
from agent_comms.cli_bridges.shared import BridgeLog
from agent_comms.field_codec import FieldCodec
from agent_comms_hooks.watch import SessionConnectionClosed, WatchClaim, WatchRecord


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
        tmp_path: Path, wire_reply: bytes) -> None:
    endpoint = tmp_path / 'startup.sock'

    async def close_at_startup(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(wire_reply)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(close_at_startup, str(endpoint))
    async with server:
        with pytest.raises(SessionConnectionClosed):
            await anext(AttachMonitor().lines(endpoint))


def test_enrol_is_idempotent_and_refuses_another_command(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    claim = WatchClaim('session')
    record = WatchRecord('viewer', str(tmp_path), ('bridge', 'watch'), tmp_path)
    claim.enrol(record)
    assert claim.state().record == record
    with claim.held(record):
        claim.enrol(record)
        with pytest.raises(ValueError):
            claim.enrol(WatchRecord('viewer', str(tmp_path), ('other', 'watch'), tmp_path))
        assert not claim.has_reader()
        with claim.reader_held():
            assert claim.has_reader()
