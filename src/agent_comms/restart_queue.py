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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated


from .child_process import OutlivesLauncherLifetime, Platform, ProcessIdentity, SpawnedProcess
from .comms import Comms, wire
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .lifecycle_capability import Active, LifecycleCapability, Pending
from .errors import RelationViolationError
from .field_codec import FieldCodec, WireOmitDefault
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .native_arguments import GivenBinary, NativeArguments
from .owner_cutover import PreserveOwnerRuntime
from .owner_launch import RetainedOwnerLaunch
from .owner_lifecycle import OwnerRestartSelection
from .owner_restart import OwnerRestartRequest, StoppedOwnerFailure
from .private_path import PrivateDirectoryRole
from .restart_refusals import RestartRefusal
from .restart_scope import OwnersNamed
from .store_files import _atomic_write_text, _store_lock
from .thread_identity import RecordedThreadIncarnation
from .watched import ChangeSignal, DirectorySource, FileSource

if TYPE_CHECKING:
    from .threads import Thread

DIRECTORY = "owner-restart-queue"


@dataclass(frozen=True, kw_only=True)
class RestartState(LifecycleCapability, DeclaredFamily, affix="Restart"):
    reason: Annotated[str, WireOmitDefault()] = ""

    def enqueue(self, previous: QueuedRestart, selection: OwnerRestartSelection,
                target: RestartTarget) -> QueuedRestart:
        # A terminal write interrupted before its move is still settled.
        return previous

    def restart(self, comms: Comms, record: QueuedRestart, owner: Thread) -> Iterator[QueuedRestart]:
        return iter(())


class PendingRestart(Pending, Active, RestartState):

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
    def restart(self, comms: Comms, record: QueuedRestart, owner: Thread) -> Iterator[QueuedRestart]:
        try:
            request = OwnerRestartRequest(
                OwnersNamed((owner.name,)),
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

    @property
    def is_current_watcher(self) -> bool:
        return self.launch_runtime == LaunchRuntime.running()

    def require_watcher(self, comms: Comms) -> None:
        self.require_available(comms)
        current = RestartEnvironment.inherit(
            comms.owners.restart_environment(RestartEnvironment.inherit(os.environ).encode(), self.launch_runtime)
        )
        if not self.is_current_watcher or self.runtime != current:
            raise ValueError("Watcher target runtime changed")


@dataclass(frozen=True)
class QueuedRestart:
    id: str
    selection: OwnerRestartSelection
    target: RestartTarget
    state: RestartState

    @property
    def name(self) -> str:
        return self.selection.name

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
                    'Restart queue uses a retired layout; existing records require explicit conversion'
                )
            yield self

    def active_path(self, owner: RecordedThreadIncarnation) -> Path:
        return self.active_directory / f'{owner.lookup}.json'

    def settled_path(self, record: QueuedRestart) -> Path:
        owner = record.selection.identity.incarnation.require_recorded()
        directory = self.directory / 'settled' / owner.lookup
        directory.mkdir(mode=0o700, exist_ok=True)
        PrivateDirectoryRole.require(directory.lstat())
        return directory / f'{uuid.UUID(record.id).hex}.json'

    def read(self, path: Path) -> QueuedRestart:
        if path.is_symlink():
            raise ValueError('Restart queue record must not be a symlink')
        return FieldCodec.decode(QueuedRestart, json.loads(path.read_text(encoding='utf-8')))

    def path_for(self, record: QueuedRestart) -> Path:
        for current in record.state.items_with(Active, record):
            return self.active_path(current.selection.identity.incarnation.require_recorded())
        return self.settled_path(record)

    def move(self, path: Path, record: QueuedRestart) -> None:
        destination = self.path_for(record)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        os.rename(path, destination)
        for parent in (path.parent, destination.parent):
            Platform.current().sync_directory(parent)

    def save(self, record: QueuedRestart) -> None:
        path = self.active_path(record.selection.identity.incarnation.require_recorded())
        _atomic_write_text(path, json.dumps(FieldCodec.encode(record), sort_keys=True))
        if not record.state.items_with(Active, record):
            self.move(path, record)

    def _active_record(self, path: Path) -> Iterator[QueuedRestart]:
        record = self.read(path)
        if path != self.active_path(record.selection.identity.incarnation.require_recorded()):
            raise ValueError('Restart queue record has another owner key')
        if record.state.items_with(Active, record):
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


