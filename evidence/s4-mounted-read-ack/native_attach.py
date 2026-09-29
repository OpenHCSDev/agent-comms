"""Real installed ACP/native attachment and painted ACK on copied retained source."""

import asyncio
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from toad.app import ToadApp
from toad.widgets.transcript_history import TranscriptHistory

from agent_comms.comms import wire
from agent_comms.native_package import verify_native_package


async def main():
    original = wire().registry.require("nra-architecture")
    original_path = Path(original.session_file)
    original_stat = original_path.stat()
    package = Path(
        "/home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/"
        "node_modules/@earendil-works/pi-coding-agent"
    )
    verify_native_package(package)
    with TemporaryDirectory(
        dir=Path(__file__).resolve().parents[2] / ".artifacts", prefix="s4-actual-acp-"
    ) as directory:
        root = Path(directory)
        source = root / "retained.jsonl"
        shutil.copyfile(original_path, source)
        comms = wire(root / "wire")
        root_id = comms.messaging.initialize_private_initial_protocol()
        os.environ.update(
            AGENT_COMMS_ROOT=str(comms.root),
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
            AGENT_COMMS_NATIVE_CONFIG_DIR=str(root / "native-config"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_DATA_HOME=str(root / "data"),
        )
        comms.threads.register(
            replace(
                original,
                worktree=str(root),
                session_file=str(source),
                process_identity=None,
                active_turn=None,
                goal=None,
            )
        )
        agent = {
            "identity": "s4-native-ack",
            "name": "Agent Comms",
            "short_name": "agent",
            "protocol": "acp",
            "type": "coding",
            "run_command": {"*": f"{sys.executable} -m agent_comms.acp"},
            "actions": {},
        }
        app = ToadApp(agent_data=agent, project_dir=str(root), agent_session_id=original.name)
        try:
            async with app.run_test(size=(120, 42)) as pilot:
                await app.screen.wait_content_ready()
                async with asyncio.timeout(40):
                    while not (
                        app.screen.conversation.agent_ready
                        and any(
                            any(p.page.events for p in h.pages)
                            for h in app.screen.conversation.query(TranscriptHistory)
                        )
                    ):
                        await pilot.pause(0.05)
                view = app.screen.conversation
                print(
                    json.dumps(
                        {
                            "stage": "attached",
                            "histories": [
                                {
                                    "height": h.region.height,
                                    "pages": len(h.pages),
                                    "events": sum(len(p.page.events) for p in h.pages),
                                }
                                for h in view.query(TranscriptHistory)
                            ],
                            "page_events": len(
                                comms.transcripts.thread_transcript_page(original.name).events
                            ),
                        }
                    ),
                    flush=True,
                )
                view.window.scroll_end(animate=False, immediate=True)
                async with asyncio.timeout(20):
                    while not comms.bus.reads.read().transcripts:
                        await pilot.pause(0.05)
                history = next(
                    h for h in view.query(TranscriptHistory) if any(p.page.events for p in h.pages)
                )
                async with asyncio.timeout(15):
                    while not (
                        history.region.height > 0
                        and history.region.overlaps(view.window.content_region)
                    ):
                        view.window.scroll_end(animate=False, immediate=True)
                        await pilot.pause(0.05)
                await pilot.pause(0.2)
                region = view.window.content_region
                paint = "\n".join(
                    strip.crop(region.x, region.right).text
                    for strip in app.screen._compositor.render_strips()[region.y : region.bottom]
                )
                history = view.query_one(TranscriptHistory)
                assert history.region.height > 0 and history.region.overlaps(region)
                assert len(paint.strip()) > 80
                assert comms.bus.reads.read().transcripts
                assert app._exception is None
                result = {
                    "source_owner": original.name,
                    "copied_bytes": original_stat.st_size,
                    "actual_acp_ready": True,
                    "history_height": history.region.height,
                    "painted_characters": len(paint.strip()),
                    "prompts_sent": 0,
                    "durable_native_read": True,
                }
                print(json.dumps(result), flush=True)
        finally:
            for thread in comms.registry.all_threads().values():
                if thread.process_alive:
                    comms.owners.stop(thread.name)
            await asyncio.get_running_loop().shutdown_default_executor()
        assert original_path.stat().st_size == original_stat.st_size
        assert original_path.stat().st_mtime_ns == original_stat.st_mtime_ns


if __name__ == "__main__":
    asyncio.run(main())
