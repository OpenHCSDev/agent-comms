"""Thread registration and cross-store identity transactions."""

from __future__ import annotations

import asyncio
import re
from abc import abstractmethod
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms.coordination_errors import CoordinationError

from .channel_targets import DeclaredTag
from .child_process import NO_PROCESS, ProcessIdentity, ProcessState
from .cli_result import CliResult
from .goal_waits import GoalWaits
from .native_entries import ImportedSessionWriter
from .locked_store import PatternFile, RuntimeFile
from .process_claim import ClaimHeldError, ProcessSingleton
from .registration import Registration
from .relationships import RelationshipStore
from .repo import Checkout, LaunchCheckout
from .thread_execution import NativeThreadExecution
from .thread_status import StoppedThreadStatus
from .view_unread import transcript_read_state

if TYPE_CHECKING:
    from .registry_store import RegistryEdit
    from .cli_bridges.history import History
    from .tool_caller import ThreadCaller
from .agent_activity import AgentActivity
from .agent_backend import RuntimeLocation
from .catalog_store import ChannelCatalog
from .collaboration_ledger import CollaborationLedger
from .config_options import RunningSelection
from .errors import RelationViolationError
from .field_codec import (
    DeclaredWireType,
    FieldCodec,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
)
from .importing import ImportLimits, ImportedConversation, ThreadImported
from .launch_runtime import LaunchRuntime, TagListText
from .message_bus import MessageBus
from .native_arguments import ConfiguredBinary
from .native_settings import NativeThreadDefaults
from .owner_lifecycle import LaunchInput, OwnerLifecycle, TaskLaunchInput
from .pi_vocabulary import NoThinkingOverride, ThinkingChoice, ThinkingLevelField
from .registry_provenance import RegistryNames
from .session_file import RegistrySessionLocation
from .thread_identity import AgentThreadRole, ThreadRole
from .thread_model import DeclaredModel, ThreadModel, UndeclaredModel
from .thread_owned_state import ThreadOwnedState
from .thread_text import ForkedParent, GivenThreadText, NoThreadText, ThreadText
from .threads import KeptModel, ModelChoice, NextTurnConfiguration, Thread
from .native_session_reopen import PiSessionId
from .tool_result import ToolResult


@dataclass(frozen=True, slots=True)
class RenameThreadResult(ToolResult):
    previous: str
    current: str
    changed: bool


class TagChoice(SubclassUnion, affix="Tags"):
    """Select explicit, inherited or changed tags from an existing set."""

    def for_new(self) -> frozenset[str]:
        return self.for_child(frozenset())

    @abstractmethod
    def for_child(self, parent: frozenset[str]) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class GivenTags(DeclaredWireType, TagChoice):
    """Tags spelled as comma-separated text on the command line, in the editor and in tools."""

    wire_type = str
    tags: frozenset[str]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls(TagListText.from_text(FieldCodec.decode(cls.wire_type, data)))

    def to_wire(self) -> str:
        return TagListText.encode(self.tags)

    def for_child(self, parent: frozenset[str]) -> frozenset[str]:
        return self.tags


@dataclass(frozen=True, slots=True)
class InheritedTags(NullMember, TagChoice):
    def for_child(self, parent: frozenset[str]) -> frozenset[str]:
        return parent


@dataclass(frozen=True, slots=True)
class ChangedTags(RecordMember, TagChoice):
    add: frozenset[str]
    remove: frozenset[str]

    def for_child(self, parent: frozenset[str]) -> frozenset[str]:
        return (parent | self.add) - self.remove


class ImportDirectory(SubclassUnion, affix="Directory"):
    """Where an imported thread works: a given directory, or its source's project."""

    @abstractmethod
    def project(self, snapshot: ImportedConversation) -> str: ...

    @abstractmethod
    def local_directory(self) -> Path:
        """The directory a running harness's session is asked for."""


