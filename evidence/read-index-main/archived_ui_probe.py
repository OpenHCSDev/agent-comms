"""Installed Toad: copied archived history, actual project watch, normal interpreter exit."""

import asyncio
import faulthandler
import json
import os
import shutil
from pathlib import Path

import psutil


def prepare():
    stage = Path(os.environ["WATCHER_ACCEPTANCE_STAGE"])
    source = Path(os.environ["WATCHER_ARCHIVED_WIRE"])
    root = stage / "wire"
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("*.sock"))
    from dataclasses import replace
    from agent_comms.field_codec import FieldCodec
    from agent_comms.historical_views import HistorySource
    from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
    from agent_comms.store_files import file_revision
    from agent_comms.wire_log import WireLog

    # Rebind the owned copy's current checkpoint and archive references.
    # The source, native files and durable read positions remain untouched.
    for name in ("transcript_reply_index.sqlite3", "private_bus_checkpoint.sqlite3"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            (root / (name + suffix)).unlink(missing_ok=True)
    log = WireLog(root / "bus.jsonl")
    marker = log.read_metadata_unlocked(required=True)
    log.write_metadata_unlocked(replace(marker, checkpoint_version=None, checkpoint_seal=None))
    install_private_bus_checkpoint(log)
    manifest = root / "history_sources.json"
    sources = [FieldCodec.decode(HistorySource, item) for item in json.loads(manifest.read_text())]
    manifest.write_text(json.dumps([FieldCodec.encode(replace(
        item, root=str(root / "history" / Path(item.root).name),
        snapshot_bus_revision=file_revision(root / "history" / Path(item.root).name / "bus.jsonl"),
        snapshot_registry_revision=file_revision(root / "history" / Path(item.root).name / "registry.json"),
    )) for item in sources]))
    os.environ["AGENT_COMMS_ROOT"] = str(root)
    for key, name in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state")):
        os.environ[key] = str(stage / name)


async def until(predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(.02)


async def main():
    from agent_comms.comms import wire
    from toad.app import ToadApp
    from toad.widgets.comms_chat import CommsChatView

    comms = wire()
    project = Path(comms.registry.require("agent-comms-ux").worktree)
    before = comms.bus.log.latest_sequence()
    app = ToadApp(project_dir=str(project))
    report = {"messages_sent": 0, "project": str(project), "views": []}
    async with app.run_test(size=(140, 48)) as pilot:
        await pilot.pause()
        original = app.current_mode
        conversation = app.screen.conversation
        conversation.watch_agent_ready(True)
        watcher = conversation._directory_watcher
        conversation.watch_agent_ready(True)
        assert conversation._directory_watcher is watcher
        await until(lambda: watcher._observation is not None
                    and watcher._observation.process.pid is not None)
        child_pid = watcher._observation.process.pid
        report["watch_child"] = child_pid
        for target, kind in (("#comms", "channel"), ("agent-comms-ux", "dm"),
                             ("pr95-selected-pi-summary-owner", "dm")):
            await app.open_comms_session(owner_mode=original, project_path=project,
                                         me="user", target=target, kind=kind)
            await until(lambda: bool(app.screen.query(CommsChatView)))
            chat = app.screen.query_one(CommsChatView)
            await until(lambda: chat.target == target and chat._history_initialized)
            await pilot.pause()
            assert app._exception is None
            report["views"].append({"target": target, "history_entries": len(chat._history)})
        assert comms.bus.log.latest_sequence() == before
    await asyncio.to_thread(watcher.join, 3)
    assert not watcher.is_alive() and not psutil.pid_exists(child_pid)
    assert app._exception is None
    report["watcher_and_child_joined"] = True
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(35)
    prepare()
    asyncio.run(main())
    faulthandler.cancel_dump_traceback_later()
    print("PASS: interpreter shutdown completed", flush=True)
