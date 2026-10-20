"""Exercise the real stall rule, Git receipts and messages on a copied isolated bus.

No agent process or provider is started. The original registry remains unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.standing import FlatWheelSpin
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CheckedCommit, CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import StallWatcherIncarnation
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsHistoryTool, CommsTodoCreateTool
from agent_comms.todos import (
    DistanceReading, FlatTrend, MechanicalOrderRun, MeasuredProgressSource,
    PendingProblemOrder, PresentCheckout, PullRequests,
)
from agent_comms.transcript_events import AssistantTranscript


@dataclass(frozen=True)
class StallReceipt:
    copied_root: str
    spare: bool
    holder: str
    strikes: int
    readings: tuple[DistanceReading, ...]
    message: str
    original_unchanged: bool


def git(directory: Path, *args: str) -> str:
    return subprocess.check_output(
        ("git", "-C", str(directory), "-c", "user.name=proof", "-c",
         "user.email=proof@localhost", *args), text=True).strip()


def prove(directory: Path, spare: bool) -> StallReceipt:
    directory.mkdir(parents=True)
    original = Comms(directory / "original")
    unit = directory / "unit"
    unit.mkdir()
    git(unit, "init", "-q", "-b", "main")
    (unit / "left").write_text("2\n")
    git(unit, "add", "left")
    git(unit, "commit", "-q", "-m", "baseline")
    git(unit, "remote", "add", "origin", ".")
    git(unit, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(unit, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(unit, "switch", "-q", "-c", "fix/proof")

    names = ("lead", "worker", "spare") if spare else ("lead", "worker")
    for name in names:
        original.registry.declare(original.registry.new_thread(
            name, frozenset({"comms"}), str(unit), execution=ExternalThreadExecution))
        original.goals.update_goal(name, SetGoalAction(text="Isolated proof brief"))
    original.goals.update_goal("lead", SetGoalAction(
        text="Demonstrate stall enforcement", goal_id="request", order=RequestGoalOrder()))
    config = CommanderConfig(
        commander="lead", root=original.root, cli=Path(sys.executable),
        state=directory / "state", board=directory / "board.md",
        narrative=directory / "narrative.md", repos=(), worktrees=directory / "wt",
        scratch=directory / "scratch", disk=directory)
    config.write(directory)
    check = OrderCheck(str(unit), "cat left", CountCheckResult(0))
    order = PendingProblemOrder(
        id="proof-stall-order", owner=FileLine(str(Path(__file__).resolve()), 1),
        run=MechanicalOrderRun(), check=check, edits=("Reduce the remaining count",),
        banned=("No competing owner",), report="Report the real count",
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))
    CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(
        problem="P1", repo="proof/repo", text="Repair the stalled work", order=order,
        request="request", commander="lead", deadline=RecordedEventTime(time.time() + 600),
    )).apply(original, ThreadCaller("lead"))
    for stage in ("first reading", "second reading"):
        git(unit, "commit", "-q", "--allow-empty", "-m", stage)
        run = asyncio.run(check.run())
        CheckedCommit(check.command, run.outcome, run.stdout.decode()).store(
            str(unit), git(unit, "rev-parse", "HEAD"))
    for name in names[1:]:
        original.transcripts.external(name).finish(
            turn_id="proof-turn", item_id="proof-answer", events=(AssistantTranscript(
                "The remaining distance has not fallen", position="answer", timestamp=time.time()),))

    before = original.goals.todos.get("P1")
    copied_root = directory / "copy"
    shutil.copytree(original.root, copied_root)
    copied = Comms(copied_root)
    source = MeasuredProgressSource(before, PresentCheckout(unit), PullRequests())
    readings = source.readings()
    trend = readings.trend
    if not isinstance(trend, FlatTrend):
        raise AssertionError(f"Expected a real flat Git/check trend, got {trend}")
    FlatWheelSpin(copied.goals.todos.get("P1"), trend.since).enforce(copied, "lead")
    after = copied.goals.todos.get("P1")
    expected = "spare" if spare else "worker"
    assert after.holder == expected
    assert len(after.strikes) == 1
    assert after.strikes[0].actor == StallWatcherIncarnation.watcher()
    assert len(after.released) == int(spare)
    page = CommsHistoryTool(peer="spare" if spare else "#comms").apply(
        copied, ThreadCaller("lead"))
    message = next(entry.body for entry in page.messages if "P1" in entry.body)
    assert ("STALL with no idle unit" in message) is not spare
    unchanged = original.goals.todos.get("P1") == before
    assert unchanged
    return StallReceipt(str(copied_root), spare, after.held().owner, len(after.strikes),
                        readings.readings, message, unchanged)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    for spare in (False, True):
        receipt = prove(directory / ("spare" if spare else "busy"), spare)
        print(json.dumps(FieldCodec.encode(receipt)))


if __name__ == "__main__":
    main()
