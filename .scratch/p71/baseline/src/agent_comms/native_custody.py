"""The session's Pi child: empty, borrowed by a turn, retained idle, or retiring."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .child_process import AttachedChild, ChildOutcome, join_retirement
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
    transport: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)
    forwarding_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _close_job: asyncio.Task[ChildOutcome | BaseException] | None = field(default=None, init=False, repr=False)

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

    @property
    def retired(self) -> bool:
        job = self._close_job
        if job is None or not job.done():
            return False
        return not job.cancelled() and job.exception() is None and isinstance(job.result(), ChildOutcome)

    def forward_input(self, operation: Callable[[], Coroutine[None, None, None]]) -> None:
        if self.forwarding_task is None:
            self.forwarding_task = asyncio.create_task(operation())

    async def finish_input(self) -> None:
        """Retire this input's ingress without retiring a retained idle child."""
        forwarding, self.forwarding_task = self.forwarding_task, None
        if forwarding is not None:
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)
        self.reader.pending.cancel_all()

    async def shutdown(self, *, graceful: bool = False) -> ChildOutcome:
        """Join the one gateway, ingress, process, pipe and resource retirement.

        A settled non-retained protocol gets its existing bounded EOF grace;
        refusals and interruptions terminate immediately. The first request
        owns that choice and every caller joins the same complete operation.
        """
        if self._close_job is None:
            self._close_job = asyncio.create_task(self._shutdown(graceful=graceful))
        job = self._close_job
        try:
            result = await join_retirement(job)
        except asyncio.CancelledError as cancellation:
            if isinstance(error := job.result(), BaseException):
                raise cancellation from error
            raise
        if isinstance(result, BaseException):
            raise result
        return result

    async def _shutdown(self, *, graceful: bool) -> ChildOutcome | BaseException:
        # Return the original error: asyncio otherwise discards a typed
        # CancelledError after its first observation by a task-result reader.
        try:
            try:
                try:
                    await self.finish_input()
                finally:
                    await self.transport.aclose()
            finally:
                try:
                    outcome = await self.proc.finish() if graceful else await self.proc.stop()
                except BaseException as error:
                    if self.proc.retired:
                        try:
                            await self._release_resources()
                        except asyncio.CancelledError:
                            raise
                        except BaseException as cleanup_error:
                            raise error from cleanup_error
                    raise
                else:
                    await self._release_resources()
        except BaseException as error:
            return error
        return outcome

    async def _release_resources(self) -> None:
        try:
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        finally:
            self.resources.close()

    @asynccontextmanager
    async def failures(self):
        """Retain original bounded child output before transporting its failure."""
        try:
            yield
        except BaseException as error:
            await self.shutdown()
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

    @property
    def settled(self) -> NativeCustody:
        return self

    async def closed(self) -> None:
        """Complete this custody's retirement, if any."""

class EmptyNative(NativeCustody):
    available = False

    async def inspect(self, persistent, prepare, request):
        # The runtime owner supplies its existing selected-session preparation:
        # real saved launch/idle attestation/retention, never a priming prompt.
        await prepare()
        acquired = persistent.custody.idle()
        return await acquired.inspect_acquired(persistent, request)


@dataclass
class ChildNative(NativeCustody):
    child: PiSessionChild
    available = True

    @abstractmethod
    def _shutdown_successor(self) -> NativeCustody: ...

    def retire(self, successor=None):
        following = self._shutdown_successor() if successor is None else successor
        return RetiringNative(self.child, following)

    async def closed(self) -> None:
        await self.child.shutdown()


@dataclass
class SuccessorNative(ChildNative):
    successor: NativeCustody

    def _shutdown_successor(self) -> NativeCustody:
        return self.successor


@dataclass
class RetiringNative(SuccessorNative):
    available = False

    def retire(self, successor=None):
        if successor is not None:
            self.successor = successor
        return self

    @property
    def settled(self) -> NativeCustody:
        return self.successor if self.child.retired else self


@dataclass
class BorrowedNative(SuccessorNative):

    async def inspect(self, persistent, prepare, request):
        # The active TurnSession owns receive/correlation. Borrow its original
        # pending response instead of starting a competing reader.
        async with request.pending_response(
            self.child.reader,self.child.proc.stdin
        ) as response:
            return (await response).data.require_payload()

@dataclass
class RetainedNative(ChildNative):
    retained = True
    identity: NativeSessionIdentity
    revision: SessionRevision

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

    def _shutdown_successor(self) -> NativeCustody:
        return EmptyNative()

