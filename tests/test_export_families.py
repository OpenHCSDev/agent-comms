"""Wire exports against pinned golden bytes and receipts."""

import json
from itertools import product
from pathlib import Path

import pytest

from agent_comms.messaging.exporting import (
    ChannelScope,
    DmScope,
    EverythingScope,
    FullLimit,
    MaxBytesLimit,
    RecentLimit,
    SelectableWireExportFormat,
    WireExportBoundary,
    WireExportLimit,
    WireExportScope,
    WireTranscriptExporter,
)
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.nominal.field_codec import FieldCodec

FIXTURES = Path(__file__).parent / "fixtures" / "wire_exports"


def rows():
    return [
        Message("alice", "#team", "hello\ncafé", InfoKind, timestamp=float(i), seq=i)
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
        (member() for member in SelectableWireExportFormat.members_with(SelectableWireExportFormat)),
    )
)


@pytest.mark.parametrize("scope,limit,format", CASES)
def test_pre_refactor_golden_bytes_and_entire_receipt(tmp_path, scope, limit, format):
    name = f"{scope.declared_name}-{limit.declared_name}.{format.declared_name}"
    destination = tmp_path / name
    acknowledgement = (
        WireTranscriptExporter(
            format=format,
            scope=scope,
            limit=limit,
            boundary=WireExportBoundary(4, 4.0),
        )
        .export(rows(), destination)
        .to_wire()
    )
    acknowledgement.pop("destination")
    assert destination.read_bytes() == (FIXTURES / name).read_bytes()
    assert acknowledgement == json.loads((FIXTURES / "receipts.json").read_text())[name]
    assert FieldCodec.decode(WireExportScope, scope.to_wire()) == scope
    assert FieldCodec.decode(WireExportLimit, limit.to_wire()) == limit


def test_dm_scope_requires_two_distinct_participants():
    with pytest.raises(ValueError):
        DmScope("ab")
