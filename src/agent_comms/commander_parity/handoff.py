"""Actual current/native refusals to transfer delivered checked work."""

from __future__ import annotations

import contextlib
import io
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from ..change_report import FileLine
from ..child_process import ChildOutcome, ChildResult, ExitedOutcome
from ..commander.config import CommanderConfig
from ..commander.order import OrderFile
from ..recorded_time import RecordedEventTime
from ..field_codec import FieldCodec
from ..goal_actions import OrderGoalAction
from ..tools import CommsOrderTool
from . import ParityComparison, UnverifiedParity
from .lifecycle import LifecycleRun, MeasuredCommander


@dataclass(frozen=True)
class HandoffComparison(ParityComparison):
    cases: tuple[
        tuple[str, ChildOutcome, ChildOutcome, tuple[str, ...], tuple[str, ...], bool], ...
    ]

    @property
    def passed(self) -> bool:
        return len(self.cases) == 2 and all(
            all(
                (
                    expected == ExitedOutcome(1),
                    actual == ExitedOutcome(1),
                    bool(reference),
                    reference == refusal,
                    unchanged,
                )
            )
            for _, expected, actual, reference, refusal, unchanged in self.cases
        )

    def describe(self) -> str:
        return f"actual finished-order transfer refusals (current/native): {self.cases!r}"


class FinishedHandoffCase:
    @staticmethod
    def state(run: LifecycleRun, pid: str, receiver: str) -> str:
        return json.dumps(
            FieldCodec.encode(
                (
                    run.commander.comms.goals.todos.get(pid),
                    run.commander.comms.registry.require(run.worker).goal,
                    run.commander.comms.registry.require(receiver).goal,
                )
            ),
            sort_keys=True,
        )

    @staticmethod
    def current(run: LifecycleRun, receiver: str, document: OrderFile, pid: str) -> ChildResult:
        return run.invoke(
            "import sys; from agent_comms.commander.order import Commander, OrderFile; "
            "Commander.model_validate_json(sys.argv[1]).send(sys.argv[2], "
            "OrderFile.read(sys.argv[3]), reassign=sys.argv[4], traced=sys.argv[5])",
            run.commander.model_dump_json(),
            receiver,
            json.dumps(FieldCodec.encode(document)),
            "Compare the actual refusal to hand completed checked work to another real unit.",
            f"{Path(__file__).resolve()}:1",
        )

    @staticmethod
    def native(run: LifecycleRun, receiver: str, document: OrderFile, pid: str) -> ChildResult:
        context = run.commander
        todo = context.comms.goals.todos.get(pid)
        tool = CommsOrderTool(
            owner=receiver,
            order=OrderGoalAction(
                problem=pid,
                expected_revision=todo.revision,
                assigner=context.incarnation,
                order=document.order,
                review_at=RecordedEventTime(time.time() + 3600),
                commander=context.commander,
                request=document.order.request(
                    context.comms.registry.require(context.commander), (pid,)
                ),
                traced=FileLine.from_wire(f"{Path(__file__).resolve()}:1"),
            ),
        )
        return run.invoke(
            "from pathlib import Path; import sys; from agent_comms.comms import Comms; "
            "from agent_comms.field_codec import FieldCodec; "
            "from agent_comms.tool_caller import ThreadCaller; "
            "from agent_comms.tools import CommsOrderTool; "
            "ThreadCaller(sys.argv[2]).invoke(FieldCodec.decode(CommsOrderTool, "
            "FieldCodec.parse(sys.argv[3])), Comms(Path(sys.argv[1])))",
            str(context.root),
            context.commander,
            json.dumps(FieldCodec.encode(tool)),
        )

    @classmethod
    def read(cls) -> ParityComparison:
        authority = MeasuredCommander.read(CommanderConfig.path())
        owners = tuple(
            dict.fromkeys(
                todo.held().owner
                for todo in authority.comms.goals.todos.held_for_commander(authority.incarnation)
            )
        )[-2:]
        if len(owners) < 2:
            return UnverifiedParity("Finished-work transfer needs two real registered unit holders")
        cases = []
        for label, execute in (("current", cls.current), ("native", cls.native)):
            with LifecycleRun.open(extra_threads=owners) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    receiver = next(owner for owner in owners if owner != run.worker)
                    delivered = run.complete(run.create())
                    before = cls.state(run, delivered.id, receiver)
                    document = OrderFile.read(run.document.read_text())
                    document = replace(
                        document,
                        problems=(delivered.id,),
                        order=replace(document.order, id=uuid4().hex),
                    )
                    run.decisions.write_text(json.dumps([receiver]))
                    reference = run.invoke(
                        "from pathlib import Path; import sys; from agent_comms.comms import Comms; "
                        "todo=Comms(Path(sys.argv[1])).goals.todos.get(sys.argv[2]); "
                        "todo.order.require_transfer(todo)",
                        str(run.commander.root),
                        delivered.id,
                    )
                    attempt = execute(run, receiver, document, delivered.id)
                    cases.append(
                        (
                            label,
                            reference.outcome,
                            attempt.outcome,
                            tuple(reference.stderr.decode(errors="replace").splitlines()[-1:]),
                            tuple(attempt.stderr.decode(errors="replace").splitlines()[-1:]),
                            before == cls.state(run, delivered.id, receiver),
                        )
                    )
        return HandoffComparison(tuple(cases))
