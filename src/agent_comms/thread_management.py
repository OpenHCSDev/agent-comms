"""Thread registration and cross-store identity transactions."""

from __future__ import annotations

import asyncio
import re
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms.coordination_errors import CoordinationError

from .child_process import NO_PROCESS, ProcessIdentity, ProcessState
from .cli_result import CliResult
from .goal_waits import GoalWaits
from .native_entries import ImportedSessionWriter
from .native_session_files import NativeSessionFiles
from .registration import Registration
from .relationships import RelationshipStore
from .repo import Checkout, LaunchCheckout
from .thread_execution import NativeThreadExecution
from .thread_status import StoppedThreadStatus
from .view_unread import transcript_read_state

if TYPE_CHECKING:
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
from .store_files import AtomicFile
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

    def declaration(self, registry: Registration) -> Thread:
        return Thread(name=self.name, task=self.task.thread_text(), tags=self.tags.for_new(),
                      worktree=Checkout(self.project.path), model=self.model, thinking_level=self.thinking_level)

    @contextmanager
    def prepared(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Iterator[Thread]:
        project = self.project.path
        thread = self.declaration(threads.registry)
        threads.registry.require_declaration_available(thread)
        location = thread.runtime.new_location(threads.root, project)
        with threads.registry.store.guarded_write():
            saved = RegistrySessionLocation.from_path(location.read_key, threads.root)
            yield replace(thread, session_file=saved)


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
        return replace(self.declaration(threads.registry),
                       parent=ForkedParent(parent.name), tags=self.tags.for_child(parent.tags),
                       backend=parent.backend, session_file=saved)

    @contextmanager
    def prepared(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Iterator[Thread]:
        # Copy saved history before locking the registry for its declaration.
        parent = self._parent(threads)
        threads.registry.require_declaration_available(self.child(parent, parent.runtime, threads))
        location = asyncio.run(parent.backend.fork(parent, pi_bin, runtime, threads.root))
        with threads.registry.store.guarded_write():
            current = self._parent(threads)
            if current.incarnation != parent.incarnation or current.runtime != parent.runtime:
                raise RelationViolationError(
                    f"Parent thread {self.parent!r} changed its saved session during the fork; "
                    f"the created fork {location} was not registered."
                )
            yield self.child(current, location, threads)


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
        with self.registry.store.guarded_write():
            if self.registry.snapshot().taken(name):
                raise ValueError(f"Thread name {name!r} is already reserved.")
            self.registry.require_declaration_available(thread, StoppedThreadStatus())
            self._write_imported_session(session_path, snapshot, project)
            try:
                self.registry._declare_unlocked(thread, StoppedThreadStatus())
                self.bus.mark_delivered_through(name, self.bus.bus.revision())
            except (OSError, ValueError, CoordinationError):
                if name in self.registry:
                    self.registry.remove(name)
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
        with self.registry.store.guarded_write():
            current = self.registry.snapshot().require(name)
            current.execution.require_external()
            self._write_imported_session(session_path, snapshot, Path(current.worktree))
            rehomed = replace(
                current, execution=NativeThreadExecution, process_identity=NO_PROCESS,
                session_file=RegistrySessionLocation.from_path(session_path, self.root),
            )
            self.registry.require_declaration_available(rehomed, StoppedThreadStatus())
            bridge = snapshot.harness.retire_session(snapshot.source_id)
            try:
                self.registry._declare_unlocked(rehomed, StoppedThreadStatus())
            except (OSError, ValueError, CoordinationError, RelationViolationError):
                bridge.restore()
                session_path.unlink(missing_ok=True)
                raise
        return ThreadImported.of(name, session_path, snapshot)

    def _write_imported_session(
        self, session_path: Path, snapshot: ImportedConversation, project: Path,
    ) -> None:
        NativeSessionFiles.prepare_private_directory(session_path.parent)
        AtomicFile(session_path).write_bytes(snapshot.session_bytes(project))

    def claim_thread(
        self,
        wanted: str,
        *,
        tags: frozenset[str],
        worktree: str,
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
            worktree=Checkout.from_wire(worktree),
            process_identity=process,
            model=model,
            thinking_level=thinking_level,
            auto_title_pending=auto_title_pending,
            role=role,
        )
        return self.registry._claim_unlocked(thread)

    def rename_self(self, new_name: str, *, caller: ThreadCaller) -> RenameThreadResult:
        """Rename the bound caller's own thread, retaining its old aliases."""
        with self.registry.store.guarded_write():
            if self.registry.require(caller.name).auto_title_pending and (
                len(new_name) > 48 or len(re.split(r"[-_]+", new_name)) > 6
            ):
                raise ValueError("Choose a concise topic title: at most 6 words and 48 characters.")
            return self._rename_thread_unlocked(
                caller.name, new_name, title=GivenThreadText(new_name.replace("-", " ").replace("_", " "))
            )

    def set_project(self, name: str, path: str) -> ProjectChangeResult:
        """Move one thread's project, preserving its owner and conversation."""
        if not path.strip():
            raise ValueError("Project path cannot be empty.")
        with self.registry.store.guarded_write():
            thread = self.registry.require(name)
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = Path(thread.worktree) / target
            try:
                target = target.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"Cannot open project directory: {target}") from error
            if not target.is_dir():
                raise ValueError(f"Project path is not a directory: {target}")
            previous = str(thread.project_path)
            current = str(target)
            if current == previous:
                return ProjectChangeResult(thread.name, previous, current, False)
            self.registry.register(
                thread.moved_to_project(current), self.registry.status(thread.name),
            )
            return ProjectChangeResult(thread.name, previous, current, True)

    def rename_managed_thread(
        self, name: str, display_name: str, *, owner: ProcessIdentity
    ) -> RenameThreadResult:
        """Rename a locally managed running thread after proving process ownership."""
        with self.registry.store.guarded_write():
            thread = self.registry.require(name)
            if thread.process_identity != owner:
                raise RelationViolationError(
                    f"Process {owner.pid} does not own thread {thread.name!r}."
                )
            new_name = self.registry.snapshot().claim_name(display_name, owner=thread.name)
            return self._rename_thread_unlocked(thread.name, new_name, title=GivenThreadText(display_name))

    def _rename_thread(
        self, name: str, new_name: str, *, title: ThreadText = NoThreadText()
    ) -> RenameThreadResult:
        # Every rename checks and changes the same current registry declaration.
        with self.registry.store.guarded_write():
            return self._rename_thread_unlocked(name, new_name, title=title)

    def _rename_thread_unlocked(
        self, name: str, new_name: str, *, title: ThreadText = NoThreadText()
    ) -> RenameThreadResult:
        previous, current = self.registry.rename(name, new_name)
        thread = self.registry.require(current)
        titled = replace(thread, auto_title_pending=False, title=title.applied(thread.title))
        if titled != thread:
            self.registry.register(titled, self.registry.status(current))
        changed = previous != current
        if changed:
            self.agents.activity.rename_thread(previous, current)
            self.agents.runtime_info.rename_thread(previous, current)
            self.ledger.rename_thread(previous, current)
            with self.catalog.editing() as document:
                document.rename_thread(previous, current)
        return RenameThreadResult(previous, current, changed)

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
        with self.registry.store.guarded_write():
            thread = self.registry.require(original.name)
            thread.require_original_model_configuration(original)
            thread.execution.require_native()
            updated = configuration.configured(thread)
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def initialize_native_configuration(
        self, name: str, *, model: ThreadModel, thinking_level: type[ThinkingChoice],
    ) -> Thread:
        """Persist one first-known producer observation through its declaration."""
        with self.registry.store.guarded_write():
            thread = self.registry.require(name)
            updated = thread.initialized_native_configuration(
                model=model, thinking_level=thinking_level,
            )
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

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
        with self.registry.store.guarded_write():
            current = self.registry.require(original.name)
            if current != original:
                raise RelationViolationError("Original thread changed before native source publication")
            attached = replace(
                current.initialized_native_configuration(
                    model=saved_model, thinking_level=NoThinkingOverride,
                ),
                process_identity=process,
                session_file=RegistrySessionLocation.from_path(location.read_key, self.root),
            )
            self.registry.register(attached)
            return attached

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        with self.registry.store.guarded_write():
            self._archive_unlocked((self.registry.require(name),))

    def _archive_unlocked(self, originals: Sequence[Thread]) -> None:
        self.registry.archive_originals(originals)
        self.agents.runtime_info.remove_threads(originals)

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
        with self.registry.store.guarded_write():
            return self._delete_unlocked((self.registry.require(name),))[0]

    def _delete_unlocked(self, originals: Sequence[Thread]) -> tuple[ThreadDeleted, ...]:
        """Remove stopped declarations and everything they own.

        The bus, goal history and uncertain-input records are the record and
        stay; their readers resolve a removed name as a deleted thread. Every
        refusal happens before the registry withdraws input permission.
        """
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
        self.registry.begin_delete_originals(originals)
        for state in states:
            state.remove_threads(originals)
        # A failure from here leaves a Deleting declaration to retry, not orphans.
        freed = {name: sum(item.delete() for item in files) for name, files in sessions.items()}
        detached = self.registry.remove_originals(originals)
        return tuple(
            ThreadDeleted(original.name, freed.get(original.name, 0), detached[original.name])
            for original in originals
        )

    def create(self, spec: NewThreadSpec) -> Thread:
        """Prepare the session, then register and launch the thread with its task as first input."""
        resolved_bin = ConfiguredBinary().configured()
        runtime = LaunchRuntime.running()
        with spec.prepared(self, resolved_bin, runtime) as child:
            child = self.registry._declare_unlocked(child)
        # A failed launch cannot erase a declaration another starter reserved,
        # nor erase the first input if reservation committed before the failure.
        return self.owners._launch_owner(
            child, resolved_bin, initial_input=spec.task, launch_runtime=runtime,
        )

