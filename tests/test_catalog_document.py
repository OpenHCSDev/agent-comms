"""Lossless one-way catalog migration and one atomic publication boundary."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from agent_comms.bus import Bus
from agent_comms.comms import wire
from agent_comms.messages import Message, InfoKind


def test_independent_writers_merge_under_wire_then_document_lock(tmp_path):
    Bus.install(Bus.path_in(tmp_path))

    def create(index):
        client = wire(tmp_path)
        name = f"tag-{index}"
        client.channels.create_tag(name)
        client.channels.set_channel_pinned(name, True)
        client.channels.set_channel_any_mode(name, True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(create, range(8)))
    comms = wire(tmp_path)
    document = comms.channels.catalog.read()
    with comms.bus.opened_activity() as activity:
        pass
    assert document.tags == {f"tag-{i}" for i in range(8)}
    assert all(
        document.require_channel(f"#tag-{i}", activity).pinned and document.require_channel(f"#tag-{i}", activity).any_mode
        for i in range(8)
    )


def test_message_decode_fields_are_declared_not_a_second_roster():
    @dataclass(frozen=True, slots=True)
    class AnnotatedMessage(Message):
        annotation: str = ""

    original = AnnotatedMessage("alpha", "#api", "hello", InfoKind, annotation="retained")
    assert AnnotatedMessage.from_wire(original.to_wire()) == original
    with pytest.raises(ValueError):
        Message.from_wire({**original.to_wire(), "extension": "not supported"})
    with pytest.raises(ValueError):
        Message.from_wire(
            {**Message("alpha", "#api", "hello", InfoKind).to_wire(), "notice": "false"}
        )
    historical = Message.from_wire({"from": "alpha", "to": "#api", "text": "old", "type": "info"})
    assert historical.timestamp == 0.0
    assert historical.display_metadata == {}


def test_message_integer_timestamp_keeps_original_identity():
    original = Message("alpha", "#api", "old", InfoKind, timestamp=123, seq=7)
    encoded = original.to_wire()
    restored = Message.from_wire(encoded)
    assert restored.to_wire() == encoded
    assert type(restored.timestamp) is int
    assert restored.message_id == original.message_id
