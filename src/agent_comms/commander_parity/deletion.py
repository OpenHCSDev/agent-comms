"""Deletion intent parity through real orders, pushed commits and native delivery checks."""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path

from ..change_report import FileLine
from ..child_process import CountPrinted, ExitedOutcome
from ..commander.order import OrderFile, deadline
from ..field_codec import FieldCodec
from ..goal_actions import CreateProblemGoalAction
from ..goals import CountCheckResult, OrderCheck
from ..plan_phases import RecordPlanePhase
from ..progress_claims import MergedClaim
from ..repo import Repo
from ..todos import (
    CheckedCommitDelivery,
    CommitDelivery,
    DeliveredProblemOrder,
    FamilyDeletion,
    MechanicalOrderRun,
    Todo,
)
from ..tool_caller import ThreadCaller
from ..tools import CommsOrderDoneTool, CommsTodoCreateTool, ProblemOrdered
from . import ParityComparison, ParityRow
from .lifecycle import LifecycleObservation, LifecycleRun, MeasuredCommander


@dataclass(frozen=True)
class DeletionComparison(ParityComparison):
    current: LifecycleObservation
    native: LifecycleObservation

    @property
    def passed(self) -> bool:
        return (
            self.current.passed and self.native.passed and self.current.checks == self.native.checks
        )

    def describe(self) -> str:
        return f"current: {self.current.describe()}; native: {self.native.describe()}"

    @classmethod
    def read(cls) -> DeletionComparison:
        return cls(CurrentDeletionPath.read(), NativeDeletionPath.read())


class DeletionPath(ABC):
    """Both adapters exercise the same scenario on independent LifecycleRun copies."""

    @classmethod
    @abstractmethod
    def send(cls, run: LifecycleRun, document: OrderFile) -> Todo: ...

    @classmethod
    @abstractmethod
    def delivery_command(cls, run: LifecycleRun, todo: Todo, commit: str) -> tuple[str, ...]: ...

    @staticmethod
    def commit(repository: Path, text: str, message: str) -> str:
        (repository / "family.py").write_text(text)
        Repo.git(repository, "add", "family.py")
        Repo.git(repository, "commit", "-qm", message)
        Repo.git(repository, "push", "-q", "origin", "main")
        return Repo.git(repository, "rev-parse", "HEAD").strip()

    @classmethod
    def read(cls) -> LifecycleObservation:
        with LifecycleRun.open() as original:
            # Keep source checkouts out of the copied worker's storage accounting.
            # Limits remain unchanged; all detached checks use this persistent owned base.
            rule = original.commander.comms.registry.storage_rule
            with tempfile.TemporaryDirectory(
                prefix="parity-deletion-", dir=rule.checkout_base
            ) as checkouts:
                root = original.commander.root
                (root / "storage.json").write_text(
                    json.dumps(FieldCodec.encode(replace(rule, checkout_base=Path(checkouts))))
                )
                (root / "commander.json").write_text(original.commander.model_dump_json())
                run = replace(original, commander=MeasuredCommander.read_root(root))
                return cls.exercise(run)

    @classmethod
    def exercise(cls, run: LifecycleRun) -> LifecycleObservation:
        repository = Path(run.commander.comms.registry.require(run.worker).worktree)
        remote = repository.parent / "deletion-origin.git"
        Repo.git(repository, "init", "-q", "-b", "main")
        Repo.git(repository, "config", "user.name", "Deletion parity")
        Repo.git(repository, "config", "user.email", "deletion-parity@localhost")
        Repo.git(repository, "init", "--bare", "-q", "--initial-branch=main", str(remote))
        Repo.git(repository, "remote", "add", "origin", str(remote))
        family = "class RetiredDecision:\n    pass\n\n"
        cls.commit(
            repository,
            family + "def decisions():\n    return [RetiredDecision()]\n",
            "Declare the obsolete decision family and its caller",
        )
        count = OrderCheck(
            str(repository),
            shlex.join(
                (
                    sys.executable,
                    "-c",
                    "import ast;from pathlib import Path;"
                    "print(sum(isinstance(node,ast.ClassDef) for node in "
                    "ast.walk(ast.parse(Path('family.py').read_text()))))",
                )
            ),
            CountCheckResult(0),
        )
        deletion = FamilyDeletion(
            "obsolete decision family", FileLine(str(repository / "family.py"), 1), count
        )
        initial = OrderFile.read(run.document.read_text())
        order = replace(
            initial.order,
            check=OrderCheck(
                str(repository),
                shlex.join((sys.executable, "-c", "import family;assert not family.decisions()")),
            ),
            run=MechanicalOrderRun(),
            deletes=deletion,
        )
        document = replace(initial, order=order)
        run.document.write_text(json.dumps(FieldCodec.encode(document)))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            before = cls.send(run, document)
        partial = cls.commit(
            repository,
            family + "def decisions():\n    return []\n",
            "Repair the caller but leave one obsolete family site",
        )
        refused = run.invoke_command(cls.delivery_command(run, before, partial))
        after_refusal = run.commander.comms.goals.todos.get(before.id)
        remaining = count.recorded(partial, str(repository))
        complete = cls.commit(
            repository,
            "def decisions():\n    return []\n",
            "Delete the last obsolete family declaration",
        )
        accepted = run.invoke_command(cls.delivery_command(run, before, complete))
        after_acceptance = run.commander.comms.goals.todos.get(before.id)
        zero = count.recorded(complete, str(repository))
        primary = order.check.recorded(partial, str(repository))
        delivered = after_acceptance.order
        checks = (
            ("native order retained deletion intent", before.order.checked().deletes == deletion),
            (
                "partial commit passed primary check",
                bool(primary) and order.check.result.distance(primary[0]) == CountPrinted(0),
            ),
            (
                "partial commit retained one family site",
                bool(remaining) and count.result.distance(remaining[0]) == CountPrinted(1),
            ),
            ("nonzero delivery refused", refused.outcome == ExitedOutcome(1)),
            ("refusal preserved pending order", before == after_refusal and after_refusal.pending),
            (
                "zero count checked on delivered commit",
                bool(zero) and count.result.distance(zero[0]) == CountPrinted(0),
            ),
            (
                "zero delivery accepted",
                accepted.outcome == ExitedOutcome(0) and not after_acceptance.pending,
            ),
            (
                "accepted delivery names the zero-site commit",
                isinstance(delivered, DeliveredProblemOrder)
                and isinstance(delivered.delivery, CheckedCommitDelivery)
                and delivered.delivery.delivered_commit == complete,
            ),
            ("accepted delivery retained deletion intent", delivered.checked().deletes == deletion),
        )
        return LifecycleObservation(
            CommsOrderDoneTool.__qualname__,
            len((refused, accepted)),
            checks,
            output.getvalue() + f"partial={partial}; zero={complete}; "
            f"refused={refused.outcome.summary}: "
            f"{refused.stdout.decode(errors='replace')}{refused.stderr.decode(errors='replace')}; "
            f"accepted={accepted.outcome.summary}: "
            f"{accepted.stdout.decode(errors='replace')}{accepted.stderr.decode(errors='replace')}",
        )


