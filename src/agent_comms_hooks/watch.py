"""The session's existing hook/socket boundary; standard library only."""
from __future__ import annotations

import hashlib
import json
import socket
import sys
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


class HookBridgeUnavailable(RuntimeError):
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
        return UnboundSession()


class HookSession(ABC):
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
    endpoint: SessionEndpoint

    def when_bound[Answer](self, unbound: Callable[[], Answer], bound: Callable[[], Answer]) -> Answer:
        return bound()

    def invoke(self, program: type[ClaudeHookProgram], record: Mapping[str, object]) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(HookAnswer.timeout)
                peer.connect(str(self.endpoint.socket_path))
                peer.sendall(json.dumps({
                    SessionWire.family_discriminator: program.command(), "payload": record,
                }).encode() + b"\n")
                with peer.makefile("rb") as stream:
                    raw = stream.readline()
            if not raw:
                raise HookBridgeUnavailable("The bound session bridge closed without answering the hook")
            answer = HookAnswer.replied(json.loads(raw))
        except (OSError, ValueError, KeyError) as error:
            raise HookBridgeUnavailable(
                f"Bound session {self.endpoint.thread} cannot answer its hook: {error}. "
                "Start its existing agent-comms-claude-bridge; no unbound fallback was used."
            ) from error
        answer.print(program)


class SessionHookProgram(ClaudeHookProgram, ABC):
    """The boundary reads once, sends to the existing session, and prints its answer."""

    @classmethod
    def main(cls) -> None:
        try:
            record = json.load(sys.stdin)
            hook = ClaudeHook.decode(record)
            SessionEndpoint(hook.session_id).session().invoke(cls, record)
        except Exception as error:
            # This process boundary reports the failure; it never substitutes an unbound answer.
            cls.failed(error)
