"""Installed mounted DM receipt follows actual compositor paint, not fetch."""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from toad.app import ToadApp
from toad.navigation_target import DirectTarget
from toad.widgets.comms_chat import CommsChatView

from agent_comms.comms import wire
from agent_comms.threads import Thread


async def until(pilot, predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await pilot.pause(0.02)


async def main():
    with TemporaryDirectory(
        dir=Path(__file__).resolve().parents[2] / ".artifacts", prefix="s4-dm-"
    ) as directory:
        root = Path(directory)
        os.environ.update(
            AGENT_COMMS_ROOT=str(root / "wire"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_DATA_HOME=str(root / "data"),
        )
        comms = wire(root / "wire")
        comms.threads.register(Thread("peer", frozenset(), str(root)))
        viewer = comms.messaging.user_identity(str(root)).name
        first = comms.messaging.send_message("peer", viewer, "FIRST_DM_PAINT_RECEIPT")
        app = ToadApp(project_dir=str(root))
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            owner = app.current_mode
            mode = await app.open_comms_session(
                owner_mode=owner, project_path=root, me=viewer, target=DirectTarget("peer")
            )
            chat = app.screen.query_one(CommsChatView)
            await until(pilot, lambda: comms.bus.pending_count(viewer, "peer") == 0)
            await pilot.pause(0.1)
            region = chat.window.content_region
            strips = app.screen._compositor.render_strips()
            paint = "\n".join(
                strip.crop(region.x, region.right).text
                for strip in strips[region.y : region.bottom]
            )
            assert "FIRST_DM_PAINT_RECEIPT" in paint
            assert first.seq in {seq for source, seq in chat._painted_message_keys() if not source}
            await app.switch_mode(owner)
            late = comms.messaging.send_message("peer", viewer, "HIDDEN_DM_NOT_ACKED")
            comms.views.dm_display_page("peer", worktree=str(root))
            await pilot.pause(0.2)
            assert comms.bus.pending_count(viewer, "peer") == 1
            assert late.seq not in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
            await app.switch_mode(mode)
            await until(pilot, lambda: comms.bus.pending_count(viewer, "peer") == 0)
            assert late.seq in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
            assert app._exception is None
        await asyncio.get_running_loop().shutdown_default_executor()
    print("DM actual paint ACK, hidden fetch without ACK, and refocus ACK passed")


if __name__ == "__main__":
    asyncio.run(main())
