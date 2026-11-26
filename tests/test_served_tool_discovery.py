"""Fresh-process serving discovery and client isolation for observation/ReadTool families."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_observation_serving_families_acquire_served_tools_in_fresh_process() -> None:
    """An observation child worker resolves ReadTool, UiRead, CapturesTranscriptPage, and
    OwnerScopedRead without relying on unrelated RootFile walks to import served_tools."""
    source = Path(__file__).resolve().parents[1] / "src"
    program = f"""
import json, sys
sys.path.insert(0, {str(source)!r})
from agent_comms.ui_model.observation import (
    CapturesTranscriptPage,
    OwnerScopedRead,
    ReadTool,
    UiRead,
)
from agent_comms.transport.tools.sidebar import CommsRegistrySliceTool

before_loaded = "agent_comms.transport.served_tools.served" in sys.modules
served = ReadTool.serving(CommsRegistrySliceTool(threads=()))
after_loaded = "agent_comms.transport.served_tools.served" in sys.modules
print(json.dumps({{
    "before_loaded": before_loaded,
    "after_loaded": after_loaded,
    "served_name": type(served).__name__,
    "read_tool_count": len(ReadTool.members_with(ReadTool)),
    "ui_read_count": len(UiRead.members_with(UiRead)),
    "captures_count": len(CapturesTranscriptPage.members_with(CapturesTranscriptPage)),
    "owner_scoped_count": len(OwnerScopedRead.members_with(OwnerScopedRead)),
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    data = json.loads(completed.stdout)
    assert data["before_loaded"] is False
    assert data["after_loaded"] is True
    assert data["served_name"] == "ServedCommsRegistrySliceTool"
    assert data["read_tool_count"] >= 45
    assert data["ui_read_count"] >= 4
    assert data["captures_count"] >= 1
    assert data["owner_scoped_count"] >= 2


def test_client_field_codec_on_read_families_does_not_load_served_tools_or_packaging() -> None:
    """Client/Android FieldCodec encode/decode on NotificationRead, BusRead, UiRead,
    ReadTool, ReadRequest, and ToolRequest never imports served_tools or packaging."""
    source = Path(__file__).resolve().parents[1] / "src"
    program = f"""
import json, sys
sys.path.insert(0, {str(source)!r})

class BlockPackaging:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "packaging" or fullname.startswith("packaging."):
            raise ModuleNotFoundError(f"No module named {{fullname!r}}")

sys.meta_path.insert(0, BlockPackaging())

from agent_comms.messaging.bus_read import BusRead, UiRead
from agent_comms.messaging.notification_reads import LiveNotificationRead, NotificationRead
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.transport.request import ReadRequest, ToolRequest
from agent_comms.transport.tool_request import ReadTool
from agent_comms.transport.tools.sidebar import CommsRegistrySliceTool

wire_notif = FieldCodec.encode(LiveNotificationRead(()))
decoded_notif = FieldCodec.decode(NotificationRead, wire_notif)
decoded_ui = FieldCodec.decode(UiRead, wire_notif)
decoded_bus = FieldCodec.decode(BusRead, wire_notif)

wire_tool = FieldCodec.encode(CommsRegistrySliceTool(threads=()))
decoded_req = FieldCodec.decode(ToolRequest, wire_tool)
decoded_read = FieldCodec.decode(ReadRequest, wire_tool)
read_tool_members = len(ReadTool._members)

served_modules = sorted(
    name for name in sys.modules
    if name.startswith("agent_comms.transport.served_tools.")
)
packaging_modules = sorted(
    name for name in sys.modules
    if name == "packaging" or name.startswith("packaging.")
)
print(json.dumps({{
    "decoded_notif": type(decoded_notif).__name__,
    "decoded_ui": type(decoded_ui).__name__,
    "decoded_bus": type(decoded_bus).__name__,
    "decoded_req": type(decoded_req).__name__,
    "decoded_read": type(decoded_read).__name__,
    "read_tool_members": read_tool_members,
    "served_modules": served_modules,
    "packaging_modules": packaging_modules,
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    data = json.loads(completed.stdout)
    assert data["decoded_notif"] == "LiveNotificationRead"
    assert data["decoded_ui"] == "LiveNotificationRead"
    assert data["decoded_bus"] == "LiveNotificationRead"
    assert data["decoded_req"] == "CommsRegistrySliceTool"
    assert data["decoded_read"] == "CommsRegistrySliceTool"
    assert data["served_modules"] == []
    assert data["packaging_modules"] == []
