"""CLI commands own their options, boundary decoding and operation bodies.

The parser is a projection of dataclass fields; DeclaredFamily is the sole
command catalog. Existing CLI spellings belong to the store_files.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import shlex
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from agent_comms.cli_bridges.codex_records import CodexRolloutProvenance
from agent_comms.cli_verbs import cli_command
from agent_comms.cli_verbs.cli_result import CliResult, TargetEditResult
from agent_comms.comms import Comms
from agent_comms.execution.child_process import NoProcess, ProcessState
from agent_comms.execution.input_origin import InputProvenance
from agent_comms.execution.owner_lifecycle import OwnerRestartResult
from agent_comms.execution.pi_vocabulary import ServiceTier, ThinkingLevelField
from agent_comms.execution.session_file import SessionFile
from agent_comms.execution.turn_context import (
    ContextManifest,
    ContextSegment,
    ContributionCoordinates,
    Provenance,
    ProviderContext,
    TurnContext,
)
from agent_comms.importing import ThreadImported
from agent_comms.messaging.history_result import HistoryPageResult
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    Argument,
    PathText,
    RecordMember,
    WireForm,
    WireOmitDefault,
    boundary_decoder,
)
from agent_comms.nominal.lifecycle_capability import Complete
from agent_comms.planning.goals import Goal
from agent_comms.planning.orders import Todo
from agent_comms.presentation import ThreadRecord, ThreadView
from agent_comms.projects.checkouts import Checkout, CheckoutStart
from agent_comms.projects.head_review import PullRequestHead
from agent_comms.projects.repo import Repo
from agent_comms.projects.shell_effects import (
    AcquireRepositoryStorageEffect,
    CreateCheckoutStorageEffect,
    RemoveCheckoutStorageEffect,
)
from agent_comms.projects.storage_projection import StorageProjection
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.registry.threads import Thread
from agent_comms.storage.typed_table import SQLiteSchemaDifference, StoreRewrite

if TYPE_CHECKING:
    from agent_comms.cli_support.prepared_checkout import PreparedCheckoutCliCommand


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d|w)", value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("duration must look like 30s, 15m, 24h, 7d, or 2w")
    amount = float(match.group(1))
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def _tags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


@boundary_decoder
def _tag_choice_text(value: str | None) -> str | None:
    """Command-line and editor text is the given tags' wire; no text inherits."""
    return value


def _tag_text(value: frozenset[str] | None) -> str | None:
    return None if value is None else ', '.join(sorted(value))


def _shell_words(value: str) -> list[str]:
    return shlex.split(value)


def _message_reference(value: str) -> dict[str, object]:
    """CLI spelling decodes once into a wire reference."""
    sequence, message_id = value.split(":", 1)
    if int(sequence) <= 0 or not message_id:
        raise ValueError("Source must be a committed sequence:message_id reference")
    return {"seq": int(sequence), "message_id": message_id}


@dataclass(frozen=True)
class ThreadStoppedResult(RecordMember, TargetEditResult):
    stopped: str


@dataclass(frozen=True)
class ThreadArchivedResult(RecordMember, TargetEditResult):
    archived: str


@dataclass(frozen=True)
class ThreadCreatedResult(RecordMember, TargetEditResult):
    created: str
    process_identity: ProcessState

    def open_targets(self) -> tuple[str, ...]:
        return (self.created,)


@dataclass(frozen=True)
class ThreadTagsResult(RecordMember, TargetEditResult):
    thread: str
    tags: frozenset[str]


@dataclass(frozen=True)
class TeammatesResult(CliResult):
    teammates: tuple[str, ...]


@dataclass(frozen=True)
class RegisteredRepository:
    """One repository the team works on, and the checkout it reads it from."""

    github: str
    checkout: Path


@dataclass(frozen=True)
class RepositoriesResult(CliResult):
    repositories: tuple[RegisteredRepository, ...]


@dataclass(frozen=True)
class TagRenamedResult(RecordMember, TargetEditResult):
    renamed: str
    name: str


@dataclass(frozen=True)
class ViewDeletedResult(RecordMember, TargetEditResult):
    deleted_view: str


@dataclass(frozen=True)
class TargetReadResult(RecordMember, TargetEditResult):
    read: str


@dataclass(frozen=True)
class ThreadPinnedResult(RecordMember, TargetEditResult):
    thread: str
    pinned: bool


