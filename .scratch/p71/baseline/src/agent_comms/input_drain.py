"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass, replace
from contextlib import aclosing
from functools import partial
from pathlib import Path
from typing import Any

from acp.schema import PromptResponse

from .acp_extension import (
    PromptRequest,
    InputDeliveryChangedUpdate,
    InputStartedUpdate,
    QueueChangedUpdate,
    QueueScope,
    QueueProjection,
    encode_updates,
)
from .agent_backend import InputContent, InputId, SendNow, TurnInboxItem
from .comms import Comms
from .coordination_errors import CoordinationError, IdentityConflict
from .drain_failures import drain_failures
from .coordination_cohort import next_sealed_assignment
from .coordinator import Coordination
from .cursor_owner import CursorOwner
from .child_process import join_retirement
from .native_input_owner import RegistryOwner
from .input_attempt import InputAttempt
from .input_disposition import InputDispositions, InputDocument
from .maintenance_barrier import MaintenanceBarrier, MaintenanceStatus
from .input_effects import InputEffects
from .queued_input import DeliveredInput, InputHandoffRefused, QueuedInput
from .private_bus_checkpoint import Addressed
from .private_path import FileRevision
from .registry_document import RegistryEntry
from .registry_store import RegistryEntryRevision
from .routing import ScheduledTurn
from .runtime import UNBOUND_CONTROLLER, RuntimeServer
from .schedule_rules import ProjectResumeCheck, WakeScheduleCheck
from .session_lifecycle import SessionLifecycle
from .store_files import _store_lock
from .thread_identity import AdmissionIdentity
from .threads import Thread
from .turn_input_source import OriginalTurnInput, AcceptedFollowingInput
from .wire_metadata import WireMetadata
from .wire_watch import WatchSource, WireWatch
from .goal_presentation import GoalExecutionState
from .goal_waits import GoalWaits
from .errors import RelationViolationError, UnregisteredThreadError
from .turn_context import TurnContext, UserFollowupSegment

AGENT_PREFIX = "!agent "


@dataclass(frozen=True, slots=True)
class TickRevision:
    """What an observation tick depends on, with the stores every owner writes apart.

    Every owner's entry lives in the one registry file and every owner's rows
    in the one coordination store, so their revisions change when any owner's
    part does. This owner's registry entry and its private observation of the
    coordination store say whether its own part did.
    """

    registry: tuple[FileRevision | None, ...]
    coordination: tuple[FileRevision | None, ...]
    # The shared stores every publisher's appends change: the bus log and its
    # marker, and the sealed checkpoint that records which rows address which
    # recipient. They are compared apart from this owner's own stores.
    bus: tuple[FileRevision | None, ...]
    checkpoint: FileRevision | None
    other: tuple

    @classmethod
    def paths(
        cls, root, *, registry: Path, registry_guard: Path,
        bus: Path, bus_marker: Path, auth: Path,
    ) -> TickRevisionPaths:
        """Declare every physical file read by a tick in its comparison groups."""
        return TickRevisionPaths(
            registry=(registry, registry_guard),
            coordination=(
                root / "coordination.sqlite3",
                root / "coordination.sqlite3-wal",
            ),
            bus=(bus, bus_marker),
            checkpoint=root / "private_bus_checkpoint.sqlite3",
            goal_waits=root / GoalWaits.filename,
            goal_attempts=(
                root / "goal-private" / "goal_attempts.sqlite3",
                root / "goal-private" / "goal_attempts.sqlite3-wal",
            ),
            auth=auth,
            maintenance=MaintenanceBarrier(registry).witness_paths,
        )