def enqueue(comms: Comms, selection: OwnerRestartSelection, *,
            launch_runtime: LaunchRuntime) -> QueuedRestart:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    queue = RestartQueue(comms.root / DIRECTORY)
    snapshot = comms.registry.snapshot()
    owner = selection.require_current(snapshot)
    status = snapshot.status(owner.name)
    owner.require_restart_owner(status)
    launch = RetainedOwnerLaunch.capture(selection, snapshot)
    target = RestartTarget.capture(comms, launch.environment, launch_runtime)
    target.require_available(comms)
    # Queueing itself never signals or interrupts the owner, even if it is busy.
    with queue.locked():
        for previous in queue.active_for(owner.incarnation.require_recorded()):
            result = previous.state.enqueue(previous, selection, target)
            if result.state.items_with(Active, result):
                break
            queue.save(result)
        else:
            result = QueuedRestart(
                uuid.uuid4().hex, selection, target, PendingRestart()
            )
            queue.save(result)
    # A new watcher can safely race an existing watcher: flock permits one runner.
    _start_watcher(comms.root, target)
    return result


def _start_watcher(root: Path, target: RestartTarget) -> SpawnedProcess:
    # The resident watcher inherits only its declared runtime configuration.
    return SpawnedProcess.launch(
        (str(target.launch_runtime.interpreter), "-m", "agent_comms.restart_queue", str(root.resolve())),
        env=target.runtime.encode(),
        runtime=target.launch_runtime,
        lifetime=OutlivesLauncherLifetime,
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
            for pending in record.state.items_with(Pending, record):
                cancelled = pending.transition(CancelledRestart())
                queue.save(cancelled)
                changed.append(cancelled)
    return changed


def step(comms: Comms) -> None:
    queue = RestartQueue(comms.root / DIRECTORY)
    with queue.locked():
        initial_records = tuple(queue.active_records())
    for initial in initial_records:
        if not initial.state.items_with(Pending, initial) or not initial.target.is_current_watcher:
            continue
        with queue.locked():
            current = tuple(queue.active_for(initial.selection.identity.incarnation.require_recorded()))
            if current != (initial,):
                continue
            record = initial
            snapshot = comms.registry.snapshot()
            try:
                owner = record.selection.require_current(snapshot)
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
                        record.target.require_watcher(comms)
                    except ValueError as error:
                        record = record.transition(BlockedRestart(reason=str(error)))
                    else:
                        # Persist before any side effect; crashes cannot authorize replay.
                        record = record.transition(AttemptingRestart())
            queue.save(record)
        for result in record.state.restart(comms, record, owner):
            with queue.locked():
                queue.save(result)


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
        def release_if_empty():
            # Enqueue and release share this lock, so no request is orphaned.
            with queue.locked():
                pending = tuple(pending for record in queue.active_records()
                                for pending in record.state.items_with(Pending, record))
                if any(record.target.is_current_watcher for record in pending):
                    return False
                fcntl.flock(lock, fcntl.LOCK_UN)
                if pending:
                    _start_watcher(comms.root, pending[0].target)
                return True

        sources = tuple(FileSource(path) for path in comms.bus.bus.source_paths(comms.root))
        async with ChangeSignal.opened(DirectorySource(queue.active_directory), *sources) as changes:
            while True:
                # Admit the committed source before checking the queued owners.
                changes.prepare()
                await Coordination.run_worker(lambda: step(comms))
                if await Coordination.run_worker(release_if_empty):
                    return
                await changes.wait()
    finally:
        os.close(lock)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agent_comms.restart_queue ROOT")
    comms = wire(sys.argv[1])
    try:
        asyncio.run(run(comms))
    except (OSError, ValueError, CoordinationError) as exc:
        queue = RestartQueue(comms.root / DIRECTORY)
        with queue.locked():
            for record in queue.active_records():
                if record.state.items_with(Pending, record) and record.target.is_current_watcher:
                    queue.save(record.transition(
                        BlockedRestart(reason=f"Watcher failed: {type(exc).__name__}: {exc}")
                    ))
        raise


if __name__ == "__main__":
    main()
