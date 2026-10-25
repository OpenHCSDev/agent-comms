"""Process-lifetime watch claims.

A watched process holds an exclusive flock on its claim for its whole life and
writes there the command that started it. The kernel drops the lock when the
process dies, so a claim with a record and no holder is a watch that lapsed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from agent_comms.process_claim import ProcessSingleton
from agent_comms_hooks.board import SessionBoard, UnexpectedBoardReply

if TYPE_CHECKING:
    from agent_comms.cli_bridges.monitor import SessionRequest
    from agent_comms.cli_bridges.shared import Serve


class SessionRefusedError(ValueError):
    """A session endpoint rejected the control request, not malformed wire data."""


class NoSessionBridge(RuntimeError):
    """No bridge holds this claim: nothing is delivered until connect starts one."""

    def __init__(self, session: str) -> None:
        super().__init__(
            f'No session bridge is running for session {session}; its log names why it stopped. '
            'Start it with agent-comms-claude-bridge connect.')


@dataclass(frozen=True)
class DeafWatch:
    """A session bridge with no Monitor reading it: every line to it waits unread. Core's
    heartbeat and the session's own tool-call hook say it in this one sentence."""

    since: str

    def line(self) -> str:
        return (f"RE-ARM the commander watch: its bridge has no confirmed Monitor {self.since},"
                " so every Core line to it waits unread -> re-arm its watch:"
                " agent-comms-claude-bridge watch (bare, no pipe)")


@dataclass(frozen=True)
class WatchRecord:
    bus: str
    root: str
    command: tuple[str, ...]
    base_state: Path

    @classmethod
    def decode(cls, bus: str, root: str, command: Sequence[str], base_state: str) -> WatchRecord:
        """Normalize the saved JSON array before constructing the immutable record."""
        return cls(bus, root, tuple(command), Path(base_state))

    def to_json(self) -> str:
        """The saved command carries its declared base directory as path text."""
        return json.dumps({**asdict(self), "base_state": str(self.base_state)})


@dataclass(frozen=True)
class HeldClaim:
    """The open lock description that keeps a claim live. The kernel ties the flock to this
    description, so a duplicate inherited through exec keeps the claim held."""

    lock: TextIO

    def record(self, record: WatchRecord) -> HeldClaim:
        self.lock.truncate(0)
        self.lock.write(record.to_json())
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
    def claim(self) -> ProcessSingleton:
        return ProcessSingleton(self.path)

    @property
    def reader_claim(self) -> ProcessSingleton:
        return ProcessSingleton(self.path.with_suffix(".reader.lock"))

    @property
    def launch_claim(self) -> ProcessSingleton:
        return ProcessSingleton(self.path.with_suffix(".launch.lock"))

    @contextmanager
    def launching(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.launch_claim.waited():
            yield

    @contextmanager
    def held(self, record: WatchRecord) -> Iterator[HeldClaim]:
        """One watch per conversation, independent of --state and bus aliases."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.claim.claimed() as lock:
            yield HeldClaim(lock).record(record)

    @contextmanager
    def adopted(self, descriptor: int, record: WatchRecord) -> Iterator[HeldClaim]:
        """A replaced bridge's successor takes over the description its predecessor kept
        locked through exec, so the claim never reads as lapsed, and records its own command."""
        with self.claim.adopted(descriptor) as lock:
            yield HeldClaim(lock).record(record)

    @contextmanager
    def reader_held(self) -> Iterator[None]:
        with self.reader_claim.claimed():
            yield

    def has_reader(self) -> bool:
        return self.reader_claim.held_elsewhere()

    def deaf(self) -> tuple[DeafWatch, ...]:
        """A running bridge whose reader claim nobody holds; a session with no bridge is
        not deaf, it never bound one."""
        if self.claim.held_elsewhere() and not self.has_reader():
            return (DeafWatch("now (no Monitor holds its reader claim)"),)
        return ()

    def state(self) -> WatchState:
        if not self.path.exists():
            return UnclaimedWatch()
        with self.claim.observed() as claim:
            if claim.held:
                return LiveWatch(self, WatchRecord.decode(**json.load(claim.file)))
            text = claim.file.read()
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

    def bindings(self) -> tuple[WatchRecord, ...]:
        """No session binding exists until this watch is enrolled."""
        return ()

    def board(self) -> tuple[str, ...]:
        """Only a running bridge can derive a board."""
        return ()

    def announce_absence(self, announce: Callable[[], None]) -> bool:
        """No bridge holds the claim: the reader says delivery is down, once per outage."""
        announce()
        return True

    def name_wait(self, announce: Callable[[], None]) -> bool:
        """An absent holder is not a wait to name."""
        return False

    def await_socket(self, claim: WatchClaim, absent: OSError) -> None:
        """No bridge holds the claim, so its socket is gone, not between bridges."""
        raise NoSessionBridge(claim.thread) from absent


class UnclaimedWatch(WatchState):
    pass


@dataclass(frozen=True)
class RecordedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord

    def bindings(self) -> tuple[WatchRecord, ...]:
        return (self.record,)

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


class LiveWatch(RecordedWatch):
    def board(self) -> tuple[str, ...]:
        """The lines this session's bridge derives from Core when asked; a refusal from a
        bridge older than the request, or no answer at all, prints as unreadable."""
        try:
            return SessionBoard.asked(self.claim.socket_path).lines
        except (OSError, UnexpectedBoardReply) as error:
            return SessionBoard.unreadable(error).lines

    def reconnect(self, serve: Serve, request: SessionRequest) -> None:
        raise RuntimeError(
            'An older watch still owns this session, or an install is replacing it. '
            'No second bridge launched. Stop that Monitor once, then connect.')

    def end(self, request: Callable[[], SessionRequest]) -> None:
        request().exchange(self.claim.socket_path)

    def enrol(self, claim: WatchClaim, record: WatchRecord) -> None:
        if self.record != record:
            raise ValueError('This session is already bound to another bridge command')

    def announce_absence(self, announce: Callable[[], None]) -> bool:
        """A held claim's absent socket is an install's swap, not an outage."""
        return False

    def name_wait(self, announce: Callable[[], None]) -> bool:
        """Name a held claim's replacement once through the reader's protocol."""
        announce()
        return True

    def await_socket(self, claim: WatchClaim, absent: OSError) -> None:
        """The claim's lock outlives the install's exec, so a held claim's absent socket is
        the swap's gap: wait for the successor however long it starts."""
        time.sleep(.05)


class LapsedWatch(RecordedWatch):
    pass
