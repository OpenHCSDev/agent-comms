"""Private fleet snapshots for replaying Core's own standing rules."""

from __future__ import annotations

import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from .active_route import LocalRoute
from .commander_heartbeat import CommanderObservation
from .comms import Comms
from .field_codec import PathText
from .launch_runtime import LaunchRuntime
from .locked_store import RegistryFile, RootFile
from .maintenance_barrier import MaintenanceBarrier
from .phase_evidence import MainTree
from .repo import Repo
from .repository_name import RepositoryName
from .root_runtime import RootRuntime
from .storage_rules import StorageRule
from .todos import ListedPullRequest, ProgressRead, PullRequest, Todo, UnmergedListedPullRequest
from .work_branches import BranchComparison, BranchDifference, UnpushedHead, WorkBranchError


@dataclass(frozen=True)
class FleetRootCopy:
    source: Annotated[Path, PathText]
    root: Annotated[Path, PathText]
    captured_at: float
    bytes: int

    @classmethod
    def capture(cls, source: Path, into: Path) -> FleetRootCopy:
        """Read source files only; SQLite owners use their read-only backup path."""
        barrier = MaintenanceBarrier(RegistryFile.path_in(source))
        before = barrier._current_unlocked()
        before.require_open()
        floor = StorageRule.load(source).floor_gib * 1024**3
        paths = tuple(path for path in source.rglob("*") if stat.S_ISREG(path.lstat().st_mode))
        size = sum(path.stat().st_size for path in paths)
        into.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(into.parent).free < floor + size:
            raise ValueError("Fleet copy would consume the bus's free-space floor")
        into.mkdir(mode=0o700)
        witnesses = frozenset(barrier.witness_paths)
        copied = 0
        for resource, owner in RootFile.owned_files(source, paths).items():
            original = source / resource.path
            if original in witnesses:
                continue
            target = into / resource.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            owner.copy_file(original, target)
            target.chmod(0o600)
            copied += target.stat().st_size
        after = barrier._current_unlocked()
        after.require_open()
        if after != before:
            raise ValueError("Maintenance changed during fleet capture; discard this copy")
        RootRuntime.publish(into, LaunchRuntime.running())
        return cls(source, into, time.time(), copied)

    def observations(self) -> tuple[CommanderObservation, ...]:
        comms = Comms(LocalRoute(self.root))
        project = comms.registry.store.read().commander_project
        return tuple(
            CommanderObservation.read(comms, commander)
            for commander in project.commanders(comms.registry.snapshot())
        )


@dataclass(frozen=True)
class GitReference:
    name: str
    head: str

    def resolves(self, argument: str) -> bool:
        return argument in (
            self.name,
            self.name.removeprefix("refs/remotes/"),
            self.name.removeprefix("refs/heads/"),
        )


@dataclass(frozen=True)
class GitWorktree:
    branch: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class PullComparison:
    base: str
    head: str
    compared: BranchComparison


@dataclass(frozen=True)
class FleetGitRead:
    checkout: Annotated[Path, PathText]
    difference: BranchDifference | None
    unpublished: tuple[UnpushedHead, ...] | None
    comparisons: tuple[PullComparison, ...]
    references: tuple[GitReference, ...]
    uncommitted: tuple[Annotated[Path, PathText], ...]
    holds_work: bool
    worktrees: tuple[GitWorktree, ...]

    def arguments(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            next(
                (reference.head for reference in self.references if reference.resolves(argument)),
                argument,
            )
            for argument in arguments
        )


@dataclass(frozen=True)
class UnreadFleetGit:
    checkout: Annotated[Path, PathText]
    error: str


@dataclass(frozen=True)
class FleetPullRead:
    repo: RepositoryName
    selection: type[ListedPullRequest]
    problem: str
    requests: tuple[PullRequest, ...]


