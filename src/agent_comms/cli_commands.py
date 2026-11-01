"""CLI commands own their options, boundary decoding and operation bodies.

The parser is a projection of dataclass fields; DeclaredFamily is the sole
command catalog. Existing CLI spellings belong to the store_files.
"""

from __future__ import annotations

from agent_comms import cli_command


import argparse
import dataclasses
import re
import shlex
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TYPE_CHECKING


from .activity import ActivityState
from .child_process import NoProcess, ProcessState
from .cli_result import CliResult
from .comms import Comms
from .nominal.declared_family import DeclaredFamily
from .nominal.field_codec import Argument, PathText, WireForm, WireOmitDefault, boundary_decoder
from .history_result import HistoryPageResult
from .goals import Goal
from .lifecycle_capability import Complete
from .importing import ThreadImported
from .input_origin import InputProvenance
from .message_reference import MessageReference
from .owner_lifecycle import OwnerRestartResult
from .pi_vocabulary import ServiceTier, ThinkingLevelField
from .presentation import ThreadRecord, ThreadView
from .repo import Checkout, CheckoutStart, Repo
from .storage_projection import StorageProjection
from .shell_effects import CheckoutStorageEffect, CreateCheckoutStorageEffect, RemoveCheckoutStorageEffect
from .thread_model import ThreadModel
from .threads import Thread
from .todos import Todo
from .head_review import PullRequestHead
from .tool_result import ToolResult
from .turn_context import CodexRolloutProvenance, ContextManifest, ContextSegment, ContributionCoordinates, Provenance, TurnContext
from .typed_table import StoreRewrite

from agent_comms.session_file import SessionFile

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
class ThreadStoppedResult(CliResult, ToolResult):
    stopped: str


@dataclass(frozen=True)
class ThreadArchivedResult(CliResult, ToolResult):
    archived: str


@dataclass(frozen=True)
class ThreadCreatedResult(CliResult, ToolResult):
    created: str
    process_identity: ProcessState

    def open_targets(self) -> tuple[str, ...]:
        return (self.created,)


@dataclass(frozen=True)
class ThreadTagsResult(CliResult):
    thread: str
    tags: frozenset[str]


@dataclass(frozen=True)
class CommandersResult(CliResult):
    leads: tuple[str, ...]
    commanders: tuple[str, ...]


@dataclass(frozen=True)
class TagRenamedResult(CliResult):
    renamed: str
    name: str


@dataclass(frozen=True)
class ViewDeletedResult(CliResult):
    deleted_view: str


@dataclass(frozen=True)
class TargetReadResult(CliResult):
    read: str


@dataclass(frozen=True)
class ThreadPinnedResult(CliResult):
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
class ThreadConfiguredResult(CliResult):
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
class OwnersRestartedResult(CliResult):
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


@dataclass(frozen=True)
class CoreContextInspection(CliResult):
    """Current core contributors; native_error says why the native base is absent."""
    scope: str
    input_supplied: bool
    core_context: TurnContext
    segments: tuple[CoreSegmentText, ...]
    native_error: str


@dataclass(frozen=True)
class NativeSourceSpans:
    segment: int
    sha256: str
    spans: tuple[ContributionCoordinates, ...]


@dataclass(frozen=True)
class NativeContextInspection(CliResult):
    """The next native base together with the current core contributors."""
    scope: str
    input_supplied: bool
    core_context: TurnContext
    segments: tuple[CoreSegmentText, ...]
    native_manifest: tuple[ContextSegment, ...]
    native_provider_context: Mapping[str, object]
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
class ReconciledStoresResult(CliResult):
    """The bus files whose schema the reconcile changed, relative to the bus root."""

    rewritten: tuple[Annotated[Path, PathText], ...]


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

    @abstractmethod
    def source(self) -> Repo: ...

    def require_transfer(self, ctx: Comms, repository: Repo) -> None:
        raise ValueError('Checkout transfer requires a current held problem')


@dataclass(frozen=True)
class OrderCheckoutGrant(CheckoutGrant):
    todo: Todo

    def require(self) -> None:
        self.todo.require_assignee(self.thread.incarnation, self.todo.held().generation)

    def source(self) -> Repo:
        return self.todo.repository

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
    repository: Repo

    def require(self) -> None:
        self.thread.goal.require().require_id(self.goal.id)
        if self.goal.state.items_with(Complete, self.goal):
            raise ValueError("A completed goal cannot allocate a checkout.")

    def source(self) -> Repo:
        return self.repository


class CheckoutOperation(DeclaredFamily, affix="CheckoutOperation"):
    """What a checkout command does to the caller's isolated repository. Each
    member owns its required authority, storage check and Git work, so adding
    and retiring stay in the Checkout owner."""

    @classmethod
    @abstractmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread,
              destination: Path) -> CheckoutResult: ...


class AddCheckoutOperation(CheckoutOperation):
    """Create from origin's declared default or repair the checkout's pinned submodules;
    the caller's order or goal grants the allocation."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread,
              destination: Path) -> CheckoutResult:
        grant = command.grant(ctx, thread)
        grant.require()
        repository = command.source(ctx, grant)
        projection = StorageProjection(ctx.registry.storage_rule, thread, ctx)
        if destination.exists():
            CheckoutStorageEffect(destination, repository).check(projection)
            Checkout(destination).prepare()
        else:
            branch = repository.default_branch()
            Repo.git(repository.checkout, "fetch", "origin", branch)
            start = CheckoutStart.of(repository.checkout, f"work/{destination.name}", f"origin/{branch}")
            CreateCheckoutStorageEffect(destination, repository, start).check(projection)
            Checkout.add(repository.checkout, destination, start)
        return CheckoutResult(destination)


class TransferCheckoutOperation(CheckoutOperation):
    """Relocate the caller's retained work into its current held allocation."""

    @classmethod
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread,
              destination: Path) -> CheckoutResult:
        source, = command.source_checkout
        source = source.expanduser().absolute()
        grant = command.grant(ctx, thread)
        requested = command.source(ctx, grant)
        registered = ctx.registry.snapshot().commander_project.require().repos
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
    def apply(cls, command: PreparedCheckoutCliCommand, ctx: Comms, thread: Thread,
              destination: Path) -> CheckoutResult:
        RemoveCheckoutStorageEffect(destination).check(
            StorageProjection(ctx.registry.storage_rule, thread, ctx))
        Checkout(destination).retire()
        return CheckoutResult(destination)


@dataclass(frozen=True)
class RouteReplacedResult(CliResult):
    root: str
    route: str


