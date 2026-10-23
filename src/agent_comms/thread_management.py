"""Thread registration and cross-store identity transactions."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self
from uuid import uuid4

from agent_comms.coordination_contracts import MAX_IDENTIFIER_CHARS
from agent_comms.coordination_errors import CoordinationError, ParticipantNotRegistered

from .child_process import NO_PROCESS, ProcessIdentity, ProcessState
from .cli_result import CliResult
from .coordination_database import CoordinationStore
from .native_entries import ImportedSessionWriter
from .registration import Registration
from .thread_status import StoppedThreadStatus
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .cli_bridges.history import History
    from .tool_caller import ThreadCaller
from .agent_activity import AgentActivity
from .agent_backend import RuntimeLocation
from .catalog_store import ChannelCatalog
from .collaboration_ledger import CollaborationLedger
from .errors import RelationViolationError
from .importing import ImportLimits, ImportSnapshot, ThreadImported
from .field_codec import FieldCodec, NullMember, RecordMember, SingleFieldValue, SubclassUnion
from .launch_runtime import LaunchRuntime, TagListText
from .message_bus import MessageBus
from .native_arguments import ConfiguredBinary
from .native_settings import NativeThreadDefaults
from .owner_lifecycle import LaunchInput, OwnerLifecycle, TaskLaunchInput
from .pi_vocabulary import NoThinkingOverride, ThinkingChoice, ThinkingLevelField
from .private_registry_guard import (
    PrivateOwnerRenameIntent,
    PrivateRegistryGuard,
    PublicRegistry,
    RegistryPrivacy,
    _require_no_private_owner_rename,
)
from .registry_document import RegistrySnapshot
from .registry_provenance import RegistryNames
from .store_files import _atomic_write_text
from .thread_identity import AgentThreadRole, ThreadRole
from .thread_text import ForkedParent, GivenThreadText, NoThreadText, SavedSessionFile, ThreadText
from .thread_model import DeclaredModel, ThreadModel, UndeclaredModel
from .thread_owned_state import ThreadOwnedState
from .threads import KeptModel, ModelChoice, NextTurnConfiguration, Thread, current_thread


class TagChoice(SubclassUnion, affix="Tags"):
    """Select explicit, inherited or changed tags from an existing set."""

    def for_new(self) -> frozenset[str]:
        return self.for_child(frozenset())

    @abstractmethod
    def for_child(self, parent: frozenset[str]) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class GivenTags(TagChoice):
    """Tags spelled as comma-separated text on the command line, in the editor and in tools."""

    tags: frozenset[str]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls(TagListText.from_text(FieldCodec.decode(str, data)))

    def to_wire(self) -> str:
        return TagListText.encode(self.tags)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(str)

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
    def project(self, snapshot: ImportSnapshot) -> str: ...

    @abstractmethod
    def local_directory(self) -> Path:
        """The directory a running harness's session is asked for."""