@dataclass(frozen=True)
class FleetInputs:
    captured_at: float
    git: tuple[FleetGitRead, ...]
    unread_git: tuple[UnreadFleetGit, ...]
    pulls: tuple[FleetPullRead, ...]
    main_trees: tuple[MainTree, ...]

    @classmethod
    def capture(cls, copy: FleetRootCopy) -> FleetInputs:
        """Capture the declared Git and forge answers; never manufacture incidents."""
        comms = Comms(LocalRoute(copy.root))
        snapshot = comms.registry.snapshot()
        project = snapshot.commander_project.require()
        commanders = project.commanders(snapshot)
        todos = tuple(
            todo
            for commander in commanders
            for todo in comms.goals.todos.for_commander(commander.incarnation)
        )
        reader = ProgressRead(todos)
        pulls = tuple(
            FleetPullRead(todo.repo, selection, todo.id, reader.listed(todo.repo, selection, todo))
            for todo in todos
            for selection in ListedPullRequest.members_with(ListedPullRequest)
        )
        held = tuple(
            todo
            for commander in commanders
            for todo in comms.goals.todos.held_for_commander(commander.incarnation)
        )
        primary_paths = {
            snapshot.require(todo.held().owner).project_path.resolve() for todo in held
        }
        paths = set(primary_paths)
        paths.update(
            path.resolve()
            for todo in held
            for path in comms.registry.storage_rule.checkout_paths(
                snapshot.require(todo.held().owner)
            )
        )
        paths.update(repo.checkout.resolve() for repo in project.repos)
        paths.update(
            work.checkout.resolve()
            for todo in todos
            if not todo.holder_state.current_assignments()
            for work in comms.registry.storage_rule.work_for_problem(
                todo, comms.registry, comms.goals.todos
            )
        )
        git, unread = [], []
        for path in sorted(paths):
            try:
                checkout = reader.checkout(path)
                branches = checkout.branches
                difference = reader.branch_difference(path) if path in primary_paths else None
                requests = tuple(
                    request
                    for selection in pulls
                    if selection.selection is UnmergedListedPullRequest
                    and any(
                        todo.id == selection.problem
                        and snapshot.require(todo.held().owner).project_path.resolve() == path
                        for todo in held
                    )
                    for request in selection.requests
                )
                comparisons = tuple(
                    PullComparison(
                        request.base,
                        request.head,
                        branches.pull_request_diff(request.base, request.head),
                    )
                    for request in requests
                )
                references = tuple(
                    GitReference(*line.split("\t"))
                    for line in checkout.git(
                        "for-each-ref", "--format=%(refname)%09%(objectname)"
                    ).splitlines()
                )
                references = (
                    *references,
                    GitReference("HEAD", checkout.git("rev-parse", "HEAD").strip()),
                )
                unpublished = branches.unpushed(difference) if difference is not None else None
                git.append(
                    FleetGitRead(
                        path,
                        difference,
                        unpublished,
                        comparisons,
                        references,
                        branches.uncommitted(),
                        branches.local_work,
                        tuple(
                            GitWorktree(branch, paths)
                            for branch, paths in branches.worktrees().items()
                        ),
                    )
                )
            except (OSError, WorkBranchError) as error:
                unread.append(UnreadFleetGit(path, str(error)))
        return cls(
            time.time(),
            tuple(git),
            tuple(unread),
            pulls,
            tuple(MainTree.of(repo) for repo in project.repos),
        )

    def checkout(self, path: Path) -> FleetGitRead:
        path = path.resolve()
        for row in self.git:
            if row.checkout == path:
                return row
        for unread in self.unread_git:
            if unread.checkout == path:
                raise WorkBranchError(unread.error)
        raise WorkBranchError(f"No captured Git input for {path}")

    def listed(
        self, repo: RepositoryName, selection: type[ListedPullRequest], todo: Todo
    ) -> tuple[PullRequest, ...]:
        return next(
            row.requests
            for row in self.pulls
            if (row.repo, row.selection, row.problem) == (repo, selection, todo.id)
        )

    def branch_difference(self, path: Path) -> BranchDifference:
        difference = self.checkout(path).difference
        if difference is None:
            raise WorkBranchError(f"No captured branch comparison for {path}")
        return difference

    def unpublished(self, path: Path) -> tuple[UnpushedHead, ...]:
        unpublished = self.checkout(path).unpublished
        if unpublished is None:
            raise WorkBranchError(f"No captured publication comparison for {path}")
        return unpublished

    def main_tree(self, repo: Repo) -> MainTree:
        return next(tree for tree in self.main_trees if tree.repo.github == repo.github)

    def comparison(self, path: Path, base: str, head: str) -> BranchComparison:
        return next(
            row.compared
            for row in self.checkout(path).comparisons
            if (row.base, row.head) == (base, head)
        )
