"""Run real checked-order native dispatch on a new scratch bus and retain its reply.

No fake transport, provider or history is used. Failed attempts remain retained.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from abc import abstractmethod
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar
from uuid import uuid4

from agent_comms.change_report import FileLine
from agent_comms.cli_bridges.shared import DeclaredOptions
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import CommanderContext
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText, Argument
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.messages import Message
from agent_comms.plan_phases import RecordPlanePhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import ReportDelivery, PendingProblemOrder, TriageScope
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsHistoryTool, CommsOrderDoneTool, CommsStandingRulesTool
from agent_comms.wire_watch import WatchSource, WireWatch
from agent_comms.work_dispatch import CheckedPhaseTask


class Reply(DeclaredFamily, affix="Reply"):
    arrived: ClassVar[bool] = False

    @classmethod
    def read(cls, comms: Comms, commander: str, unit: str, token: str) -> Reply:
        history = CommsHistoryTool(peer=unit, limit=20).apply(comms, ThreadCaller(commander))
        for message in history.messages:
            if message.sender == unit and token in message.body:
                return ReceivedReply(message)
        return AwaitedReply()

    @abstractmethod
    def require_message(self) -> Message: ...


class AwaitedReply(Reply):
    def require_message(self) -> Message:
        raise TimeoutError("The native unit has not replied; retain its original attempt")


@dataclass(frozen=True)
class ReceivedReply(Reply):
    message: Message
    arrived = True

    def require_message(self) -> Message:
        return self.message


async def first_reply(comms: Comms, commander: str, unit: str, token: str) -> Message:
    async with asyncio.timeout(180):
        async with aclosing(
            WireWatch.observations(
                WatchSource.modified_file(comms.bus.log.path),
                WatchSource.modified_file(comms.registry.store.path),
            )
        ) as changes:
            async for _ in changes:
                observed = Reply.read(comms, commander, unit, token)
                if observed.arrived:
                    return observed.require_message()
                if not comms.registry.require(unit).process_alive:
                    raise RuntimeError(f"Unit exited before replying; inspect {comms.root}")
    raise TimeoutError(f"No first reply; preserve {comms.root}")


async def stop_after_turn(comms: Comms, unit: str) -> None:
    async with asyncio.timeout(180):
        async with aclosing(
            WireWatch.observations(
                WatchSource.modified_file(comms.registry.store.path),
            )
        ) as changes:
            async for _ in changes:
                turn = comms.registry.require(unit).turn_record.read()
                if not turn.executing:
                    turn.require_idle()
                    comms.owners.stop(unit)
                    return


@dataclass(frozen=True, kw_only=True)
class ProofCommand(DeclaredFamily, DeclaredOptions, affix="ProofCommand"):
    root: Annotated[Path, PathText, Argument(flags=("--root",))]

    @abstractmethod
    def apply(self, project: Path) -> int: ...


@dataclass(frozen=True, kw_only=True)
class CheckProofCommand(ProofCommand):
    help = "check the actual native reply"
    commander: Annotated[str, Argument(flags=("--commander",))]
    unit: Annotated[str, Argument(flags=("--unit",))]
    token: Annotated[str, Argument(flags=("--token",))]

    def apply(self, project: Path) -> int:
        print(int(Reply.read(Comms(self.root), self.commander, self.unit, self.token).arrived))
        return 0


@dataclass(frozen=True, kw_only=True)
class RunProofCommand(ProofCommand):
    help = "create a fresh scratch root and run its real rotation proof"

    def apply(self, project: Path) -> int:
        root = self.root.expanduser().resolve()
        allowed = StorageRule().scratch_base
        if not root.is_relative_to(allowed) or root.exists():
            raise ValueError("Proof needs a NEW root under the owner's agent-scratch directory")
        home = root.parent / "home"
        home.mkdir(parents=True)
        comms = Comms(root)
        comms.messaging.initialize_private_initial_protocol()
        # A fresh interpreter acquires the scratch commander configuration.
        # Keep the actual storage declaration across its isolated HOME.
        (root / "storage.json").write_text(
            json.dumps(FieldCodec.encode(comms.registry.storage_rule))
        )
        commander = comms.registry.declare(
            comms.registry.new_thread(
                "dispatch-proof-commander",
                frozenset(),
                str(project),
                model="anthropic/claude-sonnet-4-5",
                execution=ExternalThreadExecution,
            ),
            StoppedThreadStatus(),
        )
        comms.goals.todos.add_request(commander.incarnation,
            'Verify one rotation-started native unit on this scratch bus only.')
        CommanderConfig(
            commander=commander.name,
            root=root,
            cli=Path(sys.executable).with_name("agent-comms"),
            state=root.parent / "state",
            repos=(),
            worktrees=comms.registry.storage_rule.checkout_base,
            scratch=root.parent,
            disk=root.parent,
        ).write(home)
        return subprocess.run(
            (sys.executable, __file__, "observe", "--root", str(root)),
            env={**os.environ, "HOME": str(home)},
            check=False,
        ).returncode


@dataclass(frozen=True, kw_only=True)
class ObserveProofCommand(ProofCommand):
    help = "observe the freshly configured scratch rotation"

    def apply(self, project: Path) -> int:
        root = self.root.resolve()
        context = CommanderContext.read_root(root)
        comms = context.comms
        commander = comms.registry.require(context.commander)
        problem = f"P{time.time_ns()}"
        unit = f"unit-{problem.lower()}-r0"
        token = f"ROTATION_REPLY_{uuid4().hex}"
        check_command = shlex.join(
            (
                sys.executable,
                str(Path(__file__).resolve()),
                "check",
                "--root",
                str(root),
                "--commander",
                commander.name,
                "--unit",
                unit,
                "--token",
                token,
            )
        )
        order = PendingProblemOrder(
            id=uuid4().hex, scope=TriageScope(),
            owner=FileLine(__file__, 1),
            check=OrderCheck(str(project), check_command, CountCheckResult(1)),
            edits=(
                f"Use comms_send to send exactly {token} to {commander.name}, then return. "
                "Verify your first real reply; make no source edits.",
            ),
            banned=("No helper agents, live-bus writes, source edits or extra tool calls.",),
            report="Send the requested token once and return; the proof commander checks delivery.",
            at=RecordedEventTime(1),
            first_call_deadline=RecordedEventTime(121),
            run=MechanicalOrderRun(),
        )
        RecordPlanePhase.task = CheckedPhaseTask(
            problem,
            Repo.of(project).github,
            "Verify rotation starts a native unit and its reply.",
            order,
            review_seconds=300,
        )
        print(f"ROOT {root}", flush=True)
        source = subprocess.check_output(
            ("git", "-C", str(project), "rev-parse", "HEAD"), text=True
        )
        print(f"SOURCE {source.strip()}", flush=True)
        for line in context.call(CommsStandingRulesTool()).broken:
            print(line, flush=True)
        assigned = comms.goals.todos.get(problem)
        received_order = FieldCodec.decode(PendingProblemOrder, FieldCodec.encode(assigned.order))
        started = comms.registry.require(assigned.held().owner)
        print(
            f"UNIT {started.name} model={started.model} pid={started.require_process().pid}",
            flush=True,
        )
        print(
            f"ORDER {received_order.id} problem={problem} checkout={started.worktree}", flush=True
        )
        (root.parent / "order.json").write_text(json.dumps(FieldCodec.encode(assigned), indent=2))
        before = comms.registry.all_threads()
        for line in context.call(CommsStandingRulesTool()).broken:
            print(line, flush=True)
        assert comms.registry.all_threads().keys() == before.keys(), "duplicate unit"
        after = FieldCodec.decode(
            PendingProblemOrder, FieldCodec.encode(comms.goals.todos.get(problem).order)
        )
        assert after.id == received_order.id, "duplicate order"
        print("SECOND_ROTATION no duplicate unit or order", flush=True)
        found = asyncio.run(first_reply(comms, commander.name, started.name, token))
        print(f"FIRST_REPLY {found.sender}: {found.body}", flush=True)
        artifact = root.parent / "reply.json"
        artifact.write_text(json.dumps(FieldCodec.encode(found), indent=2))
        CommsOrderDoneTool(
            problem=problem,
            order_id=received_order.id,
            delivery=ReportDelivery(report=found.body),
            claims=(),
        ).apply(comms, ThreadCaller(commander.name))
        asyncio.run(stop_after_turn(comms, started.name))
        print("STOPPED after its turn ended; original history retained", flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for member in ProofCommand.members_with(ProofCommand):
        member.add_parser(commands)
    arguments = parser.parse_args()
    return (
        ProofCommand.member_named(arguments.command)
        .from_arguments(arguments)
        .apply(Path(__file__).resolve().parents[1])
    )


if __name__ == "__main__":
    raise SystemExit(main())
