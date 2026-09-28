"""Lossless one-way catalog migration and one atomic publication boundary."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from agent_comms.catalog_store import ChannelCatalog
from agent_comms.comms import wire
from agent_comms.display_order import ChannelSort
from agent_comms.messages import Message, MessageType


def test_update_failure_restores_whole_document_not_just_mode(tmp_path):
    comms = wire(tmp_path)
    comms.channels.create_tag("api")
    comms.channels.set_channel_pinned("api", True)
    before = comms.channels.catalog.path.read_bytes()
    with patch.object(ChannelCatalog, "_write_unlocked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            comms.channels.set_channel_any_mode("api", True)
    assert comms.channels.catalog.path.read_bytes() == before
    assert wire(tmp_path).channels.catalog.read().resolve("#api").pinned
    assert not comms.channels.catalog.read().resolve("#api").any_mode


def test_independent_writers_merge_under_wire_then_document_lock(tmp_path):
    def create(index):
        client = wire(tmp_path)
        name = f"tag-{index}"
        client.channels.create_tag(name)
        client.channels.set_channel_pinned(name, True)
        client.channels.set_channel_any_mode(name, True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(create, range(8)))
    document = wire(tmp_path).channels.catalog.read()
    assert document.tags == {f"tag-{i}" for i in range(8)}
    assert all(
        document.resolve(f"#tag-{i}").pinned and document.resolve(f"#tag-{i}").any_mode
        for i in range(8)
    )


def test_message_decode_fields_are_declared_not_a_second_roster():
    @dataclass(frozen=True, slots=True)
    class AnnotatedMessage(Message):
        annotation: str = ""

    original = AnnotatedMessage("alpha", "#api", "hello", MessageType.INFO, annotation="retained")
    assert AnnotatedMessage.from_wire(original.to_wire()) == original
    with pytest.raises(ValueError, match="Unknown fields"):
        Message.from_wire({**original.to_wire(), "extension": "not supported"})
    with pytest.raises(ValueError):
        Message.from_wire(
            {**Message("alpha", "#api", "hello", MessageType.INFO).to_wire(), "notice": "false"}
        )
    historical = Message.from_wire({"from": "alpha", "to": "#api", "text": "old", "type": "info"})
    assert historical.timestamp == 0.0
    assert historical.display_metadata == {}


def test_initial_publication_rechecks_current_catalog_revision(tmp_path, monkeypatch):
    from agent_comms import audience_manifest
    from agent_comms.errors import RelationViolationError
    from test_private_human_ingress import _root

    comms, _store, _root_id, _lookups = _root(tmp_path)
    comms.channels.create_tag("team")
    sequence = comms.bus.log.latest_sequence()
    original = audience_manifest.freeze_audience

    def capture_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        # A noncooperating writer changes the source during admission. The
        # already-held wire lock is not itself evidence of unchanged bytes.
        with comms.channels.catalog.editing() as document:
            document.list_order = ChannelSort.CREATED
        return result

    monkeypatch.setattr(audience_manifest, "freeze_audience", capture_then_change)
    with pytest.raises(RelationViolationError, match="registry/catalog revision changed"):
        comms.messaging.send_user_message("#team", "must not append", worktree=str(tmp_path))
    assert comms.bus.log.latest_sequence() == sequence


def test_message_integer_timestamp_keeps_original_identity():
    original = Message("alpha", "#api", "old", MessageType.INFO, timestamp=123, seq=7)
    encoded = original.to_wire()
    restored = Message.from_wire(encoded)
    assert restored.to_wire() == encoded
    assert type(restored.timestamp) is int
    assert restored.message_id == original.message_id
