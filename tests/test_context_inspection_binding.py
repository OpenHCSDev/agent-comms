"""A complete inspection reply keeps the server's imported-session reference."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.cli_bridges.history import OfflineHistory
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.execution.native_session_reopen import NativeSessionIdentity, PiSessionId
from agent_comms.execution.session_file import RegistrySessionLocation, SessionFile
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.harness_install import CodexHarness
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.pi_payloads import NativeContextData
from agent_comms.registry.thread_text import AnySession
from conftest import ui_core_read
from agent_comms.transport.tools.sidebar import CommsContextInspectionTool, ContextInspectionRead


@pytest.fixture
def imported_inspection(comms, tmp_path):
    source = tmp_path / "codex-session.jsonl"
    records = (
        {"type": "session_meta", "payload": {"id": "original", "cwd": str(tmp_path)}},
        {"type": "compacted", "payload": {"message": "Imported summary", "replacement_history": [
            {"type": "message", "role": "system", "content": "Original imported instruction"},
        ]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Original imported question"},
        ]}},
    )
    source.write_text("".join(json.dumps({"timestamp": "2026-10-18T00:00:00Z", **record}) + "\n"
                              for record in records))
    imported = comms.threads.import_thread(
        OfflineHistory.stored(CodexHarness, source, AnySession()), name="imported")
    read = ui_core_read(comms, CommsContextInspectionTool(owner=imported.thread))
    assert read.imported
    return comms, read


def roundtrip(read: ContextInspectionRead) -> ContextInspectionRead:
    return FieldCodec.decode(ContextInspectionRead, FieldCodec.parse(
        FieldCodec.compact(FieldCodec.encode(read))))


def test_complete_imported_inspection_keeps_native_session_path(imported_inspection, monkeypatch):
    comms, original = imported_inspection
    expected = original.entry.thread.runtime.path
    payload = NativeContextData(
        counter="pi.estimateTokens",
        identity=NativeSessionIdentity(PiSessionId("selected-native"), expected), segments=())
    original_open = Path.open

    def client_open(path, *args, **kwargs):
        if path.is_relative_to(comms.root):
            raise AssertionError("A remote inspection must not open the server filesystem")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", client_open)
    decoded = roundtrip(original)
    assert decoded.page == original.page
    assert decoded.source == original.source
    assert decoded.annotations == original.annotations
    assert decoded.imported == original.imported
    assert payload.require_session_file(decoded.entry.thread.runtime.path) is payload
    assert decoded.entry.thread.runtime.path == expected


def test_complete_imported_inspection_keeps_launch_and_rejects_changed_session(imported_inspection):
    comms, original = imported_inspection
    decoded = roundtrip(original)
    assert decoded.entry.thread.same_launch(original.entry.thread)
    assert roundtrip(decoded).entry.thread.same_launch(decoded.entry.thread)
    changed = replace(decoded.entry.thread, session_file=RegistrySessionLocation.from_path(
        comms.root / "imported_sessions" / "another.jsonl", comms.root))
    assert not decoded.entry.thread.same_launch(changed)
    payload = NativeContextData(
        counter="pi.estimateTokens",
        identity=NativeSessionIdentity(PiSessionId("selected-native"), original.entry.thread.runtime.path),
        segments=())
    with pytest.raises(ValueError, match="different selected native session"):
        payload.require_session_file(changed.runtime.path)


def test_copied_bus_inspection_uses_its_own_session_root(imported_inspection):
    comms, original = imported_inspection
    copied_root = comms.root.parent / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied_root)
    copied = wire(copied_root)
    read = roundtrip(ui_core_read(
        copied, CommsContextInspectionTool(owner=original.entry.thread.name)))
    relative = original.entry.thread.session_file.path
    assert read.entry.thread.runtime.path == str(copied_root / relative)
    assert read.entry.thread.incarnation == original.entry.thread.incarnation
    assert not read.entry.thread.same_launch(original.entry.thread)
    payload = NativeContextData(
        counter="pi.estimateTokens",
        identity=NativeSessionIdentity(PiSessionId("selected-native"), original.entry.thread.runtime.path),
        segments=())
    with pytest.raises(ValueError, match="different selected native session"):
        payload.require_session_file(read.entry.thread.runtime.path)


def test_portable_imported_session_still_requires_its_registry(imported_inspection):
    comms, original = imported_inspection
    saved = FieldCodec.encode(original.entry.thread.session_file)
    assert "root" not in saved
    decoded = FieldCodec.decode(SessionFile, saved)
    assert not decoded.path.is_absolute()
    with pytest.raises(RelationViolationError, match="must be bound by its registry"):
        decoded.location(original.entry.thread.backend)
    copied_root = comms.root.parent / "copied-bus"
    copied = decoded.bound_to(copied_root)
    assert copied.location(original.entry.thread.backend).path == str(copied_root / decoded.path)
