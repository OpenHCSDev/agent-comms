"""Checkout grants, operations, and prepared command base for isolated worktrees."""

from __future__ import annotations

import dataclasses
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize, PathText
from agent_comms.nominal.lifecycle_capability import Complete
from agent_comms.planning.goals import Goal
from agent_comms.planning.orders import Todo
from agent_comms.projects.checkouts import Checkout, CheckoutStart, RepositoryReads
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_sources import RepositorySource
from agent_comms.projects.shell_effects import (
    AcquireRepositoryStorageEffect,
    CreateCheckoutStorageEffect,
    RemoveCheckoutStorageEffect,
)
from agent_comms.projects.storage_projection import StorageProjection
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.threads import Thread
from agent_comms.transport.tool_caller import ThreadCaller


@dataclass(frozen=True)
class CheckoutResult(CliResult):
    checkout: Annotated[Path, PathText]


@dataclass(frozen=True)
class TransferredCheckoutResult(CheckoutResult):
    source: Annotated[Path, PathText]
    branch: str
    head: str


@dataclass(frozen=True)
class CheckoutGrant(DeclaredFamily, affix="CheckoutGrant"):
    """The caller's existing work authority, not a synthetic problem order."""

    thread: Thread

    @abstractmethod
    def require(self) -> None: ...

    def revalidate(self, ctx: CliBus) -> None:
        """A long acquisition cannot spend a retired caller or a changed goal."""
        current = ctx.registry.require(self.thread.name)
        if current.incarnation != self.thread.incarnation:
            raise ValueError('The checkout caller incarnation changed during acquisition')
        dataclasses.replace(self, thread=current).require()

    def require_transfer(self, ctx: CliBus, repository: Repo) -> None:
        raise ValueError('Checkout transfer requires a current held problem')


@dataclass(frozen=True)
class OrderCheckoutGrant(CheckoutGrant):
    todo: Todo

    def require(self) -> None:
        self.todo.require_assignee(self.thread.incarnation, self.todo.held().generation)

    def revalidate(self, ctx: CliBus) -> None:
        super().revalidate(ctx)
        ctx.goals.todos.get(self.todo.id).require_assignee(
            self.thread.incarnation, self.todo.held().generation)

    def require_transfer(self, ctx: CliBus, repository: Repo) -> None:
        current = ctx.goals.todos.get(self.todo.id)
        if current != self.todo:
            raise ValueError(f'Target problem {self.todo.id} changed before checkout transfer')
        current.state.require_open(current)
        self.require()
        if repository.github not in (current.repo, *current.order.source_repositories()):
            raise ValueError(f'{repository.github} is not a source repository of {current.id}')


@dataclass(frozen=True)
class GoalCheckoutGrant(CheckoutGrant):
    goal: Goal

    def require(self) -> None:
        current = self.thread.goal.require().require_id(self.goal.id)
        if current.state.items_with(Complete, current):
            raise ValueError("A completed goal cannot allocate a checkout.")


class CheckoutOperation(DeclaredFamily, affix="CheckoutOperation"):
    """What a checkout command does to the caller's isolated repository. Each
    member owns its required authority, storage check and Git work, so adding
    and retiring stay in the Checkout owner."""

    @classmethod
    @abstractmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: CliBus, thread: Thread) -> CheckoutResult: ...


@dataclass(frozen=True, kw_only=True)
class PreparedCheckoutCliCommand(CliCommand, declared_name=None):
    caller: Annotated[
        ThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--from",),
            options={"type": ThreadCaller, "help": "Calling thread"},
        ),
    ]
    operation: Annotated[type[CheckoutOperation], Argument(flags=("operation",))]
    source_checkout: Annotated[
        tuple[Annotated[Path, PathText], ...],
        Argument(flags=("--source",), options={"nargs": 1,
                 "help": "Exact owned checkout to transfer or retire; required for transfer"}),
    ] = ()

    @abstractmethod
    def declared_destination(self, rule: StorageRule, thread: Thread) -> Path: ...

    def destination(self, ctx: CliBus, thread: Thread) -> Path:
        rule = ctx.registry.storage_rule
        return rule.reused_checkout(thread, self.source(ctx, thread).github,
                                    self.declared_destination(rule, thread), RepositoryReads())

    @abstractmethod
    def grant(self, ctx: CliBus, thread: Thread) -> CheckoutGrant: ...

    @abstractmethod
    def source(self, ctx: CliBus, thread: Thread) -> RepositorySource: ...

    def apply(self, ctx: CliBus) -> CliResult:
        thread = ctx.registry.require(self.caller.own_thread())
        return self.operation.apply(self, ctx, thread)


