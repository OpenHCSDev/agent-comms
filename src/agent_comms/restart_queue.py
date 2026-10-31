"""Durable, incarnation-guarded, event-driven idle owner restarts (Linux only).

A queue request is bound to one exact owner incarnation. The watcher never
retries an attempted restart: a crash or ambiguous exception requires operator
review. No credentials or owner environment are written to the queue.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self, TextIO

from agent_comms_hooks.process_claim import (
    ClaimHeldError,
    ProcessSingleton,
)


from .cli_result import CliResult, ResultShower
from .child_process import OutlivesLauncherLifetime, Platform, ProcessIdentity, SpawnedProcess
from .comms import Comms, wire
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, RecordMember, RestartId, WireOmitDefault
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .lifecycle_capability import Active, LifecycleCapability, Pending
from .locked_store import RuntimeFile
from .maintenance_barrier import MaintenanceBarrier, MaintenanceHeldError, MaintenanceHolder
from .native_arguments import GivenBinary, NativeArguments
from .owner_cutover import PreserveOwnerRuntime
from .owner_launch import RetainedOwnerLaunch
from .owner_lifecycle import OwnerRestartSelection
from .owner_restart import OwnerRestartRequest, StoppedOwnerFailure
from .private_path import PrivateDirectoryRole
from .restart_refusals import RestartRefusal
from .restart_scope import OwnersNamed
from .thread_identity import OwnerIdentity, RecordedThreadIncarnation, ThreadIncarnationRecord
from .typed_table import Column, DurableStore, FrozenColumn, Generated, Index, StoreRows, TypedTable, sql_literal
from .tool_result import ToolResult
from .watched import ChangeSignal, FileSource

DIRECTORY = "owner-restart-queue"


@dataclass(frozen=True, kw_only=True)
class RestartState(LifecycleCapability, DeclaredFamily, affix="Restart"):
    reason: Annotated[str, WireOmitDefault()] = ""

    @classmethod
    def membership_sql(cls, capability: type[LifecycleCapability]) -> str:
        names = ", ".join(sql_literal(member) for member in cls.members_with(capability))
        return f"json_extract({{state}}, '$.{cls.family_discriminator}') IN ({names})"

    def enqueue(self, previous: QueuedRestart, selection: OwnerRestartSelection,
                target: RestartTarget) -> QueuedRestart:
        return previous

    def restart(self, comms: Comms, record: QueuedRestart) -> Iterator[QueuedRestart]:
        return iter(())

    def settle(self, record: QueuedRestart) -> QueuedRestart:
        """Retain attempted and terminal answers without inventing a retry."""
        return record


class PendingRestart(Pending, Active, RestartState):

    def settle(self, record: QueuedRestart) -> QueuedRestart:
        if not Platform.current().matches(record.selection.process):
            return record.transition(StaleRestart(reason="Owner process exited"))
        return record

    def enqueue(self, previous: QueuedRestart, selection: OwnerRestartSelection,
                target: RestartTarget) -> QueuedRestart:
        if previous.selection == selection and previous.target == target:
            return previous
        return previous.transition(StaleRestart(
            reason=f"Superseded pending target {previous.target.interpreter} by {target.interpreter}"
        ))


class RestartAlreadyAttemptingError(RelationViolationError):
    """An attempted restart cannot be replaced or replayed by enqueue."""


class AttemptingRestart(Active, RestartState):
    def restart(self, comms: Comms, record: QueuedRestart) -> Iterator[QueuedRestart]:
        try:
            request = OwnerRestartRequest(
                OwnersNamed((record.name,)),
                expected=record.selection,
                runtime=record.target.runtime,
                agent_bin=GivenBinary(record.target.agent_bin),
                launch_runtime=record.target.launch_runtime,
            )
            (acknowledgement,) = PreserveOwnerRuntime().restart(comms.owners, request)
        except RestartRefusal as refusal:
            yield record.transition(refusal.queue_state())
        except StoppedOwnerFailure as exc:
            yield record.transition(UncertainRestart(reason=str(exc)))
        except (OSError, ValueError) as exc:
            yield record.transition(UncertainRestart(reason=f"{type(exc).__name__}: {exc}"))
        else:
            yield record.transition(RestartedRestart(
                previous=record.selection.process,
                current=ProcessIdentity.capture(acknowledgement.pid),
            ))

    def enqueue(self, previous: QueuedRestart, selection: OwnerRestartSelection,
                target: RestartTarget) -> QueuedRestart:
        raise RestartAlreadyAttemptingError(
            f"Restart {previous.id} for {previous.name} is already attempting"
        )


class CancelledRestart(RestartState):
    pass


class StaleRestart(RestartState):
    pass


class BlockedRestart(RestartState):
    pass


class UncertainRestart(RestartState):
    pass


@dataclass(frozen=True, kw_only=True)
class RestartedRestart(RestartState):
    previous: ProcessIdentity
    current: ProcessIdentity


@dataclass(frozen=True)
class RestartTarget:
    interpreter: str
    runtime: RestartEnvironment
    agent_bin: str

    @classmethod
    def capture(cls, comms: Comms, source: Mapping[str, str], selected: LaunchRuntime):
        runtime = RestartEnvironment.inherit(
            comms.owners.restart_environment(RestartEnvironment.inherit(os.environ).encode(), selected)
        )
        return cls(
            str(selected.interpreter),
            runtime,
            comms.owners.restart_entrypoint(source[NativeArguments.binary_key], selected),
        )

    @property
    def launch_runtime(self) -> LaunchRuntime:
        return LaunchRuntime(Path(self.interpreter))

    def require_available(self, comms: Comms) -> None:
        self.launch_runtime.require_available()
        if self.agent_bin != comms.owners.restart_entrypoint(self.agent_bin, self.launch_runtime):
            raise ValueError("Target agent entrypoint differs from reviewed launch authority")
        if Path(self.agent_bin).is_absolute() and not os.access(self.agent_bin, os.X_OK):
            raise ValueError("Target agent entrypoint is unavailable")


class RestartRows(StoreRows):
    """The restart store discovers its rows through this membership declaration."""


@dataclass(frozen=True)
class QueuedRestart(RecordMember, RestartRows, TypedTable):
    id: Annotated[RestartId, FrozenColumn(primary_key=True)]
    selection: Annotated[OwnerRestartSelection, FrozenColumn()]
    target: Annotated[RestartTarget, FrozenColumn()]
    state: RestartState

    owner_birth: ClassVar[Generated] = Generated(float, Column(
        generated="json_extract(selection, '$."
        + FieldCodec.record_key(OwnerRestartSelection, "identity") + "."
        + FieldCodec.record_key(OwnerIdentity, "incarnation") + "."
        + FieldCodec.record_key(ThreadIncarnationRecord, "created_at") + "')",
        check="owner_birth > 0"))
    active: ClassVar[Generated] = Generated(bool, Column(generated=RestartState.membership_sql(Active)))
    pending: ClassVar[Generated] = Generated(bool, Column(generated=RestartState.membership_sql(Pending)))
    indexes = (
        Index(("owner_birth",), unique=True, where="active = 1"),
        Index(("active", "owner_birth", "id")),
        Index(("pending", "id")),
    )

    @classmethod
    def decodes_tagged_records(cls) -> bool:
        """The public restart record keeps its original plain-field wire shape."""
        return False

    @property
    def name(self) -> str:
        return self.selection.name

    @property
    def owner(self) -> RecordedThreadIncarnation:
        return self.selection.identity.incarnation.require_recorded()

    def transition(self, state: RestartState) -> Self:
        return replace(self, state=state)


@dataclass(frozen=True)
class RestartQueued(CliResult, ToolResult):
    restart: QueuedRestart

    def show(self, shower: ResultShower, label: str, title: str) -> None:
        shower.show_done(f"{label}: runs when {self.restart.name} is idle", title)


class RestartQueueWatcher(ProcessSingleton, RuntimeFile):
    """The queue reader's process claim is runtime state, never carried content."""

    filename: ClassVar[str] = f"{DIRECTORY}/watcher.lock"


