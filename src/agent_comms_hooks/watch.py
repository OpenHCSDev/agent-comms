"""The session's existing hook/socket boundary; standard library only."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import ClassVar

from . import (
    ClaudeHook,
    ClaudeHookProgram,
    HookRecord,
)
from .board import SessionWire
from .process_claim import ProcessSingleton
from .shell_commands import ConnectCommand


class SessionFailure(Exception):
    """A session command failed; it does not declare the serving process dead."""


class SessionCommandError(RuntimeError, SessionFailure):
    """A session command could not complete."""


class NoSessionBridge(SessionCommandError):
    """No bridge holds this claim; its recovery command remains available."""

    def __init__(self, session: str) -> None:
        super().__init__(
            f'No session bridge is running for session {session}; its log names why it stopped. '
            f'Start it with {ConnectCommand.program.name} {ConnectCommand.command_name()}.')


class HookBridgeUnavailable(RuntimeError, SessionFailure):
    """A bound session cannot answer its hook; it did not become unbound."""


@dataclass(frozen=True)
class HookAnswer(HookRecord, SessionWire):
    """The resident hook's output and process result, not another saved record."""

    request: ClassVar[str] = "hook-answer"
    timeout: ClassVar[float] = 30
    stdout: str = ""
    stderr: str = ""
    status: int = 0

    @classmethod
    def replied(cls, record: dict[str, object]) -> HookAnswer:
        member = record.pop(cls.family_discriminator)
        if member != cls.request:
            raise HookBridgeUnavailable(f"The session bridge refused the hook: {member}: {record!r}")
        return cls.decode(record)

    def print(self, program: type[ClaudeHookProgram]) -> None:
        sys.stdout.write(self.stdout)
        sys.stderr.write(self.stderr)
        if self.status:
            raise SystemExit(program.failure_status(self.status))


@dataclass(frozen=True)
class SessionEndpoint:
    """One locator, shared by the lightweight hook and the kernel-held WatchClaim."""

    thread: str
    home: Path = field(default_factory=Path.home)

    @staticmethod
    def environment_session() -> str:
        """Claude exports the identity shared by its hooks, its bare Monitor command and
        every Bash call its subagents make."""
        session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if not session:
            raise ValueError("Claude did not export CLAUDE_CODE_SESSION_ID")
        return session

    @classmethod
    def directory(cls, home: Path) -> Path:
        return home / ".local/state/agent-comms/cli-watch"

    @classmethod
    def endpoints(cls, home: Path) -> tuple[Path, ...]:
        return tuple(sorted(cls.directory(home).glob("*.sock")))

    @property
    def path(self) -> Path:
        key = hashlib.sha256(self.thread.encode()).hexdigest()
        return self.directory(self.home) / f"{key}.lock"

    @property
    def claim(self) -> ProcessSingleton:
        return ProcessSingleton(self.path)

    @staticmethod
    def record_beside(claim: Path) -> Path:
        return claim.with_suffix(".record")

    @property
    def record_path(self) -> Path:
        return self.record_beside(self.path)

    @property
    def socket_path(self) -> Path:
        # sockaddr_un is short. This is the existing claim-derived address.
        return self.path.with_name(self.path.stem[:32] + ".sock")

    def session(self) -> HookSession:
        """An enrolled record establishes binding, not a successful socket connection.

        Live and lapsed enrolled claims are both bound. An unrecorded launch or a
        session never enrolled has no bus; the endpoint never guesses its role.
        """
        if self.record_path.exists():
            return BoundSession(self)
        return UnboundSession(self)

    def absent(self) -> HookSession:
        """The kernel distinguishes a held install swap from a lapsed enrolled bridge."""
        return SwappingSession(self) if self.claim.held_elsewhere() else AbsentSession(self)


class SocketWatch:
    """An absent unheld socket is down; both transport and watch control use this policy."""

    def await_socket(self, claim: SessionEndpoint, absent: OSError) -> None:
        raise NoSessionBridge(claim.thread) from absent


class HeldSocketWatch(SocketWatch):
    """An inherited claim stays held through an install's exec and its socket gap."""

    def await_socket(self, claim: SessionEndpoint, absent: OSError) -> None:
        time.sleep(.05)


