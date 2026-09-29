"""Durable, incarnation-guarded, event-driven idle owner restarts (Linux only).

A queue request is bound to one exact owner incarnation. The watcher never
retries an attempted restart: a crash or ambiguous exception requires operator
review. No credentials or owner environment are written to the queue.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import shlex
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from typing import ClassVar
from collections.abc import Mapping

from .child_process import ProcessIdentity, Platform, ParentedProcess
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .owner_lifecycle import OwnerRestartSelection
from .private_nk_entrypoint import ROOT_ID_ENV, PACKAGE_ENV
from .restart_refusals import RestartRefusal
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux-only execution; tools still import on Windows
    fcntl = None

from .comms import Comms, wire
from .errors import RelationViolationError
from .store_files import _store_lock

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
class RestartEnvironment:
    """Declared inheritance policy for the credential-free resident watcher.

    Private launch variable spellings belong to PrivateNkLaunch. Owner credentials
    stay in /proc and are read only from the exact selected process at execution.
    """

    home: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "HOME"}
    )
    path: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "PATH", "runtime": True}
    )
    pythonpath: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": "PYTHONPATH", "runtime": True},
    )
    virtual_env: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": "VIRTUAL_ENV", "runtime": True},
    )
    config: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "XDG_CONFIG_HOME"}
    )
    root: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": "AGENT_COMMS_ROOT", "runtime": True},
    )
    root_id: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": ROOT_ID_ENV, "runtime": True},
    )
    package: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": PACKAGE_ENV, "runtime": True},
    )
    owner_key: ClassVar[str] = "AGENT_COMMS_THREAD"
    binary_key: ClassVar[str] = "AGENT_COMMS_AGENT_BIN"
    arguments_key: ClassVar[str] = "AGENT_COMMS_AGENT_ARGS"

    @classmethod
    def inherit(cls, environment: Mapping[str, str]):
        return cls(**{f.name: environment.get(f.metadata["wire_name"]) for f in fields(cls)})

    def encode(self) -> dict[str, str]:
        return FieldCodec.encode(self)

    @classmethod
    def require_owner(cls, environment: Mapping[str, str], name: str):
        if environment.get(cls.owner_key) != name or not environment.get(cls.binary_key):
            raise ValueError("Owner launch configuration cannot be verified")

    @classmethod
    def restart_arguments(cls, environment: Mapping[str, str], *, agent_bin: str | None = None):
        arguments = environment.get(cls.arguments_key)
        return dict(
            agent_bin=agent_bin if agent_bin is not None else environment[cls.binary_key],
            agent_args=shlex.split(arguments) if arguments is not None else None,
        )

    def apply_runtime(self, source: Mapping[str, str]) -> dict[str, str]:
        """Preserve source credentials/settings; replace the declared runtime fields."""
        result = dict(source)
        encoded = self.encode()
        for declaration in fields(self):
            if declaration.metadata.get("runtime"):
                name = declaration.metadata.get("wire_name")
                result.pop(name, None)
                if name in encoded:
                    result[name] = encoded[name]
        return result


@dataclass(frozen=True)
class RestartTarget:
    interpreter: str
    runtime: RestartEnvironment
    agent_bin: str

    @classmethod
    def capture(cls, comms: Comms, source: Mapping[str, str]):
        runtime = RestartEnvironment.inherit(
            comms.owners.restart_environment(RestartEnvironment.inherit(os.environ).encode())
        )
        return cls(
            sys.executable,
            runtime,
            comms.owners.restart_entrypoint(source[RestartEnvironment.binary_key]),
        )

    def require_watcher(self, comms: Comms) -> None:
        current = RestartEnvironment.inherit(
            comms.owners.restart_environment(RestartEnvironment.inherit(os.environ).encode())
        )
        if self.interpreter != sys.executable or self.runtime != current:
            raise ValueError("Watcher target runtime changed")
        if not Path(self.interpreter).is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")
        if Path(self.agent_bin).is_absolute() and not os.access(self.agent_bin, os.X_OK):
            raise ValueError("Target agent entrypoint is unavailable")


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


def _owner_environment(pid: int, name: str, interpreter: str) -> dict[str, str]:
    """Read an exact owner's launch configuration without journaling secrets."""
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
    if os.fsdecode(command) != interpreter:
        raise ValueError("Selected source owner interpreter changed")
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    env = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
    RestartEnvironment.require_owner(env, name)
    return env