@dataclass(frozen=True, slots=True)
class TickRevisionPaths:
    """Physical tick dependencies, shared by revision capture and file watching."""

    registry: tuple[Path, ...]
    coordination: tuple[Path, ...]
    bus: tuple[Path, ...]
    checkpoint: Path
    goal_waits: Path
    goal_attempts: tuple[Path, ...]
    auth: Path
    maintenance: tuple[Path, Path]

    @property
    def other(self) -> tuple[Path, ...]:
        return self.goal_waits, *self.goal_attempts, self.auth, *self.maintenance

    @property
    def watch_sources(self) -> tuple[WatchSource, ...]:
        # The marker is the final durable publication for bus bytes and their
        # checkpoint. Watching the intermediate files wakes readers before the
        # append is complete and repeats the same observation several times.
        replaced = (*self.registry, *self.bus[1:], self.goal_waits, self.auth, *self.maintenance)
        modified = (*self.coordination, *self.goal_attempts)
        return (
            *(WatchSource.file(path) for path in replaced),
            *(WatchSource.modified_file(path) for path in modified),
        )

    def capture(self, local: tuple) -> TickRevision:
        revisions = lambda paths: tuple(FileRevision.capture(path) for path in paths)
        return TickRevision(
            registry=revisions(self.registry),
            coordination=revisions(self.coordination),
            bus=revisions(self.bus),
            checkpoint=FileRevision.capture(self.checkpoint),
            other=(revisions(self.other), *local),
        )


@dataclass(frozen=True, slots=True)
class PrivateObservation:
    """One drain's observation: its message cut, registry entry and own coordination work."""

    wire: tuple
    entry: RegistryEntry
    work: tuple

    async def drain(self, inputs, session_id: str, root_id: str) -> int:
        await DeliveredInput.recover_interrupted(inputs, session_id, root_id)
        return await inputs.effects._drain_private_nk(session_id, root_id)


class ExecutionWaitingObservation(PrivateObservation):
    """A previous execution blocks new input until its stored state changes."""

    async def drain(self, inputs, session_id: str, root_id: str) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class SettledTick:
    """The revision a settled tick started from, and this owner's registry entry then.

    admission_after_seq is this owner's admitted floor, read from the durable
    marker at settle time.
    """

    revision: TickRevision
    entry: RegistryEntry
    admission_after_seq: int | None = None
    through_seq: int = 0


