"""The session's Pi child: empty, borrowed by a turn, retained idle, or retiring."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Coroutine, Mapping
from contextlib import AbstractContextManager, AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NoReturn

from .child_process import AttachedChild, ChildOutcome, join_retirement
from .declared_family import DeclaredFamily
from .launch_runtime import INHERITED_ENVIRONMENT
from .native_attestation import NativeAttestation, PendingAttestation
from .native_failures import NativePiUnavailable, NativeRequestFailure
from .native_pi import NativePiRpcLaunch
from .native_session_reopen import NativeSessionIdentity
from .pi_events import PiEvent
from .pi_rpc import PiRpcChannel
from .private_path import PrivateSocketRole
from .session_revision import SessionRevision

if TYPE_CHECKING:
    from typing import Self

    from .diagnostics import PublicationMeasurements
    from .native_package import VerifiedNativePackage
    from .participant_identity import ParticipantIdentity
    from .pi_commands import ExtensionUiResponse, PiCommand
    from .pi_native_backend import PiNativeBackend
    from .pi_payloads import PiResponseData, StateData
    from .threads import Thread
    from .turn_context import Provenance, TurnContext


class RetirementState(DeclaredFamily):
    """The shutdown result owns completion, failure and cancellation behavior."""

    retired: ClassVar[bool] = False

    @property
    def release_failure(self) -> BaseException | None:
        return None

    @abstractmethod
    def require_outcome(self) -> ChildOutcome: ...

    def interrupted(self, cancellation: asyncio.CancelledError) -> NoReturn:
        raise cancellation


class RetirementPending(RetirementState):
    def require_outcome(self) -> ChildOutcome:
        raise RuntimeError("Pi child retirement has not completed")


@dataclass(frozen=True)
class Retired(RetirementState):
    outcome: ChildOutcome
    retired: ClassVar[bool] = True

    def require_outcome(self) -> ChildOutcome:
        return self.outcome


@dataclass(frozen=True)
class RetirementFailed(RetirementState):
    error: BaseException

    @property
    def release_failure(self) -> BaseException:
        return self.error

    def require_outcome(self) -> ChildOutcome:
        raise self.error

    def interrupted(self, cancellation: asyncio.CancelledError) -> NoReturn:
        raise cancellation from self.error


class InputForwarder(ABC):
    """The child's forwarding state owns starting and retiring its task."""

    @property
    @abstractmethod
    def tasks(self) -> tuple[asyncio.Task[None], ...]: ...

    @abstractmethod
    def start(self, operation: Callable[[], Coroutine[None, None, None]]) -> InputForwarder: ...

    @abstractmethod
    async def finish(self) -> None: ...

    @abstractmethod
    async def finish_if_done(self, native: PiChild, completed: Collection[object]) -> bool: ...


class NoInputForwarder(InputForwarder):
    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        return ()

    def start(self, operation: Callable[[], Coroutine[None, None, None]]) -> InputForwarder:
        return RunningInputForwarder(asyncio.create_task(operation()))

    async def finish(self) -> None:
        pass

    async def finish_if_done(self, native: PiChild, completed: Collection[object]) -> bool:
        return False


