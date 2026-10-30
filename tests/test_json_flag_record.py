"""The two transport users inherit one codec and one argument parser."""

import argparse
import json

import pytest

from agent_comms.cli_options import JsonFlagRecord
from agent_comms.field_codec import FieldCodec
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel


@pytest.fixture(params=[
    (NativeThreadDefaults, NativeThreadDefaults(model=DeclaredModel('selected/model'))),
    (RecordedThreadIncarnation, RecordedThreadIncarnation("selected-owner", 1.0)),
])
def selected_record(request):
    return request.param


def test_records_share_the_field_codec_and_transport_implementation(selected_record):
    record_type, selected = selected_record
    assert record_type.to_json is JsonFlagRecord.to_json
    assert record_type.from_json.__func__ is JsonFlagRecord.from_json.__func__
    assert record_type.from_arguments.__func__ is JsonFlagRecord.from_arguments.__func__
    assert json.loads(selected.to_json()) == FieldCodec.encode(selected)
    assert selected.cli_arguments == (record_type.value_flag, selected.to_json())
    assert record_type.from_json(selected.to_json()) == selected


def test_present_flags_decode_and_missing_flags_refuse(selected_record):
    record_type, selected = selected_record
    assert record_type.from_arguments(selected.cli_arguments) == selected
    for arguments in ((), ("unrelated-command", "--other-flag")):
        with pytest.raises(ValueError):
            record_type.from_arguments(arguments)


def test_malformed_explicit_flags_refuse(selected_record):
    record_type, _ = selected_record
    for arguments in ((record_type.value_flag,), (record_type.value_flag, "not json"),
                      (record_type.value_flag, '{"unknown":true}')):
        with pytest.raises(argparse.ArgumentError):
            record_type.from_arguments(arguments)


def test_required_parser_attachment_uses_the_declared_flag_and_destination(selected_record):
    record_type, selected = selected_record
    parser = argparse.ArgumentParser()
    record_type.add_cli_arguments(parser, required=True, dest="selected")
    assert parser.parse_args(selected.cli_arguments).selected == selected
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(())
    assert missing.value.code == 2


def test_settings_schema_still_comes_only_from_its_declared_data_fields():
    assert FieldCodec.encode(NativeThreadDefaults()) == {
        "model": "openai-codex/gpt-6.1-sol", "thinking_level": "high",
    }
    assert set(FieldCodec.record_schema(NativeThreadDefaults)["properties"]) == {
        "model", "thinking_level",
    }
