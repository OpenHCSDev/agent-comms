"""Thread registration and authority transactions over their determining document."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate

from .catalog_store import ChannelCatalog
from .child_process import NoProcess, ProcessIdentity
from .errors import RelationViolationError
from .goal_actions import GoalActionContext, SetGoalAction
from .goal_history import GoalHistoryEntry, GoalHistoryStore
from .maintenance_barrier import MaintenanceBarrier
from .native_input_owner import RegistryOwner
from .private_registry_guard import PrivateRegistryGuard
from .registration_change import RegistrationChange
from .registry_document import RegistryDocument, RegistrySnapshot
from .registry_store import RegistryEdit, RegistryEntryRevision, RegistryStore
from .routing import TurnRouting
from .storage_rules import StorageRule
from .thread_execution import CommanderForkExecution
from .thread_identity import GenerationCounter, TurnId
from .thread_status import (
    ConnectedThreadStatus,
    NoBridgeThreadStatus,
    RunningThreadStatus,
    ThreadStatus,
)
from .thread_text import ForkedParent
from .threads import Thread

if TYPE_CHECKING:
    from .goal_management import Goals

_RUNNING_STATUS = RunningThreadStatus()


class ProjectThreadSelection:
    """The registry's complete answer, including absence and ambiguity."""

    @classmethod
    def from_threads(cls, threads: tuple[Thread, ...]) -> ProjectThreadSelection:
        if not threads:
            return NoProjectThread()
        if len(threads) == 1:
            return FoundProjectThread(threads[0])
        return SeveralProjectThreads(threads)


@dataclass(frozen=True, slots=True)
class NoProjectThread(ProjectThreadSelection):
    """There is no declared ACP thread at this project."""


@dataclass(frozen=True, slots=True)
class FoundProjectThread(ProjectThreadSelection):
    thread: Thread


@dataclass(frozen=True, slots=True)
class SeveralProjectThreads(ProjectThreadSelection):
    threads: tuple[Thread, ...]