def enqueue(comms: Comms, name: str) -> QueuedRestart:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    directory = _directory(comms)
    with _store_lock(comms._wire_lock_path):
        snapshot = comms.registry.snapshot()
        owner = comms.registry.require(name)
        status = snapshot.statuses[owner.name]
        if not owner.role.executable or not status.active or not owner.process_alive:
            raise RelationViolationError("Queued restart requires a live agent owner")
        selection = OwnerRestartSelection.capture(snapshot, owner.name)
        Platform.current().require(selection.process)
        source_interpreter = os.fsdecode(
            Path(f"/proc/{selection.process.pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        )
        source = _owner_environment(selection.process.pid, owner.name, source_interpreter)
        Platform.current().require(selection.process)
        target = RestartTarget.capture(comms, source)
        target.require_watcher(comms)
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
                    uuid.uuid4().hex, selection, source_interpreter, target, PendingRestart()
                )
                _save(directory / f"{result.id}.json", result)
    # A new watcher can safely race an existing watcher: flock permits one runner.
    _start_watcher(comms.root, target.runtime)
    return result


def _start_watcher(root: Path, environment: RestartEnvironment) -> ParentedProcess:
    # The resident watcher inherits only its declared runtime configuration.
    return ParentedProcess.launch(
        (sys.executable, "-m", "agent_comms.restart_queue", str(root.resolve())),
        env=environment.encode(),
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
                owner.require_idle()
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
            Platform.current().require(record.selection.process)
            original = _owner_environment(owner.pid, owner.name, record.source_interpreter)
            Platform.current().require(record.selection.process)
            launch = RestartEnvironment.restart_arguments(
                original, agent_bin=record.target.agent_bin
            )
            (receipt,) = comms.owners.restart_owners(
                [owner.name],
                expected=record.selection,
                environment=record.target.runtime.apply_runtime(original),
                **launch,
            )
        except RestartRefusal as refusal:
            record = record.transition(refusal.queue_state())
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


@contextmanager
def _watch(root: Path, directory: Path):
    libc = ctypes.CDLL(None, use_errno=True)
    init = libc.inotify_init1
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    add = libc.inotify_add_watch
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    fd = init(os.O_CLOEXEC | os.O_NONBLOCK)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    for path in (root, directory):
        if add(fd, os.fsencode(path), 0x00000080 | 0x00000008) < 0:
            error = ctypes.get_errno()
            os.close(fd)
            raise OSError(error, "inotify_add_watch")
    try:
        yield fd
    finally:
        os.close(fd)


def run(comms: Comms) -> None:
    if sys.platform != "linux" or fcntl is None:
        raise ValueError("Queued restarts require Linux inotify and /proc")
    directory = _directory(comms)
    lock = os.open(directory / "watcher.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Watch before the first step, so no turn settlement can be missed.
        with _watch(comms.root, directory) as fd:
            while True:
                step(comms)
                # Exit after the last pending request, allowing a subsequently
                # installed runtime to own a new watcher. Release the watcher
                # lease under the same queue lock used by enqueue: a new
                # request cannot slip between the empty check and release.
                with _store_lock(directory / "queue"):
                    if not any(record.state.pending for _, record in _records(comms)):
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        return
                select.select([fd], [], [])
                os.read(fd, 65536)
    finally:
        os.close(lock)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agent_comms.restart_queue ROOT")
    comms = wire(sys.argv[1])
    try:
        run(comms)
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
