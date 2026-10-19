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

from textual.pilot import Pilot
from toad.app import ToadApp
from toad.navigation_target import FeedTarget
from toad.screens.commander import CommanderScreen, CommanderUnit
from toad.widgets.conversation import Conversation

from agent_comms.active_route import LocalRoute
from agent_comms.change_report import FileLine
from agent_comms.child_process import NoProcess
from agent_comms.comms import Comms
from agent_comms.errors import ToolRefusedError
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, NoGoal, OrderCheck, RequestGoalOrder
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import MechanicalOrderRun, OperationDelivery, PendingProblemOrder
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsOrderDoneTool, CommsSendTool, CommsTodoCreateTool

COMMANDER = "claude-code"
UNITS = ("pi-cn-phase2", "pi-cn-phase5")


def prepare(output: Path, units: tuple[str, ...], ordered_at: float) -> None:
    live = Comms(LocalRoute.default())
    root = output / "bus"
    if root.exists():
        raise ValueError("Scratch bus already exists; preserve it and choose another output path.")
    root.mkdir(parents=True)
    comms = Comms(root)
    source_notes = []
    for name in (COMMANDER, *units):
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
            source_notes.append(f"{name}: {source} -> {copied} ({copied.stat().st_size} bytes)")
        comms.registry.declare(
            replace(original, process_identity=NoProcess(), session_file=session, goal=NoGoal()),
            StoppedThreadStatus(),
        )
    comms.goals.update_goal(
        COMMANDER,
        SetGoalAction(
            text="Verify the commander page on copied real unit sessions",
            goal_id="page-request",
            order=RequestGoalOrder(),
        ),
    )
    now = time.time()
    for index, name in enumerate(units):
        comms.goals.update_goal(
            name, SetGoalAction(text="Copied unit for read-only page verification")
        )
        order = PendingProblemOrder(
            id=f"commander-page-{name}",
            owner=FileLine(__file__, 1),
            check=OrderCheck(str(output), "printf 1", CountCheckResult(1)),
            edits=("Render the canonical order owner without copying its state",),
            banned=("Do not launch a provider or change the original saved session",),
            report="Report the actual visible commander page and its expanded unit card",
            at=RecordedEventTime(ordered_at if not index else now),
            first_call_deadline=RecordedEventTime(now + 120),
            run=MechanicalOrderRun(),
        )
        CommsTodoCreateTool(
            owner=name,
            order=CreateProblemGoalAction(
                problem=f"P{index + 1}",
                order=order,
                request="page-request",
                deadline=RecordedEventTime(now - 60 if not index else now + 1800),
                commander=COMMANDER,
                repo="OpenHCSDev/agent-comms",
                text="Show the real order in the commander page",
            ),
        ).apply(comms, ThreadCaller(COMMANDER))
    try:
        CommsSendTool(
            sender=COMMANDER,
            target="unregistered-page-target",
            body="Verify that the commander can read an actual refused send.",
        ).apply(comms, ThreadCaller(COMMANDER))
    except ToolRefusedError as error:
        source_notes.append(f"Actual refused send: {error}")
    else:
        raise AssertionError("The deliberately absent target unexpectedly accepted a send.")
    (output / "sources.txt").write_text("\n".join(source_notes) + "\n")


async def journey(
    app: ToadApp, pilot: Pilot, output: Path, units: tuple[str, ...], momentum: bool
) -> None:
    await pilot.pause(2)
    # Open a real bus conversation and submit through its actual local-command boundary.
    await app.selected_session.open_sidebar_target(FeedTarget())
    conversation = app.selected_session.query_one(Conversation)
    await pilot.pause(1)
    conversation.prompt.text = "/commander " + COMMANDER
    conversation.prompt.focus()
    await pilot.press("enter")
    async with asyncio.timeout(90):
        while not isinstance(app.screen, CommanderScreen) or len(app.screen.rows) != len(units):
            await pilot.pause(0.2)
    screen = app.screen
    await pilot.pause(1)

    def capture(stage: str) -> None:
        # The text is from the actual laid-out terminal strips, not the model.
        text = "\n".join(strip.text for strip in screen._compositor.render_strips())
        (output / f"{stage}.txt").write_text(text + "\n")
        subprocess.run(("import", "-window", "root", str(output / f"{stage}.png")), check=True)

    capture("level-0")
    card = next(row for row in screen.query(CommanderUnit) if row.unit.name == units[0])
    if momentum:
        assert "m/commit; next commit" in str(card.momentum_widget.content)
        assert "completion unknown" in str(card.momentum_widget.content)
    await pilot.click(card.query_one("CollapsibleTitle"))
    async with asyncio.timeout(90):
        while "Latest checkout commits:" not in str(card.detail_widget.content):
            await pilot.pause(0.2)
    await pilot.pause(1)
    assert units[0] in str(card.title)
    if len(units) > 1:
        assert screen.rows[next(unit for unit in screen.rows if unit.name == units[1])].collapsed
    capture("level-1")

    def deliver() -> None:
        comms = Comms(output / "bus")
        todo = comms.goals.todos.get("P1")
        assert isinstance(todo.order, PendingProblemOrder)
        CommsOrderDoneTool(
            problem=todo.id,
            order_id=todo.order.id,
            delivery=OperationDelivery(),
            claims=(WorkingClaim(str(output / "level-0.png")),),
        ).apply(comms, ThreadCaller(COMMANDER))

    await app.preparation.run_thread(deliver)
    async with asyncio.timeout(90):
        while "Delivered, waiting on install" not in str(card.title):
            await pilot.pause(0.2)
    await pilot.pause(0.5)
    if momentum:
        assert "order delivered" in str(card.momentum_widget.content)
    capture("live-delivery")
    # Verify collapse/return does not replace the underlying conversation or its draft.
    await pilot.press("escape")
    assert app.selected_session.query_one(Conversation) is conversation
    (output / "result.txt").write_text(
        "PASS: normal Toad, real scratch orders, level 0, expanded real turns, "
        "and live delivery update without reopening.\n"
    )
    app.exit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--units", nargs="+", default=UNITS)
    parser.add_argument("--ordered-at", type=float, default=time.time() - 1200)
    parser.add_argument("--momentum", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        prepare(args.output, tuple(args.units), args.ordered_at)
        return
    os.environ.update(AGENT_COMMS_ROOT=str(args.output / "bus"))
    app = ToadApp(project_dir=str(args.output))
    app.run(
        auto_pilot=lambda pilot: journey(app, pilot, args.output, tuple(args.units), args.momentum)
    )
    if app._exception:
        raise app._exception


if __name__ == "__main__":
    main()
