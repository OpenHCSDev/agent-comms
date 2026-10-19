"""Physical Toad journey on a scratch bus, using copies of real saved unit sessions.

Run --prepare once, then run this file inside st on an isolated Xvfb display.
No provider is started, no synthetic transcript is written, and the original
sessions/registry are only read. Source and capture paths are recorded explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from agent_comms.active_route import LocalRoute
from agent_comms.change_report import FileLine
from agent_comms.child_process import NoProcess
from agent_comms.comms import Comms
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, NoGoal, OrderCheck, RequestGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import MechanicalOrderRun, PendingProblemOrder
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsTodoCreateTool

COMMANDER = "claude-code"
UNITS = ("pi-cn-phase2", "pi-cn-phase5")


def prepare(output: Path) -> None:
    live = Comms(LocalRoute.default())
    root = output / "bus"
    if root.exists():
        raise ValueError("Scratch bus already exists; preserve it and choose another output path.")
    root.mkdir(parents=True)
    comms = Comms(root)
    receipts = []
    for name in (COMMANDER, *UNITS):
        original = live.registry.require(name)
        session = original.session_file
        if session:
            source = Path(session)
            # Freeze a complete-line cut of the real saved session, not manufactured events.
            with source.open("rb") as stream:
                captured = stream.read(os.fstat(stream.fileno()).st_size)
            copied = output / f"{name}.jsonl"
            copied.write_bytes(captured.rsplit(b"\n", 1)[0] + b"\n")
            copied.chmod(0o600)
            session = str(copied)
            receipts.append(f"{name}: {source} -> {copied} ({copied.stat().st_size} bytes)")
        comms.registry.declare(replace(original, process_identity=NoProcess(),
                                      session_file=session, goal=NoGoal()), StoppedThreadStatus())
    comms.goals.update_goal(COMMANDER, SetGoalAction(
        text="Verify the commander page on copied real unit sessions", goal_id="page-request",
        order=RequestGoalOrder()))
    now = time.time()
    for index, name in enumerate(UNITS):
        comms.goals.update_goal(name, SetGoalAction(text="Copied unit for read-only page verification"))
        order = PendingProblemOrder(
            id=f"commander-page-{name}", owner=FileLine(__file__, 1),
            check=OrderCheck(str(output), "printf 1", CountCheckResult(1)),
            edits=("Render the canonical order owner without copying its state",),
            banned=("Do not launch a provider or change the original saved session",),
            report="Report the actual visible commander page and its expanded unit card",
            at=RecordedEventTime(now - 1200 if not index else now),
            first_call_deadline=RecordedEventTime(now + 120), run=MechanicalOrderRun())
        CommsTodoCreateTool(owner=name, order=CreateProblemGoalAction(
            problem=f"P{index + 1}", order=order, request="page-request",
            deadline=RecordedEventTime(now - 60 if not index else now + 1800), commander=COMMANDER,
            repo="OpenHCSDev/agent-comms", text="Show the real order in the commander page",
        )).apply(comms, ThreadCaller(COMMANDER))
    (output / "sources.txt").write_text("\n".join(receipts) + "\n")


async def journey(app, pilot, output: Path) -> None:
    from textual.widgets import Static
    from toad.navigation_target import FeedTarget
    from toad.screens.commander import CommanderScreen, CommanderUnit
    from toad.widgets.conversation import Conversation

    await pilot.pause(2)
    # Open a real bus conversation and submit through its actual local-command boundary.
    await app.screen.open_sidebar_target(FeedTarget())
    conversation = app.screen.query_one(Conversation)
    await pilot.pause(1)
    conversation.prompt.text = "/commander " + COMMANDER
    conversation.prompt.focus()
    await pilot.press("enter")
    async with asyncio.timeout(90):
        while not isinstance(app.screen, CommanderScreen) or len(app.screen.rows) != len(UNITS):
            await pilot.pause(0.2)
    screen = app.screen
    await pilot.pause(1)
    def capture(stage: str) -> None:
        # The text is from the actual laid-out terminal strips, not the model.
        text = "\n".join(screen.render_line(y).text for y in range(screen.size.height))
        (output / f"{stage}.txt").write_text(text + "\n")
        subprocess.run(("import", "-window", "root", str(output / f"{stage}.png")), check=True)
    capture("level-0")
    card = next(row for row in screen.query(CommanderUnit) if row.unit.name == UNITS[0])
    await pilot.click(card.query_one("CollapsibleTitle"))
    async with asyncio.timeout(90):
        while "Latest checkout commits:" not in str(card.query_one(Static).content):
            await pilot.pause(0.2)
    await pilot.pause(1)
    capture("level-1")
    # Verify collapse/return does not replace the underlying conversation or its draft.
    await pilot.press("escape")
    assert app.screen.query_one(Conversation) is conversation
    (output / "result.txt").write_text("PASS: normal Toad, real scratch orders, level 0 and expanded card.\n")
    app.exit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        prepare(args.output)
        return
    os.environ["AGENT_COMMS_ROOT"] = str(args.output / "bus")
    from toad.app import ToadApp
    app = ToadApp(project_dir=str(args.output))
    app.run(auto_pilot=lambda pilot: journey(app, pilot, args.output))
    if app._exception:
        raise app._exception


if __name__ == "__main__":
    main()