@dataclass(frozen=True, slots=True)
class RestartQueue(DurableStore):
    """One atomic row transition; active membership is computed from its state.

    The unique active-owner index excludes two intents for one incarnation.
    An Attempting row commits before the caller may perform the restart; no
    queue scan ever selects that row for another attempt.
    """

    root: Path
    filename: ClassVar[str] = f"{DIRECTORY}/queue.sqlite3"
    tables = RestartRows

    def __post_init__(self) -> None:
        PrivateDirectoryRole.make_parents(self.directory)
        PrivateDirectoryRole.require(
            self.directory.lstat(), RelationViolationError("Restart queue directory must be private"))

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def watcher_claim(self) -> RestartQueueWatcher:
        return RestartQueueWatcher(RestartQueueWatcher.path_in(self.root))

    @staticmethod
    def _active(db: sqlite3.Connection, **key: object) -> list[QueuedRestart]:
        return QueuedRestart.select(db, active=True, order_by=("id",), **key)

    def _settle(self, db: sqlite3.Connection) -> None:
        """Dead pending owners settle atomically; attempted rows remain untouched."""
        for record in QueuedRestart.select(db, pending=True):
            settled = record.state.settle(record)
            if settled != record:
                settled.upsert(db)

    def history(self, owner: RecordedThreadIncarnation) -> list[QueuedRestart]:
        with self.snapshot() as db:
            return [
                *self._active(db, owner_birth=owner.created_at),
                *QueuedRestart.select(db, active=False, owner_birth=owner.created_at,
                                      order_by=("id",)),
            ]

    def enqueue(self, comms: Comms, selection: OwnerRestartSelection, *,
                launch_runtime: LaunchRuntime) -> QueuedRestart:
        Platform.current().require_watched_procfs(
            ValueError("Queued restarts require Linux inotify and /proc"))
        snapshot = comms.registry.snapshot()
        owner = selection.require_current(snapshot)
        owner.require_restart_owner(snapshot.status(owner.name))
        launch = RetainedOwnerLaunch.capture(selection, snapshot, comms.root)
        target = RestartTarget.capture(comms, launch.environment, launch_runtime)
        target.require_available(comms)
        # Queueing never signals or interrupts the owner, even when it is busy.
        with self.writing() as db:
            self._settle(db)
            for previous in self._active(db, owner_birth=owner.incarnation.require_recorded().created_at):
                result = previous.state.enqueue(previous, selection, target)
                if result.state.items_with(Active, result):
                    break
                result.upsert(db)
            else:
                result = QueuedRestart(RestartId.mint(), selection, target, PendingRestart())
                result.insert(db)
        self.start_watcher()
        return result

    def cancel(self, owner: RecordedThreadIncarnation) -> list[QueuedRestart]:
        """Cancel pending requests; an attempted restart cannot be undone."""
        changed = []
        with self.writing() as db:
            self._settle(db)
            for record in self._active(db, owner_birth=owner.created_at):
                for pending in record.state.items_with(Pending, record):
                    cancelled = pending.transition(CancelledRestart())
                    cancelled.upsert(db)
                    changed.append(cancelled)
        return changed

    def _ready(self) -> list[QueuedRestart]:
        with self.writing() as db:
            self._settle(db)
            return QueuedRestart.select(db, pending=True, order_by=("id",))

    def _attempt(self, comms: Comms, initial: QueuedRestart) -> tuple[QueuedRestart, ...]:
        with self.writing() as db:
            if self._active(db, owner_birth=initial.owner.created_at) != [initial]:
                return ()
            record = initial
            snapshot = comms.registry.snapshot()
            try:
                owner = record.selection.require_current(snapshot)
                comms.owners.require_restartable(owner)
            except RestartRefusal as refusal:
                record = record.transition(refusal.queue_state())
            except RelationViolationError:
                return ()  # Busy owners retain their pending request.
            else:
                try:
                    record.target.require_available(comms)
                except ValueError as error:
                    record = record.transition(BlockedRestart(reason=str(error)))
                else:
                    record = record.transition(AttemptingRestart())
            record.upsert(db)
        return (record,)

    def _complete(self, attempted: QueuedRestart, result: QueuedRestart) -> None:
        with self.writing() as db:
            if QueuedRestart.require(db, id=attempted.id) != attempted:
                raise RelationViolationError("Restart attempt changed before its result committed")
            result.upsert(db)

    def step(self, comms: Comms) -> None:
        for initial in self._ready():
            for attempt in self._attempt(comms, initial):
                # The transaction has committed Attempting before any side effect.
                for result in attempt.state.restart(comms, attempt):
                    self._complete(attempt, result)

    def start_watcher(self) -> SpawnedProcess:
        """The queue's reader runs current code; each target keeps its own launch runtime."""
        runtime = LaunchRuntime.running()
        return SpawnedProcess.launch(
            (str(runtime.interpreter), "-m", "agent_comms.restart_queue", str(self.root.resolve())),
            env=MaintenanceHolder.outside(RestartEnvironment.inherit(os.environ).encode()), runtime=runtime,
            lifetime=OutlivesLauncherLifetime,
        )

    def release_if_empty(self, held: TextIO) -> bool:
        # Enqueue and release share SQLite's writer transaction, so no request is orphaned.
        with self.writing() as db:
            if QueuedRestart.exists(db, pending=True):
                return False
            self.watcher_claim.release(held)
            return True

    def block_failed(self, error: Exception) -> None:
        with self.writing() as db:
            for record in QueuedRestart.select(db, pending=True):
                record.transition(BlockedRestart(
                    reason=f"Watcher failed: {type(error).__name__}: {error}"
                )).upsert(db)

    async def run(self, comms: Comms) -> None:
        Platform.current().require_watched_procfs(
            ValueError("Queued restarts require Linux inotify and /proc"))
        with ExitStack() as claim:
            try:
                held = claim.enter_context(self.watcher_claim.claimed())
            except ClaimHeldError:
                return
            barrier = MaintenanceBarrier(comms.registry.store.path)
            sources = tuple(FileSource(path) for path in (
                *type(self).source_paths(self.root),
                *comms.bus.bus.source_paths(comms.root),
                *barrier.witness_paths,
            ))
            async with ChangeSignal.opened(*sources) as changes:
                while True:
                    changes.prepare()
                    try:
                        barrier.read().require_open()
                        await Coordination.run_worker(lambda: self.step(comms))
                        if await Coordination.run_worker(lambda: self.release_if_empty(held)):
                            return
                    except MaintenanceHeldError:
                        pass  # The same request resumes after the maintenance witness changes.
                    await changes.wait()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agent_comms.restart_queue ROOT")
    comms = wire(sys.argv[1])
    queue = RestartQueue(comms.root)
    try:
        asyncio.run(queue.run(comms))
    except (OSError, ValueError, CoordinationError) as exc:
        queue.block_failed(exc)
        raise


if __name__ == "__main__":
    main()