@dataclass(frozen=True, slots=True)
class GivenDirectory(SingleFieldValue, ImportDirectory):
    path: str

    def project(self, snapshot: ImportSnapshot) -> str:
        return self.path

    def local_directory(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(frozen=True, slots=True)
class SourceDirectory(NullMember, ImportDirectory):
    def project(self, snapshot: ImportSnapshot) -> str:
        return snapshot.project

    def local_directory(self) -> Path:
        return Path.cwd()


@dataclass(frozen=True, slots=True, kw_only=True)
class NewThreadSpec:
    """Prepare the session, then register the thread under its registry lock."""

    name: str
    project: str
    model: ThreadModel
    thinking_level: ThinkingLevelField
    task: LaunchInput = field(default_factory=LaunchInput)
    tags: TagChoice = InheritedTags()

    @classmethod
    def from_task(cls, *, task: str = "", **values) -> NewThreadSpec:
        return cls(task=TaskLaunchInput(task) if task else LaunchInput(), **values)

    def declaration(self, project: str, registry: Registration) -> Thread:
        return registry.new_thread(name=self.name, task=self.task.thread_text(), tags=self.tags.for_new(),
                      worktree=project, model=self.model, thinking_level=self.thinking_level)

    @contextmanager
    def prepared(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Iterator[Thread]:
        project = Path(self.project).expanduser().resolve(strict=True)
        thread = self.declaration(str(project), threads.registry)
        threads.registry.require_declaration_available(thread)
        location = thread.runtime.new_location(threads.root, project)
        with threads.registry.store.locked():
            yield replace(thread, session_file=SavedSessionFile(location.read_key))


@dataclass(frozen=True, slots=True, kw_only=True)
class ForkSpec(NewThreadSpec):
    """A fork differs only in its parent-derived source and inherited settings."""

    parent: str

    def _parent(self, threads: ThreadManagement) -> Thread:
        parent = threads.registry.require(self.parent)
        parent.execution.require_native()
        parent.runtime.require_forkable()
        return parent

    def child(self, parent: Thread, runtime: RuntimeLocation, registry: Registration) -> Thread:
        return replace(self.declaration(self.project, registry), parent=ForkedParent(parent.name),
                       tags=self.tags.for_child(parent.tags),
                       backend=parent.backend, session_file=SavedSessionFile(runtime.read_key))

    @contextmanager
    def prepared(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Iterator[Thread]:
        # Copy saved history before locking the registry for its declaration.
        parent = self._parent(threads)
        threads.registry.require_declaration_available(self.child(parent, parent.runtime, threads.registry))
        location = asyncio.run(parent.backend.fork(parent, pi_bin, runtime))
        with threads.registry.store.locked():
            current = self._parent(threads)
            if current.incarnation != parent.incarnation or current.runtime != parent.runtime:
                raise RelationViolationError(
                    f"Parent thread {self.parent!r} changed its saved session during the fork; "
                    f"the created fork {location} was not registered."
                )
            yield self.child(current, location, threads.registry)


@dataclass(frozen=True, slots=True)
class ThreadDeleted(CliResult):
    deleted: str
    session_bytes_freed: int
    detached_children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenameThreadResult(ToolResult):
    previous: str
    current: str
    changed: bool


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
        limits: ImportLimits | None = None,
    ) -> ThreadImported:
        """Import a stopped, resumable snapshot of any harness session, without adopting its executor."""
        snapshot = ImportSnapshot.read(history, limits or ImportLimits())
        directory = worktree.project(snapshot)
        if not directory:
            raise ValueError("The source has no project directory; supply --worktree.")
        project = Path(directory).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The imported project does not exist locally; supply --worktree.")
        session_path = ImportedSessionWriter.path_in(self.root, session_id=uuid4().hex)
        thread = model.configured(self.registry.new_thread(
            name,
            tags,
            str(project),
            session_file=SavedSessionFile(str(session_path)),
            title=snapshot.title,
        ))
        with self.registry.store.locked():
            if self.registry.snapshot().taken(name):
                raise ValueError(f"Thread name {name!r} is already reserved.")
            self.registry.require_declaration_available(thread, StoppedThreadStatus())
            session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Session output must remain in an owner-controlled directory.
            info = session_path.parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ValueError("Imported session directory is not owner-controlled.")
            _atomic_write_text(session_path, snapshot.pi_session(project))
            try:
                self.registry._declare_unlocked(thread, StoppedThreadStatus())
                self.bus.mark_delivered_through(name, self.bus.bus.revision())
            except (OSError, ValueError, CoordinationError):
                if name in self.registry:
                    self.registry.remove(name)
                session_path.unlink(missing_ok=True)
                raise
        return ThreadImported(
            name,
            str(session_path),
            snapshot.harness,
            snapshot.source_id,
            len(snapshot.messages),
            snapshot.messages_seen - len(snapshot.messages),
            snapshot.truncated_messages,
            snapshot.notices,
            snapshot.historical_instructions,
        )

    def restore_stopped(self, source: RegistrySnapshot, names: Sequence[str]) -> tuple[str, ...]:
        """Restore historical declarations and their private delivery identities.

        Registry restoration owns collision checks and strips execution authority;
        the registry's privacy registers what restored threads need beyond it.
        Repeating this operation repairs an interrupted coordinator registration
        without replacing existing owners or importing historical delivery state.
        """
        selected = tuple(dict.fromkeys(names))
        with self.registry.store.locked():
            restored = self.registry.restore_stopped(source, selected)
            self._privacy().register_restored(self.registry.require(name) for name in selected)
            return restored

    def _privacy(self) -> RegistryPrivacy:
        with self.registry.store.locked():
            return self.registry.store.private_guard_unlocked()

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
        thread = self.registry.new_thread(
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
        with self.registry.store.locked():
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
        with self.registry.store.locked():
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
        with self.registry.store.locked():
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
        with self.registry.store.locked():
            return self._rename_thread_unlocked(name, new_name, title=title)

    def _rename_thread_unlocked(
        self, name: str, new_name: str, *, title: ThreadText = NoThreadText()
    ) -> RenameThreadResult:
        return ThreadRename.chosen(self, self.registry.require(name), new_name).applied(title)

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
        with self.registry.store.locked():
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
        with self.registry.store.locked():
            thread = self.registry.require(name)
            updated = thread.initialized_native_configuration(
                model=model, thinking_level=thinking_level,
            )
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def resolve_thread_model(
        self, name: str, default: DeclaredModel = NativeThreadDefaults.model,
    ) -> str:
        """Prefer the registered choice, then runtime observation, then default."""
        thread = self.registry.require(name)
        return thread.model.otherwise(self.agents.agent_info_of(thread.name).model_or(default.text))

    def attach_session(
        self, original: Thread, source_path: str, *, process: ProcessState,
    ) -> Thread:
        """Read Pi state first, then publish its model under the registry lock."""
        path = Path(source_path).expanduser().resolve()
        location = original.backend.location_type(str(path))
        saved_model = ThreadModel.from_choice(location.model_choice) if path.is_file() else UndeclaredModel()
        with self.registry.store.locked():
            current = self.registry.require(original.name)
            if current != original:
                raise RelationViolationError("Original thread changed before native source publication")
            attached = replace(
                current.initialized_native_configuration(
                    model=saved_model, thinking_level=NoThinkingOverride,
                ),
                process_identity=process,
                session_file=SavedSessionFile(location.read_key),
            )
            self.registry.register(attached)
            return attached

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        with self.registry.store.locked():
            self._archive_unlocked((self.registry.require(name),))

    def _archive_unlocked(self, originals: Sequence[Thread]) -> None:
        self.registry.archive_originals(originals)
        self.agents.runtime_info.remove_threads(originals)

    @property
    def thread_owned_states(self) -> tuple[ThreadOwnedState, ...]:
        """Every store whose rows end with a thread declaration."""
        from .goal_waits import GoalWaits
        from .relationships import RelationshipStore
        from .view_unread import transcript_read_state

        states = (
            self.agents.activity,
            self.agents.runtime_info,
            self.ledger,
            self.catalog,
            self.owners.releases,
            self.bus.reads,
            transcript_read_state(self.bus.reads.path),
            GoalWaits.at(self.root),
            RelationshipStore.at(self.root),
        )
        ThreadOwnedState.require_complete(states)
        return states

    def delete(self, name: str) -> ThreadDeleted:
        """Permanently delete one stopped or archived thread."""
        with self.registry.store.locked():
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

    def adopt_current(self) -> Thread:
        """Declare and register the current process's thread from env."""
        thread = current_thread(self.registry)
        return self.registry.declare(thread)


@dataclass(frozen=True, slots=True)
class ThreadRename(ABC):
    """One rename, its kind derived from the registry's privacy."""

    privacy: ClassVar[type[RegistryPrivacy]]
    kinds: ClassVar[dict[type[RegistryPrivacy], type[ThreadRename]]] = {}
    threads: ThreadManagement
    before: Thread
    new_name: str
    registry_privacy: RegistryPrivacy

    def __init_subclass__(cls, **kwargs):
        # slots=True rebuilds the class; its final declaration replaces the first.
        super(ThreadRename, cls).__init_subclass__(**kwargs)
        cls.kinds[cls.privacy] = cls

    @classmethod
    def chosen(cls, threads: ThreadManagement, before: Thread, new_name: str) -> ThreadRename:
        privacy = threads._privacy()
        return cls.kinds[type(privacy)](threads, before, new_name, privacy)

    @abstractmethod
    def renamed(self) -> tuple[str, str]:
        """Move the registry name, after whatever this kind's owner stores need first."""

    def registry_renamed(self) -> tuple[str, str]:
        return self.threads.registry.rename(self.before.name, self.new_name)

    def applied(self, title: ThreadText) -> RenameThreadResult:
        previous, current = self.renamed()
        threads = self.threads
        thread = threads.registry.require(current)
        titled = replace(thread, auto_title_pending=False, title=title.applied(thread.title))
        if titled != thread:
            threads.registry.register(titled, threads.registry.status(current))
        if previous == current:
            return RenameThreadResult(previous, current, False)
        threads.agents.activity.rename_thread(previous, current)
        threads.agents.runtime_info.rename_thread(previous, current)
        threads.ledger.rename_thread(previous, current)
        with threads.catalog.editing() as document:
            document.rename_thread(previous, current)
        return RenameThreadResult(previous, current, True)


class PlainRename(ThreadRename):
    privacy = PublicRegistry

    def renamed(self) -> tuple[str, str]:
        return self.registry_renamed()


@dataclass(frozen=True, slots=True)
class PrivateOwnerRename(ThreadRename):
    """Moves a committed coordinator owner under a durable intent, so a crash between the two
    stores fails closed instead of turning an old attempt into a new send."""

    privacy = PrivateRegistryGuard
    registry_privacy: PrivateRegistryGuard

    def renamed(self) -> tuple[str, str]:
        from .coordinator import Coordination

        root, registry = self.threads.root, self.threads.registry
        before, new_name = self.before, self.new_name
        _require_no_private_owner_rename(root)
        coordinator = CoordinationStore.path_in(root)
        if not coordinator.exists() or before.name == new_name:
            return self.registry_renamed()
        # Validate registry/SQL name refusals before renaming the private owner.
        # Registry readers cannot select a different owner while both stores change.
        registry.status(before.name).require_self_rename()
        if registry.snapshot().taken(new_name, owner=before.name):
            raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
        Thread.require_name(new_name)
        if len(new_name) > MAX_IDENTIFIER_CHARS:
            raise ValueError("Private coordinator owner name exceeds its bound.")
        with Coordination(str(coordinator)) as store:
            try:
                person = store.participants.get(before.incarnation.require_recorded().lookup)
            except ParticipantNotRegistered:
                return self.registry_renamed()
            if not person.committed or person.owner_identity.owner_thread != before.name:
                raise RelationViolationError(
                    "Private coordinator owner differs from the registered owner.")
            generation = person.owner_identity.generation
            PrivateOwnerRenameIntent(lookup=person.lookup, old=before.name, new=new_name,
                                     generation=generation,
                                     wire_root_id=self.registry_privacy.root_id.hex(),
                                     ).write(root)
            store.participants.rename_owner(
                person.lookup, new_name, expected_generation=generation)
            try:
                return self.registry_renamed()
            except BaseException as error:
                # Registry persistence can fail after the SQL CAS. If its atomic snapshot still
                # names the old owner, restore that name without revoking its active attempt.
                uncertain = RelationViolationError(
                    "Private owner rename is uncertain; inspect both authorities.")
                try:
                    if registry.require(before.name).name == before.name:
                        store.participants.rename_owner(
                            person.lookup, before.name, expected_generation=generation)
                except BaseException as compensation:
                    # Ambiguous dual-store failure: the inspection needs both failures.
                    raise uncertain from compensation
                raise uncertain from error

    def applied(self, title: ThreadText) -> RenameThreadResult:
        result = ThreadRename.applied(self, title)
        # The registry lock and renamed() bind a present intent to this rename;
        # it completes only after both authorities and the ancillary stores agree.
        if PrivateOwnerRenameIntent.path_in(self.threads.root).exists():
            PrivateOwnerRenameIntent.complete(self.threads.root)
        return result