@dataclass(frozen=True, slots=True)
class GivenDirectory(SingleFieldValue, ImportDirectory):
    path: str

    def project(self, snapshot: ImportedConversation) -> str:
        return self.path

    def local_directory(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(frozen=True, slots=True)
class SourceDirectory(NullMember, ImportDirectory):
    def project(self, snapshot: ImportedConversation) -> str:
        return snapshot.project

    def local_directory(self) -> Path:
        return Path.cwd()


@dataclass(frozen=True, slots=True, kw_only=True)
class NewThreadSpec:
    """Prepare the session, then register the thread under its registry lock."""

    name: str
    project: LaunchCheckout
    model: ThreadModel
    thinking_level: ThinkingLevelField
    task: LaunchInput = field(default_factory=LaunchInput)
    tags: TagChoice = InheritedTags()

    @classmethod
    def from_task(cls, *, task: str = "", **values) -> NewThreadSpec:
        return cls(task=TaskLaunchInput(task) if task else LaunchInput(), **values)

    def declaration(self) -> Thread:
        return Thread(name=self.name, task=self.task.thread_text(), tags=self.tags.for_new(),
                      worktree=self.project, model=self.model, thinking_level=self.thinking_level)

    def declare(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Thread:
        project = self.project.path
        thread = self.declaration()
        threads.registry.require_declaration_available(thread)
        location = thread.runtime.new_location(threads.root, project)
        saved = RegistrySessionLocation.from_path(location.read_key, threads.root)
        return threads.registry.declare(replace(thread, session_file=saved))


@dataclass(frozen=True, slots=True, kw_only=True)
class ForkSpec(NewThreadSpec):
    """A fork differs only in its parent-derived source and inherited settings."""

    parent: str

    def _parent(self, threads: ThreadManagement) -> Thread:
        parent = threads.registry.require(self.parent)
        parent.execution.require_native()
        parent.runtime.require_forkable()
        return parent

    def child(self, parent: Thread, runtime: RuntimeLocation, threads: ThreadManagement) -> Thread:
        saved = RegistrySessionLocation.from_path(runtime.read_key, threads.root)
        return replace(self.declaration(),
                       parent=ForkedParent(parent.name), tags=self.tags.for_child(DeclaredTag.for_child(parent.tags)),
                       backend=parent.backend, session_file=saved)

    def declare(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Thread:
        # Copy saved history before locking the registry for its declaration.
        parent = self._parent(threads)
        threads.registry.require_declaration_available(self.child(parent, parent.runtime, threads))
        location = asyncio.run(parent.backend.fork(parent, pi_bin, runtime, threads.root))
        return threads.registry.declare_child(
            parent, lambda current: self.child(current, location, threads),
        )


class ThreadDeletionClaim(ProcessSingleton, RuntimeFile, PatternFile):
    """One cleanup controller per recorded participant; unrelated deletions remain independent."""

    filename = ".thread-delete-{lookup}.lock"

    @classmethod
    def for_thread(cls, root: Path, thread: Thread) -> Self:
        return cls(cls.path_in(root, lookup=thread.incarnation.require_recorded().lookup))


@dataclass(frozen=True, slots=True)
class ThreadDeleted(CliResult):
    deleted: str
    session_bytes_freed: int
    detached_children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectChangeResult:
    thread: str
    previous: str
    current: str
    changed: bool


@dataclass(eq=False, repr=False)
class ThreadManagement:
    root: Path
    registry: Registration
    bus: MessageBus
    catalog: ChannelCatalog
    agents: AgentActivity
    owners: OwnerLifecycle
    ledger: CollaborationLedger

    def import_thread(
        self,
        history: History,
        *,
        name: str,
        worktree: ImportDirectory = SourceDirectory(),
        model: ModelChoice = KeptModel(),
        tags: frozenset[str] = frozenset(),
        limits: ImportLimits = ImportLimits(),
    ) -> ThreadImported:
        """Import a stopped, resumable thread from any harness session, not its executor."""
        snapshot = history.imported(limits)
        directory = worktree.project(snapshot)
        if not directory:
            raise ValueError("The source has no project directory; supply --worktree.")
        project = Path(directory).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The imported project does not exist locally; supply --worktree.")
        session_path = ImportedSessionWriter.path_in(self.root, session_id=PiSessionId.fresh())
        thread = model.configured(Thread(
            name,
            tags,
            Checkout(project),
            session_file=RegistrySessionLocation.from_path(session_path, self.root),
            title=snapshot.title,
        ))
        self.registry.snapshot().require_fresh_name(name)
        self.registry.require_declaration_available(thread, StoppedThreadStatus())
        with ImportedSessionWriter.preparing(session_path, snapshot, project) as publish:
            with self.registry.store.editing() as edit:
                edit.document.require_fresh_name(name)
                publish()
                try:
                    self.registry._declare_in(edit, thread, StoppedThreadStatus())
                except (OSError, ValueError, CoordinationError):
                    if name in self.registry:
                        edit.document.remove(name)
                        edit.commit()
                    session_path.unlink(missing_ok=True)
                    raise
        return ThreadImported.of(name, session_path, snapshot)

    def rehome_thread(
        self, history: History, *, name: str, limits: ImportLimits = ImportLimits(),
    ) -> ThreadImported:
        """Move an external unit onto native Pi on its existing declaration.

        The birth (``created_at``), goal, orders, storage roots and inbox cursor stay;
        only the execution and the session change. The harness's own bridge is retired
        first, so one inbox never has two deliverers.
        """
        snapshot = history.imported(limits)
        session_path = ImportedSessionWriter.path_in(self.root, session_id=PiSessionId.fresh())
        selected = self.registry.snapshot()
        original = selected.require(name)
        original.execution.require_external()
        with ImportedSessionWriter.preparing(session_path, snapshot, original.worktree.path) as publish:
            with self.registry.store.editing() as edit:
                current = edit.document.require(name)
                if not edit.original.snapshot.same_thread(current, selected, original):
                    raise RelationViolationError("External owner changed while preparing its native session.")
                rehomed = replace(
                    current, execution=NativeThreadExecution, process_identity=NO_PROCESS,
                    session_file=RegistrySessionLocation.from_path(session_path, self.root),
                )
                self.registry.require_declaration_available(rehomed, StoppedThreadStatus())
                publish()
                bridge = snapshot.harness.retire_session(snapshot.source_id)
                try:
                    self.registry._declare_in(edit, rehomed, StoppedThreadStatus())
                except (OSError, ValueError, CoordinationError, RelationViolationError):
                    bridge.restore()
                    session_path.unlink(missing_ok=True)
                    raise
        return ThreadImported.of(name, session_path, snapshot)

    def claim_thread(
        self,
        wanted: str,
        *,
        tags: frozenset[str],
        worktree: Checkout,
        process: ProcessState = NO_PROCESS,
        model: ThreadModel = UndeclaredModel(),
        thinking_level: type[ThinkingChoice] = NoThinkingOverride,
        auto_title_pending: bool = False,
        role: type[ThreadRole] = AgentThreadRole,
    ) -> Thread:
        """Atomically register a unique thread; its inbox starts at its creation."""
        thread = Thread(
            name=RegistryNames.thread_slug(wanted),
            tags=tags,
            worktree=worktree,
            process_identity=process,
            model=model,
            thinking_level=thinking_level,
            auto_title_pending=auto_title_pending,
            role=role,
        )
        return self.registry._claim_unlocked(thread)

    def rename_self(self, new_name: str, *, caller: ThreadCaller) -> RenameThreadResult:
        """Rename the bound caller's own thread, retaining its old aliases."""
        with self.registry.store.editing() as edit:
            if edit.document.require(caller.name).auto_title_pending and (
                len(new_name) > 48 or len(re.split(r"[-_]+", new_name)) > 6
            ):
                raise ValueError("Choose a concise topic title: at most 6 words and 48 characters.")
            return self._rename_thread_in(
                edit, caller.name, new_name, title=GivenThreadText(new_name.replace("-", " ").replace("_", " "))
            )

    def set_project(self, name: str, path: str) -> ProjectChangeResult:
        """Move one thread's project, preserving its owner and conversation."""
        if not path.strip():
            raise ValueError("Project path cannot be empty.")
        original = self.registry.require(name)
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = original.worktree.path / target
        try:
            target = target.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Cannot open project directory: {target}") from error
        if not target.is_dir():
            raise ValueError(f"Project path is not a directory: {target}")
        project = Checkout(target)

        def move(current: Thread) -> Thread:
            current.require_original_project(original)
            return current if current.project_path == target else current.moved_to_project(project)

        updated = self.registry.update_thread(name, move)
        previous, current = str(original.project_path), str(updated.project_path)
        return ProjectChangeResult(updated.name, previous, current, current != previous)

    def rename_managed_thread(
        self, name: str, display_name: str, *, owner: ProcessIdentity
    ) -> RenameThreadResult:
        """Rename a locally managed running thread after proving process ownership."""
        with self.registry.store.editing() as edit:
            thread = edit.document.require(name)
            if thread.process_identity != owner:
                raise RelationViolationError(
                    f"Process {owner.pid} does not own thread {thread.name!r}."
                )
            new_name = edit.document.claim_name(display_name, owner=thread.name)
            return self._rename_thread_in(edit, thread.name, new_name, title=GivenThreadText(display_name))

    def _rename_thread(
        self, name: str, new_name: str, *, title: ThreadText = NoThreadText()
    ) -> RenameThreadResult:
        # Every rename checks and changes the same current registry declaration.
        with self.registry.store.editing() as edit:
            return self._rename_thread_in(edit, name, new_name, title=title)

    def _rename_thread_in(
        self, edit: RegistryEdit, name: str, new_name: str, *, title: ThreadText = NoThreadText()
    ) -> RenameThreadResult:
        previous, current = edit.document.rename(name, new_name)
        thread = edit.document.require(current)
        titled = replace(thread, auto_title_pending=False, title=title.applied(thread.title))
        if titled != thread:
            self.registry._commit_registration(edit, edit.document.prepare_registration(
                titled, edit.document.status(current), new_owner=False))
        edit.commit()
        changed = previous != current
        if changed:
            edit.released.append(partial(self._rename_stores, previous, current))
        return RenameThreadResult(previous, current, changed)

    def _rename_stores(self, previous: str, current: str) -> None:
        self.agents.activity.rename_thread(previous, current)
        self.agents.runtime_info.rename_thread(previous, current)
        self.ledger.rename_thread(previous, current)
        self.catalog.change(lambda document: document.rename_thread(previous, current))

    def heartbeat(self, name: str) -> None:
        self.registry.heartbeat(name)

    def set_thread_configuration(
        self, original: Thread, configuration: NextTurnConfiguration,
    ) -> Thread:
        """Publish next-turn settings together against their acquired declaration.

        Progress and goals belong to the current document. A changed selection
        or project invalidates the native settings result acquired by the caller.
        """
        Thread.require_declaration(original)

        def configure(thread: Thread) -> Thread:
            thread.require_original_model_configuration(original)
            thread.execution.require_native()
            return configuration.configured(thread)

        return self.registry.update_thread(original.name, configure)

    def initialize_native_configuration(
        self, name: str, *, model: ThreadModel, thinking_level: type[ThinkingChoice],
    ) -> Thread:
        """Persist one first-known producer observation through its declaration."""
        return self.registry.update_thread(
            name, lambda thread: thread.initialized_native_configuration(
                model=model, thinking_level=thinking_level,
            ),
        )

    def running_selection(self, name: str) -> RunningSelection:
        thread = self.registry.require(name)
        return RunningSelection(
            thread,
            thread.model.applied(self.agents.agent_info_of(thread.name).model),
        )

    def resolve_thread_model(
        self, name: str, default: DeclaredModel = NativeThreadDefaults.model,
    ) -> str:
        """The running model, otherwise the declared default."""
        return self.running_selection(name).model.otherwise(default.text)

    def attach_session(
        self, original: Thread, source_path: str, *, process: ProcessState,
    ) -> Thread:
        """Read Pi state first, then publish its model under the registry lock."""
        path = Path(source_path).expanduser().resolve()
        location = original.backend.location_type(str(path))
        saved_model = ThreadModel.from_choice(location.model_choice) if path.is_file() else UndeclaredModel()
        attached = replace(
            original.initialized_native_configuration(
                model=saved_model, thinking_level=NoThinkingOverride,
            ),
            process_identity=process,
            session_file=RegistrySessionLocation.from_path(location.read_key, self.root),
        )
        return self.registry.replace_original(original, attached)

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        with self.registry.store.editing() as edit:
            self._archive_in(edit, (edit.document.require(name),))

    def _archive_in(self, edit: RegistryEdit, originals: Sequence[Thread]) -> None:
        edit.document.archive_originals(originals)
        edit.commit()
        edit.released.append(partial(self.agents.runtime_info.remove_threads, originals))

    @property
    def thread_owned_states(self) -> tuple[ThreadOwnedState, ...]:
        """Every store whose rows end with a thread declaration."""
        states = (
            self.agents.activity,
            self.agents.runtime_info,
            self.ledger,
            self.catalog,
            self.owners.releases,
            self.bus.reads,
            transcript_read_state(self.bus.reads.path),
            GoalWaits(self.root),
            RelationshipStore.at(self.root),
        )
        ThreadOwnedState.require_complete(states)
        return states

    def delete(self, name: str) -> ThreadDeleted:
        """Permanently delete one stopped or archived thread."""
        return self._delete_originals((self.registry.require(name),))[0]

    def _delete_originals(self, originals: Sequence[Thread]) -> tuple[ThreadDeleted, ...]:
        """Remove stopped declarations and everything they own.

        The bus, goal history and uncertain-input records are the record and
        stay; their readers resolve a removed name as a deleted thread. Every
        refusal happens before the registry withdraws input permission.
        """
        with ExitStack() as cleanup:
            for original in originals:
                try:
                    cleanup.enter_context(ThreadDeletionClaim.for_thread(self.root, original).claimed())
                except ClaimHeldError as error:
                    raise RelationViolationError(
                        f"Thread {original.name!r} already has a permanent-deletion controller.") from error
            states = self.thread_owned_states
            sessions = {}
            for original in originals:
                if original.process_alive:
                    raise RelationViolationError(
                        f"Thread {original.name!r} still has a live process; stop it first."
                    )
                sessions[original.name] = original.runtime.storage_files()
                for files in sessions[original.name]:
                    files.require_unreferenced()
            with self.registry.store.editing() as edit:
                edit.document.require_originals(originals)
                for original in originals:
                    if not edit.document.require(original.name).same_launch(original):
                        raise RelationViolationError("Thread session changed while preparing permanent deletion.")
                edit.document.begin_delete_originals(originals)
                edit.commit()
            for state in states:
                state.remove_threads(originals)
            # A failure from here leaves a Deleting declaration to retry, not orphans.
            freed = {name: sum(item.delete() for item in files) for name, files in sessions.items()}
            with self.registry.store.editing() as edit:
                detached = edit.document.remove_originals(originals)
                edit.commit()
            return tuple(
                ThreadDeleted(original.name, freed.get(original.name, 0), detached[original.name])
                for original in originals
            )

    def create(self, spec: NewThreadSpec) -> Thread:
        """Prepare the session, then register and launch the thread with its task as first input."""
        resolved_bin = ConfiguredBinary().configured()
        runtime = LaunchRuntime.running()
        child = spec.declare(self, resolved_bin, runtime)
        # A failed launch cannot erase a declaration another starter reserved,
        # nor erase the first input if reservation committed before the failure.
        return self.owners._launch_owner(
            child, resolved_bin, initial_input=spec.task, launch_runtime=runtime,
        )