class CurrentDeletionPath(DeletionPath):
    @classmethod
    def send(cls, run: LifecycleRun, document: OrderFile) -> Todo:
        return run.create()

    @classmethod
    def delivery_command(cls, run: LifecycleRun, todo: Todo, commit: str) -> tuple[str, ...]:
        return (
            sys.executable,
            "-c",
            "import sys;from agent_comms.commander.order import Commander,DoneVerb;"
            "DoneVerb(unit=sys.argv[2],message=sys.argv[3],sha=sys.argv[4]).run("
            "Commander.read_root(sys.argv[1]))",
            str(run.commander.root),
            run.worker,
            todo.order.checked().id,
            commit,
        )


class NativeDeletionPath(DeletionPath):
    @classmethod
    def send(cls, run: LifecycleRun, document: OrderFile) -> Todo:
        result = run.commander.call(
            CommsTodoCreateTool(
                owner=run.worker,
                order=CreateProblemGoalAction(
                    problem="P1",
                    repo=Repo.of(Path(document.order.check.directory)).github,
                    text=document.defect,
                    order=document.order,
                    request=document.order.request(
                        run.commander.comms.registry.require(run.commander.commander), ("P1",)
                    ),
                    deadline=deadline(run.due),
                    commander=run.commander.commander,
                ),
            )
        )
        if not isinstance(result, ProblemOrdered):
            raise TypeError("Native creation returned no ordered problem")
        return result.todo

    @classmethod
    def delivery_command(cls, run: LifecycleRun, todo: Todo, commit: str) -> tuple[str, ...]:
        request = CommsOrderDoneTool(
            problem=todo.id,
            order_id=todo.order.checked().id,
            delivery=CommitDelivery(commit=commit, delivered_commit=commit),
            claims=(MergedClaim(commit),),
        )
        return request.cli_command(ThreadCaller(run.commander.commander)).argv(run.commander.root)


class DeletionTargetParityRow(ParityRow):
    number = 31
    capability = "An order states its intent as a deletion target"
    current = "Current order command with a declared FamilyDeletion"
    native = "CommsTodoCreateTool and CommsOrderDoneTool check the delivered family count"
    phases = (RecordPlanePhase,)
    comparison = DeletionComparison.read
