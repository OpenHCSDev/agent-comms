"""Watcher digests retain native calls without treating triage replies as speech."""

import json
from collections import defaultdict
from types import SimpleNamespace

from agent_comms.execution.native_entries import NativeEntry
from agent_comms.execution.selected.triage import (
    FullSelectedTriage,
    SelectedTriage,
)
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.pi_payloads import PiMessage
from agent_comms.team.tool_watch import emit, follow
from agent_comms.transport.native_tools import NativeToolDeclaration


def test_follow_reads_the_message_family_without_an_assistant_probe(monkeypatch):
    entry = SimpleNamespace(messages=(PiMessage(),))
    monkeypatch.setattr(NativeEntry, "read", lambda line: entry)

    follow(None, defaultdict(list), "worker", b"record\n")


def test_coordination_call_digests_its_arguments():
    call = NativeToolDeclaration.decode('coordination-1', 'comms_threads', {})
    assert call.digest() == 'comms_threads {}'


def test_emit_suppresses_triage_speech_but_keeps_calls(capsys):
    for member in SelectedTriage.members_with(SelectedTriage):
        emit({}, "worker", json.dumps(FieldCodec.encode(member())))
        assert capsys.readouterr().out == ""

    calls = ("read source.py", "edit source.py")
    turns = {"worker": list(calls)}
    emit(turns, "worker", json.dumps(FieldCodec.encode(FullSelectedTriage())))
    output = capsys.readouterr().out
    assert not turns
    assert all(call in output for call in calls)
    assert "says:" not in output

    sentence = "The source change is ready."
    emit({}, "worker", sentence)
    assert f"says: {sentence}" in capsys.readouterr().out
