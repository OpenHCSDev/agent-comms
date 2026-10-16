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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from .child_process import ParentedProcess, ProcessIdentity
from .comms import Comms, wire
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .native_arguments import NativeArguments
from .native_settings import NativeThreadDefaults
from .owner_cutover import PreserveOwnerRuntime
from .owner_launch import RetainedOwnerLaunch
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .owner_lifecycle import OwnerRestartSelection
from .owner_restart import OwnerRestartRequest, StoppedOwnerFailure
from .restart_refusals import RestartRefusal
from .store_files import _store_lock
from .wire_watch import DirectoryWatchSource, WatchSource, WireWatch

DIRECTORY = "owner-restart-queue"


@dataclass(frozen=True, kw_only=True)
class RestartState(DeclaredFamily, affix="Restart"):
    reason: str = field(default="", metadata={"wire_omit_default": True})
    pending: ClassVar[bool] = False
    attempting: ClassVar[bool] = False
    active: ClassVar[bool] = False


class PendingRestart(RestartState):
    pending = active = True


class AttemptingRestart(RestartState):
    attempting = active = True


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
    def capture(cls, comms: Comms, source: NativeThreadDefaults):
        selected = LaunchRuntime.current()
        runtime = RestartEnvironment.inherit(
            comms.owners.restart_environment(RestartEnvironment.inherit(os.environ).encode(), selected)
        )
        return cls(
            str(selected.interpreter),
            runtime,
            comms.owners.restart_entrypoint(source.binary, selected),
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

    def require_watcher(self, comms: Comms) -> None:
        self.require_available(comms)
        current = RestartEnvironment.inherit(
            comms.owners.restart_environment(RestartEnvironment.inherit(os.environ).encode(), self.launch_runtime)
        )
        if self.launch_runtime != LaunchRuntime.running() or self.runtime != current:
            raise ValueError("Watcher target runtime changed")


@dataclass(frozen=True)
class QueuedRestart:
    id: str
    selection: OwnerRestartSelection
    source_interpreter: str
    target: RestartTarget
    state: RestartState

    @property
    def name(self) -> str:
        return self.selection.name

    def transition(self, state: RestartState):
        return replace(self, state=state)


def _directory(comms: Comms) -> Path:
    path = comms.root / DIRECTORY
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Restart queue directory must be private and not a symlink")
    return path


def _save(path: Path, record: QueuedRestart) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(FieldCodec.encode(record), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _records(comms: Comms):
    for path in sorted(_directory(comms).glob("*.json")):
        if path.is_symlink():
            raise ValueError("Restart queue record must not be a symlink")
        yield path, FieldCodec.decode(QueuedRestart, json.loads(path.read_text(encoding="utf-8")))


def enqueue(comms: Comms, name: str) -> QueuedRestart:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    directory = _directory(comms)
    with _store_lock(comms._wire_lock_path):
        snapshot = comms.registry.snapshot()
        owner = comms.registry.require(name)
        status = snapshot.statuses[owner.name]
        owner.require_restart_owner(status)
        selection = OwnerRestartSelection.capture(snapshot, owner.name)
        launch = RetainedOwnerLaunch.capture(owner, snapshot)
        target = RestartTarget.capture(comms, launch.native)
        target.require_available(comms)
        # Queueing itself never signals or interrupts the owner, even if it is busy.
        with _store_lock(directory / "queue"):
            for _, previous in _records(comms):
                if previous.name == owner.name and previous.state.active:
                    if previous.selection != selection or previous.target != target:
                        raise RelationViolationError("Previous owner restart requires review")
                    result = previous
                    break
            else:
                result = QueuedRestart(
                    uuid.uuid4().hex, selection, launch.interpreter, target, PendingRestart()
                )
                _save(directory / f"{result.id}.json", result)
    # A new watcher can safely race an existing watcher: flock permits one runner.
    _start_watcher(comms.root, target)
    return result


def _start_watcher(root: Path, target: RestartTarget) -> ParentedProcess:
    # The resident watcher inherits only its declared runtime configuration.
    return ParentedProcess.launch(
        (str(target.launch_runtime.interpreter), "-m", "agent_comms.restart_queue", str(root.resolve())),
        env=target.runtime.encode(),
    )


def status(comms: Comms, name: str) -> list[QueuedRestart]:
    canonical = comms.registry.require(name).name
    with _store_lock(_directory(comms) / "queue"):
        return [record for _, record in _records(comms) if record.name == canonical]


def cancel(comms: Comms, name: str) -> list[QueuedRestart]:
    """Cancel only requests not yet attempted; never claim a signal was undone."""
    canonical = comms.registry.require(name).name
    directory = _directory(comms)
    changed = []
    with _store_lock(directory / "queue"):
        for path, record in _records(comms):
            if record.name == canonical and record.state.pending:
                cancelled = record.transition(CancelledRestart())
                _save(path, cancelled)
                changed.append(cancelled)
    return changed


def step(comms: Comms) -> None:
    directory = _directory(comms)
    for path, initial in _records(comms):
        if not initial.state.pending:
            continue
        with _store_lock(directory / "queue"):
            record = FieldCodec.decode(QueuedRestart, json.loads(path.read_text(encoding="utf-8")))
            if record != initial or not record.state.pending:
                continue
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
            _save(path, record)
        if not record.state.attempting:
            continue
        try:
            request = OwnerRestartRequest(
                (owner.name,),
                expected=record.selection,
                runtime=record.target.runtime,
                agent_bin=record.target.agent_bin,
                source_interpreter=record.source_interpreter,
                launch_runtime=record.target.launch_runtime,
            )
            (receipt,) = PreserveOwnerRuntime().restart(comms.owners, request)
        except RestartRefusal as refusal:
            record = record.transition(refusal.queue_state())
        except StoppedOwnerFailure as exc:
            # An attempted restart remains uncertain. This watcher cannot keep
            # credentials or authorize installation/input replay in its queue.
            exc.abandon()
            record = record.transition(UncertainRestart(reason=f'{exc}: {exc.__cause__}'))
        except Exception as exc:
            record = record.transition(UncertainRestart(reason=f"{type(exc).__name__}: {exc}"))
        else:
            record = record.transition(
                RestartedRestart(
                    previous=record.selection.process,
                    current=ProcessIdentity.capture(receipt.pid),
                )
            )
        with _store_lock(directory / "queue"):
            _save(path, record)


async def run(comms: Comms) -> None:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    directory = _directory(comms)
    lock = os.open(directory / "watcher.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Watch before the first step, so no turn settlement can be missed.
        async for _ in WireWatch.observations(
            WatchSource.file(comms.registry.store.path),
            WatchSource.file(comms.registry.store.guard_path),
            WatchSource.file(comms.root / "coordination.sqlite3"),
            WatchSource.file(comms.root / "coordination.sqlite3-wal"),
            DirectoryWatchSource(directory),
        ):
            await Coordination.run_worker(lambda: step(comms))
            # Check emptiness and release under enqueue's lock, so a new
            # request cannot slip between the empty check and release.
            def release_if_empty():
                with _store_lock(directory / "queue"):
                    if any(record.state.pending for _, record in _records(comms)):
                        return False
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    return True

            if await Coordination.run_worker(release_if_empty):
                return
    finally:
        os.close(lock)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agent_comms.restart_queue ROOT")
    comms = wire(sys.argv[1])
    try:
        asyncio.run(run(comms))
    except Exception as exc:
        directory = _directory(comms)
        with _store_lock(directory / "queue"):
            for path, record in _records(comms):
                if record.state.pending:
                    _save(
                        path,
                        record.transition(
                            BlockedRestart(reason=f"Watcher failed: {type(exc).__name__}: {exc}")
                        ),
                    )
        raise


if __name__ == "__main__":
    main()