class InputObservation:
    """File-driven observation and process-local wake behavior."""

    def _tick_paths(self) -> TickRevisionPaths:
        configuration = self.sessions.config.configuration
        return TickRevision.paths(
            self.comms.root,
            registry=self.comms.registry.store.path,
            registry_guard=self.comms.registry.store.guard_path,
            bus=self.comms.bus.log.path,
            bus_marker=self.comms.bus.log.metadata_path,
            auth=configuration.native_config / "auth.json",
        )

    def _tick_revision(
        self, session_id: str, paths: TickRevisionPaths | None = None,
    ) -> TickRevision:
        """Stat every declared physical dependency plus process-local inputs."""
        return (paths or self._tick_paths()).capture((
            self.sessions.bindings.get(session_id),
            self.auto_wake,
            self.sessions.runtime_enabled,
            self.effects._private_nk_native_package,
        ))

    def _retire_observer(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self.drain_tasks.get(session_id) is task:
            self._observation_watchers.pop(session_id, None)

    def wake_observer(self, session_id: str) -> None:
        # A process-local wake can carry work that did not change an input file.
        self._repeat_private_observations.add(session_id)
        watcher = self._observation_watchers.get(session_id)
        if watcher is not None:
            watcher.notify()

    async def retry_observer(self, session_id: str) -> None:
        await asyncio.sleep(0.05)
        self.wake_observer(session_id)

    def register_wake(self, session_id: str, task: asyncio.Task[None]) -> None:
        self.wake_tasks[session_id] = task

        def finished(completed: asyncio.Task[None]) -> None:
            # A deferred wake has not consumed its queue or assignment. The
            # maintenance witness itself wakes the observer on READY; waking
            # again now would spin on the same closed phase.
            if (not completed.cancelled() and completed.exception() is None
                    and isinstance(completed.result(), MaintenanceStatus)):
                return
            self.wake_observer(session_id)

        task.add_done_callback(finished)

    def _repeat_observation_ready(self, session_id: str, drained: int) -> bool:
        if drained != 0 or session_id not in self._repeat_private_observations:
            return False
        if self.pending_turns.get(session_id):
            return False
        if session_id in self.effects.turns.turn_tasks:
            return False
        return session_id not in self.backend_inboxes

    def _indexed_changes_for_owner(
        self, settled: SettledTick, revision: TickRevision, lookup: str,
    ) -> tuple[WireMetadata, bool]:
        """Read this owner's address aggregate only from the captured index cut."""
        previous_info, current_info = settled.revision.bus[0], revision.bus[0]
        if previous_info is None or current_info is None or revision.checkpoint is None:
            raise RelationViolationError("Wire address index has no stable file revision")
        if previous_info.identity != current_info.identity:
            raise RelationViolationError("Wire tail does not extend the settled source")
        return Addressed.observed_after(
            self.comms.bus.log, revision.checkpoint, current_info,
            revision.bus[1], lookup, settled.through_seq,
        )


    def ensure_live_drain(self, session_id: str) -> None:
        """Keep one background task per session pushing inbox messages live."""
        if self.closing:
            return
        if session_id in self.drain_tasks and not self.drain_tasks[session_id].done():
            return

        task = self.runtime.background(self.observe(session_id))
        self.drain_tasks[session_id] = task
        task.add_done_callback(
            lambda completed: self._retire_observer(session_id, completed)
        )

    def _in_standby(self, session_id: str) -> bool:
        """The published goal execution waits on other threads.

        A standby projects its targets' turns and its closed-group recovery
        walks the whole wait graph, so other threads' entries concern it.
        """
        _goal, execution = self.effects.turns.goals.goal_execution_signatures.get(
            session_id, (None, None),
        )
        return execution is not None and execution.state is GoalExecutionState.STANDBY

    async def _own_entry(self, session_id: str) -> RegistryEntryRevision:
        return await Coordination.run_worker(partial(
            self.comms.registry.entry, self.sessions.require(session_id),
        ))

    def _quiescent(self, session_id: str) -> bool:
        """No process-local work or retry is waiting on the next tick."""
        wake = self.wake_tasks.get(session_id)
        if session_id in self._repeat_private_observations:
            return False
        return (
            session_id in self._idle_private_revisions
            and not self.effects.cursors.delivery(session_id).needs_refresh
            and not self.pending_turns.get(session_id)
            and session_id not in self.effects.turns.turn_tasks
            and session_id not in self.backend_inboxes
            and (wake is None or wake.done())
        )

    async def observe(self, session_id: str) -> None:
        settled: SettledTick | None = None
        paths = self._tick_paths()
        async with aclosing(WireWatch.observations(*paths.watch_sources)) as observations:
            async for watcher in observations:
                self._observation_watchers[session_id] = watcher
                # A tick whose stores and inputs are unchanged since a settled
                # tick has nothing new to observe; any change runs it in full.
                # Another owner's registry or coordination change is not this
                # owner's: its own entry and private observation are what it
                # last settled.
                revision = await Coordination.run_worker(
                    partial(self._tick_revision, session_id)
                )
                idle = (
                    settled is not None
                    and revision.other == settled.revision.other
                    and self._quiescent(session_id)
                )
                # The settled entry is still current only while the registry is unchanged.
                entry = settled.entry if idle else None
                entry_current = idle and revision.registry == settled.revision.registry
                if idle and not entry_current:
                    # A standby's wait graph reads other threads' entries.
                    idle = not self._in_standby(session_id)
                    if idle:
                        read = await self._own_entry(session_id)
                        # Settle at the revision the entry was read from, not
                        # at the stat taken while the write was in flight.
                        revision = replace(revision, registry=read.revision)
                        entry, entry_current = read.entry, True
                        idle = entry == settled.entry
                shared_unchanged = (
                    settled is not None
                    and revision.bus == settled.revision.bus
                    and revision.checkpoint == settled.revision.checkpoint
                    and revision.coordination == settled.revision.coordination
                )
                if idle and shared_unchanged:
                    settled = SettledTick(
                        revision, entry, settled.admission_after_seq, settled.through_seq,
                    )
                    continue
                if idle and not shared_unchanged:
                    # Only shared stores grew (every publisher's appends change
                    # them); each shared store answers per owner before any row
                    # is decoded for this owner. Standby owners resolve the same
                    # per-owner answers: their wait graph needs a full tick only
                    # when their own part (addressed rows, floor or coordination
                    # rows) changed.
                    outcome = await self._shared_changes_concern_other_owners(
                        session_id, settled, revision, entry,
                    )
                    if outcome is not None:
                        settled = outcome
                        continue
                settled = None
                thread = self.sessions.require(session_id)
                if not entry_current:
                    # Read after the revision: the tick observes at least this entry.
                    read = await self._own_entry(session_id)
                    revision = replace(revision, registry=read.revision)
                    entry = read.entry
                owner = entry.owner_identity
                async with drain_failures(
                    self.comms, thread, owner, partial(self.retry_observer, session_id),
                ):
                    # A stored model change precedes the messages the same tick hands over.
                    await self.sessions.config.sync_thread(session_id)
                    drained = await self.drain_inbox(session_id)
                    if self._in_standby(session_id):
                        # Recovery reads only stores in the revision, so it runs
                        # when one of them changed, not on a clock.
                        await Coordination.run_worker(partial(
                            self.comms.goals.recover_closed_goal_wait, session_id,
                        ))
                    await ProjectResumeCheck(
                        session_id=session_id, inputs=self, turns=self.effects.turns,
                    ).schedule()
                    await self.effects.turns.goals.schedule_goal(session_id)
                    current = await Coordination.run_worker(partial(self.comms.registry.require, thread))
                    if (
                        not current.executing
                        and session_id not in self.effects.turns.turn_tasks
                        and session_id not in self.backend_inboxes
                    ):
                        if not current.executing and self._quiescent(session_id):
                            marker = await Coordination.run_worker(self._bus_marker)
                            settled = SettledTick(
                                revision, entry, marker.admission_after_seq, marker.last_seq,
                            )
                        elif self._repeat_observation_ready(session_id, drained):
                            # The first empty private observation may advance its
                            # own durable cursor. Re-read that transition now;
                            # healthy file notification remains event-driven.
                            watcher.notify()
                        elif self.effects.cursors.delivery(session_id).needs_refresh:
                            # Cursor publication has process-local work even when
                            # no wake task exists to produce another file change.
                            watcher.notify()

    def _bus_marker(self):
        return self.comms.bus.log._private_marker_unlocked()

    async def _shared_changes_concern_other_owners(
        self, session_id: str, settled: SettledTick, revision: TickRevision,
        entry: RegistryEntry,
    ) -> SettledTick | None:
        """Resolve shared-store changes per owner before any row is decoded.

        Every publisher's append changes the shared bus log, its marker, the
        sealed checkpoint and the coordination store, but none of that is this
        owner's concern until the address record shows rows this owner has not
        admitted, or this owner's own coordination rows changed. The floor
        comparison and the sealed address read answer the bus and checkpoint
        components; _own_coordination_unchanged answers the coordination
        component. An unavailable answer keeps the full tick — unavailable
        never means 'not mine'.
        """
        admission_after_seq, through_seq = settled.admission_after_seq, settled.through_seq
        try:
            if (
                revision.bus != settled.revision.bus
                or revision.checkpoint != settled.revision.checkpoint
            ):
                marker, addressed = await Coordination.run_worker(partial(
                    self._indexed_changes_for_owner,
                    settled,
                    revision,
                    entry.thread.incarnation.lookup,
                ))
                if marker.admission_after_seq != settled.admission_after_seq or addressed:
                    return None
                admission_after_seq, through_seq = marker.admission_after_seq, marker.last_seq
            if revision.coordination != settled.revision.coordination:
                if not await self._own_coordination_unchanged(session_id, entry):
                    return None
        except (
            OSError,
            ValueError,
            TypeError,
            sqlite3.Error,
            CoordinationError,
            RelationViolationError,
        ):
            return None
        return SettledTick(revision, entry, admission_after_seq, through_seq)

    def _private_observation_revision(
        self, session_id: str, root_id: str, wire: tuple, entry: RegistryEntry,
        store: Coordination,
    ) -> PrivateObservation:
        """Original owner/work observations, never delivery or replay authority.

        Other owners' registry phases and SQL writes cannot change this owner's
        observation. Committed wire messages, own pending/recovered work and
        physical replacement still can. Silent context records consume bytes,
        not the message cut used to select delivery.
        """
        owner = RegistryOwner(thread=entry.thread, admission_generation=entry.admission_generation)
        with store.session.read():
            participant = store.participants.get(owner.thread.incarnation.lookup)
            pending = next_sealed_assignment(
                store, participant.lookup, owner.thread.name, after_seq=wire[-1],
            )
            cursor = CursorOwner(
                thread=owner.thread, admission_generation=owner.admission_generation,
                wire_root_id=root_id, generation=participant.participant_generation,
            ).cursor(store.session._connection)
            original = store.session.path.stat()
        observation = PrivateObservation
        if (
            participant.pointer.unresolved
            and not self.owns_execution(session_id, owner.thread)
        ):
            observation = ExecutionWaitingObservation
        return observation(wire, entry, (
            root_id,
            self.effects._private_nk_native_package,
            self.auto_wake,
            self.sessions.runtime_enabled,
            self.sessions.bindings.get(session_id),
            session_id in self.backend_inboxes,
            session_id in self.effects.turns.turn_tasks,
            participant,
            pending,
            cursor,
            (original.st_dev, original.st_ino),
            tuple(FileRevision.capture(path) for path in
                  MaintenanceBarrier(self.comms.registry.store.path).witness_paths),
        ))

    async def _observe_private_revision(self, session_id: str, root_id: str) -> PrivateObservation:
        # Pending acquisition is not an inbox failure or a completed read.
        # Cancellation retires only this observation's waiting descriptor.
        def wire_revision(source):
            if source.marker.root_id != root_id:
                raise IdentityConflict("private drain original root changed")
            return (
                source.witness.source_identity,
                source.committed_sequence(),
                source.marker.admission_after_seq,
            )
        wire = await self.comms.bus.log.read_certified_async(wire_revision)
        entry = (await self._own_entry(session_id)).entry
        return await self._observe_coordination(session_id, root_id, wire, entry)

    async def _observe_coordination(
        self, session_id: str, root_id: str, wire: tuple, entry: RegistryEntry,
    ) -> PrivateObservation:
        # No physical bus custody survives into registry/SQL consumer work.
        # The original coordinator owns its bounded read acquisition policy;
        # the observation must not replace it with a zero-wait failure.
        return await Coordination.run_read_async(
            self.comms.root / "coordination.sqlite3",
            partial(self._private_observation_revision, session_id, root_id, wire, entry),
        )

    async def _own_coordination_unchanged(self, session_id: str, entry: RegistryEntry) -> bool:
        """Only the coordination store changed since this owner's last idle drain: are its rows?

        The caller's tick found the bus log, its marker and checkpoint unchanged
        and holds the current registry entry, so the settled message cut and
        this entry still stand; only this owner's coordination rows are read.
        """
        async with self.drain_locks.setdefault(session_id, asyncio.Lock()):
            settled = self._idle_private_revisions.get(session_id)
            if settled is None:
                return False
            observed = await self._observe_coordination(
                session_id, self.effects._configured_root_id(), settled.wire, entry,
            )
            return observed == settled


class InputDrain(InputObservation):
    def owns_execution(self, session_id: str, owner: Thread) -> bool:
        """An attached turn can take follow-ups; a predecessor cannot."""
        return (
            owner.turn_state.busy
            or session_id in self.backend_inboxes
            or session_id in self.effects.turns.turn_tasks
        )

    def __init__(
        self,
        comms: Comms,
        sessions: SessionLifecycle,
        runtime: RuntimeServer,
        effects: InputEffects,
        auto_wake: bool,
    ):
        self.comms = comms
        self.sessions = sessions
        self.runtime = runtime
        self.effects = effects
        self.drain_tasks: dict[str, asyncio.Task[None]] = {}
        self.backend_inboxes: dict[str, asyncio.Queue[TurnInboxItem]] = {}
        self.queued_inputs: dict[str, dict[str, QueuedInput]] = {}
        self.queue_lock = asyncio.Lock()
        self.restored_inputs: dict[str, dict[str, QueuedInput]] = {}
        self.queue_revisions: dict[str, int] = {}
        self.following_sources: dict[str, dict[str, AcceptedFollowingInput]] = {}
        self.original_sources: dict[str, OriginalTurnInput] = {}
        self.dispositions = InputDispositions(comms.root)
        self.auto_wake = auto_wake
        self.pending_turns: dict[str, list[ScheduledTurn]] = {}
        self.drain_locks: dict[str, asyncio.Lock] = {}
        self.closing = False
        self.wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._observation_watchers: dict[str, WireWatch] = {}
        # Only a completed, quiescent observation can suppress another scan.
        # These revisions never authorize delivery, acceptance or a native send.
        self._idle_private_revisions: dict[str, PrivateObservation] = {}
        # A changed read or process-local wake needs a full observation.
        # A completed stable drain consumes this pending work.
        self._repeat_private_observations: set[str] = set()

    async def clear_queued_inputs(self, session_id: str) -> None:
        """Drop prompts queued against the backend for this session.

        Pi's own queue is cleared, then the local deferred-display list is
        reset. Callers may re-send the prompts they want to keep.
        """
        async with self.queue_lock:
            inbox = self.backend_inboxes.get(session_id)
            if inbox is not None:
                inbox.put_nowait({"type": "clear_queue"})
            self.queued_inputs[session_id] = {
                key: retained
                for key, item in self.queued_inputs.get(session_id, {}).items()
                if (retained := item.after_clear()) is not None
            }
            self.restored_inputs.pop(session_id, None)
        await self.emit_queue_state(session_id)

    async def queue_binding(self, session_id: str) -> QueueScope | None:
        try:
            owner, admission = await Coordination.run_worker(partial(
                self.comms.registry.live_owner_with_admission,
                self.sessions.bindings.get(session_id, session_id),
            ))
        except (RelationViolationError, UnregisteredThreadError):
            # No live local owner turn: the queue has no current scope.
            return None
        return QueueScope(
            session_id,
            AdmissionIdentity(owner.incarnation, admission),
            owner.pid,
        )

    async def queue_state(self, session_id: str) -> QueueChangedUpdate:
        """The producer owns the complete exact-ID queue projection."""
        scope = await self.queue_binding(session_id)
        revision = self.queue_revisions.get(session_id, 0) + 1
        self.queue_revisions[session_id] = revision
        return QueueChangedUpdate(scope, revision, QueueProjection.capture(
            scope,
            tuple(self.queued_inputs.get(session_id, {}).values()),
            tuple(self.restored_inputs.get(session_id, {}).values()),
        ))

    async def emit_queue_state(self, session_id: str, *, client: Any = None) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=(await self.queue_state(session_id)).acp_chunk(),
        )

    async def emit_input_started(
        self,
        session_id: str,
        text: str | None,
        input_id: str | None = None,
        source_scope: QueueScope | None = None,
        *,
        native_id: str | None,
        client: Any = None,
    ) -> None:
        scope = await self.queue_binding(session_id)
        if source_scope is None or scope is None or not source_scope.relation(scope).current:
            scope = None
        revision = None
        if scope is not None:
            revision = self.queue_revisions.get(session_id, 0) + 1
            self.queue_revisions[session_id] = revision
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=InputStartedUpdate(input_id, text, scope, revision, native_id).acp_chunk(),
        )

    async def emit_input_disposition(
        self, session_id: str, row: InputAttempt, client: Any = None
    ) -> None:
        await self.emit_input_delivery_changed(session_id, input_id=row.public_id, client=client)

    async def emit_input_delivery_changed(
        self, session_id: str, *, input_id: str | None = None, client: Any = None
    ) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=InputDeliveryChangedUpdate(input_id).acp_chunk(),
        )

    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None:
        """Refresh the producer-owned delivery ledger; this never replays an input."""
        await self.emit_input_delivery_changed(session_id, client=client)

    def input_keys(self, session_id: str) -> frozenset[str]:
        """Live captured source membership, never rebuilt from durable notices."""
        original = self.original_sources.get(session_id)
        following = frozenset(
            key for source in self.following_sources.get(session_id, {}).values()
            for key in source.keys
        )
        return following.union(original.keys) if original is not None else following

    async def awaiting_input_keys(self, session_id: str) -> frozenset[str] | None:
        """Derive delivery notices from existing owner queues; never create new authority."""
        snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
        owner = snapshot.require(self.sessions.require(session_id))
        if owner.pid != os.getpid() or not snapshot.status(owner.name).running:
            return None
        keys = set(self.input_keys(session_id))
        keys.update(
            self.dispositions.bus_key(turn.origin, owner)
            for turn in self.pending_turns.get(session_id, ())
            if turn.origin is not None
        )
        return frozenset(keys)

    async def drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self.drain_locks.setdefault(session_id, asyncio.Lock()):
            return await self.drain_owned_inbox(session_id)

    async def _drain_private_if_changed(self, session_id: str, root_id: str) -> int:
        # The caller still validates the current private marker each time. The
        # ordinary loop still synchronizes configuration and schedules goals.
        before = await self._observe_private_revision(session_id, root_id)
        if self._idle_private_revisions.get(session_id) == before:
            self._repeat_private_observations.discard(session_id)
            await self.effects.cursors.refresh(session_id, self.sessions.require(session_id))
            return 0
        self._idle_private_revisions.pop(session_id, None)
        result = await before.drain(self, session_id, root_id)
        # Never absorb a message/owner/recovery change during a suspended read,
        # or skip queued independent work after a completed native turn.
        if result == 0:
            if before == await self._observe_private_revision(session_id, root_id):
                self._idle_private_revisions[session_id] = before
                self._repeat_private_observations.discard(session_id)
            else:
                self._repeat_private_observations.add(session_id)
        else:
            self._repeat_private_observations.discard(session_id)
        return result

    async def drain_owned_inbox(self, session_id: str) -> int:
        # The revision read below checks this root against the bus marker and
        # the checkpoint, so no separate exclusive bus read is needed here.
        private_root = self.effects._configured_root_id()
        pushed = await self._drain_private_if_changed(session_id, private_root)
        WakeScheduleCheck(session_id=session_id, inputs=self).schedule()
        return pushed

    @property
    def background_wakes_disabled(self) -> bool:
        return not self.auto_wake or not self.sessions.runtime_enabled

    async def drain_count(self, session_id: str) -> int:
        return await self.drain_inbox(session_id)

    async def accept_followup(
        self,
        session_id: str,
        *,
        text: str,
        display_text: str,
        request: PromptRequest,
        images: tuple[Any, ...],
    ) -> PromptResponse:
        inbox = self.backend_inboxes[session_id]
        controller = self.runtime.controller.get()
        if controller is UNBOUND_CONTROLLER:
            controller = self.sessions.client
        followup = UserFollowupSegment.capture(text.removeprefix(AGENT_PREFIX))
        async with QueuedInput.reserve(
            self, session_id, self.sessions.require(session_id), text=display_text,
            prompt=TurnContext.render_segments((followup,), images=images).text,
            echo=request.defer_display, images=images, controller=controller,
            input_id=request.input_id, origin=request.origin,
        ) as (item, owner, source, custody):
            item = request.accepted(item, source, owner)
            followup = replace(followup, provenance=(*followup.provenance, source.context_provenance()))
            rendered = TurnContext.render_segments((followup,), images=images)
            item.place(self, session_id, inbox, InputContent(
                text=item.prompt, images=tuple(images), contributions=rendered.contributions,
            ))
            request.enqueue_control(inbox, item.input_id)
            custody.pop_all()
        await request.publish_acceptance(self, session_id)
        return PromptResponse(
            stop_reason="end_turn",
            field_meta=encode_updates(InputDeliveryChangedUpdate(item.input_id)),
        )

    def require_inbox(self, session_id: str, inbox: asyncio.Queue[TurnInboxItem]) -> None:
        if self.backend_inboxes.get(session_id) is not inbox:
            raise InputHandoffRefused("Running turn inbox retired before input placement")

    def bind_native_turn(
        self, session_id: str, owner: Thread, admission: int, turn_id: str
    ) -> asyncio.Queue[TurnInboxItem]:
        """Transfer existing live inputs to the new lease, never read them from disk."""
        inbox = self.backend_inboxes.setdefault(session_id, asyncio.Queue())
        self.sessions.config.turn_started(session_id, owner)
        for input_id, item in self.queued_inputs.get(session_id, {}).items():
            self.queued_inputs[session_id][input_id] = item.bind_turn(
                owner, admission, turn_id
            )
        return inbox

    async def send_now(self, session_id: str) -> None:
        async with self.queue_lock:
            inbox = self.backend_inboxes.get(session_id)
            queued = self.queued_inputs.get(session_id, {})
            if inbox is not None and queued:
                for key, item in tuple(queued.items()):
                    queued[key] = item.immediate()
                inbox.put_nowait(SendNow(tuple(InputId(input_id) for input_id in queued)))

    async def close(self) -> None:
        """Retire input producers before their sessions and turns are closed."""
        self.closing = True
        tasks = (*self.drain_tasks.values(), *self.wake_tasks.values())
        for task in tasks:
            task.cancel()
        try:
            await join_retirement(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            self.drain_tasks.clear()
            self.wake_tasks.clear()
            self._observation_watchers.clear()
            self._idle_private_revisions.clear()
            self._repeat_private_observations.clear()

    async def input_started(
        self,
        session_id: str,
        input_id: str | None,
        source_scope: QueueScope,
    ) -> None:
        original = self.original_sources[session_id]
        input_id = input_id if input_id is not None else original.accepted_id
        source = self.following_sources.get(session_id, {}).get(input_id)
        started_keys = original.keys if input_id is None else source.keys if source else ()
        document = await Coordination.run_worker(partial(self.dispositions.read, (*started_keys, *original.notice_keys)))
        for key in started_keys:
            row = document.rows.get(key)
            if row is not None and not row.unresolved:
                await self.emit_input_disposition(session_id, row)
        row = document.lookup(started_keys[0] if len(started_keys) == 1 else None)
        if input_id is None and original.notice_keys:
            row = document.lookup(original.notice_keys[0])
        row = row.require_started(source_scope.admission_generation)
        if input_id is None and original.notice_keys:
            input_id = row.public_id
        item = self.queued_inputs.get(session_id, {}).get(input_id or "")
        text = await item.started(self) if item else original.notice_text if input_id == row.public_id else None
        await self.emit_input_started(
            session_id,
            text,
            input_id,
            source_scope=source_scope,
            native_id=row.native_id,
        )
        self.queued_inputs.get(session_id, {}).pop(input_id or "", None)
        await self.emit_queue_state(session_id)

    async def input_refused(self, session_id: str, input_id: str | None) -> None:
        if input_id is None:
            return
        source = self.following_sources.get(session_id, {}).pop(input_id, None)
        if source is not None:
            # Native write was refused; keep the durable notice, never replay it.
            await self.emit_input_delivery_changed(session_id)
        item = self.queued_inputs.get(session_id, {}).pop(input_id, None)
        if item is not None:
            await item.not_started(self)
            await self.emit_queue_state(session_id)

    def finish_original_inputs(self, keys: tuple[str, ...]) -> InputDocument:
        """The joined retirement owns the original input and its wire cut."""
        with _store_lock(self.comms._wire_lock_path):
            return self.dispositions.settle_unbound(keys)

    async def finish_turn_inputs(
        self, session_id: str, inbox: asyncio.Queue[TurnInboxItem]
    ) -> None:
        """Retire this turn's live capabilities; retain UNKNOWN only as notices."""
        if self.backend_inboxes.get(session_id) is inbox:
            self.backend_inboxes.pop(session_id, None)
            self.sessions.config.settings_in_use.pop(session_id, None)
        while not inbox.empty():
            inbox.get_nowait()
        try:
            original = self.original_sources.pop(session_id, None)
            if original is not None:
                await Coordination.run_worker(partial(self.finish_original_inputs, original.keys))
        finally:
            # Cancellation joins the original input write before retiring loop
            # capabilities. No callback delivery is required to burn a grant or
            # retain accepted queued input for the next distinct turn.
            self.following_sources.pop(session_id, None)
            remaining = self.queued_inputs.pop(session_id, {})
            for item in remaining.values():
                await item.not_started(self)
            if remaining:
                self.restored_inputs.setdefault(session_id, {}).update(
                    {
                        key: restored
                        for key, item in remaining.items()
                        if (restored := item.restore_after_turn()) is not None
                    }
                )
        await self.emit_input_delivery_changed(session_id)
        if remaining:
            await self.emit_queue_state(session_id)
