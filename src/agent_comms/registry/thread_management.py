from __future__ import annotations

import asyncio
import re
from abc import abstractmethod
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Self

from agent_comms.agent_backend import RuntimeLocation
from agent_comms.cli_bridges.history import History
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.config_options import RunningSelection
from agent_comms.coordination.errors import CoordinationError
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import (
    NO_PROCESS,
    ProcessIdentity,
    ProcessState,
)
from agent_comms.execution.imported_conversation import ImportedConversation
from agent_comms.execution.child_process import NO_PROCESS, ProcessIdentity, ProcessState
from agent_comms.execution.imported_messages import ImportLimits
from agent_comms.execution.launch_runtime import LaunchRuntime, TagListText
from agent_comms.execution.native_arguments import ConfiguredBinary, NativeArguments
from agent_comms.execution.native_entries import ImportedSessionWriter
from agent_comms.execution.native_session_reopen import PiSessionId
from agent_comms.execution.owner_lifecycle import (
    LaunchInput,
    OwnerLifecycle,
    TaskLaunchInput,
)
from agent_comms.execution.pi_vocabulary import (
    NoThinkingOverride,
    ThinkingChoice,
    ThinkingLevelField,
)
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.importing import ThreadImported
from agent_comms.messaging.channel_targets import DeclaredTag
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.read_basis import ReadAuthority
from agent_comms.nominal.field_codec import (
    DeclaredWireType,
    FieldCodec,
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
)
from agent_comms.planning.goal_waits import GoalWaits
from agent_comms.projects.checkouts import Checkout, LaunchCheckout
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.collaboration_ledger import CollaborationLedger
from agent_comms.registry.heartbeat_delivery import HeartbeatDelivery
from agent_comms.registry.native_settings import NativeThreadDefaults
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.registry_store import RegistryEdit
from agent_comms.registry.relationships import RelationshipStore
from agent_comms.registry.thread_catalog import ThreadCatalog
from agent_comms.registry.thread_identity import (
    AgentThreadRole,
    ThreadIncarnation,
    ThreadRole,
)
from agent_comms.registry.thread_model import (
    DeclaredModel,
    ThreadModel,
    UndeclaredModel,
)
from agent_comms.registry.thread_owned_state import ThreadOwnedState
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.thread_text import ForkedParent, GivenThreadText, ThreadText
from agent_comms.registry.threads import (
    ExternalThread,
    KeptModel,
    ModelChoice,
    NativeThread,
    NextTurnConfiguration,
    Thread,
)
from agent_comms.registry.view_unread import transcript_read_state
from agent_comms.storage.locked_store import PatternFile, RuntimeFile
from agent_comms.transport.tool_declaration import NativeRequestTools
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks.process_claim import ClaimHeldError, ProcessSingleton


@dataclass(frozen=True, slots=True)
class ChangeResult(ToolResult):
    """A value that moved from previous to current; changed says whether it differs."""

    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class RenameThreadResult(ChangeResult):
    """A thread display name change."""


