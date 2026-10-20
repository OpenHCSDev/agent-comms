"""Metrics read a complete prefix; strict replay keeps the physical cut."""
from contextlib import contextmanager
from io import DEFAULT_BUFFER_SIZE
import json

import pytest

from agent_comms.bus_activity_index import BusActivityIndex, ChannelActivity
from agent_comms.bus_projection import NoCheckpoint
from agent_comms.bus_display_index import BusDisplayIndex, DisplaySnapshot
from agent_comms.bus_page_index import BusPageIndex
from agent_comms.coordination_errors import StaleRevision
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import (
    ChannelDisplayScope,
)
from agent_comms.wire_log import AbsentWireSnapshot, CurrentSnapshotOpening, WireLog
from agent_comms.wire_metadata import WireMetadata


@contextmanager
def opened(path, *indexes):
    if path.stat().st_mode & 0o777 != 0o600:
        path.chmod(0o600)
    log = WireLog(path)
    for index in indexes:
        log.register_append_index(index)
    log.write_metadata_unlocked(WireMetadata(last_seq=2, claimed_after_seq=2, writer_protocol_version=1, wire_root_id="0" * 32))
    with log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
        yield source


def semantics():
    return (ChannelDisplayScope("#team", frozenset({"#team"})),)


def row(sequence):
    message = Message("sender", "#team", f"message {sequence}", InfoKind,
                      seq=sequence, timestamp=float(sequence))
    return json.dumps(message.to_wire()).encode() + b"\n"


@pytest.mark.parametrize("data,complete", [
    (b"", 0), (b"{}\n", 3), (b'{}\n{"valid":true}', 3),
    (b'{"unfinished":', 0), (b"{}\n" + b"x" * (DEFAULT_BUFFER_SIZE + 30), 3),
])
def test_complete_line_cut_is_not_the_physical_cut(tmp_path, data, complete):
    path = tmp_path / "bus.jsonl"
    path.write_bytes(data)
    with opened(path) as source:
        assert source.complete_prefix.boundary == complete
        assert source.boundary == source.revision.size == len(data)
        assert source.stream.tell() == 0


def test_absent_bus_has_a_named_empty_display_result(tmp_path):
    source = AbsentWireSnapshot(WireMetadata(), 0)
    result = BusDisplayIndex(tmp_path / "bus.jsonl", "viewer").snapshot(source, semantics())
    assert isinstance(result, DisplaySnapshot)
    assert result.activity == {"#team": ChannelActivity()}


def test_unavailable_opened_source_is_not_empty_metrics(tmp_path):
    path = tmp_path / "bus.jsonl"
    path.write_bytes(row(1))
    with opened(path) as source:
        path.write_bytes(b"")
        with pytest.raises(StaleRevision):
            BusDisplayIndex(path, "viewer").snapshot(source, semantics())


def test_torn_append_uses_checkpoint_then_reads_only_completed_suffix(tmp_path, monkeypatch):
    path = tmp_path / "bus.jsonl"
    first, second = row(1), row(2)
    path.write_bytes(first)
    index = BusDisplayIndex(path, "viewer")
    with opened(path, index) as source, BusPageIndex(path) as pages:
        pages.sync(source)
        initial = index.snapshot(source, semantics())
    with path.open("ab") as stream:
        stream.write(second[:len(second) // 2])
    parsed = []
    decode = Message.from_wire

    def measured(value):
        parsed.append(value["seq"])
        return decode(value)

    with opened(path, index) as source, BusPageIndex(path) as pages:
        pages.sync(source)
        with monkeypatch.context() as reading:
            reading.setattr(Message, "from_wire", measured)
            partial = index.snapshot(source, semantics())
        assert partial.activity == initial.activity
        assert partial.offset == len(first) < partial.source.size
        assert parsed == []
    with path.open("ab") as stream:
        stream.write(second[len(second) // 2:])
    with opened(path, index) as source, BusPageIndex(path) as pages:
        pages.sync(source)
        with monkeypatch.context() as reading:
            reading.setattr(Message, "from_wire", measured)
            completed = index.snapshot(source, semantics())
        assert completed.activity == {"#team": ChannelActivity(last_message=2.0)}
        assert completed.offset == len(first) + len(second)
        assert parsed == [2]


def test_activity_uses_the_same_complete_line_cut(tmp_path):
    path = tmp_path / "bus.jsonl"
    first = row(1)
    path.write_bytes(first)
    index = BusActivityIndex(path)
    with opened(path, index) as source:
        initial = index.snapshot(source)
    with path.open("ab") as stream:
        stream.write(b'{"observation":')
    with opened(path, index) as source:
        assert index.snapshot(source) == initial
        assert source.complete_prefix.boundary == len(first) < source.boundary


@pytest.mark.parametrize("index_type,index_args,read_args", [
    (BusDisplayIndex, ("viewer",), (semantics(),)), (BusActivityIndex, (), ()),
])
def test_checkpoint_write_failure_is_visible_and_not_remembered(tmp_path, index_type, index_args, read_args):
    path = tmp_path / "bus.jsonl"
    path.write_bytes(row(1))
    index = index_type(path, *index_args)
    with opened(path, index) as source, BusPageIndex(path) as pages:
        pages.sync(source)
        index.path.mkdir()  # Real publication failure, not a mocked write.
        with pytest.raises(OSError):
            index.snapshot(source, *read_args)
        assert index._held == NoCheckpoint()
        assert source._append_checkpoints[index] == NoCheckpoint()
