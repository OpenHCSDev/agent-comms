#!/usr/bin/env python3
"""Restricted SSH entrypoint for the repository's isolated Actions runner.

GitHub owns queue order and run identity. The existing install gate owns source
coverage and rehearsal. A workflow receives exit status, never bus data or logs.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal, Self

from install_gate import (
    REPO,
    ExternalRecord,
    GateError,
    RepositoryAnswer,
    RunSummary,
    Target,
    run,
)

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import ReviewedHeadVerdict

WORKFLOW = Path(".github/workflows/debt-ratchet.yml")


@dataclass(frozen=True)
class ApprovedHead(ExternalRecord):
    state: Literal["success"]

    @classmethod
    def require(cls, host: Host, head: str) -> None:
        query = f'.statuses | map(select(.context == "{ReviewedHeadVerdict.context}")) | first'
        answer = run("gh", "api", f"repos/{host.repository}/commits/{head}/status", "--jq", query)
        FieldCodec.decode(cls, FieldCodec.parse(answer))


@dataclass(frozen=True)
class PullRequestNumber(ExternalRecord):
    number: int


@dataclass(frozen=True)
class QueuedPullRequest(ExternalRecord):
    number: int
    head: str = field(metadata={"wire_name": "headRefOid"})
    branch: str = field(metadata={"wire_name": "headRefName"})
    base: Literal["main"] = field(metadata={"wire_name": "baseRefName"})
    cross_repository: Literal[False] = field(metadata={"wire_name": "isCrossRepository"})
    draft: Literal[False] = field(metadata={"wire_name": "isDraft"})
    state: Literal["OPEN"]

    @classmethod
    def for_head(cls, host: Host, head: str) -> Self:
        links = FieldCodec.decode(
            tuple[PullRequestNumber, ...],
            FieldCodec.parse(
                run(
                    "gh",
                    "api",
                    f"repos/{host.repository}/commits/{head}/pulls",
                    "--jq",
                    f'[.[] | select(.head.sha == "{head}" and .state == "open")'
                    " | {number: .number}]",
                )
            ),
        )
        fields = ",".join(key for _, key in FieldCodec.record_fields(cls))
        candidates = tuple(
            FieldCodec.decode(
                cls,
                FieldCodec.parse(
                    run(
                        "gh", "pr", "view", link.number, "--repo", host.repository, "--json", fields
                    )
                ),
            )
            for link in links
        )
        matching = tuple(pr for pr in candidates if pr.head == head)
        if len(matching) != 1:
            raise GateError("An approved head must identify exactly one open, local PR")
        return matching[0]


@dataclass(frozen=True)
class Actor(ExternalRecord):
    login: str


@dataclass(frozen=True)
class Administrator(ExternalRecord):
    permission: Literal["admin"]


class RunEvent(DeclaredFamily, affix="RunEvent"):
    @classmethod
    @abstractmethod
    def target(cls, host: Host, workflow: WorkflowRun) -> Target: ...


class WorkflowDispatchRunEvent(RunEvent, declared_name="workflow_dispatch"):
    @classmethod
    def target(cls, host: Host, workflow: WorkflowRun) -> Target:
        FieldCodec.decode(
            Administrator,
            FieldCodec.parse(
                run(
                    "gh",
                    "api",
                    f"repos/{host.repository}/collaborators/{workflow.actor.login}/permission",
                )
            ),
        )
        head = host.fetch(workflow.head_sha)
        base = run("git", "merge-base", "origin/main", head, cwd=host.repo)
        return Target(head, base)


class MergeGroupRunEvent(RunEvent, declared_name="merge_group"):
    @classmethod
    def target(cls, host: Host, workflow: WorkflowRun) -> Target:
        if not workflow.head_branch.startswith("gh-readonly-queue/main/"):
            raise GateError("Not a main-branch merge group")
        head = host.fetch(workflow.head_sha)
        parents = run("git", "show", "-s", "--format=%P", head, cwd=host.repo).split()
        if len(parents) != 2:
            raise GateError("The singleton merge queue requires a two-parent proposed merge")
        ApprovedHead.require(host, parents[1])
        return Target(head, parents[0])


@dataclass(frozen=True)
class WorkflowIdentity(ExternalRecord):
    head_sha: str
    head_branch: str
    status: Literal["in_progress"]
    path: str
    actor: Actor

    @classmethod
    def require(cls, host: Host, identifier: int) -> Self:
        result = FieldCodec.decode(
            cls,
            FieldCodec.parse(
                run("gh", "api", f"repos/{host.repository}/actions/runs/{identifier}")
            ),
        )
        if Path(result.path.partition("@")[0]) != WORKFLOW:
            raise GateError("The SSH key is restricted to the merge queue workflow")
        return result


@dataclass(frozen=True)
class WorkflowRun(WorkflowIdentity):
    event: type[RunEvent]


@dataclass(frozen=True)
class StatusWorkflowRun(WorkflowIdentity):
    event: Literal["status"]


class CheckFailed(subprocess.SubprocessError):
    def __init__(self, check: QueueCheck, completed: subprocess.CompletedProcess[str]):
        self.check = check
        self.completed = completed
        super().__init__(f"{check.label}: {check.summary(completed)}")


@dataclass(frozen=True)
class Host:
    repo: Path
    repository: str
    scratch: Path

    def fetch(self, head: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise GateError("A check requires a full GitHub commit identifier")
        run("git", "fetch", "origin", head, cwd=self.repo)
        run("git", "submodule", "update", "--init", "--recursive", cwd=self.repo)
        return run("git", "rev-parse", f"{head}^{{commit}}", cwd=self.repo)

    def command(self, check: QueueCheck, *arguments: str | Path | int) -> None:
        completed = subprocess.run(
            tuple(map(str, arguments)),
            cwd=self.repo,
            text=True,
            capture_output=True,
            timeout=check.limit,
            check=False,
        )
        if completed.returncode:
            raise CheckFailed(check, completed)


@dataclass(frozen=True)
class QueueCheck(DeclaredFamily, affix="QueueCheck"):
    run_id: int
    limit: ClassVar[int]

    @property
    def label(self) -> str:
        return self.declared_name.title()

    @classmethod
    def from_tokens(cls, tokens: list[str]) -> Self:
        identifier, _ = tokens
        return cls(int(identifier))

    def execute(self, host: Host) -> None:
        workflow = WorkflowRun.require(host, self.run_id)
        self.check(host, workflow.event.target(host, workflow))

    @abstractmethod
    def check(self, host: Host, target: Target) -> None: ...

    @abstractmethod
    def summary(self, completed: subprocess.CompletedProcess[str]) -> str: ...


@dataclass(frozen=True)
class RatchetOutput(ExternalRecord):
    introduced_lines: dict[str, tuple[int, ...]]


class RatchetQueueCheck(QueueCheck, declared_name=None):
    root: ClassVar[str]
    limit = 600

    @property
    def label(self) -> str:
        return "Ratchet"

    def check(self, host: Host, target: Target) -> None:
        host.command(
            self,
            sys.executable,
            "-X",
            "faulthandler",
            host.repo / "tools/debt_ratchet.py",
            "--root",
            self.root,
            "--base",
            target.base,
            "--head",
            target.head,
        )

    def summary(self, completed: subprocess.CompletedProcess[str]) -> str:
        report = FieldCodec.decode(RatchetOutput, FieldCodec.parse(completed.stdout))
        sites: list[str] = []
        for key, lines in report.introduced_lines.items():
            measure, _, path = key.partition(":")
            sites.extend(f"{measure} {path}:{line}" for line in lines)
        return "; ".join(sites) or completed.stdout.strip()


class SrcQueueCheck(RatchetQueueCheck):
    root = "src/agent_comms"


class ToolsQueueCheck(RatchetQueueCheck):
    root = Path(__file__).parent.name


class InstallQueueCheck(QueueCheck):
    limit = 3000

    def check(self, host: Host, target: Target) -> None:
        host.command(
            self,
            sys.executable,
            "-X",
            "faulthandler",
            host.repo / "tools/install_gate.py",
            "--repo",
            host.repo,
            "--revision",
            target.head,
            "--base",
            target.base,
            "--scratch",
            host.scratch,
        )

    def summary(self, completed: subprocess.CompletedProcess[str]) -> str:
        report = FieldCodec.decode(RunSummary, FieldCodec.parse(completed.stdout))
        return "; ".join(report.result.failures) or report.result.description


class CommandQueueCheck(QueueCheck):
    limit = 120

    def summary(self, completed: subprocess.CompletedProcess[str]) -> str:
        return completed.stderr.strip() or completed.stdout.strip()


class EnqueueQueueCheck(CommandQueueCheck):
    def check(self, host: Host, target: Target) -> None:
        ApprovedHead.require(host, target.head)
        pr = QueuedPullRequest.for_head(host, target.head)
        host.command(
            self,
            "gh",
            "pr",
            "merge",
            pr.number,
            "--repo",
            host.repository,
            "--auto",
            "--merge",
            "--match-head-commit",
            pr.head,
        )


@dataclass(frozen=True)
class DispatchQueueCheck(CommandQueueCheck):
    head: str
    context: str

    @classmethod
    def from_tokens(cls, tokens: list[str]) -> Self:
        identifier, _, head, context = tokens
        return cls(int(identifier), head, context)

    def execute(self, host: Host) -> None:
        StatusWorkflowRun.require(host, self.run_id)
        if self.context != ReviewedHeadVerdict.context:
            return
        head = host.fetch(self.head)
        base = run("git", "merge-base", "origin/main", head, cwd=host.repo)
        self.check(host, Target(head, base))

    def check(self, host: Host, target: Target) -> None:
        ApprovedHead.require(host, target.head)
        pr = QueuedPullRequest.for_head(host, target.head)
        host.command(
            self,
            "gh",
            "workflow",
            "run",
            WORKFLOW,
            "--repo",
            host.repository,
            "--ref",
            pr.branch,
            "-f",
            "enter_queue=true",
        )


class HostArguments(argparse.Namespace):
    request: str
    repo: Path
    scratch: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="SSH_ORIGINAL_COMMAND, supplied as one quoted argument")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args(namespace=HostArguments())
    tokens = shlex.split(args.request)
    if len(tokens) < 2:
        parser.error("Expected RUN_ID CHECK_KIND [APPROVED_HEAD]")
    check = QueueCheck.member_named(tokens[1]).from_tokens(tokens)
    repository = FieldCodec.decode(
        RepositoryAnswer,
        FieldCodec.parse(run("gh", "repo", "view", "--json", "nameWithOwner", cwd=args.repo)),
    )
    try:
        check.execute(Host(args.repo, repository.name, args.scratch))
    except CheckFailed as error:
        print(str(error), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
