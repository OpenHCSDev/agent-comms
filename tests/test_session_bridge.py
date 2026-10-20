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


def test_enrol_is_idempotent_and_refuses_another_command(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    claim = WatchClaim('session')
    record = WatchRecord('viewer', str(tmp_path), ('bridge', 'watch'))
    claim.enrol(record)
    assert claim.state().record == record
    with claim.held(record):
        claim.enrol(record)
        with pytest.raises(ValueError):
            claim.enrol(WatchRecord('viewer', str(tmp_path), ('other', 'watch')))
        assert not claim.has_reader()
        with claim.reader_held():
            assert claim.has_reader()
