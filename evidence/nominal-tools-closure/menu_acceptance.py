"""Mounted installed Toad right-click -> derived catalog -> real core stop."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from runtime_fixture import ToadApp
from toad.widgets.comms_menu import ContextMenuItem
from toad.widgets.comms_sidebar import ChannelGroup, CommsSidebar

from agent_comms.comms import wire
from agent_comms.threads import Thread

TREE = Path(__file__).resolve().parents[2]


async def main():
    with tempfile.TemporaryDirectory(dir=TREE / ".artifacts", prefix="nominal-menu-") as directory:
        root = Path(directory)
        os.environ.update(
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_DATA_HOME=str(root / "data"),
            AGENT_COMMS_ROOT=str(root / "wire"),
        )
        comms = wire(root / "wire")
        comms.threads.register(Thread("sender", frozenset({"tools"}), str(root)))
        app = ToadApp(project_dir=str(root))
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            sidebar = app.screen.query_one(CommsSidebar)
            await sidebar.sync_sessions()
            group = next(
                group for group in sidebar.query(ChannelGroup) if group.row.target_name == "#tools"
            )
            if not group.expanded:
                group.toggle_members()
            await pilot.pause()
            row = group._members["sender"]
            row.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(row, button=3)
            await pilot.pause()
            item = next(
                item for item in app.screen.query(ContextMenuItem) if item.action == "comms_stop"
            )
            assert await pilot.click(item)
            async with asyncio.timeout(10):
                while not comms.registry.status("sender").stopped:
                    await pilot.pause(0.03)
            assert app._exception is None
            receipt = {
                "action": "comms_stop",
                "subject": "sender",
                "stopped": True,
                "installed_toad": __import__("toad").__file__,
                "installed_core": __import__("agent_comms").__file__,
                "mocked_calls": False,
            }
            (TREE / "evidence/nominal-tools-closure/menu-receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
            print(json.dumps(receipt))
        await asyncio.get_running_loop().shutdown_default_executor()


if __name__ == "__main__":
    asyncio.run(main())
