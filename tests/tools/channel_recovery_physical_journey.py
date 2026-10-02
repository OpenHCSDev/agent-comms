"""Readonly channel/receiver navigation through the existing physical recorder."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recorder", required=True, type=Path)
    parser.add_argument("--runtime-bin", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--thread", default="openhcs-architecture-memory")
    parser.add_argument("--peer", default="openhcs-pr159-viewer-bind-owner")
    parser.add_argument("--channel", default="#openhcs")
    args = parser.parse_args()
    base = args.output.expanduser().resolve()
    if not base.is_relative_to(Path.home() / ".cache/agent-scratch"):
        parser.error("Evidence must be in persistent owned agent scratch")
    base.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location("channel_recovery_recorder", args.recorder)
    recorder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = recorder
    spec.loader.exec_module(recorder)

    class ChannelRecoveryJourney(recorder.PhysicalJourney):
        @classmethod
        def script(cls, options):
            marker = recorder.marker_command()
            settle = f"sleep {options.navigation_settle_seconds:g}"
            return "\n".join([
                marker + "receiver-before",
                recorder.native_click_command("phase-receiver-before-state.pickle",
                                              target="channel", name=args.channel),
                settle, marker + "channel-open",
                recorder.native_click_command("phase-channel-open-state.pickle",
                                              target="thread", name=args.peer),
                settle, marker + "receiver-open",
                recorder.native_click_command("phase-receiver-open-state.pickle",
                                              target="original_tab",
                                              original_state="phase-channel-open-state.pickle"),
                settle, marker + "channel-return", "",
            ])

    source_db = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "toad/toad.db"
    copied_db = base / "state/toad/toad.db"
    copied_db.parent.mkdir(parents=True)
    with sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(copied_db) as copied:
            source.backup(copied)
    os.environ["XDG_STATE_HOME"] = str(base / "state")
    for key in ("NO_COLOR", "PYTHONPATH", "AGENT_COMMS_RUNTIME_ROOT", "AGENT_COMMS_ACP_LAUNCHER"):
        os.environ.pop(key, None)
    os.environ["AGENT_COMMS_RUNTIME_ROOT"] = str(args.runtime_bin.resolve())

    from agent_comms.comms import wire
    from agent_comms.field_codec import FieldCodec

    def originals():
        service = wire()
        result = {"root": str(service.root), "threads": {}}
        for name in (args.thread, args.peer, "openhcs-helper"):
            thread = service.registry.require(name)
            paths = (Path(thread.session_file), Path(thread.session_file + ".input-proof"))
            result["threads"][name] = {
                "process": FieldCodec.encode(thread.process_identity),
                "files": [{"path": str(path), "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths],
            }
        for name in ("bus_meta.json", "input_dispositions.json"):
            path = service.root / name
            result[name] = {"bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return result

    before = originals()
    (base / "originals-before.json").write_text(json.dumps(before, indent=2) + "\n")
    sys.argv = [str(args.recorder), "--output", str(base / "capture"),
                "--owner", "parent-476-physical-recovery", "--capture-target", "existing_thread",
                "--journey", ChannelRecoveryJourney.declared_name, "--capture-state",
                "--review-timing", "deferred", "--fit-window", "--width", "1280",
                "--height", "900", "--startup-wait", "15", "--max-duration", "65",
                "--tail-seconds", "2", "--", "/home/ts/bin/toad-comms", args.thread]
    try:
        recorder.main()
    finally:
        after = originals()
        (base / "originals-after.json").write_text(json.dumps(after, indent=2) + "\n")
        (base / "preservation.json").write_text(json.dumps({"originals_unchanged": before == after}, indent=2) + "\n")
    if before != after:
        raise RuntimeError("Original authority changed during readonly capture; inspect preservation evidence")


if __name__ == "__main__":
    main()
