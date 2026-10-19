"""Run real tools on a scratch bus, reading a copy of an actual native session.

No authored transcript events, provider input, live-bus writes or saved-input
replay. Run setup, read order_story.py in the worker, then run finish.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import OrderCheck, RequestGoalOrder
from agent_comms.order_story import OrderStory
from agent_comms.order_work import OrderWork
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    AcceptedVerdict,
    MechanicalOrderRun,
    OperationDelivery,
    PendingProblemOrder,
    SendBackVerdict,
    StrikeReason,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import (
    CommsFleetStoryTool,
    CommsOrderDoneTool,
    CommsOrderStoryTool,
    CommsTodoCreateTool,
    CommsVerdictTool,
)


@dataclass(frozen=True)
class Journey:
    scratch: Path
    session: Path
    project: Path
    worker: str

    @property
    def comms(self) -> Comms:
        return Comms(self.scratch / "bus")

    @property
    def caller(self) -> ThreadCaller:
        return ThreadCaller("lead")

    @property
    def receipt(self) -> Path:
        return self.scratch / "lifecycle.md"

    def copy_session(self) -> None:
        self.scratch.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.session, self.scratch / "worker.jsonl")

    def show(self, stage: str) -> OrderStory:
        story = CommsOrderStoryTool(problem="P1").apply(self.comms, self.caller)
        fleet = CommsFleetStoryTool().apply(self.comms, self.caller)
        with self.receipt.open("a") as report:
            report.write(f"\n## {stage}\n\n```json\n{json.dumps(story.to_wire(), indent=2)}\n```\n")
            report.write(f"\nFleet:\n```json\n{json.dumps(fleet.to_wire(), indent=2)}\n```\n")
        print(stage, json.dumps(story.to_wire()), flush=True)
        return story

    def work(self) -> OrderWork:
        todo = self.comms.goals.todos.get("P1")
        return OrderWork.for_assigned(
            self.comms, todo.id, todo.held().owner, todo.order.issued().at.recorded_at("An order")
        )


class JourneyStep(DeclaredFamily, affix="Step"):
    @abstractmethod
    def run(self, journey: Journey) -> None: ...


class SetupStep(JourneyStep):
    def run(self, journey: Journey) -> None:
        comms = journey.comms
        if comms.registry.store.path.exists():
            raise ValueError("Journey exists: inspect it instead of replaying setup")
        journey.copy_session()
        comms.registry.declare(comms.registry.new_thread("lead", frozenset(), str(journey.project)))
        comms.registry.declare(
            comms.registry.new_thread(
                journey.worker,
                frozenset(),
                str(journey.project),
                session_file=str(journey.scratch / "worker.jsonl"),
            )
        )
        comms.goals.update_goal(
            "lead",
            SetGoalAction(
                text="Verify native stories from a real scratch order",
                goal_id="request",
                order=RequestGoalOrder(),
            ),
        )
        now = time.time()
        ordered = PendingProblemOrder(
            id="phase5-live-order",
            owner=FileLine("src/agent_comms/order_story.py", 1),
            check=OrderCheck(str(journey.project), f"test -s '{journey.receipt}'"),
            edits=("Read src/agent_comms/order_story.py:1 and describe the story output",),
            banned=("Do not change the original session or replay any saved input",),
            report="Report the order story and the remaining delivery gaps",
            at=RecordedEventTime(now),
            first_call_deadline=RecordedEventTime(now + 300),
            run=MechanicalOrderRun(),
        )
        action = CreateProblemGoalAction(
            problem="P1",
            order=ordered,
            request="request",
            deadline=RecordedEventTime(now + 3600),
            commander="lead",
            repo="OpenHCSDev/agent-comms",
            text="Read the native story owner",
        )
        CommsTodoCreateTool(owner=journey.worker, order=action).apply(comms, journey.caller)
        journey.show("Ordered")


class FinishStep(JourneyStep):
    def run(self, journey: Journey) -> None:
        journey.copy_session()
        work = journey.work()
        read = next(call for call in work.calls if "order_story.py" in call.digest)
        journey.show("Worked: actual saved native read")
        todo = journey.comms.goals.todos.get("P1")
        accepted = AcceptedVerdict(
            todo.revision,
            journey.comms.registry.require("lead").incarnation,
            todo.held(),
            read.tool_call_id,
            "and describe the story output",
        )
        CommsVerdictTool(id=todo.id, operation=accepted).apply(journey.comms, journey.caller)
        journey.show("Judged: accepted")
        AfterAcceptanceStep().run(journey)


class AfterAcceptanceStep(JourneyStep):
    def run(self, journey: Journey) -> None:
        work = journey.work()
        read = next(call for call in work.calls if "order_story.py" in call.digest)
        comms = journey.comms
        todo = comms.goals.todos.get("P1")
        back = SendBackVerdict(
            todo.revision,
            comms.registry.require("lead").incarnation,
            todo.held(),
            read.tool_call_id,
            StrikeReason(
                "The unit read the file but did not show the channel "
                "receiving the send back exactly once"
            ),
        )
        CommsVerdictTool(id=todo.id, operation=back).apply(comms, journey.caller)
        journey.show("Judged: sent back and struck")
        CommsOrderDoneTool(
            problem=todo.id,
            order_id=todo.order.issued().id,
            delivery=OperationDelivery(),
            claims=(WorkingClaim(str(journey.receipt)),),
        ).apply(comms, journey.caller)
        journey.show("Delivered: operation check passed")
        before = comms.goals.todos.path.stat().st_mtime_ns
        assert journey.show("Read again") == journey.show("Read once more")
        assert comms.goals.todos.path.stat().st_mtime_ns == before
        print("Read-only stories left the Todo store unchanged", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", type=JourneyStep.member_named)
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path.home() / ".cache/agent-scratch/pi-cn-phase5/journey",
    )
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--worker", default="pi-cn-phase5")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    arguments.step().run(
        Journey(arguments.scratch, arguments.session, arguments.project, arguments.worker)
    )


if __name__ == "__main__":
    main()
