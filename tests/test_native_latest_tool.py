"""Projection checks after the original native selector has selected a cut."""

import json

import pytest

from agent_comms.native_index import (
    NativeEntryPosition,
    NativeIndexHelper,
    NativeIndexObservation,
)
from agent_comms.native_transcript import NativeTranscript
from agent_comms.pi_helper import PiHelperError
from agent_comms.private_path import FileRevision
from agent_comms.tool_activity import (
    NativeToolSelectorUnavailable,
    NoToolActivity,
    RecordedToolActivity,
)


def selected_source(tmp_path, monkeypatch, *, timestamp="1970-01-01T00:00:00Z", tools=True):
    path = tmp_path / "source.jsonl"
    content = ([{"type": "toolCall", "id": name, "name": "bash", "arguments": {
        "command": name, "large": 9007199254740993,
    }} for name in ("first", "last")] if tools else [{"type": "text", "text": "no tool"}])
    raw = json.dumps({
        "type": "message", "id": "original", "parentId": None,
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": content},
    }).encode() + b"\n"
    path.write_bytes(raw)
    observation = NativeIndexObservation(
        FileRevision.capture(path), NativeEntryPosition("original", 0, len(raw)),
    )
    monkeypatch.setattr(NativeIndexHelper, "observe", classmethod(lambda cls, file: observation))
    return path, observation


@pytest.mark.parametrize("timestamp,expected", [
    ("1970-01-01T00:00:00Z", 0.0),
    (None, None),
    ("2026-01-01T00:00:00Z", 1767225600.0),
])
def test_original_order_arguments_and_recorded_time(tmp_path, monkeypatch, timestamp, expected):
    path, _ = selected_source(tmp_path, monkeypatch, timestamp=timestamp)
    activity = NativeTranscript(path).latest_tool()
    assert isinstance(activity, RecordedToolActivity)
    assert activity.call.tool_call_id == "last"
    assert activity.call.raw_input == {"command": "last", "large": 9007199254740993}
    assert activity.call.timestamp == expected


def test_none_requires_a_current_original_source(tmp_path, monkeypatch):
    path, selected = selected_source(tmp_path, monkeypatch)
    proven_empty = NativeIndexObservation(selected.revision, None)
    monkeypatch.setattr(NativeIndexHelper, "observe", classmethod(lambda cls, file: proven_empty))
    assert isinstance(NativeTranscript(path).latest_tool(), NoToolActivity)
    path.write_bytes(path.read_bytes() + b"partial")
    assert isinstance(NativeTranscript(path).latest_tool(), NativeToolSelectorUnavailable)


def test_missing_source_is_unavailable(tmp_path):
    activity = NativeTranscript(tmp_path / "absent").latest_tool()
    assert isinstance(activity, NativeToolSelectorUnavailable)


def test_failed_original_selector_is_not_absence(tmp_path, monkeypatch):
    path, _ = selected_source(tmp_path, monkeypatch)

    def unavailable(cls, file):
        raise PiHelperError("No exact native selector evidence")

    monkeypatch.setattr(NativeIndexHelper, "observe", classmethod(unavailable))
    assert isinstance(NativeTranscript(path).latest_tool(), NativeToolSelectorUnavailable)


def test_selected_non_tool_is_not_empty(tmp_path, monkeypatch):
    path, _ = selected_source(tmp_path, monkeypatch, tools=False)
    assert isinstance(NativeTranscript(path).latest_tool(), NativeToolSelectorUnavailable)


@pytest.mark.parametrize("position", [
    NativeEntryPosition("wrong-id", 0, 1),
    NativeEntryPosition("original", -1, 1),
    NativeEntryPosition("original", 0, 1_000_000),
])
def test_invalid_original_coordinates_fail_closed(tmp_path, monkeypatch, position):
    path, selected = selected_source(tmp_path, monkeypatch)
    invalid = NativeIndexObservation(selected.revision, position)
    monkeypatch.setattr(NativeIndexHelper, "observe", classmethod(lambda cls, file: invalid))
    assert isinstance(NativeTranscript(path).latest_tool(), NativeToolSelectorUnavailable)


def test_replacement_cannot_splice_an_index_into_an_open_reader(tmp_path, monkeypatch):
    path, selected = selected_source(tmp_path, monkeypatch)

    def replace_after_selection(cls, file):
        replacement = tmp_path / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return selected

    monkeypatch.setattr(NativeIndexHelper, "observe", classmethod(replace_after_selection))
    assert isinstance(NativeTranscript(path).latest_tool(), NativeToolSelectorUnavailable)


def test_native_session_owns_index_companions_and_detects_index_only_borrower(tmp_path):
    from agent_comms.native_session_files import NativeSessionFiles

    files = NativeSessionFiles.of(str(tmp_path / "source.jsonl"))
    for path in files.members:
        path.touch(mode=0o600)
    assert files.entry_index.name == "source.jsonl.entry-index.v2.sqlite3"
    assert files.present() == files.members
    files.require_only_declared_sidecars()
    with files.entry_index.open("rb"):
        assert files._referenced_by("/proc/self")
