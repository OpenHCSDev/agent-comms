"""Process-lifetime watch claims.

A watched process holds an exclusive flock on its claim for its whole life and
writes there the command that started it. The kernel drops the lock when the
process dies, so a claim with a record and no holder is a watch that lapsed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


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
class WatchClaim:
    """The kernel-held lifetime and saved re-arm command of one session's bridge watch."""

    thread: str

    @classmethod
    def directory(cls) -> Path:
        return Path.home() / ".local/state/agent-comms/cli-watch"

    @property
    def path(self) -> Path:
        key = hashlib.sha256(self.thread.encode()).hexdigest()
        return self.directory() / f"{key}.lock"

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
    def held(self, record: WatchRecord) -> Iterator[None]:
        """One watch per conversation, independent of --state and bus aliases."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.truncate(0)
            lock.write(json.dumps(asdict(record)))
            lock.flush()
            yield

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
            current = self.state()
            if isinstance(current, LiveWatch):
                if current.record != record:
                    raise ValueError("This session is already bound to another bridge command")
                return
            with self.held(record):
                pass


class WatchState:
    """Whether a session's claim has a recorded watch, and whether its holder lives."""


class UnclaimedWatch(WatchState):
    pass


@dataclass(frozen=True)
class RecordedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord


class LiveWatch(RecordedWatch):
    pass


class LapsedWatch(RecordedWatch):
    pass
