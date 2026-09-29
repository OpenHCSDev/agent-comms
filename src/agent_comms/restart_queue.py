"""Durable, incarnation-guarded, event-driven idle owner restarts (Linux only).

A queue request is bound to one exact owner incarnation. The watcher never
retries an attempted restart: a crash or ambiguous exception requires operator
review. No credentials or owner environment are written to the queue.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import select
import shlex
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

from .comms import Comms, wire
from .errors import RelationViolationError
from .store_files import _store_lock

DIRECTORY = "owner-restart-queue"


def _directory(comms: Comms) -> Path:
    path = comms.root / DIRECTORY
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Restart queue directory must be private and not a symlink")
    return path


def _save(path: Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(record, stream, sort_keys=True)
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
        yield path, json.loads(path.read_text(encoding="utf-8"))


def _owner_environment(pid: int, name: str, interpreter: str) -> dict[str, str]:
    """Read an exact owner's launch configuration without journaling secrets."""
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
    if os.fsdecode(command) != interpreter:
        raise ValueError("Owner interpreter changed; cross-runtime restart is not supported")
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    env = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
    if env.get("AGENT_COMMS_THREAD") != name or not env.get("AGENT_COMMS_AGENT_BIN"):
        raise ValueError("Owner launch configuration cannot be verified")
    return env


def enqueue(comms: Comms, name: str) -> dict:
    if sys.platform != "linux":
        raise ValueError("Queued restarts require Linux inotify and /proc")
    directory = _directory(comms)
    with _store_lock(comms._wire_lock_path):
        snapshot = comms.registry.snapshot()
        owner = comms.registry.require(name)
        status = snapshot.statuses[owner.name]
        if not owner.role.executable or not status.active or not owner.process_alive:
            raise RelationViolationError("Queued restart requires a live agent owner")
        incarnation = [owner.pid, owner.created_at, snapshot.admission_generations[owner.name]]
        # Queueing itself never signals or interrupts the owner, even if it is busy.
        with _store_lock(directory / "queue"):
            for _, previous in _records(comms):
                if previous["name"] == owner.name and previous["state"] in (
                    "pending",
                    "attempting",
                ):
                    if previous["incarnation"] != incarnation:
                        raise RelationViolationError("Previous owner restart requires review")
                    result = previous
                    break
            else:
                result = {
                    "version": 1,
                    "id": uuid.uuid4().hex,
                    "name": owner.name,
                    "incarnation": incarnation,
                    "interpreter": sys.executable,
                    "state": "pending",
                }
                _save(directory / f"{result['id']}.json", result)
    # A new watcher can safely race an existing watcher: flock permits one runner.
    subprocess.Popen(
        [sys.executable, "-m", "agent_comms.restart_queue", str(comms.root.resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        # The watcher is resident. It must not inherit provider credentials or
        # impersonate the requesting thread; the exact owner's environment is
        # read only at execution time and never persisted.
        env={
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "HOME",
                "PATH",
                "PYTHONPATH",
                "VIRTUAL_ENV",
                "XDG_CONFIG_HOME",
                "AGENT_COMMS_ROOT",
                "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID",
                "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE",
            }
        },
    )
    return result


def status(comms: Comms, name: str) -> list[dict]:
    canonical = comms.registry.require(name).name
    with _store_lock(_directory(comms) / "queue"):
        return [record for _, record in _records(comms) if record["name"] == canonical]


def cancel(comms: Comms, name: str) -> list[dict]:
    """Cancel only requests not yet attempted; never claim a signal was undone."""
    canonical = comms.registry.require(name).name
    directory = _directory(comms)
    changed = []
    with _store_lock(directory / "queue"):
        for path, record in _records(comms):
            if record["name"] == canonical and record["state"] == "pending":
                record.update(state="cancelled")
                _save(path, record)
                changed.append(record)
    return changed


def step(comms: Comms) -> None:
    directory = _directory(comms)
    for path, initial in _records(comms):
        if initial["state"] != "pending":
            continue
        with _store_lock(directory / "queue"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record != initial or record["state"] != "pending":
                continue
            snapshot = comms.registry.snapshot()
            owner = snapshot.threads.get(record["name"])
            expected = tuple(record["incarnation"])
            observed = (
                (owner.pid, owner.created_at, snapshot.admission_generations.get(owner.name))
                if owner is not None
                else None
            )
            if observed != expected or not snapshot.statuses[owner.name].active:
                record.update(state="stale", reason="Owner incarnation or status changed")
            elif owner.active_turn is not None:
                continue
            elif not owner.process_alive:
                record.update(state="stale", reason="Owner process exited")
            elif record["interpreter"] != sys.executable:
                record.update(state="blocked", reason="Watcher runtime changed")
            else:
                # Persist before any side effect. A crash here leaves an explicit
                # uncertain attempt, never an automatic replay.
                record["state"] = "attempting"
            _save(path, record)
        if record["state"] != "attempting":
            continue
        try:
            if not owner.process_alive:
                raise ValueError("Original owner is no longer alive")
            original = _owner_environment(owner.pid, owner.name, record["interpreter"])
            if not owner.process_alive:
                raise ValueError("Original owner changed during environment read")
            saved = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(original)
                (receipt,) = comms.owners.restart_owners(
                    [owner.name],
                    agent_bin=original["AGENT_COMMS_AGENT_BIN"],
                    agent_args=(
                        shlex.split(original["AGENT_COMMS_AGENT_ARGS"])
                        if "AGENT_COMMS_AGENT_ARGS" in original
                        else None
                    ),
                    expected_incarnations={owner.name: expected},
                )
            finally:
                os.environ.clear()
                os.environ.update(saved)
        except RelationViolationError as exc:
            reason = str(exc)
            # Only these errors attest that no signal was issued.
            if reason in (
                "Wait until the owner is idle before restart.",
                "Idle owner changed before restart fence.",
            ):
                record.update(state="pending")
            elif reason in (
                "Queued owner changed before restart.",
                "Owner selection changed before restart.",
                "Owner epochs changed before restart.",
            ):
                record.update(state="stale", reason=reason)
            else:
                record.update(state="uncertain", reason=reason)
        except Exception as exc:
            record.update(state="uncertain", reason=f"{type(exc).__name__}: {exc}")
        else:
            record.update(state="restarted", old_pid=receipt.previous_pid, new_pid=receipt.pid)
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
                    if not any(record["state"] == "pending" for _, record in _records(comms)):
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
                if record["state"] == "pending":
                    record.update(
                        state="blocked", reason=f"Watcher failed: {type(exc).__name__}: {exc}"
                    )
                    _save(path, record)
        raise


if __name__ == "__main__":
    main()