@dataclass(frozen=True)
class HookSession(SocketWatch, ABC):
    endpoint: SessionEndpoint

    def require_bound(self) -> None:
        raise HookBridgeUnavailable(
            f"Session {self.endpoint.thread} is bound to no bus thread; bind it to the bus first")

    def when_bound[Answer](self, unbound: Callable[[], Answer], bound: Callable[[], Answer]) -> Answer:
        """An unbound session supplies no enrolled record to the resident watch."""
        return unbound()

    @abstractmethod
    def invoke(self, program: type[ClaudeHookProgram], record: Mapping[str, object]) -> None: ...


class UnboundSession(HookSession):
    """A genuinely unenrolled session keeps the program's existing local answer."""

    def invoke(self, program: type[ClaudeHookProgram], record: Mapping[str, object]) -> None:
        program.unbound(record)


@dataclass(frozen=True)
class BoundSession(HookSession):
    def require_bound(self) -> None:
        """Enrollment is already established by this state; socket liveness is separate."""

    def when_bound[Answer](self, unbound: Callable[[], Answer], bound: Callable[[], Answer]) -> Answer:
        return bound()

    def answer(self, program: type[ClaudeHookProgram], record: Mapping[str, object],
               *, timeout: float = HookAnswer.timeout) -> HookAnswer:
        """One request on the one resident socket; absence is handled by the session state."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(timeout)
            peer.connect(str(self.endpoint.socket_path))
            peer.sendall(json.dumps({
                SessionWire.family_discriminator: program.wire_name(), "payload": record,
            }).encode() + b"\n")
            with peer.makefile("rb") as stream:
                raw = stream.readline()
        if not raw:
            raise HookBridgeUnavailable("The bound session bridge closed without answering the hook")
        return HookAnswer.replied(json.loads(raw))

    def invoke(self, program: type[ClaudeHookProgram], record: Mapping[str, object]) -> None:
        try:
            answer = self.answer(program, record)
        except (FileNotFoundError, ConnectionRefusedError):
            self.endpoint.absent().invoke(program, record)
            return
        except OSError as error:
            raise HookBridgeUnavailable(
                f"Bound session {self.endpoint.thread} cannot answer its hook: {error}"
            ) from error
        answer.print(program)


class AbsentSession(BoundSession):
    """Enrollment survives the outage; only the declared recovery command may bypass it."""

    def invoke(self, program: type[ClaudeHookProgram], record: Mapping[str, object]) -> None:
        program.absent(record, NoSessionBridge(self.endpoint.thread))


class SwappingSession(AbsentSession, HeldSocketWatch):
    """Wait for the held claim's successor, with the hook's existing reply budget."""

    def invoke(self, program: type[ClaudeHookProgram], record: Mapping[str, object]) -> None:
        deadline = time.monotonic() + HookAnswer.timeout
        bound = BoundSession(self.endpoint)
        while time.monotonic() < deadline:
            try:
                answer = bound.answer(program, record, timeout=max(.001, deadline - time.monotonic()))
            except (FileNotFoundError, ConnectionRefusedError) as absent:
                try:
                    self.endpoint.absent().await_socket(self.endpoint, absent)
                except NoSessionBridge as lapsed:
                    program.absent(record, lapsed)
                    return
            except OSError as error:
                raise HookBridgeUnavailable(
                    f"Bound session {self.endpoint.thread} cannot answer its hook: {error}"
                ) from error
            else:
                answer.print(program)
                return
        raise HookBridgeUnavailable(
            f"Session {self.endpoint.thread} still holds its claim but did not restore its socket "
            f"within {HookAnswer.timeout:g} seconds")


class SessionHookProgram(ClaudeHookProgram, ABC):
    """The boundary reads once, sends to the existing session, and prints its answer."""

    @classmethod
    def main(cls) -> None:
        try:
            record = json.load(sys.stdin)
            hook = ClaudeHook.decode(record)
            SessionEndpoint(hook.session_id).session().invoke(cls, record)
        except SessionFailure as error:
            cls.failed(error)


class SkillText(SessionHookProgram):
    """Startup deals through the resident after launch, not through a parallel hook."""

    events = ("SessionStart",)

    @classmethod
    def installation_members(cls) -> tuple[type[ClaudeHookProgram], ...]:
        return ()