class AddCheckoutOperation(CheckoutOperation):
    """Create from origin's declared default or repair the checkout's pinned submodules;
    the caller's order or goal grants the allocation."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: CliBus, thread: Thread) -> CheckoutResult:
        destination = command.destination(ctx, thread)
        grant = command.grant(ctx, thread)
        grant.revalidate(ctx)
        source = command.source(ctx, thread)
        rule = ctx.registry.storage_rule
        projection = StorageProjection(rule, thread, ctx)
        AcquireRepositoryStorageEffect(destination, source).check(projection)
        if destination.exists():
            source.qualify(destination, rule, thread)
            Checkout(destination).prepare()
        else:
            repository = source.acquire(rule, thread)
            grant.revalidate(ctx)
            branch = repository.default_branch()
            with repository.acquiring():
                Repo.git(repository.checkout, "fetch", "origin", branch)
            grant.revalidate(ctx)
            start = CheckoutStart.of(repository.checkout, f"work/{destination.name}", f"origin/{branch}")
            CreateCheckoutStorageEffect(destination, source, start).check(projection)
            Checkout.add(repository.checkout, destination, start)
        source.qualify(destination, rule, thread)
        grant.revalidate(ctx)
        return CheckoutResult(destination)


class AdoptCheckoutOperation(CheckoutOperation):
    """Take over one archived owner's retained tree, keeping both session paths intact."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: CliBus, thread: Thread) -> CheckoutResult:
        if len(command.source_checkout) != 1:
            raise ValueError(
                f'adopt needs exactly one --source checkout to take over, got '
                f'{len(command.source_checkout)}; pass the archived tree\'s path'
            )
        source, = command.source_checkout
        source = source.expanduser().absolute()
        grant = command.grant(ctx, thread)
        requested = command.source(ctx, thread)
        repositories = ctx.registry.snapshot().team_project.require().repos
        matching = tuple(repo for repo in repositories if repo.github == requested.github)
        if len(matching) != 1:
            raise ValueError(f'{requested.github} is not one unambiguous registered repository')
        repository, = matching
        grant.require_transfer(ctx, repository)
        return CheckoutResult(ctx.registry.adopt_checkout(
            thread, source, repository, lambda: grant.require_transfer(ctx, repository)))


class TransferCheckoutOperation(CheckoutOperation):
    """Relocate the caller's retained work into its current held allocation."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: CliBus, thread: Thread) -> CheckoutResult:
        destination = command.declared_destination(ctx.registry.storage_rule, thread)
        if len(command.source_checkout) != 1:
            raise ValueError(
                f'transfer needs exactly one --source checkout to move, got '
                f'{len(command.source_checkout)}; pass the retained tree\'s path'
            )
        source, = command.source_checkout
        source = source.expanduser().absolute()
        grant = command.grant(ctx, thread)
        requested = command.source(ctx, thread)
        registered = ctx.registry.snapshot().team_project.require().repos
        matching = tuple(entry for entry in registered if entry.github == requested.github)
        if len(matching) != 1:
            raise ValueError(f'{requested.github} is not one unambiguous registered repository')
        repository, = matching
        grant.require_transfer(ctx, repository)
        transfer = ctx.registry.transfer_checkout(
            thread, Checkout(source), destination, repository,
            lambda: grant.require_transfer(ctx, repository))
        return TransferredCheckoutResult(destination, source, transfer.branch, transfer.head)


class RemoveCheckoutOperation(CheckoutOperation):
    """Retire the caller's own checkout whatever its order's state: the path is the caller's
    allocation, the storage rule refuses its home checkout and any other unit's, and the
    Checkout owner refuses uncommitted work."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: CliBus, thread: Thread) -> CheckoutResult:
        destination = next(iter(command.source_checkout), None)
        destination = (destination.expanduser().absolute() if destination is not None
                       else command.destination(ctx, thread))
        projection = StorageProjection(ctx.registry.storage_rule, thread, ctx)
        RemoveCheckoutStorageEffect(destination).check(projection)
        repository = command.source(ctx, thread)
        if projection.reads.repository(destination).github != repository.github:
            raise ValueError(f'{destination} is not a checkout of {repository.github}')
        Checkout(destination).retire()
        return CheckoutResult(destination)