class Registration:
    """Own registration transactions; never borrow a Comms or facade's state."""

    def __init__(self, store_path: Path):
        self.store = RegistryStore(store_path)
        self.storage_rule = StorageRule.load(store_path.parent)
        self.store.read()

    def register(
        self,
        thread: Thread,
        status: ThreadStatus = _RUNNING_STATUS,
        *,
        new_owner: bool = False,
    ) -> None:
        with self.store.editing() as edit:
            change = edit.document.prepare_registration(thread, status, new_owner=new_owner)
            self._commit_registration(edit, change)

    def prepare_native_root(self) -> PrivateRegistryGuard:
        """Prepare native authority, then its complete runtime outside the registry lock."""
        with self.store.locked():
            self.store.read_snapshot_unlocked().purpose.require_write()
            guard = self.store.private_guard_unlocked().prepare_native(self.store.path)
        guard.prepare_runtime()
        return guard

    def register_commander_child(self, name: str, parent_name: str, goals: Goals) -> Thread:
        """Declare or resume one child and its own goal under the registry's lock."""
        with self.store.editing() as edit:
            parent = edit.document.require(parent_name)
            if edit.document.canonical_name(name) != name:
                raise RelationViolationError(f"Commander fork {name!r} is an alias.")
            if edit.document.taken(name):
                existing = edit.document.require(name)
                existing.execution.require_commander_fork()
                if not existing.parent.named(parent.name):
                    raise RelationViolationError(f"Thread {name!r} is not a fork of {parent.name!r}.")
                return self._declare_in(edit, existing, _RUNNING_STATUS)
            child = Thread(name, parent.tags, parent.worktree,
                parent=ForkedParent(parent.name), model=parent.model,
                thinking_level=parent.thinking_level, service_tier=parent.service_tier,
                execution=CommanderForkExecution)
            text = ('\n'.join(goal.text for goal in parent.goal.active())
                    or f'Coordinate assigned work for {parent.name}.')
            with self._preparing_declaration(edit, child, _RUNNING_STATUS) as change:
                goal = SetGoalAction(text=text).create(
                    GoalActionContext(goals, change.thread))
                return self._commit_registration(edit, replace(
                    change, thread=replace(change.thread, goal=goal))).thread

    def reserve_owner(self, original: Thread, process: ProcessIdentity) -> RegistryOwner:
        """Reserve one process against the exact stopped launch it prepared.

        The registry transaction, not a root-wide lock, prevents concurrent
        starters from replacing each other's reservation. Unrelated metadata
        comes from the current declaration rather than the launch's old copy.
        """
        with self.store.editing() as edit:
            current = edit.document.require(original.name)
            if not current.same_launch(original):
                raise RelationViolationError("Owner launch changed before process reservation.")
            if current.process_alive:
                raise RelationViolationError("Thread already has a live owner process.")
            edit.document.status(current.name).require_visible(
                "Only visible agent threads can be started.")
            owned = replace(current.without_turn(), process_identity=process)
            change = self._commit_registration(edit, edit.document.prepare_registration(
                owned, _RUNNING_STATUS, new_owner=True))
            return RegistryOwner(
                thread=change.thread,
                claimed_generation=edit.document.entry(change.thread.name).claimed_generation,
            )

    @contextmanager
    def external_process(self, name: str, process: ProcessIdentity):
        """The CLI bridge owns its registered endpoint until that same process leaves."""
        with self.store.editing() as edit:
            thread = edit.document.require(name)
            thread.execution.require_external()
            if thread.process_alive and thread.process_identity != process:
                raise RelationViolationError("Another live process owns this CLI endpoint.")
            bound = replace(thread, process_identity=process)
            self._commit_registration(edit, edit.document.prepare_registration(
                bound, ConnectedThreadStatus(), new_owner=True))
            owner = edit.document.entry(bound.name).owner_identity
        try:
            yield owner.incarnation
        finally:
            with self.store.editing() as edit:
                entry = edit.document.entry_if_present(owner.incarnation.name)
                if entry is not None and entry.owned_by(owner, process):
                    self._commit_registration(edit, edit.document.prepare_registration(
                        replace(entry.thread, process_identity=NoProcess()), NoBridgeThreadStatus(),
                        new_owner=False))

    def attach_native_session(self, original: RegistryOwner, session_file: str) -> RegistryOwner:
        """Publish an observed native source under its original executable lease.

        Registry permission checks own the determining facts. A changing progress
        phase is retained from the locked document, not compared to an old view.
        Native session identity and context proof remain with their producers.
        """
        with self.store.editing() as edit:
            change = edit.prepare_native_source(original, session_file)
            change = self._commit_registration(edit, change)
            # The document owns the next turn's selection. Publishing an
            # observed source must not rewrite this authorized turn's settings.
            observed = replace(change.thread,
                worktree=original.thread.worktree, model=original.thread.model,
                thinking_level=original.thread.thinking_level)
            return replace(original, thread=observed)

    def declare(self, thread: Thread, status: ThreadStatus = _RUNNING_STATUS) -> Thread:
        """Operational declaration and channel provenance use one locked current owner.

        Internal register remains exact state replacement; worker/public declaration
        preserves omitted provenance and a current executor before applying the same
        registration effects. No pre-read can authorize a stale owner replacement.
        """
        return self._declare_unlocked(thread, status)

    def _declare_unlocked(self, thread: Thread, status: ThreadStatus = _RUNNING_STATUS) -> Thread:
        """Cross-store declarations share the registry's own edit transaction."""
        with self.store.editing() as edit:
            return self._declare_in(edit, thread, status)

    def _claim_unlocked(self, thread: Thread) -> Thread:
        """The caller holds inbox authority; namespace allocation stays registry-owned."""
        with self.store.editing() as edit:
            requested = thread.for_claim(edit.document.claim_name(thread.name))
            return self._declare_in(edit, requested, RunningThreadStatus())

    @staticmethod
    def _prepare_declaration(document, channels, thread: Thread, status: ThreadStatus):
        """Derive one declaration after checking its complete namespace."""
        change = document.prepare_declaration(thread, status)
        for tag in change.thread.tags - channels.tags:
            channels.require_available_tag_name(tag)
        return change

    def require_declaration_available(
        self, thread: Thread, status: ThreadStatus = _RUNNING_STATUS,
    ) -> None:
        """Refuse a known-invalid declaration before its producer performs side effects."""
        catalog = ChannelCatalog.at(self.store.path.parent)
        with self.store.reading() as document, catalog.reading() as channels:
            self._prepare_declaration(document, channels, thread, status)

    def _declare_in(self, edit: RegistryEdit, thread: Thread, status: ThreadStatus) -> Thread:
        with self._preparing_declaration(edit, thread, status) as change:
            return self._commit_registration(edit, change).thread

    @contextmanager
    def _preparing_declaration(self, edit: RegistryEdit, thread: Thread,
                              status: ThreadStatus) -> Iterator[RegistrationChange]:
        catalog = ChannelCatalog.at(self.store.path.parent)
        with catalog.editing() as channels:
            change = self._prepare_declaration(edit.document, channels, thread, status)
            yield change
            channels.remember_tags(change.thread.tags)

    def _commit_registration(self, edit: RegistryEdit, change: RegistrationChange) -> RegistrationChange:
        """Every registration commits here, after inheritance kept a redeclared thread's
        roots; the registry assigns a new thread its storage roots in this one place."""
        thread = change.thread
        change = replace(change, thread=replace(
            thread, storage_roots=thread.storage_roots.assigned(self.storage_rule, thread.name)))
        change.thread.role.require_maintenance_open(
            MaintenanceBarrier(self.store.path), change.claims_owner
        )
        before = change.prior_goal
        history = None
        intent = None
        if before != change.thread.goal:
            history = GoalHistoryStore(self.store.path)
            intent = history.begin(change.thread.incarnation.wire_birth, before, change.thread.goal)
        change.apply(edit.document)
        edit.commit()
        if history is not None and intent:
            history.commit(intent)
        return change

    @staticmethod
    def changing[**P, R](operation: Callable[Concatenate[RegistryDocument, P], R]) -> Callable[
        Concatenate[Registration, P], R]:
        def change(self: Registration, /, *args: P.args, **kwargs: P.kwargs) -> R:
            with self.store.editing() as edit:
                result = operation(edit.document, *args, **kwargs)
                edit.commit()
                return result
        return change

    restore_stopped = changing(RegistryDocument.restore_stopped)
    rename = changing(RegistryDocument.rename)
    set_work_state = changing(RegistryDocument.set_work_state)
    observe_offered_tools = changing(RegistryDocument.observe_offered_tools)
    set_commander_project = changing(RegistryDocument.set_commander_project)

    def fence_idle_owners(
        self, selected: Sequence[tuple[Thread, int]]
    ) -> tuple[tuple[Thread, int], ...]:
        """Commit all original idle owner fences together, or change none."""
        with self.store.editing() as edit:
            fenced = tuple(
                (thread, edit.document.fence_idle_owner(
                    thread, expected_claimed_generation=generation
                ))
                for thread, generation in selected
            )
            edit.commit()
            return fenced

    unregister = changing(RegistryDocument.unregister)
    archive = changing(RegistryDocument.archive)
    begin_delete = changing(RegistryDocument.begin_delete)
    archive_originals = changing(RegistryDocument.archive_originals)
    begin_delete_originals = changing(RegistryDocument.begin_delete_originals)
    remove_originals = changing(RegistryDocument.remove_originals)
    change_tag = changing(RegistryDocument.change_tag)
    remove = changing(RegistryDocument.remove)
    heartbeat = changing(RegistryDocument.heartbeat)
    release_turn = changing(RegistryDocument.release_turn)

    def live_owner_with_claim(self, name: str) -> tuple[Thread, int]:
        """Read the stored process input generation, independent of metadata revisions."""
        owner = RegistryOwner.capture_local(self.snapshot(), name)
        return owner.thread, owner.claimed_generation

    def lease_live_turn_with_claim(
        self, expected: Thread, turn_id: str, *, expected_generation: int
    ) -> tuple[Thread, int]:
        """Claim a turn against the input generation, allowing unrelated metadata changes."""
        Thread.require_declaration(expected)
        turn = TurnId.for_registration(turn_id)
        generation = GenerationCounter.require_positive(expected_generation)
        with self.store.editing() as edit:
            owner = RegistryOwner.capture_local(edit.original.snapshot, expected.name)
            owner.require_claim(expected, generation)
            leased, _ = edit.lease_turn(owner.thread, turn.value, None)
            edit.commit()
            return leased, generation

    def lease_live_turn_with_generation(
        self,
        expected: Thread,
        turn_id: str,
        *,
        expected_owner_generation: int,
        routing: TurnRouting | None = None,
    ) -> tuple[Thread, int]:
        """Claim against the exact captured declaration and stable owner generation."""
        Thread.require_declaration(expected)
        turn = TurnId.for_registration(turn_id)
        generation = GenerationCounter.require_positive(expected_owner_generation)
        with self.store.editing() as edit:
            snapshot = edit.original.snapshot
            owner = RegistryOwner.capture_local(snapshot, expected.name)
            owner.require_exact(snapshot, expected, generation)
            owner.thread.turn_record.read().require_idle()
            result = edit.lease_turn(owner.thread, turn.value, routing)
            edit.commit()
            return result

    def lease_local_turn(
        self, name: str, turn_id: str, *, routing: TurnRouting | None = None
    ) -> tuple[Thread, int]:
        """Atomic local begin-turn, never reviving a stopped or replaced owner."""
        turn = TurnId.for_registration(turn_id)
        with self.store.editing() as edit:
            owner = RegistryOwner.capture_local(edit.original.snapshot, name)
            owner.thread.turn_record.read().require_idle()
            result = edit.lease_turn(owner.thread, turn.value, routing)
            edit.commit()
            return result

    @staticmethod
    def reading[**P, R](operation: Callable[Concatenate[RegistryDocument, P], R]) -> Callable[
        Concatenate[Registration, P], R]:
        def read(self: Registration, /, *args: P.args, **kwargs: P.kwargs) -> R:
            with self.store.reading() as document:
                return operation(document, *args, **kwargs)
        return read

    canonical_name = reading(RegistryDocument.canonical_name)
    last_seen = reading(RegistryDocument.seen_at)
    require = reading(RegistryDocument.require)
    status = reading(RegistryDocument.status)
    user_thread = reading(RegistryDocument.user_thread)

    def goal_history(
        self, name: str, *, goal_id: str | None = None
    ) -> tuple[GoalHistoryEntry, ...]:
        """Read this owner's recorded transitions, reconciling crash cuts first."""

        with self.store.locked():
            document = self.store._read_unlocked()
            thread = document.require(name)
            return GoalHistoryStore(self.store.path).history(
                thread.incarnation.wire_birth, thread.goal, goal_id=goal_id
            )

    def session_thread(
        self, project_path: Path, preferred: str | None = None,
    ) -> ProjectThreadSelection:
        """Select an unbound project's preferred name or sole declared ACP thread.

        Preferred names keep their previous precedence, including non-ACP
        threads. Bound sessions use strict lookup instead of project discovery.
        """
        project = Path(project_path).expanduser().resolve()
        with self.store.reading() as document:
            located = tuple(t for t in document.threads.values() if t.project_path == project)
            if preferred:
                selected = document.canonical_name(preferred)
                for thread in located:
                    if thread.name == selected:
                        return FoundProjectThread(thread)
            return ProjectThreadSelection.from_threads(tuple(
                member for thread in located for member in thread.role.acp_thread(thread)
            ))

    def all_threads(self) -> Mapping[str, Thread]:
        with self.store.reading() as document:
            return dict(document.threads)

    def schema_paths(self) -> frozenset[Path]:
        """Every SQLite file a registered thread's runtime keeps beside its saved session."""
        return frozenset(
            path for thread in self.all_threads().values()
            for files in thread.runtime.storage_files() for path in files.schema_paths
        )

    def entry(self, name: str) -> RegistryEntryRevision:
        """This thread's registry entry and the revision it was read from.

        Other threads' declarations are not decoded.
        """
        return self.store.read_entry(name)

    def snapshot(self) -> RegistrySnapshot:
        """Read related declarations and statuses from exactly one store revision."""
        with self.store.reading_snapshot() as snapshot:
            return snapshot

    def active_threads(self) -> Mapping[str, Thread]:
        with self.store.reading() as document:
            return {thread.name: thread
                    for thread in document.active_threads(document.threads.values())}

    def peers(self, exclude: str) -> Sequence[str]:
        with self.store.reading() as document:
            exclude = document.canonical_name(exclude)
            return [name for name in document.threads if name != exclude]

    def __contains__(self, name: str) -> bool:
        with self.store.reading() as document:
            name = document.canonical_name(name)
            return name in document.threads
