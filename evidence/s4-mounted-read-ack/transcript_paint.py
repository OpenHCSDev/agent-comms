"""Actual native-file snapshot publication and mounted human read receipt."""

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from toad.acp.agent import Agent
from toad.acp.messages import CommsUpdated
from toad.app import ToadApp
from toad.navigation_target import channel_target

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates, encode_updates
from agent_comms.comms import wire
from agent_comms.threads import Thread


async def until(pilot, predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await pilot.pause(0.02)


def append(source, text):
    with source.open("a") as stream:
        stream.write(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": text}})
            + "\n"
        )


async def main():
    with TemporaryDirectory(
        dir=Path(__file__).resolve().parents[2] / ".artifacts", prefix="s4-native-read-"
    ) as directory:
        root = Path(directory)
        os.environ.update(
            AGENT_COMMS_ROOT=str(root / "wire"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_DATA_HOME=str(root / "data"),
        )
        source = root / "native.jsonl"
        append(source, "SAVED_NATIVE_READ_PAINT " + "visible line\n" * 20)
        comms = wire(root / "wire")
        comms.threads.register(
            Thread("worker", frozenset({"team"}), str(root), session_file=str(source))
        )
        app = ToadApp(project_dir=str(root))
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            owner, screen = app.current_mode, app.screen
            screen.initial_coordination_root = str(comms.root)
            screen._comms_thread = "worker"
            view = screen.conversation
            view.agent = Agent(
                root,
                {
                    "name": "Read acceptance",
                    "identity": "read-acceptance",
                    "short_name": "read",
                    "protocol": "acp",
                    "run_command": {"*": "python -m agent_comms.acp"},
                },
                None,
            )
            view.post_message(
                CommsUpdated(
                    decode_updates(
                        encode_updates(
                            TranscriptSnapshotUpdate(
                                comms.transcripts.thread_transcript_page("worker")
                            )
                        )
                    )[0]
                )
            )
            await until(
                pilot, lambda: comms.views.viewer_snapshot(str(root)).thread_unread["worker"] == 0
            )
            region = view.window.content_region
            strips = app.screen._compositor.render_strips()
            paint = "\n".join(
                strip.crop(region.x, region.right).text
                for strip in strips[region.y : region.bottom]
            )
            assert "SAVED_NATIVE_READ_PAINT" in paint
            await app.open_comms_session(
                owner_mode=owner, project_path=root, me="worker", target=channel_target("#team")
            )
            append(source, "OFFSCREEN_NATIVE_REPLY")
            view.post_message(
                CommsUpdated(
                    decode_updates(
                        encode_updates(
                            TranscriptSnapshotUpdate(
                                comms.transcripts.thread_transcript_page("worker")
                            )
                        )
                    )[0]
                )
            )
            await pilot.pause(0.3)
            assert comms.views.viewer_snapshot(str(root)).thread_unread["worker"] == 1
            await app.switch_mode(owner)
            view.window.scroll_end(animate=False, immediate=True)
            await until(
                pilot, lambda: comms.views.viewer_snapshot(str(root)).thread_unread["worker"] == 0
            )
            assert app._exception is None
        await asyncio.get_running_loop().shutdown_default_executor()
    print("native snapshot actual paint ACK, offscreen publication without ACK, refocus ACK passed")


if __name__ == "__main__":
    asyncio.run(main())