@dataclass(frozen=True)
class TargetCatalogResult(CliResult):
    actions: tuple[cli_command.TargetAction, ...]

    def to_wire(self) -> object:
        return {'actions': [action.encode() for action in self.actions]}


@dataclass(frozen=True)
class UserMessageSentResult(CliResult):
    sent: MessageReference


@dataclass(frozen=True)
class ThreadConfiguredResult(RecordMember, TargetEditResult):
    configured: str
    model: ThreadModel
    thinking_level: ThinkingLevelField
    service_tier: ServiceTier


@dataclass(frozen=True)
class ConstraintPinnedResult(CliResult):
    pin: MessageReference
    source: MessageReference | InputProvenance


@dataclass(frozen=True)
class ConstraintSupersededResult(CliResult):
    supersession: MessageReference


@dataclass(frozen=True)
class ConstraintDroppedResult(CliResult):
    drop: MessageReference


@dataclass(frozen=True)
class InboxResult(HistoryPageResult):
    """The newest unread page, newest first, and how many unread messages there are in all."""

    pending: int

    def to_wire(self) -> dict[str, object]:
        return {**super().to_wire(), "pending": self.pending}


@dataclass(frozen=True)
class MergeReviewResult(CliResult):
    heads: tuple[PullRequestHead, ...]


@dataclass(frozen=True)
class WhoResult(CliResult):
    who: tuple[Annotated[ThreadView, WireForm], ...]


@dataclass(frozen=True)
class ThreadActivityRow:
    thread: str
    state: ActivityState
    detail: str
    ts: float


@dataclass(frozen=True)
class ActiveStatusResult(CliResult):
    status: tuple[ThreadActivityRow, ...]


@dataclass(frozen=True)
class ChannelsResult(CliResult):
    channels: tuple[str, ...]


@dataclass(frozen=True)
class ThreadsResult(CliResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]


@dataclass(frozen=True)
class HeartbeatResult(CliResult):
    heartbeat: str


@dataclass(frozen=True)
class SessionAttachedResult(CliResult):
    attached: str
    session_file: SessionFile
    process: Annotated[ProcessState, WireOmitDefault()] = dataclasses.field(
        default_factory=NoProcess
    )


@dataclass(frozen=True)
class CheckpointResult(CliResult, DeclaredFamily, affix="Checkpoint"):
    """What one checkpoint did with a thread's uncommitted work."""

    family_discriminator = "checkpoint"
    thread: str
    branch: str


@dataclass(frozen=True)
class CleanCheckpoint(CheckpointResult):
    """The worktree had nothing uncommitted."""


@dataclass(frozen=True)
class PushedCheckpoint(CheckpointResult):
    commit: str


@dataclass(frozen=True)
class StoppedCheckpoint(CheckpointResult, declared_name=None):
    """A checkpoint that stopped before a named push, and why."""

    reason: str


@dataclass(frozen=True)
class RefusedCheckpoint(StoppedCheckpoint):
    """The declared worktree is not a git worktree."""


@dataclass(frozen=True)
class FailedCheckpoint(StoppedCheckpoint):
    """The commit failed."""


@dataclass(frozen=True)
class PushUnknownCheckpoint(StoppedCheckpoint):
    """The commit exists locally; the push could not be named."""


@dataclass(frozen=True)
class ActivitySetResult(CliResult):
    activity: str
    state: str


@dataclass(frozen=True)
class OwnersRestartedResult(RecordMember, TargetEditResult):
    restarted: tuple[OwnerRestartResult, ...]


@dataclass(frozen=True)
class OwnerReleasedResult(CliResult):
    released: str


@dataclass(frozen=True)
class LedgerMergedResult(CliResult):
    merged: bool = True


@dataclass(frozen=True)
class SubagentsImportedResult(CliResult):
    imported: tuple[ThreadImported, ...]


@dataclass(frozen=True)
class ImportedContextSources(CliResult):
    scope: str
    sources: tuple[CodexRolloutProvenance, ...]


@dataclass(frozen=True)
class RecordedContextManifests(CliResult):
    manifests: tuple[ContextManifest, ...]
    text_recorded: bool


@dataclass(frozen=True)
class CoreSegmentText:
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]
    text: str


