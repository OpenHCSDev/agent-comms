"""One Core watch declaration serves both JSON ingress and typed construction."""
import json

import pytest
from pydantic import ValidationError

from agent_comms.cli_protocol import (
    CORE_ERROR, WATCH_LINE, ArrivedMessage, FailedCoreError, UnavailableCoreError,
)
from agent_comms.messages import InfoKind, Message


MESSAGE = Message(sender='peer', target='viewer', body='saved\nmessage',
                  type=InfoKind, timestamp=0.0, seq=9007199254740993)


@pytest.mark.parametrize('record', [
    ArrivedMessage(message=MESSAGE),
    UnavailableCoreError(error='temporary projection contention'),
    FailedCoreError(error='permanent read failure'),
])
def test_watch_members_roundtrip(record):
    decoded = WATCH_LINE.validate_json(record.model_dump_json())
    assert type(decoded) is type(record)
    assert decoded == record


def test_message_keeps_existing_journal_wire():
    arrived = WATCH_LINE.validate_json(ArrivedMessage(message=MESSAGE).model_dump_json())
    assert Message.from_wire(json.loads(arrived.payload)) == MESSAGE
    assert arrived.message.seq == 9007199254740993
    with pytest.raises(ValidationError):
        CORE_ERROR.validate_json(arrived.model_dump_json())


@pytest.mark.parametrize('wire', [
    json.dumps(MESSAGE.to_wire()),
    '{"type":"new-unknown","error":"not a declared record"}',
    '{"type":"message"}',
])
def test_watch_rejects_undeclared_or_incomplete_lines(wire):
    with pytest.raises(ValidationError):
        WATCH_LINE.validate_json(wire)
