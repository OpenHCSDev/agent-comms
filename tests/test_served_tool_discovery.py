"""Fresh-process serving discovery for observation and ReadTool families."""

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
