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
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal, Self

from install_gate import REPO, ExternalRecord, GateError, RepositoryAnswer, Target, affected, run

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec

WORKFLOW = ".github/workflows/debt-ratchet.yml"


@dataclass(frozen=True)
class ApprovedHead(ExternalRecord):
    state: Literal["success"]
    context: ClassVar[str] = "agent-comms/review"

    @classmethod
    def require(cls, host: Host, head: str) -> None:
        query = f'.statuses | map(select(.context == "{cls.context}")) | first'
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
            FieldCodec.parse(run(
                "gh", "api", f"repos/{host.repository}/commits/{head}/pulls", "--jq",
                f'[.[] | select(.head.sha == "{head}" and .state == "open") | {{number: .number}}]',
            )),
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
    def target(cls, host: Host, workflow: WorkflowRun) -> Target:
        raise NotImplementedError


class WorkflowDispatchRunEvent(RunEvent, declared_name="workflow_dispatch"):
    @classmethod
    def target(cls, host: Host, workflow: WorkflowRun) -> Target:
        FieldCodec.decode(
            Administrator,
            FieldCodec.parse(
                run(
                    "gh", "api",
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
        if result.path.partition("@")[0] != WORKFLOW:
            raise GateError("The SSH key is restricted to the merge queue workflow")
        return result


@dataclass(frozen=True)
class WorkflowRun(WorkflowIdentity):
    event: type[RunEvent]


@dataclass(frozen=True)
class StatusWorkflowRun(WorkflowIdentity):
    event: Literal["status"]


@dataclass(frozen=True)
class Host:
    repo: Path
    repository: str
    scratch: Path
    private_copies_authorized: bool

    def fetch(self, head: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise GateError("A check requires a full GitHub commit identifier")
        run("git", "fetch", "origin", head, cwd=self.repo)
        return run("git", "rev-parse", f"{head}^{{commit}}", cwd=self.repo)

    def command(self, *arguments: str | Path | int) -> None:
        subprocess.run(tuple(map(str, arguments)), cwd=self.repo, check=True)


@dataclass(frozen=True)
class QueueCheck(DeclaredFamily, affix="QueueCheck"):
    run_id: int

    @classmethod
    def from_tokens(cls, tokens: list[str]) -> Self:
        identifier, _ = tokens
        return cls(int(identifier))

    def execute(self, host: Host) -> None:
        workflow = WorkflowRun.require(host, self.run_id)
        self.check(host, workflow.event.target(host, workflow))

    def check(self, host: Host, target: Target) -> None:
        raise NotImplementedError


class RatchetQueueCheck(QueueCheck, declared_name=None):
    root: ClassVar[str]

    def check(self, host: Host, target: Target) -> None:
        host.command(
            sys.executable, "-X", "faulthandler", host.repo / "tools/debt_ratchet.py",
            "--root", self.root, "--base", target.base, "--head", target.head,
        )


class SrcQueueCheck(RatchetQueueCheck):
    root = "src/agent_comms"


class ToolsQueueCheck(RatchetQueueCheck):
    root = "tools"


class InstallQueueCheck(QueueCheck):
    def check(self, host: Host, target: Target) -> None:
        if not host.private_copies_authorized and not target.outside(
            tuple(affected(host.repo, target.base, target.head))
        ):
            raise GateError(
                "Private-copy allocation is not authorized: worker cap 3 GB, desktop bus 26 GiB"
            )
        host.command(
            sys.executable, "-X", "faulthandler", host.repo / "tools/install_gate.py",
            "--repo", host.repo, "--revision", target.head, "--base", target.base,
            "--scratch", host.scratch,
        )


class EnqueueQueueCheck(QueueCheck):
    def check(self, host: Host, target: Target) -> None:
        ApprovedHead.require(host, target.head)
        pr = QueuedPullRequest.for_head(host, target.head)
        host.command(
            "gh", "pr", "merge", pr.number, "--repo", host.repository,
            "--auto", "--merge", "--match-head-commit", pr.head,
        )


@dataclass(frozen=True)
class DispatchQueueCheck(QueueCheck):
    head: str

    @classmethod
    def from_tokens(cls, tokens: list[str]) -> Self:
        identifier, _, head = tokens
        return cls(int(identifier), head)

    def execute(self, host: Host) -> None:
        StatusWorkflowRun.require(host, self.run_id)
        head = host.fetch(self.head)
        ApprovedHead.require(host, head)
        pr = QueuedPullRequest.for_head(host, head)
        host.command(
            "gh", "workflow", "run", WORKFLOW, "--repo", host.repository,
            "--ref", pr.branch, "-f", "enter_queue=true",
        )


class HostArguments(argparse.Namespace):
    request: str
    repo: Path
    scratch: Path
    private_copies_authorized: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="SSH_ORIGINAL_COMMAND, supplied as one quoted argument")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--private-copies-authorized", action="store_true")
    args = parser.parse_args(namespace=HostArguments())
    tokens = shlex.split(args.request)
    if len(tokens) < 2:
        parser.error("Expected RUN_ID CHECK_KIND [APPROVED_HEAD]")
    check = QueueCheck.member_named(tokens[1]).from_tokens(tokens)
    repository = FieldCodec.decode(
        RepositoryAnswer,
        FieldCodec.parse(run("gh", "repo", "view", "--json", "nameWithOwner", cwd=args.repo)),
    )
    check.execute(Host(args.repo, repository.name, args.scratch, args.private_copies_authorized))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
