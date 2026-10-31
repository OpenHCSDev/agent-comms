from __future__ import annotations

import json
import os
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import (
    Callable,
    Iterator,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import (
    asdict,
    dataclass,
)
from pathlib import Path
from typing import (
    TextIO,
)

from agent_comms_hooks.board import AbsentSessionSocket, SessionBoard
from agent_comms_hooks.process_claim import ProcessSingleton
from agent_comms_hooks.watch import HeldSocketWatch, SessionCommandError, SessionEndpoint, SessionFailure, SocketWatch

from ..registry_store import RegistryStore
from ..store_files import AtomicFile

import agent_comms.cli_bridges


class SessionRefusedError(ValueError, SessionFailure):
    """A session endpoint rejected the control request, not malformed wire data."""


class SessionRequestTimeout(SessionCommandError):
    """The request's declared reply budget expired."""


class SessionConnectionClosed(SessionCommandError):
    """The peer closed before a required reply or startup probe."""


@dataclass(frozen=True)
class DeafWatch:
    """A session bridge with no Monitor reading it: every line to it waits unread. Core's
    heartbeat and the session's own tool-call hook say it in this one sentence."""

    since: str

    def line(self) -> str:
        return (f"RE-ARM this session's watch: its bridge has no confirmed Monitor {self.since},"
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
    description, so a duplicate inherited through exec keeps the claim held. The lock file
    carries no data: the holder publishes its record whole beside it, so a reader sees the
    complete old record or the complete new one, never a truncated file."""

    lock: TextIO
    saved: AtomicFile

    def record(self, record: WatchRecord) -> HeldClaim:
        self.saved.write_text(record.to_json())
        return self

    def inheritable(self) -> int:
        descriptor = os.dup(self.lock.fileno())
        os.set_inheritable(descriptor, True)
        return descriptor


@dataclass(frozen=True)
class WatchClaim(SessionEndpoint):
    """The kernel-held lifetime and saved re-arm command at the shared session endpoint."""

    @staticmethod
    def claims_in(directory: Path) -> tuple[Path, ...]:
        """Every session's claim lock in ``directory``; reader and launch claims carry a
        second suffix (``reader_claim``, ``launch_claim``)."""
        return tuple(sorted(path for path in directory.glob("*.lock") if len(path.suffixes) == 1))

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
            yield HeldClaim(lock, AtomicFile(self.record_path)).record(record)

    @contextmanager
    def adopted(self, descriptor: int, record: WatchRecord) -> Iterator[HeldClaim]:
        """A replaced bridge's successor takes over the description its predecessor kept
        locked through exec, so the claim never reads as lapsed, and records its own command."""
        with self.claim.adopted(descriptor) as lock:
            yield HeldClaim(lock, AtomicFile(self.record_path)).record(record)

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
        held = self.claim.held_elsewhere()
        return self.session().when_bound(
            unbound=lambda: ClaimingWatch() if held else UnclaimedWatch(),
            bound=lambda: self.recorded_state(held),
        )

    def recorded_state(self, held: bool) -> WatchState:
        record = WatchRecord.decode(**json.loads(self.record_path.read_text()))
        return LiveWatch(self, record) if held else LapsedWatch(self, record)

    def enrol(self, record: WatchRecord) -> None:
        with self.launching():
            self.state().enrol(self, record)


class WatchState(SocketWatch, ABC):
    """The claim state owns connection, shutdown, enrolment and discovery."""

    @abstractmethod
    def rearm(self, launch: Callable[[], None]) -> bool:
        """Whether this state needed the enrolled session's existing launcher, and ran it."""

    def connect(self, serve: Callable[[], agent_comms.cli_bridges.shared.Serve],
                request: Callable[[agent_comms.cli_bridges.shared.Serve], agent_comms.cli_bridges.monitor.SessionRequest]) -> None:
        """An unenrolled session does not construct a serve or request authority."""

    def connect_held(self, serve: Callable[[], agent_comms.cli_bridges.shared.Serve],
                     request: Callable[[agent_comms.cli_bridges.shared.Serve], agent_comms.cli_bridges.monitor.SessionRequest]) -> None:
        """A claim removed while awaiting its launch lock remains unclaimed."""

    def end(self, request: Callable[[], agent_comms.cli_bridges.monitor.SessionRequest]) -> None:
        """Only a live holder can receive a shutdown request."""

    def enrol(self, claim: WatchClaim, record: WatchRecord) -> None:
        with claim.held(record):
            pass

    def bindings(self) -> tuple[WatchRecord, ...]:
        """No session binding exists until this watch is enrolled."""
        return ()

    def commands(self) -> bool:
        """An unbound session commands nothing: the registry names the commander, and only
        a binding can point at it."""
        return False

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

class UnclaimedWatch(WatchState):
    def rearm(self, launch: Callable[[], None]) -> bool:
        raise SessionCommandError('Enroll this session before starting its Monitor')


class ClaimingWatch(WatchState):
    """A bridge holds the claim and is publishing its first record: it is serving, so it is
    neither relaunched nor announced as down, and it has no binding to report yet."""

    def rearm(self, launch: Callable[[], None]) -> bool:
        return False

    def announce_absence(self, announce: Callable[[], None]) -> bool:
        return False


@dataclass(frozen=True)
class RecordedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord

    def bindings(self) -> tuple[WatchRecord, ...]:
        return (self.record,)

    def commands(self) -> bool:
        """A saved enrollment may predate registration; only a registered recipient has a role."""
        snapshot = RegistryStore.at(Path(self.record.root)).snapshot()
        if not snapshot.taken(self.record.bus):
            return False
        return snapshot.commander_project.by_role(
            snapshot.require(self.record.bus), commander=True, commanded=False)

    def connect(self, serve: Callable[[], agent_comms.cli_bridges.shared.Serve],
                request: Callable[[agent_comms.cli_bridges.shared.Serve], agent_comms.cli_bridges.monitor.SessionRequest]) -> None:
        with self.claim.launching():
            self.claim.state().connect_held(serve, request)

    def connect_held(self, serve: Callable[[], agent_comms.cli_bridges.shared.Serve],
                     request: Callable[[agent_comms.cli_bridges.shared.Serve], agent_comms.cli_bridges.monitor.SessionRequest]) -> None:
        operation = serve()
        operation.owner.require_alive()
        verification = request(operation)
        try:
            verification.exchange(self.claim.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, SessionRefusedError):
            self.reconnect(operation, verification)

    def reconnect(self, serve: agent_comms.cli_bridges.shared.Serve, request: agent_comms.cli_bridges.monitor.SessionRequest) -> None:
        serve.launch_record(self.claim, self.record, request)


class LiveWatch(RecordedWatch, HeldSocketWatch):
    def rearm(self, launch: Callable[[], None]) -> bool:
        """The kernel-held bridge is already serving; do not launch another."""
        return False

    def board(self) -> tuple[str, ...]:
        """Preserve main's install-gap answer; other board failures remain loud."""
        try:
            return SessionBoard.asked(self.claim.socket_path).lines
        except AbsentSessionSocket:
            return ("BOARD: not read this call: an install is swapping the session bridge, "
                    "so no bridge listens yet; the board returns when the new one does",)

    def reconnect(self, serve: agent_comms.cli_bridges.shared.Serve, request: agent_comms.cli_bridges.monitor.SessionRequest) -> None:
        raise RuntimeError(
            'An older watch still owns this session, or an install is replacing it. '
            'No second bridge launched. Stop that Monitor once, then connect.')

    def end(self, request: Callable[[], agent_comms.cli_bridges.monitor.SessionRequest]) -> None:
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

class LapsedWatch(RecordedWatch):
    def rearm(self, launch: Callable[[], None]) -> bool:
        launch()
        return True
