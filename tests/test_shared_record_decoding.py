"""Shared decoding retains source identity and independent public occurrences."""
import asyncio
from dataclasses import dataclass
from typing import ClassVar

import pytest

from agent_comms.nominal.field_codec import (
    FieldCodec, MalformedRecord, RecordMember, SharedRecordMember, SharedValueDecoding,
)


@dataclass
class SharedSample(SharedRecordMember):
    values: list[int]
    calls: ClassVar[int] = 0

    @classmethod
    def from_wire(cls, data):
        cls.calls += 1
        return FieldCodec.decode_record(cls, data)


@dataclass
class OrdinarySample(RecordMember):
    values: list[int]
    calls: ClassVar[int] = 0

    @classmethod
    def from_wire(cls, data):
        cls.calls += 1
        return FieldCodec.decode_record(cls, data)


@pytest.fixture(autouse=True)
def counts():
    SharedSample.calls = OrdinarySample.calls = 0
    assert SharedValueDecoding.current.get() is None
    yield
    assert SharedValueDecoding.current.get() is None


def test_repeated_source_is_decoded_once_and_every_result_is_detached():
    raw = {"values": [1, 2]}
    first, second = FieldCodec.decode_shared(tuple[SharedSample, ...], [raw, raw])
    assert SharedSample.calls == 1
    assert first == second and first is not second
    assert first.values is not second.values and first.values is not raw["values"]
    first.values.append(3)
    assert second.values == raw["values"] == [1, 2]


def test_equal_distinct_sources_are_never_combined():
    one, two = {"values": [1]}, {"values": [1]}
    FieldCodec.decode_shared(tuple[SharedSample, ...], [one, two])
    assert SharedSample.calls == 2


def test_plain_and_unscoped_decoding_keep_their_per_occurrence_contract():
    raw = {"values": [1]}
    FieldCodec.decode_shared(tuple[OrdinarySample, ...], [raw, raw])
    assert OrdinarySample.calls == 2
    FieldCodec.decode(tuple[SharedSample, ...], [raw, raw])
    assert SharedSample.calls == 2


def test_new_read_observes_changed_raw_input_without_reusing_an_old_result():
    raw = {"values": [1]}
    first = FieldCodec.decode_shared(SharedSample, raw)
    raw["values"].append(2)
    second = FieldCodec.decode_shared(SharedSample, raw)
    assert first.values == [1] and second.values == [1, 2]
    assert SharedSample.calls == 2


@pytest.mark.parametrize("raw", ({"values": ["wrong"]}, {"values": [1], "extra": 2}, {}))
def test_failed_shared_read_preserves_refusal_and_releases_its_scope(raw):
    with pytest.raises(MalformedRecord):
        FieldCodec.decode_shared(tuple[SharedSample, ...], [raw, raw])
    assert SharedValueDecoding.current.get() is None
    assert FieldCodec.decode_shared(SharedSample, {"values": [3]}).values == [3]


@dataclass
class NestedRead(RecordMember):
    values: list[int]

    @classmethod
    def from_wire(cls, data):
        parent = SharedValueDecoding.current.get()
        assert parent is not None
        with pytest.raises(MalformedRecord):
            FieldCodec.decode_shared(SharedSample, {"values": ["wrong"]})
        assert SharedValueDecoding.current.get() is parent
        return FieldCodec.decode_record(cls, data)


def test_nested_failed_scope_restores_the_outer_read_and_its_capture():
    raw = {"values": [1]}
    result = FieldCodec.decode_shared(tuple[SharedSample, NestedRead, SharedSample], [raw, raw, raw])
    assert SharedSample.calls == 2  # one outer source plus the failing inner source
    assert result[0] == result[2] and result[0] is not result[2]


@dataclass
class SpawnedRead(RecordMember):
    values: list[int]
    child: ClassVar[asyncio.Task]

    @classmethod
    def from_wire(cls, data):
        async def later():
            return FieldCodec.decode(tuple[SharedSample, ...], [data, data])
        cls.child = asyncio.create_task(later())
        return FieldCodec.decode_record(cls, data)


@pytest.mark.asyncio
async def test_an_inherited_task_context_cannot_keep_the_finished_read_alive():
    FieldCodec.decode_shared(SpawnedRead, {"values": [1]})
    first, second = await SpawnedRead.child
    assert SharedSample.calls == 2
    assert first is not second and first.values is not second.values


@dataclass
class MutatingParent:
    first: SharedSample
    second: SharedSample

    def __post_init__(self):
        self.first.values.append(4)


def test_consumer_mutation_cannot_change_the_private_capture_or_later_read():
    raw = {"values": [1]}
    parent = FieldCodec.decode_shared(MutatingParent, {"first": raw, "second": raw})
    assert parent.first.values == [1, 4]
    assert parent.second.values == raw["values"] == [1]
    assert FieldCodec.decode_shared(SharedSample, raw).values == [1]
