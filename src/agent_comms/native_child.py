"""The session's Pi child: empty, borrowed by a turn, retained idle, or retiring."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .pi_events import PiEvent

from .child_process import AttachedChild, ChildOutcome, join_retirement
from .native_attestation import NativeAttestation, PendingAttestation
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .launch_runtime import LaunchRecord
from .native_session_reopen import NativeSessionIdentity
from .pi_rpc import PiRpcChannel
from .private_path import PrivateSocketRole
from .session_revision import SessionRevision

if TYPE_CHECKING:
    from .turn_context import Provenance


@dataclass
class PiChild:
    proc: AttachedChild
    reader: PiRpcChannel
    resources: ExitStack = field(default_factory=ExitStack, repr=False, kw_only=True)
    transport: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False, kw_only=True)
    forwarding_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _close_job: asyncio.Task[ChildOutcome | BaseException] | None = field(default=None, init=False, repr=False)

    async def reply_ui(self, response) -> None:
        await asyncio.wait_for(self.proc.write(self.reader.encode(response)), timeout=2)

    @classmethod
    async def start_command(cls, argv, *, record: LaunchRecord, cwd, env=None, pass_fds=(), event_type=PiEvent, **state):
        with state.pop('resources', ExitStack()) as resources:
            arguments = resources.enter_context(record.arguments())
            proc = await AttachedChild.start((*argv, *arguments), cwd=cwd, env=env,
                                             pass_fds=pass_fds, runtime=record.runtime)
            return cls(proc, PiRpcChannel(proc.stdout, event_type=event_type),
                       resources=resources.pop_all(), **state)

    async def request(self, command):
        if self.retired:
            raise RuntimeError('Pi child has retired')
        async with self.failures():
            response = await self.reader.request(command, self.proc.stdin)
        if response.success is not True:
            raise RuntimeError(response.error)
        return response.data.require_payload()

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
        await self.reader.finish_input()
        await self.transport.aclose()
        forwarding, self.forwarding_task = self.forwarding_task, None
        if forwarding is not None:
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)

    async def shutdown(self, *, graceful: bool = False) -> ChildOutcome:
        """Join the one gateway, ingress, process, pipe and resource retirement.

        A settled child gets its existing bounded EOF grace;
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
                            self.resources.close()
                        except asyncio.CancelledError:
                            raise
                        except BaseException as cleanup_error:
                            raise error from cleanup_error
                    raise
                else:
                    self.resources.close()
        except BaseException as error:
            return error
        return outcome

    @asynccontextmanager
    async def failures(self):
        """Retain original bounded child output before transporting its failure."""
        try:
            yield
        except BaseException as error:
            await self.shutdown()
            stderr = await self.proc.stderr_tail()
            error.add_note(f"Original native child stderr:\n{stderr or '(empty)'}")
            if isinstance(error, NativePiUnavailable):
                error.native_stderr = stderr
            raise


@dataclass
class PiSessionChild(PiChild):
    key: tuple[NativePiRpcLaunch, tuple[int, int]]
    attestation: NativeAttestation
    sensitive_diagnostics: bool = False
    coordination_revision: tuple[Provenance, ...] = ()

    def remember_coordination(self, provenance: tuple[Provenance, ...]) -> None:
        self.coordination_revision = provenance

    def forget_context(self) -> None:
        self.coordination_revision = ()

    @classmethod
    async def start(cls, key, attestation):
        launch, _ = key
        with ExitStack() as resources:
            record = launch.record
            if record.project_socket is not None:
                address = resources.enter_context(PrivateSocketRole.address(record.project_socket))
                record = replace(record, project_socket=address)
            return await cls.start_command(launch.argv, record=record, cwd=str(launch.cwd), env=launch.env,
                key=key, attestation=attestation, resources=resources.pop_all())


class NativeChildState(ABC):
    """State owns the legal child capabilities, including its retirement successor."""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    retained = False

    def managed_launch(self, command, arguments, *, package, root, directory, worktree, environment, session_file):
        return NativePiRpcLaunch.managed(
            command, arguments, package=package, root=root, directory=directory,
            worktree=worktree, environment=environment, session_file=session_file,
        )

    def reuse(self, key) -> PiSessionChild | None:
        return None

    async def context_for_input(self, thread, context):
        return context

    def idle(self) -> RetainedNative:
        raise NativePiUnavailable("Selected idle Pi child is unavailable or stale")

    async def inspect(self, persistent, prepare, request):
        """Prepare once, then inspect through the acquired idle capability."""
        await prepare()
        return await persistent.child_state.idle().inspect_acquired(persistent, request)

    def retire(self, successor: NativeChildState | None = None) -> NativeChildState:
        return successor if successor is not None else self

    @property
    def settled(self) -> NativeChildState:
        return self

    async def closed(self) -> None:
        """Finish closing this child's resources, if needed."""

class EmptyNative(NativeChildState):
    available = False


@dataclass
class ChildNative(NativeChildState):
    child: PiSessionChild
    available = True

    @abstractmethod
    def _shutdown_successor(self) -> NativeChildState: ...

    def retire(self, successor=None):
        following = self._shutdown_successor() if successor is None else successor
        return RetiringNative(self.child, following)

    async def closed(self) -> None:
        await self.child.shutdown()


@dataclass
class SuccessorNative(ChildNative):
    successor: NativeChildState

    def _shutdown_successor(self) -> NativeChildState:
        return self.successor


@dataclass
class RetiringNative(SuccessorNative):
    available = False

    async def inspect(self, persistent, prepare, request):
        raise NativePiUnavailable("Native context requires an acquired native child")

    def retire(self, successor=None):
        if successor is not None:
            self.successor = successor
        return self

    @property
    def settled(self) -> NativeChildState:
        return self.successor if self.child.retired else self


@dataclass
class BorrowedNative(SuccessorNative):

    async def inspect(self, persistent, prepare, request):
        # The active TurnSession owns receive/correlation. Borrow its original
        # pending response instead of starting a competing reader.
        async with request.pending_response(self.child.reader, self.child.proc.stdin) as response:
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

    async def inspect_acquired(self, persistent, request):
        async with persistent.lock:
            current = persistent.child_state.idle()
            data = await current.child.request(request)
            if request.writes_session:
                # This child wrote the file itself; its loaded context is the new revision.
                current.revision = SessionRevision.observe(
                    current.identity.session_file
                ).require_available()
                current.child.forget_context()
            return data

    def reuse(self, key):
        if self.child.key == key and self.current:
            self.child.attestation = PendingAttestation(self.identity)
            return self.child
        return None

    def managed_launch(self, command, arguments, *, package, root, directory, worktree, environment, session_file):
        if self.current and self.child.key[0].package == package:
            original, authentication = self.child.key
            candidate = original.retained_managed(command, arguments,
                root=root, directory=directory, worktree=worktree, environment=environment, session_file=session_file)
            if candidate is not None and (candidate, candidate.record.configuration.auth_revision()) == (
                original, authentication
            ):
                return original
        return super().managed_launch(
            command, arguments, package=package, root=root, directory=directory,
            worktree=worktree, environment=environment, session_file=session_file,
        )

    def _shutdown_successor(self) -> NativeChildState:
        return EmptyNative()

    async def context_for_input(self, thread, context):
        if thread.project_resume_due or self.child.key[0].cwd != Path(thread.worktree):
            self.child.forget_context()
        if not self.current or self.identity.session_file != thread.runtime.read_key:
            return context
        return context.after_received(self.child.coordination_revision)