@dataclass(frozen=True)
class RunningInputForwarder(InputForwarder):
    task: asyncio.Task[None]

    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        return (self.task,)

    def start(self, operation: Callable[[], Coroutine[None, None, None]]) -> InputForwarder:
        return self

    async def finish(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def finish_if_done(self, native: PiChild, completed: Collection[object]) -> bool:
        if self.task not in completed:
            return False
        await native.shutdown()
        if not self.task.cancelled():
            self.task.result()
        return True


@dataclass
class PiChild:
    proc: AttachedChild
    reader: PiRpcChannel
    resources: ExitStack = field(default_factory=ExitStack, repr=False, kw_only=True)
    transport: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False, kw_only=True)
    forwarding: InputForwarder = field(default_factory=NoInputForwarder, init=False, repr=False)
    _close_job: asyncio.Task[RetirementState] | None = field(default=None, init=False, repr=False)

    def send(self, command: PiCommand) -> asyncio.Future:
        future = self.reader.track(command)
        self.proc.send(self.reader.command_bytes(command))
        return future

    async def drain(self) -> None:
        await self.proc.drain()

    async def send_fenced(
        self, command: PiCommand, boundary: Callable[[], AbstractContextManager[None]],
        *, measurements: PublicationMeasurements,
    ) -> None:
        self.reader.track(command)
        await self.proc.send_fenced(self.reader.command_bytes(command), boundary, measurements=measurements)

    async def reply_ui(self, response: ExtensionUiResponse) -> None:
        self.send(response)
        await asyncio.wait_for(self.drain(), timeout=2)

    @classmethod
    async def start_command(
        cls,
        argv: tuple[str, ...],
        *,
        cwd: str,
        env: Mapping[str, str] = INHERITED_ENVIRONMENT,
        pass_fds: tuple[int, ...] = (),
        event_type: type[PiEvent] = PiEvent,
        **state,
    ) -> Self:
        proc = await AttachedChild.start(argv, cwd=cwd, env=env, pass_fds=pass_fds)
        return cls(proc, PiRpcChannel(proc.stdout, event_type=event_type), **state)

    async def request(self, command: PiCommand) -> PiResponseData:
        if self.retired:
            raise RuntimeError('Pi child has retired')
        async with self.failures():
            response = await self.reader.request(command, self)
        if not response.succeeded:
            raise RuntimeError(response.error.value_or("Pi gave no error text"))
        return response.data.require_payload()

    @property
    def retirement_result(self) -> RetirementState:
        """The original completed shutdown result; pending work has no result."""
        job = self._close_job
        if job is None or not job.done() or job.cancelled():
            return RetirementPending()
        error = job.exception()
        if error is not None:
            return RetirementFailed(error)
        return job.result()

    @property
    def retired(self) -> bool:
        return self.retirement_result.retired

    @property
    def release_failure(self) -> BaseException | None:
        return self.retirement_result.release_failure

    def forward_input(self, operation: Callable[[], Coroutine[None, None, None]]) -> None:
        self.forwarding = self.forwarding.start(operation)

    async def finish_input(self) -> None:
        """Retire this input's ingress without retiring a retained idle child."""
        await self.reader.finish_input()
        await self.transport.aclose()
        forwarding, self.forwarding = self.forwarding, NoInputForwarder()
        await forwarding.finish()

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
            job.result().interrupted(cancellation)
        return result.require_outcome()

    async def _shutdown(self, *, graceful: bool) -> RetirementState:
        # Retain the original error: asyncio otherwise discards a typed
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
            return RetirementFailed(error)
        return Retired(outcome)

    @asynccontextmanager
    async def failures(self) -> AsyncIterator[None]:
        """Retain original bounded child output before transporting its failure."""
        try:
            yield
        except BaseException as error:
            await self.shutdown()
            stderr = await self.proc.stderr_tail()
            error.add_note(f"Original native child stderr:\n{stderr or '(empty)'}")
            try:
                raise
            except NativeRequestFailure as failure:
                failure.attach_stderr(stderr)
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
    async def start(
        cls, key: tuple[NativePiRpcLaunch, tuple[int, int]], attestation: NativeAttestation,
    ) -> Self:
        launch, _ = key
        with ExitStack() as resources:
            environment = dict(launch.env)
            if "AGENT_COMMS_PROJECT_SOCKET" in environment:
                address = resources.enter_context(PrivateSocketRole.address(Path(environment["AGENT_COMMS_PROJECT_SOCKET"])))
                environment["AGENT_COMMS_PROJECT_SOCKET"] = str(address)
            return await cls.start_command(launch.argv, cwd=str(launch.cwd), env=environment,
                key=key, attestation=attestation, resources=resources.pop_all())


