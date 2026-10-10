"""The session's Pi child: empty, borrowed by a turn, retained idle, or retiring."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .child_process import AttachedChild
from .native_attestation import NativeAttestation, PendingAttestation
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .native_session_reopen import NativeSessionIdentity
from .pi_rpc import PiRpcChannel
from .private_path import PrivateSocketRole

if TYPE_CHECKING:
    from .session_revision import SessionRevision


@dataclass
class PiSessionChild:
    proc: AttachedChild
    reader: PiRpcChannel
    stderr_task: asyncio.Task[str]
    key: tuple[NativePiRpcLaunch, tuple[int, int]]
    attestation: NativeAttestation
    sensitive_diagnostics: bool = False
    resources: ExitStack = field(default_factory=ExitStack, repr=False)

    async def reply_ui(self, response) -> None:
        await asyncio.wait_for(self.proc.write(self.reader.encode(response)), timeout=2)

    @classmethod
    async def start(cls, key, attestation):
        launch, _ = key
        with ExitStack() as resources:
            environment = dict(launch.env)
            if "AGENT_COMMS_PROJECT_SOCKET" in environment:
                address = resources.enter_context(PrivateSocketRole.address(
                    Path(environment["AGENT_COMMS_PROJECT_SOCKET"])
                ))
                environment["AGENT_COMMS_PROJECT_SOCKET"] = str(address)
            proc = await AttachedChild.start(launch.argv, cwd=str(launch.cwd), env=environment)
            assert proc.stdout is not None and proc.stderr is not None
            return cls(
                proc,
                PiRpcChannel(proc.stdout),
                asyncio.create_task(cls.stderr_tail(proc.stderr)),
                key,
                attestation,
                resources=resources.pop_all(),
            )

    @staticmethod
    async def stderr_tail(stream: asyncio.StreamReader) -> str:
        tail = b""
        while chunk := await stream.read(4096):
            tail = (tail + chunk)[-16000:]
        return tail.decode(errors="replace").strip()

    async def close(self) -> None:
        await self.proc.stop()
        await asyncio.gather(self.stderr_task, return_exceptions=True)
        self.resources.close()

    @asynccontextmanager
    async def failures(self):
        """Retain original bounded child output before transporting its failure."""
        try:
            yield
        except BaseException as error:
            await self.close()
            stderr = await self.stderr_task
            error.add_note(f"Original native child stderr:\n{stderr or '(empty)'}")
            if isinstance(error, NativePiUnavailable):
                error.native_stderr = stderr
            raise


class NativeCustody(ABC):
    """State owns the legal child capabilities, including its retirement successor."""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    retained = False

    def managed_launch(self, command, arguments, *, worktree, environment, session_file):
        return NativePiRpcLaunch.managed(command, arguments, worktree=worktree,
            environment=environment, session_file=session_file)

    def reuse(self, key) -> PiSessionChild | None:
        return None

    def idle(self) -> RetainedNative:
        raise NativePiUnavailable("Selected idle Pi child is unavailable or stale")

    async def inspect(self, persistent, prepare, request):
        """Inspect through this custody's legal acquisition/receiver capability."""
        raise NativePiUnavailable("Native context requires an acquired native child")

    def retire(self, successor: NativeCustody | None = None) -> NativeCustody:
        return successor if successor is not None else self

    async def closed(self) -> NativeCustody:
        return self

class EmptyNative(NativeCustody):
    available = False

    async def inspect(self, persistent, prepare, request):
        # The runtime owner supplies its existing selected-session preparation:
        # real saved launch/idle attestation/retention, never a priming prompt.
        await prepare()
        acquired = persistent.custody.idle()
        return await acquired.inspect_acquired(persistent, request)


class NativeCleanupFailed(RuntimeError):
    """The exact child is gone; its failed cleanup cannot own future inputs."""

    def __init__(self, successor, error):
        self.successor = successor
        super().__init__(
            f"Native process retired, but cleanup failed: {type(error).__name__}: {error}"
        )


@dataclass
class RetiringNative(NativeCustody):
    available = False
    task: asyncio.Task[None]
    successor: NativeCustody
    child: PiSessionChild

    def retire(self, successor=None):
        if successor is not None:
            self.successor = successor
        return self

    async def closed(self):
        try:
            await asyncio.shield(self.task)
        except Exception as error:
            if self.child.proc.retired:
                raise NativeCleanupFailed(self.successor, error) from error
            raise
        return self.successor

@dataclass
class BorrowedNative(NativeCustody):
    child: PiSessionChild
    successor: NativeCustody
    available = True

    async def inspect(self, persistent, prepare, request):
        # The active TurnSession owns receive/correlation. Borrow its original
        # pending response instead of starting a competing reader.
        async with request.pending_response(
            self.child.reader,self.child.proc.stdin
        ) as response:
            return (await response).data.require_payload()

    def retire(self, successor=None):
        return RetiringNative(
            asyncio.create_task(self.child.close()),
            self.successor if successor is None else successor,
            self.child,
        )

@dataclass
class RetainedNative(NativeCustody):
    retained = True
    child: PiSessionChild
    identity: NativeSessionIdentity
    revision: SessionRevision
    available = True

    @property
    def current(self) -> bool:
        return self.child.proc.alive() and self.revision.current(self.identity.session_file)

    def idle(self):
        if not self.current:
            return super().idle()
        return self

    async def inspect(self, persistent, prepare, request):
        # Reobserve this retained child through the same preparation/AgentInfo
        # owner. An acquired child alone does not publish native usage.
        await prepare()
        acquired = persistent.custody.idle()
        return await acquired.inspect_acquired(persistent, request)

    async def inspect_acquired(self, persistent, request):
        async with persistent.lock:
            current = persistent.custody.idle()
            response = await request.exchange(
                current.child.reader,current.child.proc.stdin
            )
            if response.success is not True:
                raise ValueError(f"Native request did not succeed: {response.error}")
            if request.writes_session:
                # This child wrote the file itself; its loaded context is the new revision.
                from .session_revision import SessionRevision

                current.revision = SessionRevision.observe(
                    current.identity.session_file
                ).require_available()
            return response.data.require_payload()

    def reuse(self, key):
        if self.child.key == key and self.current:
            self.child.attestation = PendingAttestation(self.identity)
            return self.child
        return None

    def managed_launch(self, command, arguments, *, worktree, environment, session_file):
        if self.current:
            original, authentication = self.child.key
            candidate = original.retained_managed(command, arguments,
                worktree=worktree, environment=environment, session_file=session_file)
            if candidate is not None and (candidate, candidate.configuration.auth_revision()) == (
                original, authentication
            ):
                return original
        return super().managed_launch(command, arguments, worktree=worktree,
            environment=environment, session_file=session_file)

    def retire(self, successor=None):
        return RetiringNative(
            asyncio.create_task(self.child.close()),
            EmptyNative() if successor is None else successor,
            self.child,
        )

