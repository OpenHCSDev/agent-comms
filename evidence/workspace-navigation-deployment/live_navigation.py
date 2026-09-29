"""Real installed UI clicks over the actual wire, without sending user input.

Use the normal application: fixture teardown must never stop live owners.
Before activation, ACP_COMMAND selects the existing installed ACP attachment.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from agent_comms.comms import wire
from toad.app import ToadApp
from toad.widgets.channels_sidebar import ChannelsSidebar
from toad.widgets.comms_chat import CommsChatView
from toad.widgets.comms_sidebar import ChannelGroup, CommsSidebar
from toad.widgets.transcript_history import TranscriptHistory


async def until(pilot, predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await pilot.pause(.05)


def paint(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


async def main():
    comms = wire()
    owner = comms.registry.require("pr95-selected-pi-summary-owner")
    scope = Path(__file__).resolve().parent
    data = {"identity": "live-navigation-check", "name": "Agent Comms",
            "short_name": "agent", "protocol": "acp", "type": "coding",
            "run_command": {"*": os.environ.get("ACP_COMMAND", f"{sys.executable} -m agent_comms.acp")},
            "actions": {}}
    before = (comms.root / "bus.jsonl").stat().st_size
    with TemporaryDirectory(prefix="live-navigation-", dir=scope.parents[1]/".artifacts") as directory:
        for name, leaf in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
            os.environ[name] = str(Path(directory)/leaf)
        app = ToadApp(agent_data=data, project_dir=owner.worktree, agent_session_id=owner.name)
        report = {"prompts_sent": 0, "owners_stopped": 0, "channel_clicks": []}
        async with app.run_test(size=(165, 40)) as pilot:
            try:
                await app.selected_session.wait_content_ready()
                source = app.selected_session
                await until(pilot, lambda: source.conversation.agent_ready and bool(source.query(TranscriptHistory)))
                original_mode = app.selected_mode
                editor = source.conversation.prompt.prompt_text_area
                source.conversation.prompt.text = "UNSENT_NAVIGATION_DRAFT"
                await pilot.pause()
                document, undo = editor.document, editor.history
                app.screen.query_one(ChannelsSidebar).reveal()
                for name in ("#comms", "#nra", "#openhcs"):
                    roster = app.screen.query_one(CommsSidebar)
                    await until(pilot, lambda: roster.navigation_ready.is_set() and any(
                        group.row.target_name == name for group in roster.query(ChannelGroup)))
                    group = next(g for g in roster.query(ChannelGroup) if g.row.target_name == name)
                    group.row.scroll_visible(animate=False, immediate=True)
                    await pilot.pause()
                    assert await pilot.click(group.row), f"Actual channel click missed {name}"
                    await until(pilot, lambda: app.selected_mode != original_mode)
                    await app.selected_session.wait_content_ready()
                    chat = app.selected_session.query_one(CommsChatView)
                    await until(pilot, lambda: (chat._history_initialized and bool(chat._history)) or "Wire error:" in chat.status)
                    assert "Wire error:" not in chat.status, chat.status
                    assert chat._history, f"No saved rows for {name}"
                    assert app._exception is None, app._exception
                    report["channel_clicks"].append({"name": name, "saved_rows": len(chat._history), "history_initialized": True})
                    await app.select_session(original_mode)
                    await until(pilot, lambda: source.conversation.agent_ready)
                    assert source.conversation.prompt.text == "UNSENT_NAVIGATION_DRAFT"
                    assert editor.document is document and editor.history is undo
                roster = app.screen.query_one(CommsSidebar)
                group = next(g for g in roster.query(ChannelGroup) if g.row.target_name == "#comms")
                if not group.expanded:
                    group.toggle_members()
                await until(pilot, lambda: "agent-comms-ux" in group._members)
                member = group._members["agent-comms-ux"]
                member.scroll_visible(animate=False, immediate=True)
                await pilot.pause()
                count = len(app.open_tabs)
                assert await pilot.click(member), "Actual participant click missed"
                await until(pilot, lambda: app.selected_mode != original_mode)
                await app.selected_session.wait_content_ready()
                child = app.selected_session
                await until(pilot, lambda: child.conversation.agent_ready and bool(child.query(TranscriptHistory)))
                assert len(app.open_tabs) == count+1
                assert all(not tab.mode_name.startswith("pending-") for tab in app.open_tabs)
                assert child._comms_thread == "agent-comms-ux"
                report.update(participant_click="agent-comms-ux", one_new_logical_tab=True,
                              saved_native_history=True, recent_return_draft_document_undo=True,
                              exception=repr(app._exception))
                assert app._exception is None
                print(json.dumps(report), flush=True)
            finally:
                app.save_screenshot("live-navigation.svg", path=str(scope))
                scope.joinpath("live-navigation.json").write_text(json.dumps(report, indent=2)+"\n")
    report["bus_size_before"] = before
    report["bus_size_after"] = (comms.root/"bus.jsonl").stat().st_size
    scope.joinpath("live-navigation.json").write_text(json.dumps(report, indent=2)+"\n")


if __name__ == "__main__":
    asyncio.run(main())
