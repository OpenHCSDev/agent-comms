"""Pinned pre-refactor artifacts and open-world extension through public contracts."""

import json
from collections import deque
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pytest

from agent_comms.declarations import Message, MessageType
from agent_comms.exporting import (
    ChannelScope,
    DmScope,
    EverythingScope,
    FullLimit,
    JsonlFormat,
    MaxBytesLimit,
    RecentLimit,
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportScope,
    WireTranscriptExporter,
)
from agent_comms.field_codec import FieldCodec

FIXTURES = Path(__file__).parent / "fixtures" / "wire_exports"


def rows():
    return [
        Message("alice", "#team", "hello\ncafé", MessageType.INFO, timestamp=float(i), seq=i)
        for i in range(1, 5)
    ]


CASES = list(
    product(
        [
            EverythingScope(),
            ChannelScope("#team"),
            DmScope(("alice", "bob")),
        ],
        [FullLimit(), MaxBytesLimit(850), RecentLimit(2.0)],
        (member() for member in WireExportFormat.members_with(WireExportFormat)),
    )
)


@pytest.mark.parametrize("scope,limit,format", CASES)
def test_pre_refactor_golden_bytes_and_entire_receipt(tmp_path, scope, limit, format):
    name = f"{scope.declared_name}-{limit.declared_name}.{format.declared_name}"
    destination = tmp_path / name
    receipt = (
        WireTranscriptExporter(
            format=format,
            scope=scope,
            limit=limit,
            boundary=WireExportBoundary(4, 4.0),
        )
        .export(rows(), destination)
        .to_wire()
    )
    receipt.pop("destination")
    assert destination.read_bytes() == (FIXTURES / name).read_bytes()
    assert receipt == json.loads((FIXTURES / "receipts.json").read_text())[name]
    assert FieldCodec.decode(WireExportScope, scope.to_wire()) == scope
    assert FieldCodec.decode(WireExportLimit, limit.to_wire()) == limit


def test_family_names_and_boundary_decoding():
    assert WireExportScope.names() == ("everything", "channel", "dm")
    assert WireExportLimit.names() == ("full", "max_bytes", "recent")
    assert WireExportFormat.names() == ("jsonl", "text")
    for member in WireExportFormat.members_with(WireExportFormat):
        value = member()
        assert WireExportFormat.decode(value.declared_name)() == value
        assert FieldCodec.decode(WireExportFormat, FieldCodec.encode(value)) == value
    with pytest.raises(ValueError):
        WireExportFormat.decode("unknown")


def test_new_limit_works_without_exporter_codec_or_registry_edits(tmp_path):
    # Exercise the real family, restoring its registry after the extension experiment.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(WireExportLimit, "__registry__", dict(WireExportLimit.__registry__))

        @dataclass(frozen=True)
        class LastMessagesLimit(WireExportLimit):
            count: int

            def retain(self, rows, header_size, stats):
                return deque(rows, maxlen=self.count)

        limit = LastMessagesLimit(2)
        assert WireExportLimit.decode("last_messages") is LastMessagesLimit
        assert FieldCodec.decode(WireExportLimit, {"kind": "last_messages", "count": 2}) == limit
        destination = tmp_path / "last.jsonl"
        receipt = WireTranscriptExporter(
            format=JsonlFormat(),
            scope=EverythingScope(),
            limit=limit,
            boundary=WireExportBoundary(4, 4.0),
        ).export(rows(), destination)
        records = [json.loads(line) for line in destination.read_text().splitlines()]
        assert records[0]["limit"] == {"kind": "last_messages", "count": 2}
        assert [row["message"]["seq"] for row in records[1:]] == [3, 4]
        assert (receipt.source_messages, receipt.exported_messages, receipt.omitted_messages) == (
            4,
            2,
            2,
        )
        assert receipt.truncated


def test_public_families_require_their_behavior_hooks():
    from agent_comms.exporting import DmScope

    with pytest.raises(TypeError):
        WireExportScope()
    with pytest.raises(TypeError):
        WireExportLimit()
    with pytest.raises(TypeError):
        WireExportFormat()
    with pytest.raises(ValueError, match="distinct participants"):
        DmScope("ab")
