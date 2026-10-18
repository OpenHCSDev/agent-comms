"""Thread registration and cross-store identity transactions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_comms.coordination_contracts import MAX_IDENTIFIER_CHARS
from agent_comms.coordination_errors import CoordinationError, IdentityConflict

from .child_process import NoProcess, ProcessIdentity, ProcessState
from .registration import Registration
from .thread_status import StoppedThreadStatus

if TYPE_CHECKING:
    from .cli_bridges.history import History
    from .tool_caller import ThreadCaller
from .agent_activity import AgentActivity
from .catalog_store import ChannelCatalog
from .collaboration_ledger import CollaborationLedger
from .errors import RelationViolationError
from .importing import ImportLimits, ImportReceipt, ImportSnapshot
from .input_disposition import InputDispositions
from .message_bus import MessageBus
from .native_arguments import NativeArguments
from .native_settings import NativeThreadDefaults
from .launch_runtime import LaunchRuntime
from .owner_lifecycle import LaunchInput, TaskLaunchInput, OwnerLifecycle
from .pi_vocabulary import ServiceTier, ThinkingLevel
from .private_registry_guard import PRIVATE_OWNER_RENAME_PENDING, _require_no_private_owner_rename
from .registry_document import RegistrySnapshot
from .store_files import _atomic_write_text, _store_lock
from .thread_owned_state import ThreadOwnedState
from .threads import Thread, current_thread
from .agent_backend import RuntimeLocation

@dataclass(frozen=True, slots=True, kw_only=True)
class NewThreadSpec:
    """Prepares the session outside the wire lock, then registers the thread under it."""

    name: str
    project: str
    model: str
    thinking_level: type[ThinkingLevel]
    task: LaunchInput = field(default_factory=LaunchInput)
    tags: frozenset[str] | None = None

    @classmethod
    def from_task(cls, *, task: str = "", **values) -> NewThreadSpec:
        return cls(task=TaskLaunchInput(task) if task else LaunchInput(), **values)

    def declaration(self, project: str, registry: Registration) -> Thread:
        return registry.new_thread(name=self.name, task=self.task.text, tags=self.tags or frozenset(),
                      worktree=project, model=self.model, thinking_level=self.thinking_level)

    @contextmanager
    def prepared(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Iterator[Thread]:
        project = Path(self.project).expanduser().resolve(strict=True)
        thread = self.declaration(str(project), threads.registry)
        threads.registry.require_declaration_available(thread)
        location = thread.runtime.new_location(threads.root, project)
        with _store_lock(threads._wire_lock_path):
            yield replace(thread, session_file=location.read_key)


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
        return replace(self.declaration(self.project, registry), parent=parent.name,
                       tags=parent.tags if self.tags is None else self.tags,
                       backend=parent.backend, session_file=runtime.read_key)

    @contextmanager
    def prepared(self, threads: ThreadManagement, pi_bin: str, runtime: LaunchRuntime) -> Iterator[Thread]:
        # Copy saved history outside the wire lock, retaining writer→wire order.
        parent = self._parent(threads)
        threads.registry.require_declaration_available(self.child(parent, parent.runtime, threads.registry))
        location = asyncio.run(parent.backend.fork(parent, pi_bin, runtime))
        with _store_lock(threads._wire_lock_path):
            current = self._parent(threads)
            if current.incarnation != parent.incarnation or current.runtime != parent.runtime:
                raise RelationViolationError(
                    f"Parent thread {self.parent!r} changed its saved session during the fork; "
                    f"the created fork {location} was not registered."
                )
            yield self.child(current, location, threads.registry)


@dataclass(frozen=True, slots=True)
class ThreadDeleted:
    deleted: str
    session_bytes_freed: int
    detached_children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenameThreadResult:
    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class ProjectChangeResult:
    thread: str
    previous: str
    current: str
    changed: bool


class ThreadManagement:
    def __init__(
        self,
        root: Path,
        registry: Registration,
        bus: MessageBus,
        catalog: ChannelCatalog,
        agents: AgentActivity,
        owners: OwnerLifecycle,
        ledger: CollaborationLedger,
    ):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.catalog = catalog
        self.agents = agents
        self.owners = owners
        self.ledger = ledger
        self._wire_lock_path = root / "wire"

    def import_thread(
        self,
        history: History,
        *,
        name: str,
        worktree: str | None = None,
        model: str | None = None,
        tags: frozenset[str] = frozenset(),
        limits: ImportLimits | None = None,
    ) -> ImportReceipt:
        """Import a stopped, resumable snapshot of any harness session, without adopting its executor."""
        snapshot = ImportSnapshot.read(history, limits or ImportLimits())
        directory = worktree or snapshot.project
        if not directory:
            raise ValueError("The source has no project directory; supply --worktree.")
        project = Path(directory).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The imported project does not exist locally; supply --worktree.")
        session_path = self.root / "imported_sessions" / f"{uuid4().hex}.jsonl"
        thread = self.registry.new_thread(
            name,
            tags,
            str(project),
            session_file=str(session_path),
            title=snapshot.title,
            model=model,
        )
        with _store_lock(self._wire_lock_path):
            if self.registry.name_reserved(name):
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
                self.bus.mark_delivered_through(name, self.bus.log.latest_sequence())
            except (OSError, ValueError, CoordinationError):
                if name in self.registry:
                    self.registry.remove(name)
                session_path.unlink(missing_ok=True)
                raise
        return ImportReceipt(
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

        Registry restoration owns collision checks and strips execution authority.
        On a private bus, stopped subscribers still belong to frozen audiences;
        their coordinator identities must exist before any cohort can be accepted.
        Repeating this operation repairs an interrupted coordinator registration
        without replacing existing owners or importing historical delivery state.
        """
        from .coordinator import Coordination

        selected = tuple(dict.fromkeys(names))
        with _store_lock(self._wire_lock_path):
            restored = self.registry.restore_stopped(source, selected)
            with _store_lock(self.registry.store.path):
                private = self.registry.store.private_guard_unlocked() is not None
            if private:
                with Coordination(str(self.root / "coordination.sqlite3")) as store:
                    for name in selected:
                        thread = self.registry.require(name)
                        store.participants.register(
                            thread.incarnation.lookup,
                            thread.name,
                            thread.name,
                            committed=True,
                        )
            return restored

    def claim_thread(
        self,
        base_name: str,
        *,
        tags: frozenset[str],
        worktree: str,
        process: ProcessState = NoProcess(),
        model: str | None = None,
        thinking_level: str | None = None,
        auto_title_pending: bool = False,
    ) -> Thread:
        """Atomically register a unique thread; its inbox starts at its creation."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.new_thread(
                name=base_name,
                tags=tags,
                worktree=worktree,
                process_identity=process,
                model=model,
                thinking_level=thinking_level,
                auto_title_pending=auto_title_pending,
            )
            return self.registry._claim_unlocked(thread)

    def rename_self(self, new_name: str, *, caller: ThreadCaller) -> RenameThreadResult:
        """Rename the bound caller's own thread, retaining its old aliases."""
        with _store_lock(self._wire_lock_path):
            if self.registry.require(caller.name).auto_title_pending and (
                len(new_name) > 48 or len(re.split(r"[-_]+", new_name)) > 6
            ):
                raise ValueError("Choose a concise topic title: at most 6 words and 48 characters.")
            return self._rename_thread_unlocked(
                caller.name, new_name, title=new_name.replace("-", " ").replace("_", " ")
            )

    def set_project(self, name: str, path: str) -> ProjectChangeResult:
        """Move one thread's project, preserving its owner and conversation."""
        if not path.strip():
            raise ValueError("Project path cannot be empty.")
        with _store_lock(self._wire_lock_path):
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
            previous = str(Path(thread.worktree).expanduser().resolve())
            current = str(target)
            if current == previous:
                return ProjectChangeResult(thread.name, previous, current, False)
            self.registry.register(
                thread.moved_to_project(current), self.registry.status(thread.name),
            )
            return ProjectChangeResult(thread.name, previous, current, True)

    def rename_managed_thread(
        self, name: str, display_name: str, *, owner_pid: int
    ) -> RenameThreadResult:
        """Rename a locally managed running thread after proving process ownership."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            if owner_pid <= 0 or thread.process_identity != ProcessIdentity.capture(owner_pid):
                raise RelationViolationError(
                    f"Process {owner_pid} does not own thread {thread.name!r}."
                )
            base_name = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_") or "session"

            new_name = base_name
            suffix = 2
            while self.registry.name_reserved(new_name):
                if self.registry.canonical_name(new_name) == thread.name:
                    return self._rename_thread_unlocked(thread.name, new_name, title=display_name)
                new_name = f"{base_name}-{suffix}"
                suffix += 1
            return self._rename_thread_unlocked(thread.name, new_name, title=display_name)

    def _rename_thread(
        self, name: str, new_name: str, *, title: str | None = None
    ) -> RenameThreadResult:
        # Keep direct callers under the same wire lock as public self/managed
        # rename. Private owner migration and ordinary publication share it.
        with _store_lock(self._wire_lock_path):
            return self._rename_thread_unlocked(name, new_name, title=title)

    def _rename_thread_unlocked(
        self, name: str, new_name: str, *, title: str | None = None
    ) -> RenameThreadResult:
        from .coordinator import Coordination

        before = self.registry.require(name)
        private_meta = self.root / "bus_meta.json"
        if private_meta.is_symlink():
            raise RelationViolationError("Bus metadata cannot be a symlink.")
        private = False
        if private_meta.exists():
            with self.bus.log.locked():
                metadata = self.bus.log.read_metadata_unlocked(required=True)
                if metadata.private:
                    self.bus.log._private_marker_unlocked()
                    private = True
        if private:
            _require_no_private_owner_rename(self.root)
        coordinator = self.root / "coordination.sqlite3"
        intent = self.root / PRIVATE_OWNER_RENAME_PENDING
        intent_created = False
        if not private or not coordinator.exists() or before.name == new_name:
            previous, current = self.registry.rename(name, new_name)
        else:
            # Validate every deterministic registry/SQL name refusal *before*
            # advancing a committed private owner's generation. The wire lock
            # excludes cooperating sends until both authorities agree.
            self.registry.status(before.name).require_self_rename()
            if (
                self.registry.name_reserved(new_name)
                and self.registry.canonical_name(new_name) != before.name
            ):
                raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
            replace(before, name=new_name)
            if len(new_name) > MAX_IDENTIFIER_CHARS:
                raise ValueError("Private coordinator owner name exceeds its bound.")
            with Coordination(str(coordinator)) as store:
                try:
                    person = store.participants.get(before.incarnation.lookup)
                except IdentityConflict as error:
                    if str(error) != "participant aggregate is not registered":
                        raise
                    previous, current = self.registry.rename(name, new_name)
                else:
                    if not person.committed or person.owner_thread != before.name:
                        raise RelationViolationError(
                            "Private coordinator owner differs from the registered owner."
                        )
                    # Persist a fail-closed cross-store intent before SQL or
                    # registry changes. A crash leaves private publication and
                    # selected execution unavailable until manual inspection;
                    # it cannot silently turn an old attempt into a new send.
                    _atomic_write_text(
                        intent,
                        json.dumps(
                            {
                                "version": 1,
                                "lookup": person.lookup,
                                "old": before.name,
                                "new": new_name,
                                "generation": person.participant_generation,
                                "wireRootId": metadata.root_id,
                            },
                            sort_keys=True,
                        ),
                        fsync_parent=True,
                    )
                    intent_created = True
                    store.participants.advance_generation(
                        person.lookup, new_name, expected_generation=person.participant_generation
                    )
                    # An old selected attempt stays fenced in its old generation;
                    # never retry or reassign it after this ownership change.
                    try:
                        previous, current = self.registry.rename(name, new_name)
                    except BaseException as error:
                        # Registry persistence can fail after the SQL CAS. If
                        # its atomic snapshot still names the old owner, make
                        # a NEW generation for that owner instead of leaving
                        # a committed coordinator pointing at the new name.
                        # Neither generation may inherit an old native input.
                        uncertain = RelationViolationError(
                            "Private owner rename is uncertain; inspect both authorities."
                        )
                        try:
                            actual = self.registry.require(before.name).name
                            if actual == before.name:
                                store.participants.advance_generation(
                                    person.lookup,
                                    before.name,
                                    expected_generation=person.participant_generation + 1,
                                )
                        except BaseException as compensation:
                            # Ambiguous dual-store failure: the inspection
                            # needs both failures, not only the first.
                            uncertain.add_note(
                                f"Compensating generation advance failed: {compensation!r}"
                            )
                        raise uncertain from error
        thread = self.registry.require(current)
        if thread.auto_title_pending or title is not None:
            self.registry.register(
                replace(thread, auto_title_pending=False, title=title or thread.title),
                self.registry.status(current),
            )
        if previous == current:
            return RenameThreadResult(previous, current, False)
        self.agents.activity.rename_thread(previous, current)
        self.agents.runtime_info.rename_thread(previous, current)
        self.ledger.rename_thread(previous, current)
        with self.catalog.editing() as document:
            document.rename_thread(previous, current)
        if intent_created:
            # Persist completion only after both authorities and ancillary
            # stores agree. If the subsequent unlink/fsync is uncertain, the
            # previous durable record already proves a finished transition;
            # an extant file still blocks all new private sends.
            completed_intent = json.loads(intent.read_text())
            completed_intent["completed"] = True
            _atomic_write_text(
                intent, json.dumps(completed_intent, sort_keys=True), fsync_parent=True
            )
            intent.unlink()
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return RenameThreadResult(previous, current, True)

    def heartbeat(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            self.registry.heartbeat(name)

    def set_thread_configuration(
        self, original: Thread, *, model: str | None = None,
        thinking_level: str | None = None, service_tier: ServiceTier | None = None,
    ) -> Thread:
        """Publish next-turn settings together against their acquired declaration.

        Progress and goals belong to the current document. A changed selection
        or project invalidates the native settings result acquired by the caller.
        """
        Thread.require_declaration(original)
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(original.name)
            thread.require_original_model_configuration(original)
            thread.execution.require_native()
            updated = thread.with_model_configuration(
                model=model, thinking_level=thinking_level, service_tier=service_tier,
            )
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def initialize_native_configuration(
        self, name: str, *, model: str | None, thinking_level: str | None,
    ) -> Thread:
        """Persist one first-known producer observation through its declaration."""
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            updated = thread.initialized_native_configuration(
                model=model, thinking_level=thinking_level,
            )
            if updated != thread:
                self.registry.register(updated, self.registry.status(thread.name))
            return updated

    def resolve_thread_model(self, name: str, default: str = NativeThreadDefaults.model) -> str:
        """Prefer the registered choice, then runtime observation, then default."""
        thread = self.registry.require(name)
        if not thread.needs_model_observation():
            return thread.model
        info = self.agents.agent_info_of(thread.name)
        return info.model_or(default)

    def attach_session(self, original: Thread, source_path: str, *, process: ProcessState | None = None) -> Thread:
        """Attach Pi state and retain its model once, outside the wire lock."""
        path = Path(source_path).expanduser().resolve()
        location = original.backend.location_type(str(path))
        saved_model = None
        if original.needs_model_observation() and path.is_file():
            choice = location.model_choice
            saved_model = "/".join(choice) if choice is not None else None
        with _store_lock(self._wire_lock_path):
            current = self.registry.require(original.name)
            if current != original:
                raise RelationViolationError("Original thread changed before native source publication")
            attached = replace(
                current.initialized_native_configuration(model=saved_model, thinking_level=None),
                process_identity=current.process_identity if process is None else process,
                session_file=location.read_key,
            )
            self.registry.register(attached)
            return attached

    def archive(self, name: str) -> None:
        """Hide a stopped participant from presence while retaining messages."""
        with _store_lock(self._wire_lock_path):
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
        with _store_lock(self._wire_lock_path):
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
        resolved_bin = NativeArguments.configured_binary()
        runtime = LaunchRuntime.current()
        with spec.prepared(self, resolved_bin, runtime) as child:
            child = self.registry._declare_unlocked(child)
            try:
                owned = self.owners._launch_owner_unlocked(
                    child, resolved_bin, initial_input=spec.task, launch_runtime=runtime,
                )
                spec.task.record_first_input(
                    partial(InputDispositions(self.root).record, owner=owned.name, target=owned.name,
                        claim_scope=self.registry.snapshot().claimed_generations[owned.name],
                    ))
                return owned
            except OSError:
                self.registry.remove(spec.name)
                raise

    def adopt_current(self) -> Thread:
        """Declare and register the current process's thread from env."""
        thread = current_thread(self.registry)
        return self.registry.declare(thread)