@dataclass(frozen=True, slots=True)
class ThreadTitleResult(ToolResult):
    """A title set on its own; the thread's identity and aliases are untouched."""

    name: str
    title: str


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
        return NativeThread(name=self.name, task=self.task.thread_text(), tags=self.tags.for_new(),
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
        parent = threads.registry.require_member(self.parent, NativeThread)
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
class ProjectChangeResult(ChangeResult):
    thread: str


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
        thread = model.configured(NativeThread(
            name,
            tags,
            Checkout(project),
            session_file=RegistrySessionLocation.from_path(session_path, self.root),
            title=snapshot.title))
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
        original = selected.require_member(name, ExternalThread)
        with ImportedSessionWriter.preparing(session_path, snapshot, original.worktree.path) as publish:
            with self.registry.store.editing() as edit:
                current = edit.document.require(name)
                if not edit.original.snapshot.same_thread(current, selected, original):
                    raise RelationViolationError("External owner changed while preparing its native session.")
                rehomed = current.as_member(
                    NativeThread, process_identity=NO_PROCESS,
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
        thread = NativeThread(
            name=RegistryNames.thread_slug(wanted),
            tags=tags,
            worktree=worktree,
            process_identity=process,
            model=model,
            thinking_level=thinking_level,
            auto_title_pending=auto_title_pending,
            role=role)
        return self.registry._claim_unlocked(thread)

    def rename_self(self, display_name: str, *, caller: ReadAuthority) -> RenameThreadResult:
        """Rename the bound caller's own thread to the display name it was given.

        The identity is that text's slug, exactly as creation and the managed rename
        already claim one, and the title is the text itself. A caller naming a topic and
        a human labelling a thread therefore mean the same thing by the same input.
        """
        with self.registry.store.editing() as edit:
            name = caller.own_thread()
            if edit.document.require(name).auto_title_pending:
                self.require_concise_title(display_name)
            return self._rename_thread_in(
                edit, name, RegistryNames.thread_slug(display_name),
                title=GivenThreadText(display_name))

    def set_project(self, target_thread: ThreadIncarnation, path: str) -> ProjectChangeResult:
        """Move the captured thread's project, never a later occupant of its name."""
        if not path.strip():
            raise ValueError("Project path cannot be empty.")
        original = self.registry.require(target_thread.name)
        if original.incarnation != target_thread:
            raise RelationViolationError("Thread incarnation changed before project selection")
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

        updated = self.registry.update_thread(original.name, move)
        previous, current = str(original.project_path), str(updated.project_path)
        return ProjectChangeResult(
            previous=previous, current=current, changed=current != previous, thread=updated.name)

    def rename_managed_thread(
        self, original: ThreadIncarnation, display_name: str, *, owner: ProcessIdentity
    ) -> RenameThreadResult:
        """Rename only the captured thread while its process still owns it."""
        with self.registry.store.editing() as edit:
            thread = edit.document.require(original.name)
            if thread.incarnation != original:
                raise RelationViolationError("Thread incarnation changed before rename")
            if thread.process_identity != owner:
                raise RelationViolationError(
                    f"Process {owner.pid} does not own thread {thread.name!r}."
                )
            new_name = edit.document.claim_name(display_name, owner=thread.name)
            return self._rename_thread_in(edit, thread.name, new_name, title=GivenThreadText(display_name))

    def set_own_title(self, title: str, *, caller: ReadAuthority) -> ThreadTitleResult:
        """Correct the bound caller's own display title, leaving its identity alone.

        A thread imported from another harness carries that session's title and no turn
        revisits it, because import never sets ``auto_title_pending``. The party that can
        see a stale title is the thread itself, so this takes the caller's own authority
        exactly as ``rename_self`` does; a teammate could not correct its own label under
        the process-owner proof the managed rename requires.
        """
        with self.registry.store.editing() as edit:
            name = caller.own_thread()
            edit.document.status(name).require_self_rename()
            thread = edit.document.require(name)
            self.require_concise_title(title)
            self._write_title(edit, thread.name, GivenThreadText(title))
            edit.commit()
            return ThreadTitleResult(thread.name, title)

    def set_managed_title(
        self, original: ThreadIncarnation, title: str, *, owner: ProcessIdentity
    ) -> ThreadTitleResult:
        """Correct the captured thread's display title from its UI, proving its process."""
        with self.registry.store.editing() as edit:
            thread = edit.document.require(original.name)
            if thread.incarnation != original:
                raise RelationViolationError("Thread incarnation changed before retitle")
            if thread.process_identity != owner:
                raise RelationViolationError(
                    f"Process {owner.pid} does not own thread {thread.name!r}."
                )
            self.require_concise_title(title)
            self._write_title(edit, thread.name, GivenThreadText(title))
            edit.commit()
            return ThreadTitleResult(thread.name, title)

    @staticmethod
    def require_concise_title(title: str) -> None:
        """A title is rendered where a name is, so it carries the name's bound.

        The bound is unconditional here. ``rename_self`` applies it only while
        ``auto_title_pending``, and that flag is false for every imported thread, so
        inheriting that condition would leave the one path this bound exists to protect
        with no validation at all.

        Words are counted in either shape the label arrives in: the spaces a display
        name uses and the ``-``/``_`` a slug uses. Counting only the separators let a
        seven-word spaced title through, which is the shape this now accepts.
        """
        if len(title) > 48 or len(re.findall(r"[^\s_-]+", title)) > 6:
            raise ValueError("Choose a concise topic title: at most 6 words and 48 characters.")

    def _rename_thread_in(
        self, edit: RegistryEdit, name: str, new_name: str, *, title: ThreadText
    ) -> RenameThreadResult:
        previous, current = edit.document.rename(name, new_name)
        self._write_title(edit, current, title)
        edit.commit()
        changed = previous != current
        if changed:
            edit.released.append(partial(self._rename_stores, previous, current))
        return RenameThreadResult(previous, current, changed)

    def _write_title(self, edit: RegistryEdit, name: str, title: ThreadText) -> None:
        """Write the label its operation chose, applied to whatever the thread already
        carried, and clear the one flag that lets a turn choose it later."""
        thread = edit.document.require(name)
        titled = replace(thread, auto_title_pending=False, title=title.applied(thread.title))
        if titled != thread:
            self.registry._commit_registration(edit, edit.document.prepare_registration(
                titled, edit.document.status(name), new_owner=False))

    def _rename_stores(self, previous: str, current: str) -> None:
        self.agents.activity.rename_thread(previous, current)
        self.agents.runtime_info.rename_thread(previous, current)
        self.ledger.rename_thread(previous, current)
        self.catalog.change(lambda document: document.rename_thread(previous, current))

    def heartbeat(self, name: str) -> None:
        self.registry.heartbeat(name)

    def set_heartbeat_delivery(self, name: str, delivery: type[HeartbeatDelivery]) -> Thread:
        """Apply immediately without changing membership, process, goal or model settings."""
        return self.registry.update_thread(
            name, lambda thread: replace(thread, heartbeat_delivery=delivery))

    def set_thread_configuration(
        self, original: Thread, configuration: NextTurnConfiguration,
    ) -> Thread:
        """Publish next-turn settings together against their acquired declaration.

        Progress and goals belong to the current document. A changed selection
        or project invalidates the native settings result acquired by the caller.
        """
        def configure(thread: NativeThread) -> Thread:
            thread.require_original_model_configuration(original)
            return configuration.configured(thread)

        updated = self.registry.update_member(original.name, NativeThread, configure)
        ThreadCatalog.publish(updated, self.root)
        return updated

    def native_launch_arguments(
        self, thread: Thread, arguments: NativeArguments, package: Path,
    ) -> tuple[str, ...]:
        """Publish the current catalog before launching an extension that consumes it."""
        if not arguments.loads(NativeRequestTools.native_extension(package)):
            return arguments.argv
        path = ThreadCatalog.publish(thread, self.root)
        return (*arguments.argv, "--comms-catalog", str(path))

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
        saved_model = location.saved_model if path.is_file() else UndeclaredModel()
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
                    cleanup.enter_context(
                        ThreadDeletionClaim.for_thread(self.root, original).claimed())
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
            detached = self.registry.remove_originals(originals)
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
        with self.owners.launching_owner(
            child, resolved_bin, initial_input=spec.task, launch_runtime=runtime,
        ) as owner:
            return owner
