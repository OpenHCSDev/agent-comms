"""Process-lifetime watch claims.

A watched process holds an exclusive flock on its claim for its whole life and
writes there the command that started it. The kernel drops the lock when the
process dies, so a claim with a record and no holder is a watch that lapsed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from agent_comms.registry_store import RegistryStore
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from agent_comms.cli_bridges.monitor import SessionRequest
    from agent_comms.cli_bridges.shared import Serve


class SessionRefusedError(ValueError):
    """A session endpoint rejected the control request, not malformed wire data."""


@dataclass(frozen=True)
class WatchRecord:
    bus: str
    root: str
    command: tuple[str, ...]

    @classmethod
    def decode(cls, bus: str, root: str, command: Sequence[str]) -> WatchRecord:
        """Normalize the saved JSON array before constructing the immutable record."""
        return cls(bus, root, tuple(command))


@dataclass(frozen=True)
class HeldClaim:
    """The open lock description that keeps a claim live. The kernel ties the flock to this
    description, so a duplicate inherited through exec keeps the claim held."""

    lock: TextIO

    def record(self, record: WatchRecord) -> HeldClaim:
        self.lock.truncate(0)
        self.lock.write(json.dumps(asdict(record)))
        self.lock.flush()
        return self

    def inheritable(self) -> int:
        descriptor = os.dup(self.lock.fileno())
        os.set_inheritable(descriptor, True)
        return descriptor


@dataclass(frozen=True)
class WatchClaim:
    """The kernel-held lifetime and saved re-arm command of one session's bridge watch."""

    thread: str
    home: Path = field(default_factory=Path.home)

    @classmethod
    def directory(cls, home: Path) -> Path:
        return home / ".local/state/agent-comms/cli-watch"

    @classmethod
    def endpoints(cls, home: Path) -> tuple[Path, ...]:
        """Live session endpoints share the claim directory, independent of --state."""
        return tuple(sorted(cls.directory(home).glob("*.sock")))

    @property
    def path(self) -> Path:
        key = hashlib.sha256(self.thread.encode()).hexdigest()
        return self.directory(self.home) / f"{key}.lock"

    @property
    def socket_path(self) -> Path:
        # sockaddr_un is short. Keep the locator derived from the session claim.
        return self.path.with_name(self.path.stem[:32] + ".sock")

    @property
    def reader_path(self) -> Path:
        return self.path.with_suffix(".reader.lock")

    @contextmanager
    def launching(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextmanager
    def held(self, record: WatchRecord) -> Iterator[HeldClaim]:
        """One watch per conversation, independent of --state and bus aliases."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield HeldClaim(lock).record(record)

    @contextmanager
    def adopted(self, descriptor: int, record: WatchRecord) -> Iterator[HeldClaim]:
        """A replaced bridge's successor takes over the description its predecessor kept
        locked through exec, so the claim never reads as lapsed, and records its own command."""
        if not os.path.samestat(os.fstat(descriptor), os.stat(self.path)):
            raise ValueError(f'Inherited descriptor {descriptor} is not the claim {self.path}')
        os.set_inheritable(descriptor, False)
        with os.fdopen(descriptor, "a") as lock:
            # Same open description: the flock is already this process's, so this cannot block.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield HeldClaim(lock).record(record)

    @contextmanager
    def reader_held(self) -> Iterator[None]:
        with self.reader_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def has_reader(self) -> bool:
        if not self.reader_path.exists():
            return False
        with self.reader_path.open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def state(self) -> WatchState:
        if not self.path.exists():
            return UnclaimedWatch()
        with self.path.open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return LiveWatch(self, WatchRecord.decode(**json.load(lock)))
            text = lock.read()
        if not text:
            return UnclaimedWatch()
        return LapsedWatch(self, WatchRecord.decode(**json.loads(text)))

    def enrol(self, record: WatchRecord) -> None:
        with self.launching():
            self.state().enrol(self, record)


class WatchState:
    """The claim state owns connection, shutdown, enrolment and discovery."""

    def connect(self, serve: Callable[[], Serve],
                request: Callable[[Serve], SessionRequest]) -> None:
        """An unenrolled session does not construct a serve or request authority."""

    def connect_held(self, serve: Callable[[], Serve],
                     request: Callable[[Serve], SessionRequest]) -> None:
        """A claim removed while awaiting its launch lock remains unclaimed."""

    def end(self, request: Callable[[], SessionRequest]) -> None:
        """Only a live holder can receive a shutdown request."""

    def enrol(self, claim: WatchClaim, record: WatchRecord) -> None:
        with claim.held(record):
            pass

    def may_serve(self, bus: str) -> bool:
        """Whether this session can be the bus thread named; unbound, it cannot be ruled out."""
        return True

    def commander_sources(self) -> bool:
        """An unbound session may read the package before it joins a bus."""
        return True

    def commander_notice(self) -> str:
        return "Commander session is not bound to a bus."

    def await_socket(self, absent: OSError) -> None:
        """No bridge holds the claim, so its socket is gone, not between bridges."""
        raise absent


class UnclaimedWatch(WatchState):
    pass


@dataclass(frozen=True)
class RecordedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord

    def commander_sources(self) -> bool:
        snapshot = RegistryStore.at(Path(self.record.root)).read_snapshot_unlocked()
        subject = snapshot.require(self.record.bus)
        return any(
            thread.incarnation == subject.incarnation
            for thread in snapshot.commander_project.commanders(snapshot)
        )

    def commander_notice(self) -> str:
        snapshot = RegistryStore.at(Path(self.record.root)).read_snapshot_unlocked()
        commanders = snapshot.commander_project.commanders(snapshot)
        return "Commander: " + ", ".join(thread.name for thread in commanders) + "."

    def connect(self, serve: Callable[[], Serve],
                request: Callable[[Serve], SessionRequest]) -> None:
        with self.claim.launching():
            self.claim.state().connect_held(serve, request)

    def connect_held(self, serve: Callable[[], Serve],
                     request: Callable[[Serve], SessionRequest]) -> None:
        operation = serve()
        operation.owner.require_alive()
        verification = request(operation)
        try:
            verification.exchange(self.claim.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, SessionRefusedError):
            self.reconnect(operation, verification)

    def reconnect(self, serve: Serve, request: SessionRequest) -> None:
        serve.launch_record(self.claim, self.record, request)

    def may_serve(self, bus: str) -> bool:
        return self.record.bus == bus


class LiveWatch(RecordedWatch):
    def reconnect(self, serve: Serve, request: SessionRequest) -> None:
        raise RuntimeError(
            'An older watch still owns this session, or an install is replacing it. '
            'No second bridge launched. Stop that Monitor once, then connect.')

    def end(self, request: Callable[[], SessionRequest]) -> None:
        request().exchange(self.claim.socket_path)

    def enrol(self, claim: WatchClaim, record: WatchRecord) -> None:
        if self.record != record:
            raise ValueError('This session is already bound to another bridge command')

    def await_socket(self, absent: OSError) -> None:
        """The claim's lock outlives the install's exec, so a held claim's absent socket is
        the swap's gap: wait for the successor however long it starts."""
        time.sleep(.05)


class LapsedWatch(RecordedWatch):
    pass
