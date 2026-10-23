"""Boundary declarations reject malformed records without inventing another shape."""
from dataclasses import replace
import hashlib

import pytest

from agent_comms.channel_coding_tools import CodingToolRequest
from agent_comms.cli_bridges.claude_protocol import ClaudeHeader
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import (
    HistoricalInput, OwnerQueueInputDeliveryScope, UnobservedInputDeliveryScope, UnresolvedInput,
)
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.native_package import NativeSourceDeclaration
from agent_comms.tool_denial import SelectedToolDenied
from agent_comms.ui_model.delivery import DeliveryView


def test_coding_socket_record_keeps_object_arguments_and_strict_fields():
    payload = {'token': 'secret', 'call_id': 'call-1', 'name': 'read',
               'arguments': {'path': 'source.py'}}
    request = FieldCodec.decode(CodingToolRequest, payload)
    assert request.arguments == {'path': 'source.py'}
    assert request.authenticate('secret').call_id == 'call-1'
    with pytest.raises(SelectedToolDenied):
        request.authenticate('wrong')
    with pytest.raises(ValueError):
        FieldCodec.decode(CodingToolRequest, {**payload, 'unexpected': True})


def test_delivery_uses_declared_overview_and_preserves_typed_rows():
    current = UnresolvedInput(4, 'worker', 'retained input', 'input-1', 'unknown')
    historical = HistoricalInput(3, 'worker', 'earlier input', 'input-0', 'unknown',
                                 notice_dismissed=True)
    overview = DeliveryOverview((current,), 1, 2, (historical,), OwnerQueueInputDeliveryScope)
    wire = FieldCodec.encode(overview)
    delivery = DeliveryView.from_wire(wire)
    assert delivery.overview.inputs == (current,)
    assert delivery.overview.historical_inputs == (historical,)
    assert (delivery.overview.historical_count, delivery.overview.dismissed_historical_count) == (1, 2)
    assert delivery.overview.current_scope is OwnerQueueInputDeliveryScope
    assert delivery.pending == 'awaiting start'
    assert DeliveryView.from_wire(FieldCodec.encode(
        replace(overview, current_scope=UnobservedInputDeliveryScope))).pending == 'unconfirmed'
    with pytest.raises(ValueError):
        DeliveryView.from_wire({**wire, 'unexpected': True})


@pytest.mark.parametrize('payload, expected', (
    ({'type': 'user'}, 'local-command'),
    ({'type': 'user', 'isCompactSummary': True}, 'local-command'),
    ({'type': 'user', 'promptId': ''}, 'user'),
    ({'type': 'user', 'promptId': 'turn', 'isCompactSummary': True}, 'compact-summary'),
    ({'type': 'user', 'promptId': 'turn', 'isMeta': True}, 'harness-note'),
    ({'type': 'user', 'promptId': 'turn', 'isMeta': True, 'origin': {'kind': 'peer'}}, 'user'),
    ({'type': 'user', 'origin': {'kind': 'task-notification'}, 'isCompactSummary': True}, 'task-user'),
    ({'type': 'assistant', 'origin': 'not a user header'}, 'assistant'),
))
def test_claude_header_preserves_sparse_field_presence_and_precedence(payload, expected):
    assert ClaudeHeader.model_validate(payload).type == expected


def test_native_source_receipt_checks_bytes_without_installing(tmp_path):
    content = b'export default () => 1;\n'
    (tmp_path / 'source.ts').write_bytes(content)
    destination = tmp_path / 'installed.ts'
    payload = {'path': str(destination), 'snapshot': 'source.ts',
               'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
    source = FieldCodec.decode(NativeSourceDeclaration, payload).load(tmp_path)
    assert source.destination == destination and source.content == content
    assert not destination.exists()
    for changed in ({'bytes': True}, {'bytes': -1}, {'sha256': 'bad'},
                    {'path': 'relative.ts'}, {'snapshot': str(tmp_path / 'source.ts')},
                    {'unexpected': True}):
        with pytest.raises(ValueError):
            FieldCodec.decode(NativeSourceDeclaration, {**payload, **changed})


def test_raw_shape_guard_reuses_the_existing_overlay_detector():
    from debt_ratchet import RawRecordShapes

    raw = b'def read(row):\n return row["a"], row["b"], row.get("c")\n'
    typed = b'def read(row):\n return row.a, row.b, row.c\n'
    assert RawRecordShapes.count(raw, 'src/example.py') == 1
    assert RawRecordShapes.count(typed, 'src/example.py') == 0
    assert RawRecordShapes.count(raw, 'src/agent_comms/field_codec.py') == 1