@dataclass(frozen=True, kw_only=True)
class ContextInspectionInput:
    """The supplied input, core context and segments a context inspection reads."""

    input_supplied: bool
    core_context: TurnContext
    segments: tuple[CoreSegmentText, ...]


@dataclass(frozen=True, kw_only=True)
class CoreContextInspection(ContextInspectionInput, CliResult):
    """Current core contributors; native_error says why the native base is absent."""
    scope: str
    native_error: str


@dataclass(frozen=True)
class NativeSourceSpans:
    segment: int
    sha256: str
    spans: tuple[ContributionCoordinates, ...]


@dataclass(frozen=True, kw_only=True)
class NativeContextInspection(ContextInspectionInput, CliResult):
    """The next native base together with the current core contributors."""
    scope: str
    native_manifest: tuple[ContextSegment, ...]
    native_provider_context: ProviderContext
    native_source_spans: tuple[NativeSourceSpans, ...]


@dataclass(frozen=True, kw_only=True)
class NameArgument:
    name: Annotated[str, Argument(flags=("--name",))]


@dataclass(frozen=True, kw_only=True)
class ThreadArgument:
    thread: Annotated[str, Argument(flags=("--thread",))]


@dataclass(frozen=True, kw_only=True)
class WorktreeArgument:
    worktree: Annotated[Checkout, Argument(flags=("--worktree",))]


@dataclass(frozen=True)
class StoreDifference:
    """One schema object a reconcile rebuilt a store for."""

    path: Annotated[Path, PathText]
    difference: SQLiteSchemaDifference


@dataclass(frozen=True)
class ReconciledStoresResult(CliResult):
    """The bus files whose schema the reconcile changed, relative to the bus root, and the
    objects that made each one differ. An outgoing runtime's reply names no differences: the
    installer's restore (installer/upgrade.py restore_stores) decodes it with this class."""

    rewritten: tuple[Annotated[Path, PathText], ...]
    differences: tuple[StoreDifference, ...] = ()


@dataclass(frozen=True)
class PlannedStoresResult(CliResult):
    """Target-only rewrite plans; outgoing reconcile replies remain unchanged."""

    rewrites: tuple[StoreRewrite, ...]

    @property
    def rewritten(self) -> tuple[Path, ...]:
        return tuple(rewrite.path for rewrite in self.rewrites)

    @property
    def backup_files(self) -> tuple[Path, ...]:
        return tuple(path for rewrite in self.rewrites for path in rewrite.backup_files())


@dataclass(frozen=True)
class BusReadBack(CliResult):
    """What a bus's registry holds that a carry must keep: its threads."""

    threads: int

    def lines(self) -> tuple[str, ...]:
        """A read-back proves the carried bus reads; only an answered message proves an
        agent works on it, and an install answers none."""
        return (f"Read back: {self.threads} threads",
                "No live agent has answered a message on this install yet")


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

    def revalidate(self, ctx: Comms) -> None:
        """A long acquisition cannot spend a retired caller or a changed goal."""
        current = ctx.registry.require(self.thread.name)
        if current.incarnation != self.thread.incarnation:
            raise ValueError('The checkout caller incarnation changed during acquisition')
        dataclasses.replace(self, thread=current).require()

    def require_transfer(self, ctx: Comms, repository: Repo) -> None:
        raise ValueError('Checkout transfer requires a current held problem')


@dataclass(frozen=True)
class OrderCheckoutGrant(CheckoutGrant):
    todo: Todo

    def require(self) -> None:
        self.todo.require_assignee(self.thread.incarnation, self.todo.held().generation)

    def revalidate(self, ctx: Comms) -> None:
        super().revalidate(ctx)
        ctx.goals.todos.get(self.todo.id).require_assignee(
            self.thread.incarnation, self.todo.held().generation)

    def require_transfer(self, ctx: Comms, repository: Repo) -> None:
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
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread) -> CheckoutResult: ...


class AddCheckoutOperation(CheckoutOperation):
    """Create from origin's declared default or repair the checkout's pinned submodules;
    the caller's order or goal grants the allocation."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread) -> CheckoutResult:
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
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread) -> CheckoutResult:
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
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread) -> CheckoutResult:
        destination = command.declared_destination(ctx.registry.storage_rule, thread)
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
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread) -> CheckoutResult:
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


@dataclass(frozen=True)
class RouteReplacedResult(CliResult):
    root: str
    route: str
