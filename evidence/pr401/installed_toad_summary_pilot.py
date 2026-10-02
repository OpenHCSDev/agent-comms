"""Display native Pi's captured ACP compaction publication in installed Toad."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import toad
from agent_comms.acp_extension import CompactionChangedUpdate, decode_updates
from agent_comms.agent_events import CompactionEnd, CompactionStart
from runtime_fixture import ToadApp
from toad.acp.agent import Agent
from toad.widgets.agent_response import AgentResponse


class InstalledToadApp(ToadApp):
    CSS_PATH = Path(toad.__file__).parent / "toad.tcss"


async def main() -> None:
    packets = json.loads(Path(os.environ["AC_PR401_CAPTURE_PACKET"]).read_text())
    events = [
        field.event
        for packet in packets
        for field in decode_updates(packet["_meta"])
        if isinstance(field, CompactionChangedUpdate)
    ]
    assert len(events) == 2
    assert isinstance(events[0], CompactionStart)
    assert isinstance(events[1], CompactionEnd)
    assert "PR401_COMMITTED_SUMMARY" in events[1].publication_summary

    with tempfile.TemporaryDirectory(prefix="pr401-toad-") as directory:
        root = Path(directory)
        os.environ.update(
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_DATA_HOME=str(root / "data"),
            XDG_STATE_HOME=str(root / "state"),
            AGENT_COMMS_ROOT=str(root / "wire"),
        )
        app = InstalledToadApp(project_dir=str(root))
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            view = app.selected_session.conversation
            agent = Agent(
                root,
                {
                    "name": "PR401 fixture",
                    "identity": "pr401-fixture",
                    "short_name": "pr401-fixture",
                    "run_command": {"*": "true"},
                    "protocol": "acp",
                },
                "fixture",
            )
            agent.attach_surface(view)
            view.agent = agent
            for packet in packets:
                agent.updates.accept("fixture", packet)
                await pilot.pause()

            async with asyncio.timeout(8):
                while True:
                    notices = [
                        item
                        for item in view.contents.children
                        if isinstance(item, AgentResponse)
                        and "## Context compacted" in item.source
                    ]
                    if len(notices) == 1 and "PR401_COMMITTED_SUMMARY" in notices[0].source:
                        break
                    await pilot.pause(0.05)
            assert "Context estimate unavailable" not in notices[0].source
            assert events[1].publication_summary in notices[0].source

            async with asyncio.timeout(8):
                while True:
                    view.window.scroll_end(animate=False, immediate=True)
                    await pilot.pause(0.05)
                    frame = "\n".join(
                        strip.text for strip in app.screen._compositor.render_strips()
                    )
                    if "PR401_COMMITTED_SUMMARY" in frame:
                        break
            assert app._exception is None
        await asyncio.get_running_loop().shutdown_default_executor()
    print(f"installed Toad ACP/UI: committed native summary visible once ({toad.__file__})")


if __name__ == "__main__":
    asyncio.run(main())
