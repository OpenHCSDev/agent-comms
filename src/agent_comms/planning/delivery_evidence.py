from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired

from agent_comms.errors import TodoError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import RecordProblems
from agent_comms.planning.order_runs import OrderRun
from agent_comms.projects.git_errors import SourceReadFailure
from agent_comms.projects.repo import Repo
from agent_comms.projects.work_branches import WorkBranches

SOURCE_READ_FAILURES = (OSError, SourceReadFailure, TimeoutExpired)


@dataclass(frozen=True)
class DeliveryEvidence(DeclaredFamily, affix="DeliveryEvidence"):
    def artifact(self) -> str:
        raise TodoError("No application evidence was supplied at delivery; pass --artifact FILE.")

    def require_run(self, run: OrderRun) -> None:
        run.require_artifact("")


@dataclass(frozen=True)
class NoDeliveryEvidence(DeliveryEvidence):
    pass


@dataclass(frozen=True)
class FileDeliveryEvidence(DeliveryEvidence):
    path: str

    def artifact(self) -> str:
        if not Path(self.path).is_file():
            raise TodoError("Delivery evidence must name an existing file.")
        return self.path

    def require_run(self, run: OrderRun) -> None:
        run.require_artifact(self.path)


class TodoProblems(RecordProblems, TodoError):
    """Every refusal of one todo request, collected before anything is read back or written."""


class EvidenceRequirements:
    """Shared evidence checks for a source request and its resolved delivery."""

    def evidence_problems(self, evidence: DeliveryEvidence, run: OrderRun) -> dict[str, str]:
        problems: dict[str, str] = {}
        with RecordProblems.collecting(
            problems, '--seen.run', 'supply --seen FILE with the application evidence'):
            evidence.require_run(run)
        return problems


class WorkingEvidence(EvidenceRequirements):
    """An operation's working claim always needs a real artifact, even for a mechanical run."""

    def evidence_problems(self, evidence: DeliveryEvidence, run: OrderRun) -> dict[str, str]:
        problems = super().evidence_problems(evidence, run)
        with RecordProblems.collecting(
            problems, '--seen.working', 'supply --seen FILE or --screenshot FILE for the working claim'):
            evidence.artifact()
        return problems


class FreshMainCheck:
    """Undelivered work is checked against fetched main, independent of the local branch."""

    def check_base(self, repo: Path) -> str:
        return f"{Repo.remote}/{WorkBranches.main}"


class BranchPointCheck:
    """Delivered work is checked against the main commit it was built on, never main since:
    once the fix merges, current main passes, which proves nothing about the check."""

    @property
    @abstractmethod
    def work_commit(self) -> str: ...

    def check_base(self, repo: Path) -> str:
        main = f"{Repo.remote}/{WorkBranches.main}"
        line = set(Repo.git(repo, "rev-list", "--first-parent", main).split())
        for commit in Repo.git(repo, "rev-list", "--first-parent", f"{self.work_commit}^1").split():
            if commit in line:
                return commit
        raise TodoError(f"{self.work_commit} was not built on {WorkBranches.main}.")