class NativeChildState(ABC):
    """State owns the legal child capabilities, including its retirement successor."""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    retained = False

    def managed_launch(self, command: str, arguments: tuple[str, ...], *, package: VerifiedNativePackage, root: Path, owner: ParticipantIdentity, directory: Path, worktree: Path, environment: dict[str, str] | None, session_file: str | None) -> NativePiRpcLaunch:
        return NativePiRpcLaunch.managed(
            command, arguments, package=package, root=root, owner=owner, directory=directory,
            worktree=worktree, environment=environment, session_file=session_file,
        )

    def reuse(self, key: tuple[NativePiRpcLaunch, tuple[int, int]]) -> PiSessionChild | None:
        return None

    async def context_for_input(self, thread: Thread, context: TurnContext) -> TurnContext:
        return context

    def idle(self) -> RetainedNative:
        raise NativePiUnavailable("Selected idle Pi child is unavailable or stale")

    async def inspect(
        self, persistent: PiNativeBackend, prepare: Callable[[], Awaitable[StateData]],
        request: PiCommand,
    ) -> PiResponseData:
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

    def retire(self, successor: NativeChildState | None = None) -> NativeChildState:
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

    async def inspect(
        self, persistent: PiNativeBackend, prepare: Callable[[], Awaitable[StateData]],
        request: PiCommand,
    ) -> PiResponseData:
        raise NativePiUnavailable("Native context requires an acquired native child")

    def retire(self, successor: NativeChildState | None = None) -> NativeChildState:
        if successor is not None:
            self.successor = successor
        return self

    @property
    def settled(self) -> NativeChildState:
        return self.successor if self.child.retired else self


@dataclass
class BorrowedNative(SuccessorNative):

    async def inspect(
        self, persistent: PiNativeBackend, prepare: Callable[[], Awaitable[StateData]],
        request: PiCommand,
    ) -> PiResponseData:
        # The active TurnSession owns receive/correlation. Borrow its original
        # pending response instead of starting a competing reader.
        async with request.pending_response(self.child) as response:
            return (await response).data.require_payload()

@dataclass
class RetainedNative(ChildNative):
    retained = True
    identity: NativeSessionIdentity
    revision: SessionRevision

    @property
    def current(self) -> bool:
        launch = self.child.key[0]
        return self.child.proc.alive() and self.revision.current(launch.session.files(launch.root))

    def idle(self) -> RetainedNative:
        if not self.current:
            return super().idle()
        return self

    async def inspect_acquired(self, persistent: PiNativeBackend, request: PiCommand) -> PiResponseData:
        async with persistent.lock:
            current = persistent.child_state.idle()
            data = await current.child.request(request)
            if request.writes_session:
                # This child wrote the file itself; its loaded context is the new revision.
                launch = current.child.key[0]
                current.revision = SessionRevision.observe(
                    launch.session.files(launch.root)
                ).require_available()
                current.child.forget_context()
            return data

    def reuse(self, key: tuple[NativePiRpcLaunch, tuple[int, int]]) -> PiSessionChild | None:
        if self.child.key == key and self.current:
            self.child.attestation = PendingAttestation(self.identity)
            return self.child
        return None

    def managed_launch(self, command: str, arguments: tuple[str, ...], *, package: VerifiedNativePackage, root: Path, owner: ParticipantIdentity, directory: Path, worktree: Path, environment: dict[str, str] | None, session_file: str | None) -> NativePiRpcLaunch:
        if self.current and self.child.key[0].package == package:
            original, authentication = self.child.key
            candidate = original.retained_managed(command, arguments,
                root=root, owner=owner, directory=directory, worktree=worktree, environment=environment, session_file=session_file)
            if candidate is not None and (candidate, candidate.configuration.auth_revision()) == (
                original, authentication
            ):
                return original
        return super().managed_launch(
            command, arguments, package=package, root=root, owner=owner, directory=directory,
            worktree=worktree, environment=environment, session_file=session_file,
        )

    def _shutdown_successor(self) -> NativeChildState:
        return EmptyNative()

    async def context_for_input(self, thread: Thread, context: TurnContext) -> TurnContext:
        if thread.project_resume_due or self.child.key[0].cwd != Path(thread.worktree):
            self.child.forget_context()
        if not self.current or self.identity.session_file != thread.runtime.read_key:
            return context
        return context.after_received(self.child.coordination_revision)

