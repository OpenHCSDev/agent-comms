"""Actual queued owner handoff retains history and accepts a fresh native input."""
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.native_package import pinned_native_package

import asyncio
import json
import os
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from acp import spawn_agent_process
from acp.schema import TextContentBlock

import agent_comms.restart_queue as queue
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.native_arguments import NativeArguments
from agent_comms.comms import Comms
from agent_comms.child_process import AttachedChild
from agent_comms.field_codec import FieldCodec
from agent_comms.runtime import socket_path
from agent_comms.threads import Thread
from agent_comms.wire_watch import DirectoryWatchSource, WatchSource, open_wire_watcher
from explicit_private_owner_installed_journey import Subscriber


@pytest.mark.skipif(sys.platform != "linux", reason="Linux watcher")
async def test_inotify_wakes_for_registry_and_queue_changes(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    directory = root / queue.DIRECTORY
    directory.mkdir()
    watcher = open_wire_watcher(
        WatchSource.file(root / "registry.json"), DirectoryWatchSource(directory)
    )
    try:
        await watcher.prepare()
        (root / "registry.json").write_text("changed")
        await asyncio.wait_for(watcher.wait(), 1)
        await watcher.prepare()
        (directory / "next.json").write_text("queued")
        await asyncio.wait_for(watcher.wait(), 1)
    finally:
        watcher.close()


