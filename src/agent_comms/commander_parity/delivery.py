"""Compare commit delivery through the current command and the native tool."""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path

from ..child_process import ChildOutcome, ChildResult, ExitedOutcome
from ..commander.order import OrderFile
from ..field_codec import FieldCodec
from ..progress_claims import MergedClaim
from ..repo import Repo
from ..todos import CommitDelivery, FileDeliveryEvidence, Todo
from ..tools import CommsOrderDoneTool
from . import ParityComparison
from .lifecycle import LifecycleRun


@dataclass(frozen=True)
class DeliveryOutcome:
    outcome: ChildOutcome
    pending: bool
    order_line: str
    refusal: str

    @classmethod
    def saved(cls, todo: Todo, process: ChildResult) -> DeliveryOutcome:
        return cls(
            process.outcome, todo.pending, todo.order.line, process.stderr.decode(errors="replace")
        )

    @property
    def delivered(self) -> bool:
        return self.outcome.successful and not self.pending


@dataclass(frozen=True)
class DeliveryComparison(ParityComparison):
    checked_commit: str
    cases: tuple[tuple[str, str, DeliveryOutcome, DeliveryOutcome], ...]

    @property
    def passed(self) -> bool:
        return (
            len(self.cases) == 3
            and self.cases[0][2].delivered
            and all(
                (current.outcome, current.pending) == (native.outcome, native.pending)
                and current.order_line == native.order_line
                and (
                    all(
                        outcome.outcome == ExitedOutcome(1)
                        and commit in outcome.refusal
                        and "not in" in outcome.refusal
                        for outcome in (current, native)
                    )
                    if current.pending
                    else commit in current.order_line
                )
                for _, commit, current, native in self.cases
            )
        )

    def describe(self) -> str:
        return f"checked main={self.checked_commit}; outcomes={self.cases!r}"


@dataclass(frozen=True)
class DeliveryRepository:
    path: Path

    @classmethod
    def create(cls, root: Path) -> DeliveryRepository:
        path = root / "delivery-repository"
        path.mkdir()
        remote = root / "delivery-origin.git"
        Repo.git(root, "init", "--bare", "-q", str(remote))
        Repo.git(path, "init", "-q", "-b", "main")
        Repo.git(path, "config", "user.name", "parity")
        Repo.git(path, "config", "user.email", "parity@invalid")
        url = "https://github.com/parity-local/delivery.git"
        Repo.git(path, "config", f"url.{remote}.insteadOf", url)
        Repo.git(path, "remote", "add", "origin", url)
        Repo.git(path, "commit", "-q", "--allow-empty", "-m", "copied delivery base")
        Repo.git(path, "push", "-q", "origin", "main")
        Repo.git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        return cls(path)

    def cases(self) -> tuple[tuple[str, str], ...]:
        base = Repo.git(self.path, "rev-parse", "HEAD").strip()
        Repo.git(self.path, "switch", "-q", "-c", "main-answer")
        Repo.git(self.path, "commit", "-q", "--allow-empty", "-m", "answer integrated on main")
        main = Repo.git(self.path, "rev-parse", "HEAD").strip()
        Repo.git(self.path, "switch", "-q", "main")
        Repo.git(self.path, "merge", "-q", "--no-ff", "main-answer", "-m", "integrate main answer")
        Repo.git(self.path, "push", "-q", "origin", "main")
        Repo.git(self.path, "switch", "-q", "-c", "branch-answer", base)
        Repo.git(self.path, "commit", "-q", "--allow-empty", "-m", "answer integrated elsewhere")
        branch = Repo.git(self.path, "rev-parse", "HEAD").strip()
        Repo.git(self.path, "switch", "-q", "-c", "integration", base)
        Repo.git(
            self.path, "merge", "-q", "--no-ff", "branch-answer", "-m", "integrate branch answer"
        )
        Repo.git(self.path, "push", "-q", "origin", "integration")
        Repo.git(self.path, "switch", "-q", "-c", "unmerged-answer", base)
        Repo.git(self.path, "commit", "-q", "--allow-empty", "-m", "published but not integrated")
        unmerged = Repo.git(self.path, "rev-parse", "HEAD").strip()
        Repo.git(self.path, "push", "-q", "origin", "unmerged-answer")
        Repo.git(self.path, "switch", "-q", "main")
        return (
            ("merged to main", main),
            ("merged to another branch", branch),
            ("not merged", unmerged),
        )


class DeliveredCommitCase:
    @classmethod
    def create_order(cls, run: LifecycleRun, repository: DeliveryRepository) -> Todo:
        document = OrderFile.read(run.document.read_text())
        document = replace(
            document,
            order=replace(
                document.order, check=replace(document.order.check, directory=str(repository.path))
            ),
        )
        run.document.write_text(json.dumps(FieldCodec.encode(document)))
        run.decisions.write_text(json.dumps([run.worker]))
        before = {
            todo.id
            for todo in run.commander.comms.goals.todos.for_commander(run.commander.incarnation)
        }
        run.commander.problem(
            str(run.document), run.worker, run.due, f"{Path(__file__).resolve()}:1"
        )
        (created,) = (
            todo
            for todo in run.commander.comms.goals.todos.for_commander(run.commander.incarnation)
            if todo.id not in before
        )
        run.decisions.write_text("[]")
        return created

    @classmethod
    def read(cls) -> ParityComparison:
        cases = []
        with LifecycleRun.open() as run, contextlib.redirect_stdout(io.StringIO()):
            repository = DeliveryRepository.create(run.commander.root)
            run.decisions.write_text("[]")
            run.artifact.write_text(
                "Copied-bus delivered-commit comparison; no source input executed."
            )
            for name, commit in repository.cases():
                current_order = cls.create_order(run, repository)
                process = run.invoke(
                    "from agent_comms.commander.order import Commander; import sys; "
                    "Commander.model_validate_json(sys.argv[1]).done("
                    "sys.argv[2],sys.argv[3],seen=sys.argv[4])",
                    run.commander.model_dump_json(),
                    current_order.id,
                    commit,
                    str(run.artifact),
                )
                current = DeliveryOutcome.saved(
                    run.commander.comms.goals.todos.get(current_order.id), process
                )
                native_order = cls.create_order(run, repository)
                tool = CommsOrderDoneTool(
                    problem=native_order.id,
                    order_id=native_order.order.checked().id,
                    delivery=CommitDelivery(
                        commit=Repo.git(repository.path, "rev-parse", "HEAD").strip(),
                        delivered_commit=commit,
                        evidence=FileDeliveryEvidence(str(run.artifact)),
                    ),
                    claims=(MergedClaim(commit),),
                )
                process = run.invoke(
                    "from pathlib import Path; import sys; "
                    "from agent_comms.comms import Comms; "
                    "from agent_comms.field_codec import FieldCodec; "
                    "from agent_comms.tool_caller import ThreadCaller; "
                    "from agent_comms.tools import CommsOrderDoneTool; "
                    "ThreadCaller(sys.argv[2]).invoke(FieldCodec.decode(CommsOrderDoneTool, "
                    "FieldCodec.parse(sys.argv[3])),Comms(Path(sys.argv[1])))",
                    str(run.commander.root),
                    run.commander.commander,
                    json.dumps(FieldCodec.encode(tool)),
                )
                native = DeliveryOutcome.saved(
                    run.commander.comms.goals.todos.get(native_order.id), process
                )
                cases.append((name, commit, current, native))
            checked_commit = Repo.git(repository.path, "rev-parse", "HEAD").strip()
        return DeliveryComparison(checked_commit, tuple(cases))
