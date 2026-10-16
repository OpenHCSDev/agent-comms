"""Durable, incarnation-guarded, event-driven idle owner restarts (Linux only).

A queue request is bound to one exact owner incarnation. The watcher never
retries an attempted restart: a crash or ambiguous exception requires operator
review. No credentials or owner environment are written to the queue.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from .active_route import resolve_comms_route
from .child_process import ParentedProcess, ProcessIdentity
from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .owner_cutover import PreserveOwnerRuntime
from .owner_launch import OwnerRestartSelection, RetainedOwnerLaunch
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .owner_restart import OwnerRestartRequest, RestartTarget, StoppedOwnerFailure
from .restart_refusals import RestartRefusal
from .private_path import PrivateDirectoryRole
from .thread_identity import RecordedThreadIncarnation
from .store_files import _atomic_write_text, _store_lock
from .wire_watch import DirectoryWatchSource, WatchSource, WireWatch

DIRECTORY = "owner-restart-queue"


@dataclass(frozen=True, kw_only=True)
class RestartState(DeclaredFamily, affix="Restart"):
    reason: str = field(default="", metadata={"wire_omit_default": True})
    pending: ClassVar[bool] = False
    attempting: ClassVar[bool] = False
    active: ClassVar[bool] = False

    def enqueue(self, previous: QueuedRestart, candidate: QueuedRestart) -> QueuedRestart:
        # A terminal write interrupted before its move is still settled.
        return previous


class PendingRestart(RestartState):
    pending = active = True

    def enqueue(self, previous: QueuedRestart, candidate: QueuedRestart) -> QueuedRestart:
        if (previous.target == candidate.target
                and previous.launch_runtime == candidate.launch_runtime
                and previous.runtime == candidate.runtime):
            return previous
        return previous.transition(StaleRestart(
            reason=f"Superseded pending target {previous.launch_runtime.interpreter} "
                   f"by {candidate.launch_runtime.interpreter}"
        ))


class RestartAlreadyAttemptingError(RelationViolationError):
    """An attempted restart cannot be replaced or replayed by enqueue."""


class AttemptingRestart(RestartState):
    attempting = active = True

    def enqueue(self, previous: QueuedRestart, candidate: QueuedRestart) -> QueuedRestart:
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
class QueuedRestart:
    id: str
    target: RestartTarget
    source_interpreter: str
    launch_runtime: LaunchRuntime
    runtime: RestartEnvironment
<<<<<<< HEAD
    state: RestartState
=======
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
>>>>>>> origin/main

    @property
    def name(self) -> str:
        return self.target.selection.name

    @property
    def is_current_watcher(self) -> bool:
        return self.launch_runtime == LaunchRuntime.running()

    def require_watcher(self, comms: Comms) -> None:
        self.launch_runtime.require_available()
        current = RestartEnvironment.inherit(comms.owners.restart_environment(
            RestartEnvironment.inherit(os.environ).encode(), self.launch_runtime))
        if not self.is_current_watcher or self.runtime != current:
            raise ValueError("Watcher target runtime changed")

    def transition(self, state: RestartState):
        return replace(self, state=state)


class RestartQueueLayoutError(RelationViolationError):
    """The one-use queue layout migration must precede new queue writers."""


@dataclass(frozen=True)
class RestartQueue:
    """One active record per recorded owner; settled records never enter that set."""

    directory: Path

    def __post_init__(self) -> None:
        for path in (self.directory, self.active_directory, self.directory / 'settled'):
            path.mkdir(mode=0o700, exist_ok=True)
            PrivateDirectoryRole.require(path.lstat())

    @property
    def active_directory(self) -> Path:
        return self.directory / 'active'

    @property
    def lock_path(self) -> Path:
        return self.directory / 'queue'

    @contextmanager
    def locked(self):
        with _store_lock(self.lock_path):
            if next(self.directory.glob('*.json'), None) is not None:
                raise RestartQueueLayoutError(
                    'Move the old restart queue with tools/cutover/move_restart_queue.py first'
                )
            yield self

    def active_path(self, owner: RecordedThreadIncarnation) -> Path:
        return self.active_directory / f'{owner.lookup}.json'

    def settled_path(self, record: QueuedRestart) -> Path:
        owner = record.target.selection.identity.incarnation.require_recorded()
        directory = self.directory / 'settled' / owner.lookup
        directory.mkdir(mode=0o700, exist_ok=True)
        PrivateDirectoryRole.require(directory.lstat())
        return directory / f'{uuid.UUID(record.id).hex}.json'

    def read(self, path: Path) -> QueuedRestart:
        if path.is_symlink():
            raise ValueError('Restart queue record must not be a symlink')
        return FieldCodec.decode(QueuedRestart, json.loads(path.read_text(encoding='utf-8')))

    def path_for(self, record: QueuedRestart) -> Path:
        return (self.active_path(record.target.selection.identity.incarnation.require_recorded())
                if record.state.active else self.settled_path(record))

    def move(self, path: Path, record: QueuedRestart) -> None:
        destination = self.path_for(record)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        os.rename(path, destination)
        for parent in (path.parent, destination.parent):
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def save(self, record: QueuedRestart) -> None:
        path = self.active_path(record.target.selection.identity.incarnation.require_recorded())
        _atomic_write_text(path, json.dumps(FieldCodec.encode(record), sort_keys=True), fsync_parent=True)
        if not record.state.active:
            self.move(path, record)

    def _active_record(self, path: Path) -> Iterator[QueuedRestart]:
        record = self.read(path)
        if path != self.active_path(record.target.selection.identity.incarnation.require_recorded()):
            raise ValueError('Restart queue record has another owner key')
        if record.state.active:
            yield record
        else:
            self.move(path, record)

    def active_for(self, owner: RecordedThreadIncarnation) -> Iterator[QueuedRestart]:
        path = self.active_path(owner)
        if path.exists() or path.is_symlink():
            yield from self._active_record(path)

    def active_records(self) -> Iterator[QueuedRestart]:
        for path in sorted(self.active_directory.glob('*.json')):
            yield from self._active_record(path)

    def history(self, owner: RecordedThreadIncarnation) -> Iterator[QueuedRestart]:
        yield from self.active_for(owner)
        for path in sorted((self.directory / 'settled' / owner.lookup).glob('*.json')):
            yield self.read(path)


def enqueue(comms: Comms, name: str, *, launch_runtime: LaunchRuntime) -> QueuedRestart:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    queue = RestartQueue(comms.root / DIRECTORY)
    with _store_lock(comms._wire_lock_path):
        snapshot = comms.registry.snapshot()
        owner = comms.registry.require(name)
        status = snapshot.statuses[owner.name]
        owner.require_restart_owner(status)
        selection = OwnerRestartSelection.capture(snapshot, owner.name)
        launch = RetainedOwnerLaunch.capture(owner, snapshot)
<<<<<<< HEAD
        selected = comms.runtime
        selected.require_available()
        runtime = RestartEnvironment.inherit(comms.owners.restart_environment(
            RestartEnvironment.inherit(os.environ).encode(), selected))
        candidate = QueuedRestart(uuid.uuid4().hex, RestartTarget(selection, launch.native),
                                  launch.interpreter, selected, runtime, PendingRestart())
=======
        target = RestartTarget.capture(comms, launch.environment, launch_runtime)
        target.require_available(comms)
>>>>>>> origin/main
        # Queueing itself never signals or interrupts the owner, even if it is busy.
        with queue.locked():
            for previous in queue.active_for(owner.incarnation.require_recorded()):
                result = previous.state.enqueue(previous, candidate)
                if result.state.active:
                    break
                queue.save(result)
            else:
                result = candidate
                queue.save(result)
    # A new watcher can safely race an existing watcher: flock permits one runner.
    _start_watcher(comms.root, result)
    return result


def _start_watcher(root: Path, record: QueuedRestart) -> ParentedProcess:
    # The resident watcher inherits only its declared runtime configuration.
    return ParentedProcess.launch(
        record.launch_runtime.command("-m", "agent_comms.restart_queue", str(root.resolve())),
        env=record.runtime.encode(),
    )


def status(comms: Comms, name: str) -> list[QueuedRestart]:
    owner = comms.registry.require(name).incarnation.require_recorded()
    queue = RestartQueue(comms.root / DIRECTORY)
    with queue.locked():
        return list(queue.history(owner))


def cancel(comms: Comms, name: str) -> list[QueuedRestart]:
    """Cancel only requests not yet attempted; never claim a signal was undone."""
    owner = comms.registry.require(name).incarnation.require_recorded()
    queue = RestartQueue(comms.root / DIRECTORY)
    changed = []
    with queue.locked():
        for record in queue.active_for(owner):
            if record.state.pending:
                cancelled = record.transition(CancelledRestart())
                queue.save(cancelled)
                changed.append(cancelled)
    return changed


def step(comms: Comms) -> None:
    queue = RestartQueue(comms.root / DIRECTORY)
    with queue.locked():
        initial_records = tuple(queue.active_records())
    for initial in initial_records:
        if not initial.state.pending or not initial.is_current_watcher:
            continue
        with queue.locked():
            current = tuple(queue.active_for(initial.target.selection.identity.incarnation.require_recorded()))
            if current != (initial,):
                continue
            record = initial
            snapshot = comms.registry.snapshot()
            try:
                owner = record.target.selection.require_current(snapshot)
                comms.owners.require_restart_idle(owner)
            except RestartRefusal as refusal:
                record = record.transition(refusal.queue_state())
            except RelationViolationError:
                # Idle is checked before persisting an attempt or signalling.
                continue
            else:
                if not owner.process_alive:
                    record = record.transition(StaleRestart(reason="Owner process exited"))
                else:
                    try:
                        record.require_watcher(comms)
                    except ValueError as error:
                        record = record.transition(BlockedRestart(reason=str(error)))
                    else:
                        # Persist before any side effect; crashes cannot authorize replay.
                        record = record.transition(AttemptingRestart())
            queue.save(record)
        if not record.state.attempting:
            continue
        try:
            request = OwnerRestartRequest(
                (record.target,), record.launch_runtime, record.runtime, record.source_interpreter,
            )
            (receipt,) = PreserveOwnerRuntime().restart(comms.owners, request)
        except RestartRefusal as refusal:
            record = record.transition(refusal.queue_state())
        except StoppedOwnerFailure as exc:
            exc.abandon()
            record = record.transition(UncertainRestart(reason=f'{exc}: {exc.__cause__}'))
        except Exception as exc:
            record = record.transition(UncertainRestart(reason=f"{type(exc).__name__}: {exc}"))
        else:
            record = record.transition(
                RestartedRestart(
                    previous=record.target.selection.process,
                    current=ProcessIdentity.capture(receipt.pid),
                )
            )
        with queue.locked():
            queue.save(record)


async def run(comms: Comms) -> None:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    queue = RestartQueue(comms.root / DIRECTORY)
    lock = os.open(queue.directory / "watcher.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        async for _ in WireWatch.observations(
            WatchSource.file(comms.registry.store.path),
            WatchSource.file(comms.registry.store.guard_path),
            WatchSource.file(comms.root / "coordination.sqlite3"),
            WatchSource.file(comms.root / "coordination.sqlite3-wal"),
            DirectoryWatchSource(queue.active_directory),
        ):
            await Coordination.run_worker(lambda: step(comms))

            def release_if_empty():
                with queue.locked():
                    pending = tuple(record for record in queue.active_records() if record.state.pending)
                    if any(record.is_current_watcher for record in pending):
                        return False
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    if pending:
                        _start_watcher(comms.root, pending[0])
                    return True

            if await Coordination.run_worker(release_if_empty):
                return
    finally:
        os.close(lock)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agent_comms.restart_queue ROOT")
    comms = Comms(resolve_comms_route(Path(sys.argv[1])), runtime=LaunchRuntime.running())
    try:
        asyncio.run(run(comms))
    except Exception as exc:
        queue = RestartQueue(comms.root / DIRECTORY)
        with queue.locked():
            for record in queue.active_records():
                if record.state.pending and record.is_current_watcher:
                    queue.save(record.transition(
                        BlockedRestart(reason=f"Watcher failed: {type(exc).__name__}: {exc}")
                    ))
        raise


if __name__ == "__main__":
    main()
